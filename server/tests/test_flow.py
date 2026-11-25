import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from conftest import ADMIN


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_paid_subscription(client, plan="basico", email="rh@empresa.com"):
    c = client.post("/api/admin/customers", json={"name": "Empresa X", "email": email}, headers=ADMIN).json()
    s = client.post("/api/admin/subscriptions", json={"customer_id": c["id"], "plan_code": plan}, headers=ADMIN).json()
    return c, s


def activate(client, key, mid="machine-0001", host="PC-01"):
    return client.post(
        "/api/agent/activate",
        json={"license_key": key, "machine_id": mid, "hostname": host, "local_ip": "192.168.0.10"},
    )


def test_admin_requires_key(client):
    assert client.get("/api/admin/plans").status_code == 422
    assert client.get("/api/admin/plans", headers={"X-Admin-Key": "errada"}).status_code == 401
    assert len(client.get("/api/admin/plans", headers=ADMIN).json()) == 3


def test_unpaid_subscription_is_blocked(client):
    _, s = make_paid_subscription(client)
    r = activate(client, s["license_key"])
    assert r.status_code == 402
    assert "pagamento" in r.json()["detail"]


def test_paid_flow_token_and_device_limit(client, public_key):
    _, s = make_paid_subscription(client)  # plano básico = 3 computadores
    r = client.post(f"/api/admin/subscriptions/{s['id']}/payments", json={}, headers=ADMIN)
    assert r.json()["status"] == "active"

    r = activate(client, s["license_key"].lower())
    assert r.status_code == 200, r.text
    body = r.json()
    payload_b64, sig_b64 = body["token"].split(".")
    public_key.verify(_b64d(sig_b64), _b64d(payload_b64))  # assinatura válida
    payload = json.loads(_b64d(payload_b64))
    assert payload["mid"] == "machine-0001"
    assert payload["features"]["max_fps"] == 5

    # Reativar a mesma máquina não ocupa vaga nova
    assert activate(client, s["license_key"]).status_code == 200
    assert activate(client, s["license_key"], mid="machine-0002").status_code == 200
    assert activate(client, s["license_key"], mid="machine-0003").status_code == 200
    r = activate(client, s["license_key"], mid="machine-0004")
    assert r.status_code == 403 and "limite" in r.json()["detail"]


def test_heartbeat_and_events_then_cancel(client):
    c, s = make_paid_subscription(client)
    client.post(f"/api/admin/subscriptions/{s['id']}/payments", json={}, headers=ADMIN)
    dev = activate(client, s["license_key"]).json()
    h = {"X-Device-Id": str(dev["device_id"]), "X-Device-Secret": dev["device_secret"]}

    assert client.post("/api/agent/heartbeat", json={}, headers=h).status_code == 200
    bad = {**h, "X-Device-Secret": "x"}
    assert client.post("/api/agent/heartbeat", json={}, headers=bad).status_code == 401

    now = datetime.now(timezone.utc).isoformat()
    ev = {"ts": now, "kind": "click", "button": "left", "x": 10, "y": 20, "window_title": "Planilha", "process_name": "EXCEL.EXE"}
    assert client.post("/api/agent/events", json={"events": [ev]}, headers=h).status_code == 202

    cust = {"Authorization": f"Bearer {c['api_token']}"}
    evs = client.get("/api/customer/events", headers=cust).json()
    assert evs[0]["process_name"] == "EXCEL.EXE"
    assert client.get("/api/customer/me", headers=cust).json()["subscriptions"][0]["can_use"] is True

    # Outro cliente não enxerga os eventos
    c2, _ = make_paid_subscription(client, email="outra@empresa.com")
    other = {"Authorization": f"Bearer {c2['api_token']}"}
    assert client.get("/api/customer/events", headers=other).json() == []
    assert client.get(f"/api/customer/events?device_id={dev['device_id']}", headers=other).status_code == 404

    # Cancelamento imediato bloqueia o agente
    client.post(f"/api/admin/subscriptions/{s['id']}/cancel?immediate=true", headers=ADMIN)
    assert client.post("/api/agent/heartbeat", json={}, headers=h).status_code == 402


def test_entitlement_expiry_and_grace():
    from app.licensing import entitlement
    from app.models import Subscription

    now = datetime.now(timezone.utc)
    sub = Subscription(status="active", current_period_end=now - timedelta(days=2))
    assert entitlement(sub, now)[0] is True  # dentro da tolerância de 3 dias
    sub.current_period_end = now - timedelta(days=4)
    assert entitlement(sub, now) == (False, "assinatura vencida")
    sub = Subscription(status="canceled", current_period_end=now + timedelta(days=5))
    assert entitlement(sub, now)[0] is True  # usa até o fim do período pago


def _mp_sig(data_id, req_id="req-1", ts="1700000000"):
    manifest = f"id:{data_id};request-id:{req_id};ts:{ts};"
    v1 = hmac.new(b"segredo-webhook", manifest.encode(), hashlib.sha256).hexdigest()
    return {"x-signature": f"ts={ts},v1={v1}", "x-request-id": req_id}


def test_mercadopago_checkout_and_webhook(client, monkeypatch):
    from app.routes import billing

    monkeypatch.setattr(billing, "mp_post", lambda path, payload: {"id": "pre-123", "init_point": "https://mp/checkout"})
    r = client.post("/api/billing/checkout", json={"plan_code": "profissional", "name": "Loja", "email": "loja@x.com"})
    assert r.status_code == 200
    out = r.json()
    assert out["checkout_url"] == "https://mp/checkout"
    assert activate(client, out["license_key"]).status_code == 402

    calls = {"n": 0}

    def fake_get(path):
        calls["n"] += 1
        return {"preapproval_id": "pre-123", "transaction_amount": 129.9, "payment": {"id": 999, "status": "approved"}}

    monkeypatch.setattr(billing, "mp_get", fake_get)
    body = {"type": "subscription_authorized_payment", "data": {"id": "777"}}

    # Assinatura inválida é rejeitada
    assert client.post("/api/billing/webhook/mercadopago", json=body, headers={"x-signature": "ts=1,v1=00"}).status_code == 401

    assert client.post("/api/billing/webhook/mercadopago", json=body, headers=_mp_sig("777")).json() == {"ok": True}
    # Reenvio da mesma notificação não credita duas vezes
    assert client.post("/api/billing/webhook/mercadopago", json=body, headers=_mp_sig("777")).json()["duplicate"]

    subs = client.get("/api/admin/subscriptions", headers=ADMIN).json()
    sub = next(s for s in subs if s["license_key"] == out["license_key"])
    end = datetime.fromisoformat(sub["current_period_end"])
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    assert 29 <= (end - datetime.now(timezone.utc)).days <= 30
    assert activate(client, out["license_key"]).status_code == 200


def test_retention_purge(client):
    from app.main import purge_old_events

    _, s = make_paid_subscription(client)  # básico: retenção de 15 dias
    client.post(f"/api/admin/subscriptions/{s['id']}/payments", json={}, headers=ADMIN)
    dev = activate(client, s["license_key"]).json()
    h = {"X-Device-Id": str(dev["device_id"]), "X-Device-Secret": dev["device_secret"]}
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    client.post("/api/agent/events", json={"events": [{"ts": old, "kind": "click"}, {"ts": new, "kind": "click"}]}, headers=h)
    assert purge_old_events() == 1
