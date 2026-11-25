"""Janelas do agente (pywebview: no Windows usa o Edge WebView2, já incluso no Windows 10/11).

- instalador : chave de licença -> Termos de Uso (nome, CPF, cargo, aceite) -> dados para o DVR
- status     : o que está sendo monitorado e situação da licença (aberto pelo ícone da bandeja)
- aviso      : ciência do colaborador na primeira vez que ele entra no Windows

Cada janela roda em um processo próprio (`CameraDVR.exe --instalar|--status|--aviso`), assim o
agente principal nunca trava por causa da interface.
"""
from __future__ import annotations

import getpass
import json
import logging
import socket
import sys
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import httpx

from .config import AGENT_VERSION, Config, State, data_dir
from .license_client import LicenseClient, local_ip

log = logging.getLogger(__name__)

STATUS_FILE = "status.json"
NOTICE_CACHE = "aviso.json"
PENDING_ACKS = "ciencias_pendentes.json"


def page(name: str) -> str:
    return resources.files(__package__).joinpath("ui", name).read_text(encoding="utf-8")


def self_cmd(*args: str) -> list[str]:
    """Comando para abrir outra janela do próprio agente."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "cameradvr_agent", *args]


def dvr_info(cfg: Config) -> dict:
    ip = local_ip()
    return {
        "ip": ip,
        "onvif": f"http://{ip}:{cfg.onvif_port}/onvif/device_service",
        "onvif_port": cfg.onvif_port,
        "rtsp": f"rtsp://{ip}:{cfg.rtsp_port}/{cfg.stream_path}",
        "user": cfg.camera_user,
        "password": cfg.camera_password,
    }


# --- status compartilhado entre o agente e a janela de status -------------------
def write_status(**data) -> None:
    data["updated"] = time.time()
    try:
        (data_dir() / STATUS_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_status() -> dict:
    try:
        return json.loads((data_dir() / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --- aviso ao colaborador -------------------------------------------------------
def fetch_notice(cfg: Config) -> dict | None:
    """Baixa o aviso oficial do servidor e guarda em cache (para funcionar offline)."""
    cache = data_dir() / NOTICE_CACHE
    try:
        r = httpx.get(cfg.server_url.rstrip("/") + "/api/legal/notice", timeout=15)
        r.raise_for_status()
        cache.write_text(r.text, encoding="utf-8")
        return r.json()
    except (httpx.HTTPError, OSError):
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def notice_marker(user: str | None = None) -> Path:
    return data_dir() / f"aviso_{user or getpass.getuser()}.ok"


def queue_ack(notice: dict, user: str) -> None:
    path = data_dir() / PENDING_ACKS
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pending = []
    pending.append({"user_name": user, "notice_version": notice["version"], "notice_sha256": notice["sha256"],
                    "ts": datetime.now(timezone.utc).isoformat()})
    path.write_text(json.dumps(pending), encoding="utf-8")


def flush_acks(client: LicenseClient) -> None:
    """Envia ao servidor as ciências registradas (chamado pelo agente principal)."""
    path = data_dir() / PENDING_ACKS
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    left = []
    for ack in pending:
        try:
            r = client.http.post("/api/agent/notice-ack", headers=client._auth_headers(), json=ack)
            if r.status_code >= 500 or r.status_code == 401:
                left.append(ack)
        except httpx.HTTPError:
            left.append(ack)
    path.write_text(json.dumps(left), encoding="utf-8")


# --- APIs expostas ao JavaScript de cada janela ---------------------------------
class InstallerApi:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.window = None
        self.ok = False

    def get_context(self):
        return {"license_key": self.cfg.license_key, "server_url": self.cfg.server_url,
                "hostname": socket.gethostname(), "version": AGENT_VERSION}

    def get_terms(self):
        try:
            r = httpx.get(self.cfg.server_url.rstrip("/") + "/api/legal/terms", timeout=20)
            r.raise_for_status()
            return {"ok": True, **r.json()}
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"Não foi possível falar com o servidor de licenças ({e})."}

    def activate(self, license_key: str, acceptance: dict):
        self.cfg.license_key = license_key.strip().upper()
        self.cfg.save()
        state = State.load()
        state.acceptance = acceptance
        client = LicenseClient(self.cfg, state)
        lic = client.refresh()
        if not lic.valid:
            return {"ok": False, "error": lic.reason}
        self.ok = True
        return {"ok": True, "info": dvr_info(self.cfg)}

    def copy(self, text: str):
        try:
            import subprocess

            subprocess.run(["clip"], input=text.encode("utf-16-le"), check=False,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        except OSError:
            pass

    def close(self):
        if self.window:
            self.window.destroy()


class StatusApi:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.window = None

    def get_status(self):
        st = read_status()
        notice = fetch_notice(self.cfg) if not (data_dir() / NOTICE_CACHE).exists() else None
        if notice is None:
            try:
                notice = json.loads((data_dir() / NOTICE_CACHE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                notice = None
        return {"hostname": socket.gethostname(), "user": getpass.getuser(), "version": AGENT_VERSION,
                "status": st, "agent_running": bool(st) and time.time() - st.get("updated", 0) < 60,
                "notice": notice}

    def close(self):
        if self.window:
            self.window.destroy()


class NoticeApi:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.window = None
        self.notice = None

    def get_notice(self):
        self.notice = fetch_notice(self.cfg)
        return {"notice": self.notice, "user": getpass.getuser(), "hostname": socket.gethostname()}

    def acknowledge(self):
        user = getpass.getuser()
        if self.notice:
            queue_ack(self.notice, user)
        notice_marker(user).write_text(datetime.now(timezone.utc).isoformat())
        if self.window:
            self.window.destroy()


def _run(title: str, html: str, api, width: int, height: int, **kw) -> None:
    import webview

    api.window = webview.create_window(title, html=html, js_api=api, width=width, height=height,
                                       resizable=kw.get("resizable", False), on_top=kw.get("on_top", False))
    webview.start()


def run_installer(cfg: Config) -> int:
    api = InstallerApi(cfg)
    _run("Instalação do CameraDVR", page("instalador.html"), api, 760, 700)
    return 0 if api.ok else 1


def run_status(cfg: Config) -> int:
    _run("CameraDVR", page("status.html"), StatusApi(cfg), 460, 620)
    return 0


def run_notice(cfg: Config) -> int:
    # Se for fechada sem confirmar, a ciência não é registrada e o aviso reaparece no próximo login.
    _run("Aviso de monitoramento", page("aviso.html"), NoticeApi(cfg), 560, 640, on_top=True)
    return 0
