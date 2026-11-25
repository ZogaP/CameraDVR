from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy import delete, select

from .db import SessionLocal, init_db
from .models import ActivityEvent, Device, Plan, Subscription
from .routes import admin, agent, billing, customer

STATIC = Path(__file__).parent / "static"

DEFAULT_PLANS = [
    dict(code="basico", name="Básico", price_cents=4990, max_devices=3, max_fps=5, max_height=720, retention_days=15),
    dict(code="profissional", name="Profissional", price_cents=12990, max_devices=10, max_fps=10, max_height=1080, retention_days=60),
    dict(code="empresa", name="Empresa", price_cents=34990, max_devices=50, max_fps=15, max_height=1080, retention_days=180),
]


def seed_plans() -> None:
    with SessionLocal() as db:
        if db.scalar(select(Plan).limit(1)) is None:
            db.add_all(Plan(**p) for p in DEFAULT_PLANS)
            db.commit()


def purge_old_events() -> int:
    """Apaga eventos mais antigos que a retenção do plano (LGPD: minimização de dados)."""
    total = 0
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for sub in db.scalars(select(Subscription)).all():
            cutoff = now - timedelta(days=sub.plan.retention_days)
            dev_ids = select(Device.id).where(Device.subscription_id == sub.id)
            res = db.execute(
                delete(ActivityEvent).where(ActivityEvent.device_id.in_(dev_ids), ActivityEvent.ts < cutoff)
            )
            total += res.rowcount or 0
        db.commit()
    return total


async def _purge_loop():
    while True:
        try:
            await asyncio.to_thread(purge_old_events)
        except Exception:  # noqa: BLE001
            logging.exception("falha ao limpar eventos antigos")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed_plans()
    task = asyncio.create_task(_purge_loop())
    yield
    task.cancel()


app = FastAPI(title="CameraDVR - Licenças", version="0.1.0", lifespan=lifespan)
app.include_router(agent.router)
app.include_router(admin.router)
app.include_router(customer.router)
app.include_router(billing.router)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/painel", include_in_schema=False)
def painel():
    return FileResponse(STATIC / "painel.html")
