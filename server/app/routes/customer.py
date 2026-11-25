"""API do cliente (a empresa que comprou): ver computadores e registro de cliques."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..licensing import entitlement
from ..models import ActivityEvent, Customer, Device, Subscription

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
