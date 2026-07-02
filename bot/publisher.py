"""Публикация совпадений в топики форума + служебный топик «Система» + watchdog."""

import asyncio
import html
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from common import db
from common.config import settings
from common.log import setup
from common.textutils import dedup_hash

log = setup("bot.publisher")

SERVICE = "bot"
TEXT_LIMIT = 1000
SYSTEM_TOPIC_KEY = "system_topic_id"
WATCHDOG_STALE_MIN = 10


def build_link(username: str | None, chat_id: int, tg_msg_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{tg_msg_id}"
    # приватный канал/группа: t.me/c/<internal>/<msg> — internal без префикса -100
    internal = str(chat_id).removeprefix("-100")
    return f"https://t.me/c/{internal}/{tg_msg_id}"


def format_post(row: asyncpg.Record) -> str:
    text = row["text"] or ""
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT] + "…"
    link = build_link(row["username"], row["chat_id"], row["tg_msg_id"])
    source = row["chat_title"] or (f"@{row['username']}" if row["username"] else "чат")
    author = f" · автор: <code>{row['sender_id']}</code>" if row["sender_id"] else ""
    return (
        f"{html.escape(text)}\n\n"
        f"📍 <a href=\"{link}\">Оригинал</a> · {html.escape(source)}{author} "
        f"· score {row['score']:.2f}"
    )


async def _is_duplicate(pool: asyncpg.Pool, text: str) -> bool:
    """Антидубль уровня 2: тот же нормализованный текст в окне N часов."""
    recent = await pool.fetchval(
        "SELECT published_at FROM published_hashes WHERE hash = $1", dedup_hash(text)
    )
    window = timedelta(hours=settings.dedup_window_hours)
    return recent is not None and datetime.now(timezone.utc) - recent < window


async def _record_hash(pool: asyncpg.Pool, text: str) -> None:
    """Хэш фиксируется только после успешной отправки — иначе неудачная
    публикация превратила бы собственный ретрай в «дубль»."""
    await pool.execute(
        """
        INSERT INTO published_hashes (hash, published_at) VALUES ($1, now())
        ON CONFLICT (hash) DO UPDATE SET published_at = now()
        """,
        dedup_hash(text),
    )


async def publish_loop(bot: Bot, pool: asyncpg.Pool) -> None:
    """matches WHERE published_at IS NULL → пост в топик интента, с троттлингом."""
    while True:
        try:
            rows = await pool.fetch(
                """
                SELECT mt.id AS match_id, mt.score,
                       m.text, m.sender_id, m.tg_msg_id, m.chat_id,
                       i.topic_id, i.code,
                       c.username, c.title AS chat_title
                FROM matches mt
                JOIN messages m ON m.id = mt.message_id
                JOIN intents i ON i.id = mt.intent_id
                JOIN source_chats c ON c.chat_id = m.chat_id
                WHERE mt.published_at IS NULL
                ORDER BY mt.id
                LIMIT 10
                """
            )
            for row in rows:
                if await _is_duplicate(pool, row["text"] or ""):
                    # дубль: закрываем match без публикации (published_msg_id остаётся NULL)
                    await pool.execute(
                        "UPDATE matches SET published_at = now() WHERE id = $1",
                        row["match_id"],
                    )
                    continue
                try:
                    sent = await bot.send_message(
                        chat_id=settings.forum_chat_id,
                        message_thread_id=row["topic_id"],
                        text=format_post(row),
                        disable_web_page_preview=True,
                    )
                    await pool.execute(
                        "UPDATE matches SET published_msg_id = $2, published_at = now() WHERE id = $1",
                        row["match_id"],
                        sent.message_id,
                    )
                    await _record_hash(pool, row["text"] or "")
                except TelegramRetryAfter as exc:
                    log.warning(f"rate limited, sleeping {exc.retry_after}s")
                    await asyncio.sleep(exc.retry_after)
                    break  # перечитаем очередь после паузы
                # Bot API ~20 сообщений/мин в одну группу
                await asyncio.sleep(settings.publish_min_interval_sec)
            await db.heartbeat(pool, SERVICE)
        except Exception:
            log.exception("publish loop tick failed")
        await asyncio.sleep(5)


async def _system_topic_id(bot: Bot, pool: asyncpg.Pool) -> int:
    topic_id = await pool.fetchval("SELECT value FROM kv WHERE key = $1", SYSTEM_TOPIC_KEY)
    if topic_id is not None:
        return int(topic_id)
    topic = await bot.create_forum_topic(chat_id=settings.forum_chat_id, name="Система")
    await pool.execute(
        "INSERT INTO kv (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        SYSTEM_TOPIC_KEY,
        str(topic.message_thread_id),
    )
    return topic.message_thread_id


async def system_events_loop(bot: Bot, pool: asyncpg.Pool) -> None:
    """Постит system_events в топик «Система»."""
    while True:
        try:
            rows = await pool.fetch(
                "SELECT id, level, message FROM system_events "
                "WHERE posted_at IS NULL ORDER BY id LIMIT 5"
            )
            if rows:
                topic_id = await _system_topic_id(bot, pool)
                for row in rows:
                    prefix = {"warning": "⚠️ ", "error": "🔴 "}.get(row["level"], "")
                    await bot.send_message(
                        chat_id=settings.forum_chat_id,
                        message_thread_id=topic_id,
                        text=f"{prefix}{html.escape(row['message'])}",
                    )
                    await pool.execute(
                        "UPDATE system_events SET posted_at = now() WHERE id = $1", row["id"]
                    )
                    await asyncio.sleep(settings.publish_min_interval_sec)
        except Exception:
            log.exception("system events loop tick failed")
        await asyncio.sleep(10)


async def watchdog_loop(pool: asyncpg.Pool) -> None:
    """Алерт, если от юзербота/воркера нет heartbeat дольше N минут (без спама)."""
    alerted: set[str] = set()
    while True:
        try:
            rows = await pool.fetch("SELECT service, beat_at FROM service_heartbeats")
            now = datetime.now(timezone.utc)
            for row in rows:
                service, beat_at = row["service"], row["beat_at"]
                stale = now - beat_at > timedelta(minutes=WATCHDOG_STALE_MIN)
                if stale and service not in alerted:
                    await db.system_event(
                        pool,
                        f"💀 Сервис {service} молчит с {beat_at:%H:%M:%S} UTC",
                        level="error",
                    )
                    alerted.add(service)
                elif not stale:
                    alerted.discard(service)
        except Exception:
            log.exception("watchdog tick failed")
        await asyncio.sleep(60)
