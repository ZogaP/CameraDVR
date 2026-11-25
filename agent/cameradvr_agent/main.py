"""Ponto de entrada do agente.

    cameradvr-agent                 -> roda o agente
    cameradvr-agent --ativar CHAVE  -> grava a chave de licença e testa a ativação
    cameradvr-agent --info          -> mostra endereço RTSP/ONVIF e usuário/senha da câmera
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import time

from .activity import ActivityTracker
from .config import AGENT_VERSION, Config, State, data_dir
from .license_client import License, LicenseClient, local_ip
from .notice import Tray, show_first_run_notice
from .onvif import OnvifServer
from .stream import StreamManager

log = logging.getLogger("cameradvr")

HEARTBEAT_SECONDS = 300
RETRY_SECONDS = 60


def setup_logging() -> None:
    h = logging.handlers.RotatingFileHandler(data_dir() / "agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[h, logging.StreamHandler()],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lic_client = LicenseClient(cfg, State.load())
        self.license = License(False, "verificando licença", {})
        self.stream = StreamManager(cfg)
        self.onvif = OnvifServer(cfg, lambda: self.license.features, lambda: self.license.valid)
        self.activity = ActivityTracker(
            self.lic_client,
            lambda: self.license.valid and cfg.track_clicks and self.license.features.get("click_tracking", False),
        )
        self.tray = Tray(cfg, self.status)
        self._stop = threading.Event()

    def status(self) -> str:
        if self.license.valid:
            return "monitoramento ativo" + (" (transmitindo)" if self.stream.running else "")
        return f"bloqueado – {self.license.reason}"

    def apply(self) -> None:
        if self.license.valid:
            self.stream.ensure(self.license.features)
        elif self.stream.running:
            log.warning("Licença inválida (%s): transmissão interrompida", self.license.reason)
            self.stream.stop()

    def run(self) -> None:
        log.info("CameraDVR agente %s iniciando", AGENT_VERSION)
        if self.cfg.show_notice:
            show_first_run_notice(self.cfg)
            self.tray.start()
        self.onvif.start()
        self.activity.start()
        next_check = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_check:
                self.license = self.lic_client.refresh(streaming=self.stream.running)
                log.info("Licença: %s", self.license.reason)
                next_check = now + (HEARTBEAT_SECONDS if self.license.valid else RETRY_SECONDS)
            else:
                # Revalida o token local (expiração offline) entre heartbeats.
                self.license = self.lic_client.current() if self.license.valid else self.license
            self.apply()  # também reinicia o ffmpeg/mediamtx se tiverem caído
            self._stop.wait(10)
        self.shutdown()

    def shutdown(self) -> None:
        self.stream.stop()
        self.activity.stop()
        self.onvif.stop()
        self.tray.stop()

    def stop(self, *_):
        self._stop.set()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cameradvr-agent")
    p.add_argument("--ativar", metavar="CHAVE", help="grava a chave de licença e ativa este PC")
    p.add_argument("--servidor", metavar="URL", help="URL do servidor de licenças")
    p.add_argument("--info", action="store_true", help="mostra os dados para cadastrar no DVR")
    args = p.parse_args(argv)

    setup_logging()
    cfg = Config.load()
    if args.servidor:
        cfg.server_url = args.servidor
        cfg.save()
    if args.ativar:
        cfg.license_key = args.ativar.strip().upper()
        cfg.save()
        lic = LicenseClient(cfg, State.load()).refresh()
        print("Ativado com sucesso." if lic.valid else f"Falha na ativação: {lic.reason}")
        return 0 if lic.valid else 1
    if args.info:
        ip = local_ip()
        print(f"RTSP : rtsp://{ip}:{cfg.rtsp_port}/{cfg.stream_path}")
        print(f"ONVIF: http://{ip}:{cfg.onvif_port}/onvif/device_service")
        print(f"Usuário: {cfg.camera_user}\nSenha  : {cfg.camera_password}")
        return 0
    if not cfg.license_key:
        print("Nenhuma chave de licença. Use: cameradvr-agent --ativar CDVR-XXXXX-...", file=sys.stderr)
        return 2

    agent = Agent(cfg)
    signal.signal(signal.SIGINT, agent.stop)
    signal.signal(signal.SIGTERM, agent.stop)
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
