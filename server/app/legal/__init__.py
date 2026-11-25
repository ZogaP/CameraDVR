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
