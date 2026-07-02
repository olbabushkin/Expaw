"""Команды управляющего бота. Все команды — только для админов из whitelist."""

import html
import re

import asyncpg
from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from common import db
from common.config import settings

router = Router()

# Единственный не-whitelist ответ: чужим — отказ.
@router.message.middleware()
async def admin_only(handler, event: Message, data):
    if event.from_user is None or event.from_user.id not in settings.admin_ids:
        if event.text and event.text.startswith("/"):
            await event.answer("⛔ Доступ запрещён.")
        return None
    return await handler(event, data)


_INVITE_RE = re.compile(r"(?:t\.me/(?:joinchat/|\+))([\w-]+)$")
_USERNAME_RE = re.compile(r"^@?([A-Za-z]\w{3,})$")
_LINK_USERNAME_RE = re.compile(r"t\.me/([A-Za-z]\w{3,})/?$")


def parse_chat_ref(arg: str) -> tuple[str | None, str | None]:
    """Возвращает (username, invite_hash)."""
    arg = arg.strip()
    if m := _INVITE_RE.search(arg):
        return None, m.group(1)
    if m := _LINK_USERNAME_RE.search(arg):
        return m.group(1), None
    if m := _USERNAME_RE.match(arg):
        return m.group(1), None
    return None, None


@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "Команды:\n"
        "/add_chat &lt;@username|ссылка&gt; — добавить чат-источник\n"
        "/remove_chat &lt;@username|id&gt; — убрать чат\n"
        "/list_chats — список чатов\n"
        "/add_intent &lt;code&gt; &lt;название&gt; — создать интент (+топик)\n"
        "/set_prompt &lt;code&gt; &lt;текст&gt; — критерии для LLM\n"
        "/set_prefilter &lt;code&gt; слово1, слово2, /regex/ — префильтр\n"
        "/set_threshold &lt;code&gt; &lt;0..1&gt;\n"
        "/toggle_intent &lt;code&gt;\n"
        "/list_intents\n"
        "/stats — статистика за сутки\n"
        "/retry_errors — перезапустить сообщения со статусом error"
    )


@router.message(Command("add_chat"))
async def cmd_add_chat(message: Message, command: CommandObject, pool: asyncpg.Pool):
    if not command.args:
        await message.answer("Использование: /add_chat @username или ссылка t.me/...")
        return
    username, invite_hash = parse_chat_ref(command.args)
    if username is None and invite_hash is None:
        await message.answer("Не понял ссылку. Поддерживаются @username, t.me/name, t.me/+hash.")
        return
    exists = await pool.fetchval(
        """
        SELECT id FROM source_chats
        WHERE (username = $1 AND $1 IS NOT NULL) OR (invite_hash = $2 AND $2 IS NOT NULL)
        """,
        username,
        invite_hash,
    )
    if exists:
        # Повторное добавление ранее удалённого чата — снова в очередь на join.
        await pool.execute(
            "UPDATE source_chats SET status = 'pending_join', last_error = NULL, added_by = $2 "
            "WHERE id = $1 AND status IN ('left', 'error')",
            exists,
            message.from_user.id,
        )
        await message.answer("Чат уже есть в списке (если был удалён — поставлен в очередь заново).")
        return
    await pool.execute(
        "INSERT INTO source_chats (username, invite_hash, status, added_by) "
        "VALUES ($1, $2, 'pending_join', $3)",
        username,
        invite_hash,
        message.from_user.id,
    )
    await db.audit(pool, message.from_user.id, "add_chat", {"arg": command.args})
    await message.answer(
        "Поставил в очередь на вступление. Юзербот вступает с задержками "
        f"(лимит {settings.join_daily_limit}/сутки) — следи за /list_chats."
    )


