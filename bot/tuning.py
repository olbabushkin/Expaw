"""Тюнинг интента по обратной связи: промпт + префильтр.

Ключевой инвариант префильтра — НЕ ПРОПУСКАТЬ: новые ключевые слова только
добавляются к текущим (объединение), покрытие никогда не сужается. Точность
дешёвого фильтра не важна — ложных кандидатов отсеет LLM-этап.
"""

import asyncpg

from bot.keywords import improve_prompt, suggest_keywords
from common import db
from common.log import setup
from common.textutils import normalize
from worker.prefilter import _pattern_matches

log = setup("bot.tuning")

# автотюнинг: раз в столько новых реакций по интенту
AUTOTUNE_EVERY = 5
MAX_PREFILTER = 50
FEEDBACK_SAMPLE = 30


async def run_tune(pool: asyncpg.Pool, intent: asyncpg.Record) -> dict:
    """Улучшает критерии и расширяет префильтр интента. Возвращает сводку."""
    rows = await pool.fetch(
        """
        SELECT f.verdict, m.text FROM feedback f
        JOIN messages m ON m.id = f.message_id
        WHERE f.intent_id = $1 AND m.text IS NOT NULL AND m.text != ''
        ORDER BY f.created_at DESC LIMIT $2
        """,
        intent["id"],
        FEEDBACK_SAMPLE,
    )
    false_positives = [r["text"] for r in rows if r["verdict"] == -1]
    confirmed = [r["text"] for r in rows if r["verdict"] == 1]
    if not false_positives and not confirmed:
        raise ValueError("нет обратной связи")

    new_prompt = await improve_prompt(
        intent["title"], intent["llm_prompt"], false_positives, confirmed
    )
    suggested = await suggest_keywords(intent["title"], new_prompt)

    # Только расширяем: union текущего префильтра и новых предложений.
    current = list(intent["prefilter"] or [])
    added = [k for k in suggested if k not in current]
    merged = (current + added)[:MAX_PREFILTER]

    # Контроль полноты: каждое подтверждённое сообщение должно проходить префильтр.
    uncovered = [
        t for t in confirmed
        if not any(_pattern_matches(p, normalize(t)) for p in merged)
    ]

    await pool.execute(
        "UPDATE intents SET llm_prompt = $2, prefilter = $3 WHERE id = $1",
        intent["id"],
        new_prompt,
        merged,
    )
    return {
        "prompt": new_prompt,
        "added": added,
        "total_prefilter": len(merged),
        "likes": len(confirmed),
        "dislikes": len(false_positives),
        "uncovered": len(uncovered),
    }


async def maybe_autotune(pool: asyncpg.Pool, intent_id: int) -> None:
    """Автотюнинг после каждых AUTOTUNE_EVERY новых реакций по интенту.

    Счётчик-маркер в kv защищает от повторного запуска на тех же реакциях.
    """
    total = await pool.fetchval(
        "SELECT count(*) FROM feedback WHERE intent_id = $1", intent_id
    )
    marker_key = f"autotune_marker:{intent_id}"
    marker = int(
        await pool.fetchval("SELECT value FROM kv WHERE key = $1", marker_key) or 0
    )
    if total < marker + AUTOTUNE_EVERY:
        return
    # двигаем маркер до запуска, чтобы параллельная реакция не запустила второй тюнинг
    await pool.execute(
        "INSERT INTO kv (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        marker_key,
        str(total),
    )
    intent = await pool.fetchrow(
        "SELECT id, code, title, llm_prompt, prefilter FROM intents WHERE id = $1",
        intent_id,
    )
    if intent is None or not intent["llm_prompt"]:
        return
    try:
        res = await run_tune(pool, intent)
    except Exception:
        log.exception("autotune failed")
        await db.system_event(
            pool,
            f"⚠️ Автотюнинг «{intent['title']}» не удался — детали в логах бота",
            level="warning",
        )
        return
    note = (
        f"🧠 Автотюнинг «{intent['title']}» (набралось {total} реакций, "
        f"в выборке 👍{res['likes']}/👎{res['dislikes']}): критерии обновлены, "
        f"префильтр +{len(res['added'])} шаблонов (всего {res['total_prefilter']})."
    )
    if res["uncovered"]:
        note += f" ⚠️ {res['uncovered']} подтверждённых сообщений вне префильтра!"
    note += f" Применить ко всем сообщениям: /recalc (интент {intent['code']})"
    await db.system_event(pool, note)
    log.info(f"autotune done for intent {intent['code']}")
