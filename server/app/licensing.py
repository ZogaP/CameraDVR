"""Regras de licença e assinatura dos tokens entregues aos agentes.

O token é `base64url(payload_json).base64url(assinatura_ed25519)`. O agente tem a
chave pública embutida e verifica o token mesmo sem internet, até `exp`.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .config import settings
from .models import Device, Subscription


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@lru_cache
def private_key() -> Ed25519PrivateKey:
    pem = settings.license_private_key_pem
    if not pem and settings.license_private_key_file:
        with open(settings.license_private_key_file, "rb") as f:
            pem = f.read().decode()
    if not pem:
        raise RuntimeError(
            "Chave de licença não configurada. Defina LICENSE_PRIVATE_KEY_FILE ou "
            "LICENSE_PRIVATE_KEY_PEM (gere com: python -m scripts.gen_keys)."
        )
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("A chave de licença precisa ser Ed25519")
    return key


def as_aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def entitlement(sub: Subscription, now: datetime | None = None) -> tuple[bool, str]:
    """Retorna (pode_usar, motivo)."""
    now = now or datetime.now(timezone.utc)
    if sub.status == "pending":
        return False, "aguardando pagamento"
    end = as_aware(sub.current_period_end)
    if end is None:
        return False, "assinatura sem período pago"
    if sub.status == "canceled":
        # Cancelada: pode usar até o fim do período já pago, sem tolerância.
        return (now <= end), ("ok" if now <= end else "assinatura cancelada")
    if now > end + timedelta(days=settings.grace_days):
        return False, "assinatura vencida"
    return True, "ok"


def issue_token(sub: Subscription, device: Device, now: datetime | None = None) -> tuple[str, datetime]:
    now = now or datetime.now(timezone.utc)
    exp = now + timedelta(hours=settings.license_token_ttl_hours)
    end = as_aware(sub.current_period_end)
    if end is not None:
        # Nunca emitir token que vá além do período pago + tolerância.
        exp = min(exp, end + timedelta(days=settings.grace_days))
    payload = {
        "sub": sub.id,
        "dev": device.id,
        "mid": device.machine_id,
        "plan": sub.plan.code,
        "features": sub.plan.features(),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = private_key().sign(body)
    return f"{b64u(body)}.{b64u(sig)}", exp


def extend_period(sub: Subscription, now: datetime | None = None) -> None:
    """Credita um período pago (renova a partir do fim atual, ou de agora se já venceu)."""
    now = now or datetime.now(timezone.utc)
    end = as_aware(sub.current_period_end)
    start = end if end and end > now else now
    sub.current_period_end = start + timedelta(days=sub.plan.period_days)
    sub.status = "active"
