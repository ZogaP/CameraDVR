"""Aviso visível ao usuário do PC (transparência exigida pela LGPD / boa prática trabalhista).

Mostra um ícone na bandeja do sistema enquanto o monitoramento está ativo e um aviso
na primeira execução de cada usuário do Windows.
"""
from __future__ import annotations

import getpass
import logging
import sys
import threading

from .config import Config, data_dir

log = logging.getLogger(__name__)


def _message_box(text: str, title: str) -> None:
    if sys.platform == "win32":
        import ctypes

        MB_ICONINFORMATION, MB_SYSTEMMODAL = 0x40, 0x1000
        ctypes.windll.user32.MessageBoxW(None, text, title, MB_ICONINFORMATION | MB_SYSTEMMODAL)  # type: ignore[attr-defined]
    else:
        log.info("AVISO: %s", text)


def show_first_run_notice(cfg: Config) -> None:
    marker = data_dir() / f"aviso_{getpass.getuser()}.ok"
    if marker.exists():
        return

    def run():
        _message_box(cfg.notice_text, "Aviso de monitoramento")
        try:
            marker.write_text("ok")
        except OSError:
            pass

    threading.Thread(target=run, daemon=True).start()


class Tray:
    def __init__(self, cfg: Config, status_fn):
        self.cfg = cfg
        self.status_fn = status_fn
        self.icon = None

    def start(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            log.info("pystray/Pillow ausentes; ícone de bandeja desativado")
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((6, 16, 46, 48), 6, fill=(200, 30, 30))
        d.polygon([(46, 26), (60, 18), (60, 46), (46, 38)], fill=(200, 30, 30))
        d.ellipse((20, 26, 32, 38), fill=(255, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: f"Status: {self.status_fn()}", None, enabled=False),
            pystray.MenuItem("Sobre o monitoramento", lambda: _message_box(self.cfg.notice_text, "Aviso de monitoramento")),
        )
        self.icon = pystray.Icon("CameraDVR", img, "Este computador está sendo monitorado", menu)
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()

    def stop(self) -> None:
        if self.icon:
            self.icon.stop()
