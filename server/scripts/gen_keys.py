"""Gera o par de chaves Ed25519 das licenças.

    python scripts/gen_keys.py ./keys

- license_private.pem  -> fica SOMENTE no servidor (LICENSE_PRIVATE_KEY_FILE)
- license_public.pem   -> vai embutida no agente (agent/cameradvr_agent/license_public.pem)
"""
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

out = Path(sys.argv[1] if len(sys.argv) > 1 else "keys")
out.mkdir(parents=True, exist_ok=True)
key = Ed25519PrivateKey.generate()
(out / "license_private.pem").write_bytes(
    key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
)
(out / "license_public.pem").write_bytes(
    key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
)
print(f"Chaves geradas em {out.resolve()}")
