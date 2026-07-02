"""Юзербот-слушатель: push через MTProto, менеджер пула чатов, страховочная сверка.

Связь с ботом — только через БД: бот ставит статусы-задачи (pending_join /
leaving), юзербот их исполняет и пишет результат.
"""

import asyncio
import random
from datetime import datetime, timedelta, timezone

import asyncpg
from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

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


async def _join_allowed(pool: asyncpg.Pool) -> bool:
    """Консервативные лимиты: не больше N join в сутки, пауза 5–15 мин между ними."""
    row = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE joined_at > now() - interval '24 hours') AS day_cnt,
               max(joined_at) AS last_join
        FROM source_chats
        """
    )
    if row["day_cnt"] >= settings.join_daily_limit:
        return False
    if row["last_join"] is not None:
        delay = timedelta(
            minutes=random.uniform(settings.join_min_delay_min, settings.join_max_delay_min)
        )
        if datetime.now(timezone.utc) - row["last_join"] < delay:
            return False
    return True


async def process_joins(client: TelegramClient, pool: asyncpg.Pool) -> None:
    if not await _join_allowed(pool):
        return
    row = await pool.fetchrow(
        "SELECT id, username, invite_hash FROM source_chats "
        "WHERE status = 'pending_join' ORDER BY created_at LIMIT 1"
    )
    if row is None:
        return
    try:
        if row["invite_hash"]:
            try:
                updates = await client(ImportChatInviteRequest(row["invite_hash"]))
                entity = updates.chats[0]
            except UserAlreadyParticipantError:
                entity = await client.get_entity(f"https://t.me/+{row['invite_hash']}")
        else:
            entity = await client.get_entity(row["username"])
            await client(JoinChannelRequest(entity))
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
        await db.system_event(pool, f"✅ Вступил в чат «{getattr(entity, 'title', row['username'])}»")
        log.info(f"joined chat {chat_id}")
    except FloodWaitError:
        raise  # обрабатывается глобально в pool_manager
    except Exception as exc:  # noqa: BLE001
        await pool.execute(
            "UPDATE source_chats SET status = 'error', last_error = $2 WHERE id = $1",
            row["id"],
            str(exc),
        )
        await db.system_event(
            pool,
            f"⚠️ Не удалось вступить в чат {row['username'] or row['invite_hash']}: {exc}",
            level="error",
        )
        log.warning(f"join failed: {exc}")


async def process_leaves(client: TelegramClient, pool: asyncpg.Pool) -> None:
    rows = await pool.fetch(
        "SELECT id, chat_id, title FROM source_chats WHERE status = 'leaving'"
    )
    for row in rows:
        try:
            if row["chat_id"] is not None:
                await client(LeaveChannelRequest(await client.get_entity(row["chat_id"])))
            await pool.execute(
                "UPDATE source_chats SET status = 'left' WHERE id = $1", row["id"]
            )
            await db.system_event(pool, f"🚪 Вышел из чата «{row['title'] or row['id']}»")
        except FloodWaitError:
            raise
        except Exception as exc:  # noqa: BLE001
            await pool.execute(
                "UPDATE source_chats SET status = 'error', last_error = $2 WHERE id = $1",
                row["id"],
                str(exc),
            )
            log.warning(f"leave failed: {exc}")


async def pool_manager(client: TelegramClient, pool: asyncpg.Pool) -> None:
    """Фоновая корутина: join/leave-задачи из БД + кэш активных чатов + heartbeat."""
    while True:
        try:
            await refresh_active_chats(pool)
            await process_leaves(client, pool)
            await process_joins(client, pool)
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
