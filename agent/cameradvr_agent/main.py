"""Ponto de entrada do agente.

    cameradvr-agent                 -> roda o agente
    cameradvr-agent --instalar      -> assistente de instalação (chave + aceite dos Termos)
    cameradvr-agent --status        -> janela de status (também aberta pelo ícone da bandeja)
    cameradvr-agent --aviso         -> aviso de monitoramento ao colaborador
    cameradvr-agent --ativar CHAVE --nome .. --cpf .. --cargo .. --aceito-termos
                                    -> ativação sem interface (implantação em massa)
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
from . import ui
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
        show_first_run_notice()
        self.tray.start()
        self.onvif.start()
        self.activity.start()
        next_check = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_check:
                self.license = self.lic_client.refresh(streaming=self.stream.running)
                log.info("Licença: %s", self.license.reason)
                if self.license.valid:
                    ui.flush_acks(self.lic_client)
                next_check = now + (HEARTBEAT_SECONDS if self.license.valid else RETRY_SECONDS)
            else:
                # Revalida o token local (expiração offline) entre heartbeats.
                self.license = self.lic_client.current() if self.license.valid else self.license
            self.apply()  # também reinicia o ffmpeg/mediamtx se tiverem caído
            ui.write_status(valid=self.license.valid, reason=self.license.reason,
                            streaming=self.stream.running, expires_at=self.license.expires_at)
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
    p.add_argument("--instalar", action="store_true", help="abre o assistente de instalação")
    p.add_argument("--chave", metavar="CHAVE", help="pré-preenche a chave no assistente")
    p.add_argument("--status", action="store_true", help="abre a janela de status")
    p.add_argument("--aviso", action="store_true", help="abre o aviso de monitoramento")
    g = p.add_argument_group("aceite dos Termos de Uso (com --ativar)")
    g.add_argument("--nome", help="nome completo do responsável")
    g.add_argument("--cpf", help="CPF do responsável")
    g.add_argument("--cargo", help="cargo do responsável")
    g.add_argument("--aceito-termos", action="store_true",
                   help="declara que o responsável leu e aceita os Termos de Uso (GET /api/legal/terms)")
    args = p.parse_args(argv)

    setup_logging()
    cfg = Config.load()
    if args.servidor:
        cfg.server_url = args.servidor
        cfg.save()
    if args.instalar:
        if args.chave:
            cfg.license_key = args.chave.strip().upper()
            cfg.save()
        return ui.run_installer(cfg)
    if args.status:
        return ui.run_status(cfg)
    if args.aviso:
        return ui.run_notice(cfg)
    if args.ativar:
        if not (args.aceito_termos and args.nome and args.cpf and args.cargo):
            print("Para ativar é obrigatório aceitar os Termos de Uso: informe --nome, --cpf, --cargo e "
                  "--aceito-termos (ou use --instalar para o assistente).", file=sys.stderr)
            return 2
        import httpx

        try:
            terms = httpx.get(cfg.server_url.rstrip("/") + "/api/legal/terms", timeout=20).json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"Não foi possível obter os Termos de Uso: {e}", file=sys.stderr)
            return 1
        cfg.license_key = args.ativar.strip().upper()
        cfg.save()
        state = State.load()
        state.acceptance = {"terms_version": terms["version"], "terms_sha256": terms["sha256"],
                            "name": args.nome, "document": args.cpf, "role": args.cargo}
        lic = LicenseClient(cfg, state).refresh()
        print("Ativado com sucesso." if lic.valid else f"Falha na ativação: {lic.reason}")
        return 0 if lic.valid else 1
    if args.info:
        ip = local_ip()
        print(f"RTSP : rtsp://{ip}:{cfg.rtsp_port}/{cfg.stream_path}")
        print(f"ONVIF: http://{ip}:{cfg.onvif_port}/onvif/device_service")
        print(f"Usuário: {cfg.camera_user}\nSenha  : {cfg.camera_password}")
        return 0
    if not cfg.license_key:
        print("Nenhuma chave de licença. Rode: cameradvr-agent --instalar", file=sys.stderr)
        return 2

    agent = Agent(cfg)
    signal.signal(signal.SIGINT, agent.stop)
    signal.signal(signal.SIGTERM, agent.stop)
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
