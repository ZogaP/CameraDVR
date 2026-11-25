"""Registro de atividade: cliques do mouse e troca de janela/programa.

Registra: data/hora, botão, posição, título da janela, programa e usuário do Windows.
NÃO registra teclas digitadas (evita capturar senhas e dados pessoais sensíveis).
"""
from __future__ import annotations

import getpass
import logging
import queue
import sys
import threading
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)


def foreground_window() -> tuple[str | None, str | None]:
    """(título da janela, nome do executável) da janela em primeiro plano."""
    if sys.platform != "win32":
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        import psutil

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value).name()
        except psutil.Error:
            proc = None
        return buf.value[:500] or None, proc
    except Exception:  # noqa: BLE001
        return None, None


class ActivityTracker:
    BATCH_SECONDS = 15
    MAX_QUEUE = 20000

    def __init__(self, client, enabled_fn):
        """`client` é o LicenseClient (para endpoint e credenciais); `enabled_fn` diz se o plano permite."""
        self.client = client
        self.enabled_fn = enabled_fn
        self.q: queue.Queue[dict] = queue.Queue(self.MAX_QUEUE)
        self.user = getpass.getuser()
        self._last_window: tuple[str | None, str | None] = (None, None)
        self._listener = None
        self._stop = threading.Event()

    def _put(self, ev: dict) -> None:
        try:
            self.q.put_nowait(ev)
        except queue.Full:
            pass  # offline por muito tempo: descarta os mais novos

    def record_click(self, x: int, y: int, button: str) -> None:
        if not self.enabled_fn():
            return
        title, proc = foreground_window()
        self._put({
            "ts": datetime.now(timezone.utc).isoformat(), "kind": "click", "button": button,
            "x": int(x), "y": int(y), "window_title": title, "process_name": proc, "user_name": self.user,
        })

    def _watch_windows(self) -> None:
        while not self._stop.wait(2):
            if not self.enabled_fn():
                continue
            cur = foreground_window()
            if cur != self._last_window and cur != (None, None):
                self._last_window = cur
                self._put({
                    "ts": datetime.now(timezone.utc).isoformat(), "kind": "window",
                    "window_title": cur[0], "process_name": cur[1], "user_name": self.user,
                })

    def _uploader(self) -> None:
        pending: list[dict] = []
        while not self._stop.wait(self.BATCH_SECONDS):
            while len(pending) < 1000:
                try:
                    pending.append(self.q.get_nowait())
                except queue.Empty:
                    break
            if not pending or not self.client.state.device_id:
                continue
            try:
                r = self.client.http.post("/api/agent/events", headers=self.client._auth_headers(),
                                          json={"events": pending})
                if r.status_code in (202, 403, 402):
                    pending = []  # 402/403: sem direito -> não acumula
                else:
                    log.warning("envio de eventos falhou: HTTP %s", r.status_code)
            except httpx.HTTPError as e:
                log.debug("sem conexão para enviar eventos: %s", e)

    def start(self) -> None:
        try:
            from pynput import mouse

            def on_click(x, y, button, pressed):
                if pressed:
                    self.record_click(x, y, getattr(button, "name", str(button)))

            self._listener = mouse.Listener(on_click=on_click)
            self._listener.start()
        except Exception as e:  # noqa: BLE001
            log.warning("captura de cliques indisponível: %s", e)
        threading.Thread(target=self._watch_windows, daemon=True, name="window-watch").start()
        threading.Thread(target=self._uploader, daemon=True, name="event-upload").start()

    def stop(self) -> None:
        self._stop.set()
        if self._listener:
            self._listener.stop()
