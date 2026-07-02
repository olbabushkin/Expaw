"""Юзербот-слушатель: push через MTProto, менеджер пула чатов, страховочная сверка.

Аккаунт юзербота САМ НИКУДА НЕ ВСТУПАЕТ И НЕ ВЫХОДИТ: владелец вступает в чаты
сам, менеджер пула лишь проверяет членство и включает мониторинг. Связь с ботом —
только через БД (статусы pending_join / active / left).
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import asyncpg
from telethon import TelegramClient, events, utils
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready

from common import db
from common.config import settings
from common.log import setup

log = setup("userbot")

POOL_TICK_SEC = 30
SERVICE = "userbot"

# Кэш активных чатов: chat_id -> True. Обновляется в менеджере пула.
active_chats: set[int] = set()


def _msg_raw(message) -> dict:
    """Компактный служебный слепок сообщения (без полного to_dict — он огромный)."""
    return {
        "has_media": message.media is not None,
        "fwd": message.fwd_from is not None,
        "via_bot_id": message.via_bot_id,
        "reply_to_msg_id": message.reply_to_msg_id,
        "sender_bot": bool(getattr(message.sender, "bot", False)),
    }


async def save_message(pool: asyncpg.Pool, chat_id: int, message) -> bool:
    inserted = await pool.fetchval(
        """
        INSERT INTO messages (chat_id, tg_msg_id, sender_id, text, tg_date, raw)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (chat_id, tg_msg_id) DO NOTHING
        RETURNING id
        """,
        chat_id,
        message.id,
        message.sender_id,
        message.message or "",
        message.date,
        _msg_raw(message),
    )
    return inserted is not None


async def on_new_message(pool: asyncpg.Pool, event) -> None:
    chat_id = utils.get_peer_id(event.message.peer_id)
    if chat_id not in active_chats:
        return
    try:
        await save_message(pool, chat_id, event.message)
    except Exception:
        log.exception("failed to save message")


async def on_edited(pool: asyncpg.Pool, event) -> None:
    """Обновляем текст, только если сообщение ещё не ушло в классификацию."""
    chat_id = utils.get_peer_id(event.message.peer_id)
    if chat_id not in active_chats:
        return
    try:
        await pool.execute(
            """
            UPDATE messages SET text = $3
            WHERE chat_id = $1 AND tg_msg_id = $2 AND status = 'new'
            """,
            chat_id,
            event.message.id,
            event.message.message or "",
        )
    except Exception:
        log.exception("failed to update edited message")


async def refresh_active_chats(pool: asyncpg.Pool) -> None:
    rows = await pool.fetch(
        "SELECT chat_id FROM source_chats WHERE status = 'active' AND chat_id IS NOT NULL"
    )
    active_chats.clear()
    active_chats.update(r["chat_id"] for r in rows)


# Троттлинг проверок членства: не чаще раза в N секунд на чат,
# чтобы не дёргать resolve/GetParticipant слишком часто.
MEMBERSHIP_CHECK_EVERY_SEC = 300
_last_check: dict[int, float] = {}


async def _member_entity(client: TelegramClient, row: asyncpg.Record):
    """Возвращает entity чата, если аккаунт уже состоит в нём, иначе None."""
    if row["invite_hash"]:
        result = await client(CheckChatInviteRequest(row["invite_hash"]))
        return result.chat if isinstance(result, ChatInviteAlready) else None
    try:
        entity = await client.get_entity(row["username"])
        await client(GetParticipantRequest(entity, "me"))
        return entity
    except (UserNotParticipantError, ChannelPrivateError):
        return None


async def process_pending(client: TelegramClient, pool: asyncpg.Pool) -> None:
    """pending_join → active, как только владелец сам вступил в чат.

    Юзербот НЕ вступает в чаты — только проверяет членство.
    """
    rows = await pool.fetch(
        "SELECT id, username, invite_hash FROM source_chats "
        "WHERE status = 'pending_join' ORDER BY created_at"
    )
    now = time.monotonic()
    for row in rows:
        if now - _last_check.get(row["id"], 0) < MEMBERSHIP_CHECK_EVERY_SEC:
            continue
        _last_check[row["id"]] = now
        try:
            entity = await _member_entity(client, row)
            if entity is None:
                continue  # ждём, пока владелец вступит сам
            chat_id = utils.get_peer_id(entity)
            await pool.execute(
                """
                UPDATE source_chats
                SET chat_id = $2, title = $3, status = 'active', joined_at = now(), last_error = NULL
                WHERE id = $1
                """,
                row["id"],
                chat_id,
                getattr(entity, "title", None),
            )
            title = getattr(entity, "title", row["username"])
            if settings.backfill_days > 0:
                # История подтянет и точку отсчёта для страховочной сверки
                asyncio.create_task(
                    backfill_history(client, pool, chat_id, entity, title)
                )
            else:
                # Точка отсчёта: последнее сообщение, чтобы сверка не утянула
                # историю до включения мониторинга.
                try:
                    latest = await client.get_messages(entity, limit=1)
                    if latest:
                        await save_message(pool, chat_id, latest[0])
                except Exception:
                    log.exception("failed to save baseline message")
            await db.system_event(pool, f"✅ Начал мониторить «{title}»")
            log.info(f"monitoring chat {chat_id}")
        except FloodWaitError:
            raise  # обрабатывается глобально в pool_manager
        except Exception as exc:  # noqa: BLE001 — битый username/ссылка и т.п.
            await pool.execute(
                "UPDATE source_chats SET status = 'error', last_error = $2 WHERE id = $1",
                row["id"],
                str(exc),
            )
            await db.system_event(
                pool,
                f"⚠️ Не удалось проверить чат {row['username'] or row['invite_hash']}: {exc}",
                level="error",
            )
            log.warning(f"membership check failed: {exc}")


async def backfill_history(
    client: TelegramClient, pool: asyncpg.Pool, chat_id: int, entity, title: str
) -> None:
    """Фоновая загрузка истории чата за BACKFILL_DAYS при включении мониторинга.

    Идём от новых к старым до границы окна или лимита сообщений; сохранённые
    сообщения проходят обычный пайплайн (префильтр -> LLM -> публикация).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.backfill_days)
    saved = scanned = 0
    try:
        async for message in client.iter_messages(
            entity, limit=settings.backfill_max_messages
        ):
            if message.date < cutoff:
                break
            scanned += 1
            if await save_message(pool, chat_id, message):
                saved += 1
            if scanned % 200 == 0:
                await asyncio.sleep(2)  # бережём лимиты на больших чатах
        await db.system_event(
            pool,
            f"📥 «{title}»: загружена история за {settings.backfill_days} дн. — "
            f"{saved} сообщений в обработку",
        )
        log.info(f"backfill for chat {chat_id}: saved {saved} of {scanned} scanned")
    except FloodWaitError as exc:
        await db.system_event(
            pool,
            f"⏳ FloodWait {exc.seconds}s при загрузке истории «{title}», "
            f"успел сохранить {saved}",
            level="warning",
        )
        log.warning(f"backfill flood wait {exc.seconds}s for chat {chat_id}")
    except Exception:
        log.exception(f"backfill failed for chat {chat_id}")


