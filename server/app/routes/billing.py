"""Cobrança recorrente via Mercado Pago (Assinaturas / preapproval).

Fluxo:
1. Site chama POST /api/billing/checkout -> cria cliente + assinatura "pending" e
   devolve o link de pagamento (init_point) do Mercado Pago.
2. A cada cobrança aprovada o Mercado Pago chama o webhook
   (tipo `subscription_authorized_payment`) e o período é renovado.
3. Se o cliente cancelar no Mercado Pago (`subscription_preapproval` = cancelled),
   a assinatura vale até o fim do período já pago e depois o agente para.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import acceptance, schemas
from ..config import settings
from ..db import get_db
from ..licensing import extend_period
from ..models import Customer, Payment, Plan, Subscription

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["cobrança"])

MP_API = "https://api.mercadopago.com"


def _mp_headers() -> dict:
    if not settings.mp_access_token:
        raise HTTPException(503, "Mercado Pago não configurado (MP_ACCESS_TOKEN)")
    return {"Authorization": f"Bearer {settings.mp_access_token}"}


def mp_get(path: str) -> dict:
    r = httpx.get(MP_API + path, headers=_mp_headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def mp_post(path: str, payload: dict) -> dict:
    r = httpx.post(MP_API + path, json=payload, headers=_mp_headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def mp_put(path: str, payload: dict) -> dict:
    r = httpx.put(MP_API + path, json=payload, headers=_mp_headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def create_renewal_checkout(sub: Subscription) -> str:
    """Pagamento avulso de 1 período (PIX, cartão ou boleto). Devolve o link de pagamento."""
    pref = mp_post(
        "/checkout/preferences",
        {
            "items": [{
                "title": f"CameraDVR - {sub.plan.name} - renovação ({sub.plan.period_days} dias)",
                "quantity": 1,
                "unit_price": sub.plan.price_cents / 100,
                "currency_id": "BRL",
            }],
            "payer": {"email": sub.customer.email},
            "external_reference": f"renew:{sub.id}",
            "notification_url": f"{settings.public_base_url}/api/billing/webhook/mercadopago",
            "back_urls": {"success": f"{settings.public_base_url}/painel"},
            "auto_return": "approved",
        },
    )
    return pref["init_point"]


def cancel_now(sub: Subscription) -> None:
    """Cancela já: bloqueia os agentes e interrompe a cobrança recorrente no gateway."""
    sub.status = "canceled"
    sub.current_period_end = datetime.now(timezone.utc) - timedelta(seconds=1)
    if sub.gateway == "mercadopago" and sub.gateway_ref:
        try:
            mp_put(f"/preapproval/{sub.gateway_ref}", {"status": "cancelled"})
        except Exception:  # noqa: BLE001 - o bloqueio local já vale; cobrança é revisada no log
            log.exception("falha ao cancelar a recorrência %s no Mercado Pago", sub.gateway_ref)


def credit_payment(db: Session, sub: Subscription, pay_id: str, amount_cents: int) -> bool:
    """Credita um período. Idempotente: o Mercado Pago reenvia notificações."""
    if db.scalar(select(Payment).where(Payment.gateway_payment_id == pay_id)):
        return False
    db.add(Payment(subscription_id=sub.id, amount_cents=amount_cents, gateway="mercadopago", gateway_payment_id=pay_id))
    extend_period(sub)
    return True


@router.get("/plans")
def public_plans(db: Session = Depends(get_db)):
    plans = db.scalars(select(Plan).where(Plan.active).order_by(Plan.price_cents)).all()
    return [
        {
            "code": p.code,
            "name": p.name,
            "price": p.price_cents / 100,
            "period_days": p.period_days,
            "max_devices": p.max_devices,
            **p.features(),
        }
        for p in plans
    ]


@router.post("/checkout")
def checkout(body: schemas.CheckoutIn, request: Request, db: Session = Depends(get_db)):
    plan = db.scalar(select(Plan).where(Plan.code == body.plan_code, Plan.active))
    if plan is None:
        raise HTTPException(404, "plano não encontrado")
    acc = acceptance.validate(body.acceptance)
    customer = db.scalar(select(Customer).where(Customer.email == body.email))
    if customer is None:
        customer = Customer(name=body.name, email=body.email, document=body.document)
        db.add(customer)
        db.flush()
    sub = Subscription(customer_id=customer.id, plan_id=plan.id, status="pending", gateway="mercadopago")
    db.add(sub)
    db.flush()

    pre = mp_post(
        "/preapproval",
        {
            "reason": f"CameraDVR - {plan.name}",
            "external_reference": str(sub.id),
            "payer_email": customer.email,
            "back_url": f"{settings.public_base_url}/obrigado",
            "auto_recurring": {
                "frequency": plan.period_days,
                "frequency_type": "days",
                "transaction_amount": plan.price_cents / 100,
                "currency_id": "BRL",
            },
            "status": "pending",
        },
    )
    sub.gateway_ref = pre["id"]
    acceptance.record(
        db, request, kind="contratacao", customer_id=customer.id, subscription_id=sub.id,
        doc_version=acc.terms_version, doc_sha256=acc.terms_sha256,
        name=acc.name, document=acc.document, role=acc.role,
    )
    db.commit()
    return {"subscription_id": sub.id, "license_key": sub.license_key, "checkout_url": pre["init_point"]}


def verify_mp_signature(request: Request, data_id: str) -> bool:
    """Valida o cabeçalho x-signature do Mercado Pago (HMAC-SHA256)."""
    secret = settings.mp_webhook_secret
    if not secret:
        log.warning("MP_WEBHOOK_SECRET não configurado: webhook aceito sem validação de assinatura")
        return True
    sig = request.headers.get("x-signature", "")
    req_id = request.headers.get("x-request-id", "")
    parts = dict(p.strip().split("=", 1) for p in sig.split(",") if "=" in p)
    ts, v1 = parts.get("ts"), parts.get("v1")
    if not ts or not v1:
        return False
    manifest = f"id:{data_id.lower()};request-id:{req_id};ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


@router.post("/webhook/mercadopago")
async def mp_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    topic = payload.get("type") or request.query_params.get("type")
    data_id = str((payload.get("data") or {}).get("id") or request.query_params.get("data.id") or "")
    if not data_id:
        return {"ignored": True}
    if not verify_mp_signature(request, data_id):
        raise HTTPException(401, "assinatura inválida")

    if topic == "subscription_authorized_payment":
        ap = mp_get(f"/authorized_payments/{data_id}")
        payment = ap.get("payment") or {}
        if payment.get("status") != "approved":
            return {"ok": True, "status": payment.get("status")}
        sub = db.scalar(select(Subscription).where(Subscription.gateway_ref == ap.get("preapproval_id")))
        if sub is None:
            return {"ignored": True}
        pay_id = str(payment.get("id") or data_id)
        if not credit_payment(db, sub, pay_id, round(float(ap.get("transaction_amount", 0)) * 100)):
            return {"ok": True, "duplicate": True}
        db.commit()
        return {"ok": True}

    if topic == "payment":  # renovação avulsa (Checkout Pro)
        pay = mp_get(f"/v1/payments/{data_id}")
        ref = str(pay.get("external_reference") or "")
        if pay.get("status") != "approved" or not ref.startswith("renew:"):
            return {"ok": True, "status": pay.get("status")}
        sub = db.get(Subscription, int(ref.split(":", 1)[1]))
        if sub is None:
            return {"ignored": True}
        if not credit_payment(db, sub, str(pay.get("id") or data_id), round(float(pay.get("transaction_amount", 0)) * 100)):
            return {"ok": True, "duplicate": True}
        db.commit()
        return {"ok": True}

    if topic == "subscription_preapproval":
        pre = mp_get(f"/preapproval/{data_id}")
        sub = db.scalar(select(Subscription).where(Subscription.gateway_ref == pre.get("id")))
        if sub is None:
            return {"ignored": True}
        if pre.get("status") == "cancelled":
            sub.status = "canceled"
        elif pre.get("status") == "paused" and sub.status == "active":
            sub.status = "past_due"
        db.commit()
        return {"ok": True}

    return {"ignored": True}
