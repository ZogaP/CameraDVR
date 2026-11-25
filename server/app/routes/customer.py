"""API do cliente (a empresa que comprou): ver computadores e registro de cliques."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..licensing import entitlement
from ..models import ActivityEvent, Customer, Device, Subscription, TermsAcceptance

router = APIRouter(prefix="/api/customer", tags=["cliente"])


def current_customer(authorization: str = Header(...), db: Session = Depends(get_db)) -> Customer:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "use Authorization: Bearer <token>")
    c = db.scalar(select(Customer).where(Customer.api_token == token))
    if c is None:
        raise HTTPException(401, "token inválido")
    return c


@router.get("/me")
def me(c: Customer = Depends(current_customer)):
    subs = []
    for s in c.subscriptions:
        ok, reason = entitlement(s)
        subs.append(
            {
                "id": s.id,
                "plan": s.plan.name,
                "license_key": s.license_key,
                "status": s.status,
                "current_period_end": s.current_period_end,
                "max_devices": s.plan.max_devices,
                "price": s.plan.price_cents / 100,
                "period_days": s.plan.period_days,
                "auto_renew": s.gateway == "mercadopago" and s.status != "canceled",
                "devices_in_use": sum(1 for d in s.devices if d.active),
                "can_use": ok,
                "reason": reason,
            }
        )
    return {"id": c.id, "name": c.name, "email": c.email, "subscriptions": subs}


def _owned_device_ids(db: Session, c: Customer) -> list[int]:
    return list(
        db.scalars(
            select(Device.id).join(Subscription).where(Subscription.customer_id == c.id)
        )
    )


@router.get("/devices", response_model=list[schemas.DeviceOut])
def devices(c: Customer = Depends(current_customer), db: Session = Depends(get_db)):
    return db.scalars(
        select(Device).join(Subscription).where(Subscription.customer_id == c.id).order_by(Device.hostname)
    ).all()


@router.get("/events", response_model=list[schemas.EventOut])
def events(
    device_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(200, le=5000),
    c: Customer = Depends(current_customer),
    db: Session = Depends(get_db),
):
    owned = _owned_device_ids(db, c)
    if device_id is not None:
        if device_id not in owned:
            raise HTTPException(404, "dispositivo não encontrado")
        owned = [device_id]
    q = select(ActivityEvent).where(ActivityEvent.device_id.in_(owned))
    if since:
        q = q.where(ActivityEvent.ts >= since)
    if until:
        q = q.where(ActivityEvent.ts <= until)
    return db.scalars(q.order_by(ActivityEvent.ts.desc()).limit(limit)).all()


def _owned_subscription(db: Session, c: Customer, sub_id: int) -> Subscription:
    sub = db.get(Subscription, sub_id)
    if sub is None or sub.customer_id != c.id:
        raise HTTPException(404, "assinatura não encontrada")
    return sub


@router.post("/subscriptions/{sub_id}/renew")
def renew(sub_id: int, c: Customer = Depends(current_customer), db: Session = Depends(get_db)):
    """Gera o link para renovar manualmente 1 período (PIX, cartão ou boleto)."""
    from .billing import create_renewal_checkout

    sub = _owned_subscription(db, c, sub_id)
    return {"checkout_url": create_renewal_checkout(sub)}


@router.post("/subscriptions/{sub_id}/cancel")
def cancel(sub_id: int, body: schemas.CancelIn, c: Customer = Depends(current_customer), db: Session = Depends(get_db)):
    """Para a assinatura IMEDIATAMENTE: os PCs param de transmitir no próximo contato (até 5 min)."""
    from .billing import cancel_now

    if body.confirm.strip().upper() != "CANCELAR":
        raise HTTPException(422, 'digite CANCELAR para confirmar')
    sub = _owned_subscription(db, c, sub_id)
    cancel_now(sub)
    db.commit()
    return {"ok": True, "status": sub.status}


@router.get("/acceptances", response_model=list[schemas.AcceptanceOut])
def acceptances(c: Customer = Depends(current_customer), db: Session = Depends(get_db)):
    """Aceites dos termos (responsáveis) e ciências dos colaboradores."""
    return db.scalars(
        select(TermsAcceptance).where(TermsAcceptance.customer_id == c.id).order_by(TermsAcceptance.accepted_at.desc())
    ).all()


@router.get("/stats")
def stats(c: Customer = Depends(current_customer), db: Session = Depends(get_db)):
    owned = _owned_device_ids(db, c)
    now = datetime.now(timezone.utc)
    # Meia-noite de Brasília (UTC-3) = 03:00 UTC.
    day = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if day > now:
        day -= timedelta(days=1)
    week = now - timedelta(days=7)
    base = select(func.count()).select_from(ActivityEvent).where(ActivityEvent.device_id.in_(owned))
    clicks_today = db.scalar(base.where(ActivityEvent.kind == "click", ActivityEvent.ts >= day)) or 0
    top = db.execute(
        select(ActivityEvent.process_name, func.count())
        .where(ActivityEvent.device_id.in_(owned), ActivityEvent.ts >= week, ActivityEvent.process_name.is_not(None))
        .group_by(ActivityEvent.process_name)
        .order_by(func.count().desc())
        .limit(6)
    ).all()
    per_dev = dict(
        db.execute(
            select(ActivityEvent.device_id, func.count())
            .where(ActivityEvent.device_id.in_(owned), ActivityEvent.kind == "click", ActivityEvent.ts >= day)
            .group_by(ActivityEvent.device_id)
        ).all()
    )
    return {
        "clicks_today": clicks_today,
        "top_programs": [{"process_name": p, "count": n} for p, n in top],
        "clicks_today_by_device": {str(k): v for k, v in per_dev.items()},
    }
