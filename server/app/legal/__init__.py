"""Textos legais oficiais. O hash SHA-256 do texto identifica exatamente o que foi aceito."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent

TERMS_VERSION = "2026-11-25"
NOTICE_VERSION = "2026-11-25"


@lru_cache
def _load(name: str) -> tuple[str, str]:
    text = (_DIR / name).read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def terms() -> dict:
    text, h = _load("termos_de_uso.md")
    return {"version": TERMS_VERSION, "sha256": h, "text": text}


def notice() -> dict:
    text, h = _load("aviso_colaborador.md")
    return {"version": NOTICE_VERSION, "sha256": h, "text": text}


def only_digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def valid_cpf(cpf: str) -> bool:
    d = only_digits(cpf)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        total = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (total * 10 % 11) % 10 != int(d[n]):
            return False
    return True
