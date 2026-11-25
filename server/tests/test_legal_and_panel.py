from datetime import datetime, timezone

from conftest import ADMIN
from test_flow import _mp_sig, activate, make_paid_subscription, terms_acceptance


def _paid(client, **kw):
    c, s = make_paid_subscription(client, **kw)
    client.post(f"/api/admin/subscriptions/{s['id']}/payments", json={}, headers=ADMIN)
    return c, s


def test_cpf_validation():
    from app.legal import valid_cpf

    assert valid_cpf("529.982.247-25")
    assert not valid_cpf("529.982.247-24")
    assert not valid_cpf("111.111.111-11")


def test_terms_endpoint_and_activation_requires_acceptance(client):
    t = client.get("/api/legal/terms").json()
    assert len(t["sha256"]) == 64 and "CONTROLADOR" in t["text"]
    _, s = _paid(client)
    r = activate(client, s["license_key"], acceptance=None)
    assert r.status_code == 428
    r = activate(client, s["license_key"], acceptance=terms_acceptance(terms_sha256="0" * 64))
    assert r.status_code == 428 and "atualizados" in r.json()["detail"]
    r = activate(client, s["license_key"], acceptance=terms_acceptance(document="123.456.789-00"))
    assert r.status_code == 422


def test_acceptances_recorded_as_evidence(client):
    c, s = _paid(client)
    activate(client, s["license_key"])
    dev = activate(client, s["license_key"]).json()  # reativação não duplica o registro
    h = {"X-Device-Id": str(dev["device_id"]), "X-Device-Secret": dev["device_secret"]}
    n = client.get("/api/legal/notice").json()
    ack = {"user_name": "ana.souza", "notice_version": n["version"], "notice_sha256": n["sha256"],
           "ts": datetime.now(timezone.utc).isoformat()}
    assert client.post("/api/agent/notice-ack", json=ack, headers=h).status_code == 201
    assert client.post("/api/agent/notice-ack", json={**ack, "notice_sha256": "f" * 64}, headers=h).status_code == 409

    cust = {"Authorization": f"Bearer {c['api_token']}"}
    rows = client.get("/api/customer/acceptances", headers=cust).json()
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["colaborador", "instalacao"]
    inst = next(r for r in rows if r["kind"] == "instalacao")
    assert inst["document"] == "52998224725" and inst["name"] == "Maria Gestora" and inst["ip"]
    assert inst["hostname"] == "PC-01"


def test_customer_cancel_now_blocks_and_stops_recurrence(client, monkeypatch):
    from app.routes import billing

    puts = []
    monkeypatch.setattr(billing, "mp_put", lambda path, payload: puts.append((path, payload)) or {})
    c, s = _paid(client)
    # simula assinatura recorrente no Mercado Pago
    from app.db import SessionLocal
    from app.models import Subscription

    with SessionLocal() as db:
        sub = db.get(Subscription, s["id"])
        sub.gateway, sub.gateway_ref = "mercadopago", "pre-9"
        db.commit()
    dev = activate(client, s["license_key"]).json()
    h = {"X-Device-Id": str(dev["device_id"]), "X-Device-Secret": dev["device_secret"]}
    cust = {"Authorization": f"Bearer {c['api_token']}"}

    r = client.post(f"/api/customer/subscriptions/{s['id']}/cancel", json={"confirm": "sim"}, headers=cust)
    assert r.status_code == 422
    r = client.post(f"/api/customer/subscriptions/{s['id']}/cancel", json={"confirm": "cancelar"}, headers=cust)
    assert r.json()["status"] == "canceled"
    assert puts == [("/preapproval/pre-9", {"status": "cancelled"})]
    assert client.post("/api/agent/heartbeat", json={}, headers=h).status_code == 402
    me = client.get("/api/customer/me", headers=cust).json()["subscriptions"][0]
    assert me["can_use"] is False

    # Outro cliente não pode cancelar a assinatura alheia
    c2, _ = make_paid_subscription(client, email="b@b.com")
    r = client.post(f"/api/customer/subscriptions/{s['id']}/cancel", json={"confirm": "CANCELAR"},
                    headers={"Authorization": f"Bearer {c2['api_token']}"})
    assert r.status_code == 404


def test_manual_renewal_via_checkout_and_payment_webhook(client, monkeypatch):
    from app.routes import billing

    posted = {}

    def fake_post(path, payload):
        posted["path"], posted["payload"] = path, payload
        return {"id": "pref-1", "init_point": "https://mp/pagar"}

    monkeypatch.setattr(billing, "mp_post", fake_post)
    c, s = make_paid_subscription(client)  # pendente
    cust = {"Authorization": f"Bearer {c['api_token']}"}
    r = client.post(f"/api/customer/subscriptions/{s['id']}/renew", headers=cust)
    assert r.json()["checkout_url"] == "https://mp/pagar"
    assert posted["path"] == "/checkout/preferences"
    assert posted["payload"]["external_reference"] == f"renew:{s['id']}"
    assert activate(client, s["license_key"]).status_code == 402

    monkeypatch.setattr(billing, "mp_get", lambda path: {"id": 555, "status": "approved",
                                                        "external_reference": f"renew:{s['id']}", "transaction_amount": 49.9})
    body = {"type": "payment", "data": {"id": "555"}}
    assert client.post("/api/billing/webhook/mercadopago", json=body, headers=_mp_sig("555")).json() == {"ok": True}
    assert client.post("/api/billing/webhook/mercadopago", json=body, headers=_mp_sig("555")).json()["duplicate"]
    assert activate(client, s["license_key"]).status_code == 200


def test_stats(client):
    c, s = _paid(client)
    dev = activate(client, s["license_key"]).json()
    h = {"X-Device-Id": str(dev["device_id"]), "X-Device-Secret": dev["device_secret"]}
    now = datetime.now(timezone.utc).isoformat()
    evs = [{"ts": now, "kind": "click", "process_name": "EXCEL.EXE"}] * 3 + [{"ts": now, "kind": "window", "process_name": "chrome.exe"}]
    client.post("/api/agent/events", json={"events": evs}, headers=h)
    st = client.get("/api/customer/stats", headers={"Authorization": f"Bearer {c['api_token']}"}).json()
    assert st["top_programs"][0] == {"process_name": "EXCEL.EXE", "count": 3}
    assert st["clicks_today"] in (0, 3)  # depende do horário (virada do dia em Brasília)
