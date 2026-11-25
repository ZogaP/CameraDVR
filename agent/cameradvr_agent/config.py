"""Configuração e estado local do agente.

Arquivo: %ProgramData%\\CameraDVR\\config.json (Windows) ou ~/.cameradvr/config.json
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

AGENT_VERSION = "0.1.0"


def data_dir() -> Path:
    override = os.environ.get("CAMERADVR_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "CameraDVR"
    else:
        base = Path.home() / ".cameradvr"
    base.mkdir(parents=True, exist_ok=True)
    return base


def bin_dir() -> Path:
    """Pasta com ffmpeg.exe e mediamtx.exe (ao lado do executável empacotado)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "bin"
    return Path(__file__).resolve().parents[1] / "bin"


@dataclass
class Config:
    server_url: str = "https://licencas.seudominio.com.br"
    license_key: str = ""
    # Câmera (o que o DVR/NVR usa para se conectar)
    camera_user: str = "admin"
    camera_password: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    rtsp_port: int = 8554
    onvif_port: int = 8000
    stream_path: str = "tela"
    bitrate_kbps: int = 1500
    # Registro de atividade
    track_clicks: bool = True
    # Mostrar aviso ao usuário do PC (transparência / LGPD). Recomendado manter ligado.
    show_notice: bool = True
    notice_text: str = (
        "Este computador é monitorado pela empresa conforme a política interna de uso: "
        "a tela é transmitida ao sistema de vigilância e os cliques são registrados. "
        "Não são registradas teclas digitadas nem senhas."
    )

    @classmethod
    def load(cls) -> "Config":
        path = data_dir() / "config.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            cfg = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        else:
            cfg = cls()
        cfg.save()  # persiste a senha gerada e novos campos
        return cfg

    def save(self) -> None:
        path = data_dir() / "config.json"
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")


@dataclass
class State:
    device_id: int | None = None
    device_secret: str | None = None
    token: str | None = None
    license_key: str | None = None
    # Maior horário já visto; detecta relógio voltado para trás (burlar expiração offline).
    max_seen: float = 0.0

    @classmethod
    def load(cls) -> "State":
        path = data_dir() / "state.json"
        if path.exists():
            try:
                return cls(**json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, TypeError):
                pass
        return cls()

    def save(self) -> None:
        path = data_dir() / "state.json"
        path.write_text(json.dumps(asdict(self)), encoding="utf-8")
