"""Validação e registro dos aceites (prova para eventual defesa jurídica)."""
from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import legal, schemas
from .models import TermsAcceptance


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def validate(acc: schemas.AcceptanceIn | None) -> schemas.AcceptanceIn:
    """428 Precondition Required: sem aceite da versão atual, não ativa."""
    cur = legal.terms()
    if acc is None:
        raise HTTPException(428, "é obrigatório aceitar os Termos de Uso para ativar")
    if acc.terms_version != cur["version"] or acc.terms_sha256 != cur["sha256"]:
        raise HTTPException(428, "os Termos de Uso foram atualizados; leia e aceite a nova versão")
    if not legal.valid_cpf(acc.document):
        raise HTTPException(422, "CPF do responsável inválido")
    return acc


def record(
    db: Session,
    request: Request,
    *,
    kind: str,
    customer_id: int,
    doc_version: str,
    doc_sha256: str,
    subscription_id: int | None = None,
    device_id: int | None = None,
    name: str | None = None,
    document: str | None = None,
    role: str | None = None,
    user_name: str | None = None,
    hostname: str | None = None,
) -> None:
    # Não duplica o mesmo aceite (mesmo documento, PC e pessoa) a cada reativação.
    dup = db.scalar(
        select(TermsAcceptance.id).where(
            TermsAcceptance.kind == kind,
            TermsAcceptance.customer_id == customer_id,
            TermsAcceptance.device_id == device_id,
            TermsAcceptance.doc_sha256 == doc_sha256,
            TermsAcceptance.document == (legal.only_digits(document) if document else None),
            TermsAcceptance.user_name == user_name,
        )
    )
    if dup:
        return
    db.add(
        TermsAcceptance(
            kind=kind,
            customer_id=customer_id,
            subscription_id=subscription_id,
            device_id=device_id,
            doc_version=doc_version,
            doc_sha256=doc_sha256,
            name=name,
            document=legal.only_digits(document) if document else None,
            role=role,
            user_name=user_name,
            hostname=hostname,
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:300],
        )
    )
