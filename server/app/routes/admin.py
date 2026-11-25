"""API administrativa (para você, dono do produto): planos, clientes, assinaturas."""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import schemas
from ..config import settings
from ..db import get_db
from ..licensing import extend_period
from ..models import Customer, Device, Payment, Plan, Subscription

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(x_admin_key: str = Header(...)) -> None:
    if not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="chave de admin inválida")


router.dependencies.append(Depends(require_admin))


@router.get("/plans", response_model=list[schemas.PlanOut])
def list_plans(db: Session = Depends(get_db)):
    return db.scalars(select(Plan).order_by(Plan.price_cents)).all()


@router.post("/plans", response_model=schemas.PlanOut, status_code=201)
def create_plan(body: schemas.PlanIn, db: Session = Depends(get_db)):
    if db.scalar(select(Plan).where(Plan.code == body.code)):
        raise HTTPException(409, "já existe um plano com esse código")
    plan = Plan(**body.model_dump())
    db.add(plan)
    db.commit()
    return plan


@router.get("/customers", response_model=list[schemas.CustomerOut])
def list_customers(db: Session = Depends(get_db)):
    return db.scalars(select(Customer).order_by(Customer.id)).all()


@router.post("/customers", response_model=schemas.CustomerOut, status_code=201)
def create_customer(body: schemas.CustomerIn, db: Session = Depends(get_db)):
    if db.scalar(select(Customer).where(Customer.email == body.email)):
        raise HTTPException(409, "e-mail já cadastrado")
    c = Customer(**body.model_dump())
    db.add(c)
    db.commit()
    return c


@router.get("/subscriptions", response_model=list[schemas.SubscriptionOut])
def list_subscriptions(db: Session = Depends(get_db)):
    return db.scalars(select(Subscription).order_by(Subscription.id)).all()


@router.post("/subscriptions", response_model=schemas.SubscriptionOut, status_code=201)
def create_subscription(body: schemas.SubscriptionIn, db: Session = Depends(get_db)):
    plan = db.scalar(select(Plan).where(Plan.code == body.plan_code, Plan.active))
    if plan is None:
        raise HTTPException(404, "plano não encontrado")
    if db.get(Customer, body.customer_id) is None:
        raise HTTPException(404, "cliente não encontrado")
    sub = Subscription(customer_id=body.customer_id, plan_id=plan.id)
    db.add(sub)
    db.commit()
    return sub


@router.post("/subscriptions/{sub_id}/payments", response_model=schemas.SubscriptionOut)
def register_manual_payment(sub_id: int, body: schemas.ManualPaymentIn, db: Session = Depends(get_db)):
    """Registra um pagamento recebido fora do gateway (PIX manual, boleto, etc.)."""
    sub = db.get(Subscription, sub_id)
    if sub is None:
        raise HTTPException(404, "assinatura não encontrada")
    db.add(
        Payment(
            subscription_id=sub.id,
            amount_cents=body.amount_cents if body.amount_cents is not None else sub.plan.price_cents,
            gateway="manual",
            gateway_payment_id=body.reference,
        )
    )
    extend_period(sub)
    db.commit()
    return sub


@router.post("/subscriptions/{sub_id}/cancel", response_model=schemas.SubscriptionOut)
def cancel_subscription(sub_id: int, immediate: bool = False, db: Session = Depends(get_db)):
    """Cancela. Por padrão o cliente usa até o fim do período pago; `immediate=true` bloqueia já."""
    sub = db.get(Subscription, sub_id)
    if sub is None:
        raise HTTPException(404, "assinatura não encontrada")
    sub.status = "canceled"
    if immediate:
        sub.current_period_end = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    return sub


@router.post("/devices/{device_id}/deactivate")
def deactivate_device(device_id: int, db: Session = Depends(get_db)):
    """Libera a vaga do computador no plano (ex.: máquina trocada)."""
    d = db.get(Device, device_id)
    if d is None:
        raise HTTPException(404, "dispositivo não encontrado")
    d.active = False
    db.commit()
    return {"ok": True}
