"""Нормализация текста и хэш для антидубля."""

import hashlib
import re

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = _WS_RE.sub(" ", text)
    return text.strip()


def dedup_hash(text: str) -> str:
    """Хэш агрессивно нормализованного текста: без пунктуации и пробелов."""
    stripped = _PUNCT_RE.sub("", normalize(text))
    stripped = _WS_RE.sub("", stripped)
    return hashlib.sha256(stripped.encode()).hexdigest()