@router.message(Command("remove_chat"))
async def cmd_remove_chat(message: Message, command: CommandObject, pool: asyncpg.Pool):
    if not command.args:
        await message.answer("Использование: /remove_chat @username или chat_id")
        return
    arg = command.args.strip().lstrip("@")
    row = await pool.fetchrow(
        """
        SELECT id, status FROM source_chats
        WHERE username = $1 OR chat_id::text = $1 OR id::text = $1
        """,
        arg,
    )
    if row is None:
        await message.answer("Чат не найден.")
        return
    # pending_join ещё не вступили — просто помечаем left; активные — задача юзерботу.
    new_status = "leaving" if row["status"] == "active" else "left"
    await pool.execute(
        "UPDATE source_chats SET status = $2 WHERE id = $1", row["id"], new_status
    )
    await db.audit(pool, message.from_user.id, "remove_chat", {"arg": arg})
    await message.answer("Ок, юзербот выйдет из чата." if new_status == "leaving" else "Убрал из списка.")


@router.message(Command("list_chats"))
async def cmd_list_chats(message: Message, pool: asyncpg.Pool):
    rows = await pool.fetch(
        """
        SELECT c.title, c.username, c.status, c.last_error,
               (SELECT max(m.tg_date) FROM messages m WHERE m.chat_id = c.chat_id) AS last_msg
        FROM source_chats c
        WHERE c.status != 'left'
        ORDER BY c.created_at
        """
    )
    if not rows:
        await message.answer("Список чатов пуст. /add_chat, чтобы добавить.")
        return
    icons = {"active": "🟢", "pending_join": "⏳", "leaving": "🚪", "error": "❌"}
    lines = []
    for r in rows:
        name = html.escape(r["title"] or (f"@{r['username']}" if r["username"] else "invite-link"))
        last = r["last_msg"].strftime("%d.%m %H:%M") if r["last_msg"] else "—"
        line = f"{icons.get(r['status'], '•')} {name} · посл. сообщение: {last}"
        if r["status"] == "error" and r["last_error"]:
            line += f"\n   ошибка: {html.escape(r['last_error'][:100])}"
        lines.append(line)
    await message.answer("\n".join(lines))


@router.message(Command("add_intent"))
async def cmd_add_intent(message: Message, command: CommandObject, pool: asyncpg.Pool, bot: Bot):
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /add_intent dog_boarding Передержка собак")
        return
    code, title = parts[0].lower(), parts[1]
    if await pool.fetchval("SELECT 1 FROM intents WHERE code = $1", code):
        await message.answer("Интент с таким кодом уже есть.")
        return
    topic = await bot.create_forum_topic(chat_id=settings.forum_chat_id, name=title)
    await pool.execute(
        "INSERT INTO intents (code, title, topic_id) VALUES ($1, $2, $3)",
        code,
        title,
        topic.message_thread_id,
    )
    await db.audit(pool, message.from_user.id, "add_intent", {"code": code, "title": title})
    await message.answer(
        f"Интент <b>{html.escape(code)}</b> создан, топик «{html.escape(title)}» готов.\n"
        f"Теперь задай критерии: /set_prompt {code} ... и префильтр: /set_prefilter {code} ..."
    )


@router.message(Command("set_prompt"))
async def cmd_set_prompt(message: Message, command: CommandObject, pool: asyncpg.Pool):
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /set_prompt <code> <критерии интента для LLM>")
        return
    updated = await pool.execute(
        "UPDATE intents SET llm_prompt = $2 WHERE code = $1", parts[0].lower(), parts[1]
    )
    if updated == "UPDATE 0":
        await message.answer("Интент не найден.")
        return
    await db.audit(pool, message.from_user.id, "set_prompt", {"code": parts[0]})
    await message.answer("Промпт сохранён.")


