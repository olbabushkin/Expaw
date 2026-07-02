"""Worker классификации: Postgres как очередь (FOR UPDATE SKIP LOCKED).

Масштабируется на несколько экземпляров без Redis/Celery: каждый воркер
захватывает свою пачку строк, конкуренты их не видят.
"""

import asyncio

import asyncpg

from common import db
from common.config import settings
from common.log import setup
from worker.classifier import classify
from worker.prefilter import find_candidates

log = setup("worker")

SERVICE = "worker"
PREFILTER_BATCH = 50
LLM_BATCH = 5
IDLE_SLEEP_SEC = 3
RETENTION_TICK_SEC = 3600


async def load_intents(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, code, title, llm_prompt, prefilter, threshold "
        "FROM intents WHERE enabled ORDER BY id"
    )
    return [dict(r) for r in rows]


async def prefilter_pass(pool: asyncpg.Pool, intents: list[dict]) -> int:
    """new → skipped | prefiltered(+кандидаты). Возвращает число обработанных."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, text, raw FROM messages
                WHERE status = 'new'
                ORDER BY id LIMIT $1
                FOR UPDATE SKIP LOCKED
                """,
                PREFILTER_BATCH,
            )
            for row in rows:
                candidates = find_candidates(row["text"], row["raw"], intents)
                if candidates:
                    await conn.execute(
                        "UPDATE messages SET status = 'prefiltered', intent_candidates = $2 "
                        "WHERE id = $1",
                        row["id"],
                        candidates,
                    )
                else:
                    await conn.execute(
                        "UPDATE messages SET status = 'skipped' WHERE id = $1", row["id"]
                    )
            return len(rows)


async def classify_one(conn: asyncpg.Connection, row: asyncpg.Record, intents: list[dict]) -> None:
    by_code = {i["code"]: i for i in intents}
    candidates = [by_code[c] for c in row["intent_candidates"] if c in by_code]
    if not candidates:
        # интенты выключили/удалили, пока сообщение ждало
        await conn.execute("UPDATE messages SET status = 'skipped' WHERE id = $1", row["id"])
        return
    result = await classify(row["text"], candidates)
    # Сообщение относится максимум к одному интенту: модель просят выбрать
    # лучший, но на случай нескольких match=true страхуемся выбором по score.
    passed = []
    for item in result.get("results", []):
        intent = by_code.get(item.get("intent_code"))
        if intent is None:
            continue
        confidence = float(item.get("confidence", 0))
        if item.get("match") and confidence >= intent["threshold"]:
            passed.append((confidence, intent))
    if passed:
        confidence, intent = max(passed, key=lambda p: p[0])
        await conn.execute(
            """
            INSERT INTO matches (message_id, intent_id, score, llm_response)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (message_id, intent_id) DO NOTHING
            """,
            row["id"],
            intent["id"],
            confidence,
            result,  # полный ответ LLM — пригодится для тюнинга промптов
        )
    await conn.execute("UPDATE messages SET status = 'classified' WHERE id = $1", row["id"])


async def classify_pass(pool: asyncpg.Pool, intents: list[dict]) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, text, intent_candidates FROM messages
                WHERE status = 'prefiltered'
                ORDER BY id LIMIT $1
                FOR UPDATE SKIP LOCKED
                """,
                LLM_BATCH,
            )
            for row in rows:
                try:
                    await classify_one(conn, row, intents)
                except Exception as exc:  # noqa: BLE001 — ошибки API после ретраев SDK
                    log.warning(f"classification failed for message {row['id']}: {exc}")
                    await conn.execute(
                        "UPDATE messages SET status = 'error', error = $2 WHERE id = $1",
                        row["id"],
                        str(exc)[:500],
                    )
            return len(rows)


async def retention_loop(pool: asyncpg.Pool) -> None:
    """Чистка skipped-сообщений, иначе таблица распухнет."""
    while True:
        try:
            result = await pool.execute(
                "DELETE FROM messages WHERE status = 'skipped' "
                "AND created_at < now() - make_interval(days => $1)",
                settings.skipped_retention_days,
            )
            deleted = result.split()[-1]
            if deleted != "0":
                log.info(f"retention: deleted {deleted} skipped messages")
        except Exception:
            log.exception("retention tick failed")
        await asyncio.sleep(RETENTION_TICK_SEC)


async def main() -> None:
    pool = await db.create_pool()
    asyncio.create_task(retention_loop(pool))
    await db.system_event(pool, "🟢 Worker классификации запущен")
    log.info("worker started")

    while True:
        try:
            intents = await load_intents(pool)
            processed = await prefilter_pass(pool, intents)
            classified = await classify_pass(pool, intents)
            await db.heartbeat(pool, SERVICE)
            if processed == 0 and classified == 0:
                await asyncio.sleep(IDLE_SLEEP_SEC)
        except Exception:
            log.exception("worker tick failed")
            await asyncio.sleep(IDLE_SLEEP_SEC)


if __name__ == "__main__":
    asyncio.run(main())
