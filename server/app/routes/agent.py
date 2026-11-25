"""API usada pelo agente instalado no PC monitorado."""
from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..licensing import entitlement, issue_token
from ..models import ActivityEvent, Device, Subscription, new_token

router = APIRouter(prefix="/api/agent", tags=["agente"])


def _require_entitled(sub: Subscription) -> None:
    ok, reason = entitlement(sub)
    if not ok:
        # 402 Payment Required: o agente para de transmitir ao receber isto.
        raise HTTPException(status_code=402, detail=reason)


def current_device(
    x_device_id: int = Header(...),
    x_device_secret: str = Header(...),
    db: Session = Depends(get_db),
) -> Device:
    device = db.get(Device, x_device_id)
    if device is None or not hmac.compare_digest(device.device_secret, x_device_secret):
        raise HTTPException(status_code=401, detail="dispositivo não autorizado")
    if not device.active:
        raise HTTPException(status_code=403, detail="dispositivo desativado pelo administrador")
    return device


@router.post("/activate", response_model=schemas.ActivateOut)
def activate(body: schemas.ActivateIn, db: Session = Depends(get_db)):
    sub = db.scalar(select(Subscription).where(Subscription.license_key == body.license_key.strip().upper()))
    if sub is None:
        raise HTTPException(status_code=404, detail="chave de licença inválida")
    _require_entitled(sub)

    device = db.scalar(
        select(Device).where(Device.subscription_id == sub.id, Device.machine_id == body.machine_id)
    )
    if device is None or not device.active:
        in_use = db.scalar(
            select(func.count()).select_from(Device).where(Device.subscription_id == sub.id, Device.active)
        )
        if in_use >= sub.plan.max_devices:
            raise HTTPException(
                status_code=403,
                detail=f"limite do plano atingido ({sub.plan.max_devices} computadores)",
            )
    if device is None:
        device = Device(subscription_id=sub.id, machine_id=body.machine_id, hostname=body.hostname)
        db.add(device)
    device.active = True
    device.hostname = body.hostname
    device.agent_version = body.agent_version
    device.local_ip = body.local_ip
    device.device_secret = new_token()
    device.last_seen = datetime.now(timezone.utc)
    db.flush()

    token, exp = issue_token(sub, device)
    db.commit()
    return schemas.ActivateOut(device_id=device.id, device_secret=device.device_secret, token=token, expires_at=exp)


@router.post("/heartbeat", response_model=schemas.TokenOut)
def heartbeat(
    body: schemas.HeartbeatIn,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
):
    sub = device.subscription
    _require_entitled(sub)
    device.last_seen = datetime.now(timezone.utc)
    if body.local_ip:
        device.local_ip = body.local_ip
    if body.agent_version:
        device.agent_version = body.agent_version
    token, exp = issue_token(sub, device)
    db.commit()
    return schemas.TokenOut(token=token, expires_at=exp)


@router.post("/events", status_code=202)
def push_events(
    body: schemas.EventsIn,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
):
    sub = device.subscription
    _require_entitled(sub)
    if not sub.plan.click_tracking:
        raise HTTPException(status_code=403, detail="plano não inclui registro de cliques")
    for ev in body.events:
        db.add(ActivityEvent(device_id=device.id, **ev.model_dump()))
    device.last_seen = datetime.now(timezone.utc)
    db.commit()
    return {"accepted": len(body.events)}
