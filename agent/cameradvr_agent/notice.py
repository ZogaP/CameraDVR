"""Transparência obrigatória: ícone na bandeja enquanto monitora + ciência do colaborador.

Não há opção para desligar: monitoramento oculto é proibido pelos Termos de Uso.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading

from .config import Config
from .ui import notice_marker, self_cmd

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def open_window(flag: str) -> None:
    try:
        subprocess.Popen(self_cmd(flag), creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        log.warning("não foi possível abrir a janela %s: %s", flag, e)


def show_first_run_notice() -> None:
    """Abre o aviso para o usuário atual até ele confirmar a leitura (uma vez por usuário)."""
    if not notice_marker().exists():
        open_window("--aviso")


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
        d.rounded_rectangle((4, 16, 44, 48), 7, fill=(31, 94, 255))
        d.polygon([(44, 26), (60, 17), (60, 47), (44, 38)], fill=(31, 94, 255))
        d.ellipse((17, 25, 31, 39), fill=(255, 255, 255))
        d.ellipse((44, 2, 62, 20), fill=(217, 45, 32))  # ponto vermelho "gravando"
        menu = pystray.Menu(
            pystray.MenuItem("Abrir CameraDVR", lambda: open_window("--status"), default=True),
            pystray.MenuItem(lambda _: f"Status: {self.status_fn()}", None, enabled=False),
            pystray.MenuItem("Ver aviso de monitoramento", lambda: open_window("--aviso")),
        )
        self.icon = pystray.Icon("CameraDVR", img, "CameraDVR – este computador está sendo monitorado", menu)
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()

    def stop(self) -> None:
        if self.icon:
            self.icon.stop()
