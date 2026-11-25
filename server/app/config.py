"""Configuração do servidor via variáveis de ambiente."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./cameradvr.db"))
    # Chave do painel administrativo (você, o vendedor do sistema).
    admin_api_key: str = field(default_factory=lambda: _env("ADMIN_API_KEY", "troque-esta-chave"))
    # Chave privada Ed25519 (PEM) usada para assinar as licenças. Gere com scripts/gen_keys.py
    license_private_key_pem: str | None = field(default_factory=lambda: _env("LICENSE_PRIVATE_KEY_PEM"))
    license_private_key_file: str | None = field(default_factory=lambda: _env("LICENSE_PRIVATE_KEY_FILE"))
    # Validade do token de licença emitido ao agente (horas). Funciona como "tolerância offline".
    license_token_ttl_hours: int = field(default_factory=lambda: int(_env("LICENSE_TOKEN_TTL_HOURS", "72")))
    # Dias de tolerância após o vencimento antes de bloquear.
    grace_days: int = field(default_factory=lambda: int(_env("GRACE_DAYS", "3")))
    # Mercado Pago
    mp_access_token: str | None = field(default_factory=lambda: _env("MP_ACCESS_TOKEN"))
    mp_webhook_secret: str | None = field(default_factory=lambda: _env("MP_WEBHOOK_SECRET"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8080"))


settings = Settings()
