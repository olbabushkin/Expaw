"""Конфигурация из переменных окружения (общая для всех трёх сервисов)."""

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _admin_ids() -> frozenset[int]:
    raw = os.environ.get("ADMIN_IDS", "")
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://intent:intent@localhost:5432/intent_monitor"
    )

    # userbot
    tg_api_id: int = _int("TG_API_ID", 0)
    tg_api_hash: str = os.environ.get("TG_API_HASH", "")
    tg_session: str = os.environ.get("TG_SESSION", "/session/userbot")

    # bot
    bot_token: str = os.environ.get("BOT_TOKEN", "")
    admin_ids: frozenset[int] = field(default_factory=_admin_ids)
    forum_chat_id: int = _int("FORUM_CHAT_ID", 0)

    # классификация
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    llm_model: str = os.environ.get("LLM_MODEL", "claude-haiku-4-5")
    min_text_len: int = _int("MIN_TEXT_LEN", 20)

    # страховочная сверка юзербота
    sync_interval_min: int = _int("SYNC_INTERVAL_MIN", 120)
    sync_chat_pause_sec: int = _int("SYNC_CHAT_PAUSE_SEC", 8)

    # публикация
    dedup_window_hours: int = _int("DEDUP_WINDOW_HOURS", 48)
    publish_min_interval_sec: int = _int("PUBLISH_MIN_INTERVAL_SEC", 3)

    # ретеншн
    skipped_retention_days: int = _int("SKIPPED_RETENTION_DAYS", 14)


settings = Settings()