async def pool_manager(client: TelegramClient, pool: asyncpg.Pool) -> None:
    """Фоновая корутина: проверка членства в pending-чатах + кэш активных + heartbeat."""
    while True:
        try:
            await refresh_active_chats(pool)
            await process_pending(client, pool)
            await db.heartbeat(pool, SERVICE)
        except FloodWaitError as exc:
            wait = exc.seconds
            level = "error" if wait > 3600 else "warning"
            await db.system_event(pool, f"⏳ FloodWait {wait}s у юзербота", level=level)
            log.warning(f"FloodWait {wait}s, sleeping")
            await asyncio.sleep(wait)
        except Exception:
            log.exception("pool manager tick failed")
        await asyncio.sleep(POOL_TICK_SEC)


async def catchup_sweep(client: TelegramClient, pool: asyncpg.Pool) -> None:
    """Редкая страховочная сверка: ловит гэпы, которые catch_up мог пропустить."""
    while True:
        await asyncio.sleep(settings.sync_interval_min * 60)
        rows = await pool.fetch(
            "SELECT id, chat_id FROM source_chats WHERE status = 'active' AND chat_id IS NOT NULL"
        )
        for row in rows:
            chat_id = row["chat_id"]
            try:
                last_id = await pool.fetchval(
                    "SELECT coalesce(max(tg_msg_id), 0) FROM messages WHERE chat_id = $1",
                    chat_id,
                )
                fetched = 0
                async for message in client.iter_messages(
                    chat_id, min_id=last_id, limit=200
                ):
                    if await save_message(pool, chat_id, message):
                        fetched += 1
                await pool.execute(
                    "UPDATE source_chats SET last_synced_at = now() WHERE id = $1",
                    row["id"],
                )
                if fetched:
                    log.info(f"sweep recovered {fetched} messages in chat {chat_id}")
            except FloodWaitError as exc:
                log.warning(f"FloodWait {exc.seconds}s in sweep")
                await asyncio.sleep(exc.seconds)
            except Exception:
                log.exception(f"sweep failed for chat {chat_id}")
            await asyncio.sleep(settings.sync_chat_pause_sec)


async def main() -> None:
    pool = await db.create_pool()
    client = TelegramClient(
        settings.tg_session,
        settings.tg_api_id,
        settings.tg_api_hash,
        catch_up=True,           # подтягивает пропущенное за даунтайм
        flood_sleep_threshold=60,
    )

    client.add_event_handler(
        lambda e: on_new_message(pool, e), events.NewMessage(incoming=True)
    )
    client.add_event_handler(lambda e: on_edited(pool, e), events.MessageEdited())

    await client.start()
    await refresh_active_chats(pool)
    await db.system_event(pool, "🟢 Юзербот запущен")
    log.info("userbot started")

    asyncio.create_task(pool_manager(client, pool))
    asyncio.create_task(catchup_sweep(client, pool))

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
