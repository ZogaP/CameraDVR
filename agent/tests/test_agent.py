import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

os.environ["CAMERADVR_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cameradvr_agent import onvif, stream  # noqa: E402
from cameradvr_agent.config import Config, State  # noqa: E402
from cameradvr_agent.license_client import LicenseClient, machine_id, verify_token  # noqa: E402

KEY = Ed25519PrivateKey.generate()


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_token(mid, exp_delta=3600, key=KEY, features=None):
    body = json.dumps({"mid": mid, "exp": int(time.time() + exp_delta), "features": features or {"max_fps": 5}}).encode()
    return f"{b64u(body)}.{b64u(key.sign(body))}"


def test_verify_token_cases():
    pub, mid = KEY.public_key(), machine_id()
    assert verify_token(make_token(mid), pub, mid).valid
    assert verify_token(make_token(mid), pub, mid).features["max_fps"] == 5
    assert verify_token(make_token(mid, -10), pub, mid).reason.startswith("licença expirada")
    assert not verify_token(make_token("outro-pc"), pub, mid).valid
    assert not verify_token(make_token(mid, key=Ed25519PrivateKey.generate()), pub, mid).valid
    # payload adulterado (ex.: aumentar a validade) invalida a assinatura
    body, sig = make_token(mid).split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "==")) | {"exp": 9999999999}
    assert not verify_token(f"{b64u(json.dumps(forged).encode())}.{sig}", pub, mid).valid


def test_clock_rollback_detected():
    st = State(token=make_token(machine_id()), max_seen=time.time() + 86400)
    lc = LicenseClient(Config(), st, public_key=KEY.public_key(), http=object())
    assert "relógio" in lc.current().reason


def test_mediamtx_config_and_ffmpeg_args():
    cfg = Config(camera_user="admin", camera_password="se'nha")
    y = stream.mediamtx_config(cfg, "pub")
    assert "pass: 'se''nha'" in y and "rtspAuthMethods: [basic, digest]" in y
    args = stream.ffmpeg_args(cfg, {"max_fps": 7, "max_height": 720}, "pub")
    assert args[args.index("-framerate") + 1] == "7"
    assert args[-1] == "rtsp://publisher:pub@127.0.0.1:8554/tela"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg ausente")
def test_video_filter_is_valid_for_ffmpeg():
    args = stream.ffmpeg_args(Config(), {"max_fps": 5, "max_height": 360}, "pub")
    vf = args[args.index("-vf") + 1].replace(":fontfile='C\\:/Windows/Fonts/arial.ttf'", "")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=5",
                        "-frames:v", "1", "-vf", vf, "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _soap(action_xml: str, user="admin", password="segredo", digest=True) -> bytes:
    nonce = os.urandom(16)
    created = "2026-01-01T00:00:00Z"
    if digest:
        pw = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        ptype = onvif.PASSWORD_DIGEST
    else:
        pw, ptype = password, "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText"
    return f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsse="{onvif.NS['wsse']}" xmlns:wsu="{onvif.NS['wsu']}">
<s:Header><wsse:Security><wsse:UsernameToken><wsse:Username>{user}</wsse:Username>
<wsse:Password Type="{ptype}">{pw}</wsse:Password><wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>
<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>
<s:Body>{action_xml}</s:Body></s:Envelope>""".encode()


@pytest.fixture()
def onvif_server():
    cfg = Config(camera_user="admin", camera_password="segredo", onvif_port=18080)
    srv = onvif.OnvifServer(cfg, lambda: {"max_fps": 10, "max_height": 720}, lambda: True)
    import threading

    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    yield cfg
    srv.httpd.shutdown()


def _post(port, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/onvif/device_service", data=body,
                                 headers={"Content-Type": "application/soap+xml"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


MEDIA = 'xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'


def test_onvif_auth_and_stream_uri(onvif_server):
    port = onvif_server.onvif_port
    # Ação pública funciona sem senha
    code, body = _post(port, b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
                             b'<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>')
    assert code == 200 and b"UTCDateTime" in body
    # Senha errada é recusada
    code, body = _post(port, _soap(f'<trt:GetProfiles {MEDIA}/>', password="errada"))
    assert code == 400 and b"NotAuthorized" in body
    # Digest correto -> perfis e URI RTSP
    code, body = _post(port, _soap(f'<trt:GetProfiles {MEDIA}/>'))
    assert code == 200 and b"H264" in body
    ET.fromstring(body)  # XML bem-formado
    code, body = _post(port, _soap(f'<trt:GetStreamUri {MEDIA}><trt:ProfileToken>profile_1</trt:ProfileToken></trt:GetStreamUri>', digest=False))
    assert code == 200 and b"rtsp://127.0.0.1:8554/tela" in body
    code, body = _post(port, _soap(f'<trt:Inexistente {MEDIA}/>'))
    assert code == 500 and b"ActionNotSupported" in body


def test_probe_match_is_well_formed():
    dev = onvif.OnvifDevice(Config(), lambda: {}, lambda: True)
    root = ET.fromstring(onvif.probe_match_xml(dev, "uuid:abc", "192.168.0.5"))
    assert root.findtext(".//d:XAddrs", namespaces=onvif.NS) == "http://192.168.0.5:8000/onvif/device_service"
    assert root.findtext(".//a:RelatesTo", namespaces=onvif.NS) == "uuid:abc"


def test_agent_against_real_server(monkeypatch):
    """Ativa o agente contra o servidor de licenças de verdade (em memória)."""
    server_dir = Path(__file__).resolve().parents[2] / "server"
    if not (server_dir / "app").exists():
        pytest.skip("servidor não encontrado")
    from cryptography.hazmat.primitives import serialization

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/t.db")
    monkeypatch.setenv("ADMIN_API_KEY", "adm")
    monkeypatch.setenv("LICENSE_PRIVATE_KEY_PEM", KEY.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    sys.path.insert(0, str(server_dir))
    from fastapi.testclient import TestClient

    from app.main import app

    A = {"X-Admin-Key": "adm"}
    with TestClient(app) as http:
        c = http.post("/api/admin/customers", json={"name": "E", "email": "e@e.com"}, headers=A).json()
        s = http.post("/api/admin/subscriptions", json={"customer_id": c["id"], "plan_code": "profissional"}, headers=A).json()
        cfg = Config(license_key=s["license_key"])
        lc = LicenseClient(cfg, State(), public_key=KEY.public_key(), http=http)

        lic = lc.refresh()
        assert not lic.valid and "pagamento" in lic.reason  # não pagou: não usa

        http.post(f"/api/admin/subscriptions/{s['id']}/payments", json={}, headers=A)
        lic = lc.refresh()
        assert lic.valid and lic.features["max_fps"] == 10  # pagou: libera
        assert lc.refresh(streaming=True).valid  # heartbeat

        http.post(f"/api/admin/subscriptions/{s['id']}/cancel?immediate=true", headers=A)
        lic = lc.refresh()
        assert not lic.valid and lc.state.token is None  # cancelou: bloqueia na hora
