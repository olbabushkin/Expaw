"""Ступень 1: бесплатный префильтр по ключевым словам/регуляркам."""

import re

from common.config import settings
from common.textutils import normalize


def _pattern_matches(pattern: str, text_norm: str) -> bool:
    if len(pattern) > 2 and pattern.startswith("/") and pattern.endswith("/"):
        try:
            return re.search(pattern[1:-1], text_norm, re.IGNORECASE) is not None
        except re.error:
            return False
    return normalize(pattern) in text_norm


def find_candidates(text: str | None, raw: dict | None, intents: list[dict]) -> list[str]:
    """Возвращает коды интентов-кандидатов; пустой список => skipped."""
    if not text or len(text) < settings.min_text_len:
        return []
    if raw and raw.get("sender_bot"):
        return []
    text_norm = normalize(text)
    candidates = []
    for intent in intents:
        prefilter = intent["prefilter"] or []
        if not prefilter:
            continue  # интент без префильтра не участвует, пока его не настроят
        if any(_pattern_matches(p, text_norm) for p in prefilter):
            candidates.append(intent["code"])
    return candidates
