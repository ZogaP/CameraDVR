"""Modelos de dados: planos, clientes, assinaturas, dispositivos e eventos de clique."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Sempre grava em UTC e devolve com fuso (o SQLite perde o fuso sem isto)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc)
            if dialect.name == "sqlite":
                value = value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def new_license_key() -> str:
    raw = secrets.token_hex(10).upper()
    return "CDVR-" + "-".join(raw[i : i + 5] for i in range(0, 20, 5))


def new_token() -> str:
    return secrets.token_urlsafe(32)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    price_cents: Mapped[int] = mapped_column(Integer)
    period_days: Mapped[int] = mapped_column(Integer, default=30)
    max_devices: Mapped[int] = mapped_column(Integer)
    click_tracking: Mapped[bool] = mapped_column(Boolean, default=True)
    max_fps: Mapped[int] = mapped_column(Integer, default=10)
    max_height: Mapped[int] = mapped_column(Integer, default=720)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    def features(self) -> dict:
        return {
            "click_tracking": self.click_tracking,
            "max_fps": self.max_fps,
            "max_height": self.max_height,
            "retention_days": self.retention_days,
        }


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    document: Mapped[str | None] = mapped_column(String(30), nullable=True)  # CNPJ/CPF
    # Token de acesso do cliente ao painel/API dele.
    api_token: Mapped[str] = mapped_column(String(64), unique=True, default=new_token)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="customer")


class Subscription(Base):
    """Uma assinatura = uma chave de licença usada pelos agentes do cliente."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    license_key: Mapped[str] = mapped_column(String(40), unique=True, default=new_license_key)
    # pending (aguardando pagamento) | active | past_due | canceled
    status: Mapped[str] = mapped_column(String(20), default="pending")
    current_period_end: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    gateway: Mapped[str | None] = mapped_column(String(30), nullable=True)
    gateway_ref: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    customer: Mapped[Customer] = relationship(back_populates="subscriptions")
    plan: Mapped[Plan] = relationship()
    devices: Mapped[list["Device"]] = relationship(back_populates="subscription")
    payments: Mapped[list["Payment"]] = relationship(back_populates="subscription")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    gateway: Mapped[str] = mapped_column(String(30))
    gateway_payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    paid_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    subscription: Mapped[Subscription] = relationship(back_populates="payments")


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("subscription_id", "machine_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"))
    machine_id: Mapped[str] = mapped_column(String(128))
    hostname: Mapped[str] = mapped_column(String(200))
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    local_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Token secreto do dispositivo, usado nos heartbeats e envio de eventos.
    device_secret: Mapped[str] = mapped_column(String(64), default=new_token)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    subscription: Mapped[Subscription] = relationship(back_populates="devices")


class ActivityEvent(Base):
    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # click | window | session
    button: Mapped[str | None] = mapped_column(String(10), nullable=True)
    x: Mapped[int | None] = mapped_column(Integer, nullable=True)
    y: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    process_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class TermsAcceptance(Base):
    """Prova de aceite: quem aceitou, qual texto (hash), quando, de onde."""

    __tablename__ = "terms_acceptances"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    # contratacao (checkout) | instalacao (responsável no PC) | colaborador (ciência do funcionário)
    kind: Mapped[str] = mapped_column(String(20))
    doc_version: Mapped[str] = mapped_column(String(20))
    doc_sha256: Mapped[str] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    document: Mapped[str | None] = mapped_column(String(20), nullable=True)
    role: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