@router.message(Command("set_prefilter"))
async def cmd_set_prefilter(message: Message, command: CommandObject, pool: asyncpg.Pool):
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: /set_prefilter <code> слово1, слово2, /regex/\n"
            "Слова — подстроки (регистр не важен), /.../ — регулярки."
        )
        return
    keywords = [w.strip() for w in parts[1].split(",") if w.strip()]
    updated = await pool.execute(
        "UPDATE intents SET prefilter = $2 WHERE code = $1", parts[0].lower(), keywords
    )
    if updated == "UPDATE 0":
        await message.answer("Интент не найден.")
        return
    await db.audit(pool, message.from_user.id, "set_prefilter", {"code": parts[0], "n": len(keywords)})
    await message.answer(f"Префильтр сохранён: {len(keywords)} шаблон(ов).")


@router.message(Command("set_threshold"))
async def cmd_set_threshold(message: Message, command: CommandObject, pool: asyncpg.Pool):
    parts = (command.args or "").split()
    try:
        code, value = parts[0].lower(), float(parts[1])
        assert 0.0 <= value <= 1.0
    except (IndexError, ValueError, AssertionError):
        await message.answer("Использование: /set_threshold <code> <0..1>")
        return
    updated = await pool.execute(
        "UPDATE intents SET threshold = $2 WHERE code = $1", code, value
    )
    await message.answer("Порог обновлён." if updated != "UPDATE 0" else "Интент не найден.")


@router.message(Command("toggle_intent"))
async def cmd_toggle_intent(message: Message, command: CommandObject, pool: asyncpg.Pool):
    code = (command.args or "").strip().lower()
    row = await pool.fetchrow(
        "UPDATE intents SET enabled = NOT enabled WHERE code = $1 RETURNING enabled", code
    )
    if row is None:
        await message.answer("Интент не найден.")
        return
    await message.answer(f"Интент {code}: {'включён ✅' if row['enabled'] else 'выключен ⏸'}")


@router.message(Command("list_intents"))
async def cmd_list_intents(message: Message, pool: asyncpg.Pool):
    rows = await pool.fetch("SELECT * FROM intents ORDER BY id")
    if not rows:
        await message.answer("Интентов нет. /add_intent, чтобы создать.")
        return
    lines = []
    for r in rows:
        state = "✅" if r["enabled"] else "⏸"
        prompt_ok = "промпт есть" if r["llm_prompt"] else "⚠️ нет промпта"
        pf = f"{len(r['prefilter'])} шаблонов" if r["prefilter"] else "⚠️ нет префильтра"
        lines.append(
            f"{state} <b>{html.escape(r['code'])}</b> — {html.escape(r['title'])} "
            f"(порог {r['threshold']:.2f}, {prompt_ok}, {pf})"
        )
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, pool: asyncpg.Pool):
    msg_stats = await pool.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status IN ('prefiltered', 'classified')) AS to_llm,
               count(*) FILTER (WHERE status = 'error') AS errors
        FROM messages WHERE created_at > now() - interval '24 hours'
        """
    )
    intent_rows = await pool.fetch(
        """
        SELECT i.code, count(m.id) AS cnt
        FROM intents i
        LEFT JOIN matches m ON m.intent_id = i.id AND m.created_at > now() - interval '24 hours'
        GROUP BY i.code ORDER BY cnt DESC
        """
    )
    total = msg_stats["total"] or 0
    to_llm = msg_stats["to_llm"] or 0
    pct = f"{100 * to_llm / total:.1f}%" if total else "—"
    lines = [
        "📊 За 24 часа:",
        f"Сообщений: {total}",
        f"Дошло до LLM: {to_llm} ({pct})",
        f"Ошибок: {msg_stats['errors']}",
        "",
        "Совпадения по интентам:",
    ]
    lines += [f"  {r['code']}: {r['cnt']}" for r in intent_rows] or ["  —"]
    await message.answer("\n".join(lines))


@router.message(Command("retry_errors"))
async def cmd_retry_errors(message: Message, pool: asyncpg.Pool):
    result = await pool.execute(
        "UPDATE messages SET status = 'new', error = NULL WHERE status = 'error'"
    )
    n = result.split()[-1]
    await db.audit(pool, message.from_user.id, "retry_errors", {"count": n})
    await message.answer(f"Вернул в очередь: {n} сообщений.")
