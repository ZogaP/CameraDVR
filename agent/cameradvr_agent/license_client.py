"""Verificação de licença: ativa no servidor, renova por heartbeat e valida o token offline."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import platform
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from importlib import resources

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import AGENT_VERSION, Config, State

log = logging.getLogger(__name__)


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_public_key() -> Ed25519PublicKey:
    pem = resources.files(__package__).joinpath("license_public.pem").read_bytes()
    key = serialization.load_pem_public_key(pem)
    assert isinstance(key, Ed25519PublicKey)
    return key


def machine_id() -> str:
    """Identificador estável do computador (não é dado pessoal; vai como hash)."""
    raw = ""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                raw = winreg.QueryValueEx(k, "MachineGuid")[0]
        except OSError:
            pass
    else:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                raw = open(p).read().strip()
                break
            except OSError:
                continue
    if not raw:
        raw = f"{uuid.getnode()}-{platform.node()}"
    return hashlib.sha256(("cameradvr:" + raw).encode()).hexdigest()


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # não envia nada; só escolhe a interface de saída
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
    except ValueError:
        d = None
    return d if isinstance(d, str) else "licença recusada (dados inválidos)"


class LicenseError(Exception):
    """Servidor recusou a licença (não pago, limite, cancelado...)."""


@dataclass
class License:
    valid: bool
    reason: str
    features: dict
    expires_at: float = 0.0


def verify_token(token: str | None, public_key: Ed25519PublicKey, mid: str, now: float | None = None) -> License:
    if not token:
        return License(False, "sem licença", {})
    try:
        body_b64, sig_b64 = token.split(".")
        body = _b64d(body_b64)
        public_key.verify(_b64d(sig_b64), body)
        payload = json.loads(body)
    except (ValueError, InvalidSignature):
        return License(False, "token de licença inválido", {})
    if payload.get("mid") != mid:
        return License(False, "licença emitida para outro computador", {})
    now = time.time() if now is None else now
    if now > payload["exp"]:
        return License(False, "licença expirada (sem contato com o servidor)", {}, payload["exp"])
    return License(True, "ok", payload.get("features", {}), payload["exp"])


class LicenseClient:
    def __init__(self, cfg: Config, state: State, public_key: Ed25519PublicKey | None = None,
                 http: httpx.Client | None = None):
        self.cfg = cfg
        self.state = state
        self.public_key = public_key or load_public_key()
        self.mid = machine_id()
        self.http = http or httpx.Client(base_url=cfg.server_url.rstrip("/"), timeout=20)

    # --- servidor ---------------------------------------------------------
    def _auth_headers(self) -> dict:
        return {"X-Device-Id": str(self.state.device_id), "X-Device-Secret": self.state.device_secret or ""}

    def activate(self) -> None:
        r = self.http.post(
            "/api/agent/activate",
            json={
                "license_key": self.cfg.license_key,
                "machine_id": self.mid,
                "hostname": socket.gethostname(),
                "agent_version": AGENT_VERSION,
                "local_ip": local_ip(),
                "acceptance": self.state.acceptance,
            },
        )
        if r.status_code in (402, 403, 404, 422, 428):
            self._revoke()
            raise LicenseError(_detail(r))
        r.raise_for_status()
        d = r.json()
        self.state.device_id = d["device_id"]
        self.state.device_secret = d["device_secret"]
        self.state.token = d["token"]
        self.state.license_key = self.cfg.license_key
        self.state.save()

    def heartbeat(self, streaming: bool) -> None:
        r = self.http.post(
            "/api/agent/heartbeat",
            headers=self._auth_headers(),
            json={"local_ip": local_ip(), "agent_version": AGENT_VERSION, "streaming": streaming},
        )
        if r.status_code == 401:  # segredo inválido (ex.: reativado em outro lugar) -> reativa
            return self.activate()
        if r.status_code in (402, 403):
            self._revoke()
            raise LicenseError(_detail(r))
        r.raise_for_status()
        self.state.token = r.json()["token"]
        self.state.save()

    def _revoke(self) -> None:
        self.state.token = None
        self.state.save()

    # --- ciclo ------------------------------------------------------------
    def refresh(self, streaming: bool = False) -> License:
        """Fala com o servidor; se estiver offline, usa o token salvo até expirar."""
        try:
            if not self.state.device_id or self.state.license_key != self.cfg.license_key:
                self.activate()
            else:
                self.heartbeat(streaming)
        except LicenseError as e:
            log.warning("Licença recusada: %s", e)
            return License(False, str(e), {})
        except httpx.HTTPError as e:
            log.warning("Servidor de licenças indisponível (%s); usando licença em cache", e)
        return self.current()

    def current(self) -> License:
        now = time.time()
        if now < self.state.max_seen - 3600:
            return License(False, "relógio do sistema foi alterado; conecte-se à internet", {})
        if now > self.state.max_seen:
            self.state.max_seen = now
            self.state.save()
        return verify_token(self.state.token, self.public_key, self.mid, now)
