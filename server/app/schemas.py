from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PlanIn(BaseModel):
    code: str
    name: str
    price_cents: int
    period_days: int = 30
    max_devices: int
    click_tracking: bool = True
    max_fps: int = 10
    max_height: int = 720
    retention_days: int = 30


class PlanOut(PlanIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    active: bool


class CustomerIn(BaseModel):
    name: str
    email: str
    document: str | None = None


class CustomerOut(CustomerIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    api_token: str


class SubscriptionIn(BaseModel):
    customer_id: int
    plan_code: str


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    customer_id: int
    plan_id: int
    license_key: str
    status: str
    current_period_end: datetime | None


class ManualPaymentIn(BaseModel):
    amount_cents: int | None = None
    reference: str | None = None


class ActivateIn(BaseModel):
    license_key: str
    machine_id: str = Field(min_length=8, max_length=128)
    hostname: str
    agent_version: str | None = None
    local_ip: str | None = None


class ActivateOut(BaseModel):
    device_id: int
    device_secret: str
    token: str
    expires_at: datetime


class HeartbeatIn(BaseModel):
    local_ip: str | None = None
    agent_version: str | None = None
    streaming: bool | None = None


class TokenOut(BaseModel):
    token: str
    expires_at: datetime


class EventIn(BaseModel):
    ts: datetime
    kind: str = Field(pattern="^(click|window|session)$")
    button: str | None = None
    x: int | None = None
    y: int | None = None
    window_title: str | None = Field(default=None, max_length=500)
    process_name: str | None = Field(default=None, max_length=200)
    user_name: str | None = Field(default=None, max_length=200)
    extra: dict | None = None


class EventsIn(BaseModel):
    events: list[EventIn] = Field(max_length=1000)


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hostname: str
    label: str | None
    local_ip: str | None
    agent_version: str | None
    active: bool
    last_seen: datetime | None


class EventOut(EventIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int


class CheckoutIn(BaseModel):
    plan_code: str
    name: str
    email: str
    document: str | None = None
