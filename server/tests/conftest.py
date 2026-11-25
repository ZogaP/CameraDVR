import os
import sys
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_KEY = Ed25519PrivateKey.generate()
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ADMIN_API_KEY"] = "admin-test"
os.environ["LICENSE_PRIVATE_KEY_PEM"] = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
os.environ["MP_ACCESS_TOKEN"] = "TEST-token"
os.environ["MP_WEBHOOK_SECRET"] = "segredo-webhook"


@pytest.fixture(scope="session")
def public_key():
    return _KEY.public_key()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.db import Base, engine
    from app.main import app

    Base.metadata.drop_all(engine)
    with TestClient(app) as c:
        yield c


ADMIN = {"X-Admin-Key": "admin-test"}
