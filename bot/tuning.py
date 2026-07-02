"""Тюнинг интента по обратной связи: промпт + префильтр.

Ключевой инвариант префильтра — НЕ ПРОПУСКАТЬ: новые ключевые слова только
добавляются к текущим (объединение), покрытие никогда не сужается. Точность
дешёвого фильтра не важна — ложных кандидатов отсеет LLM-этап.
"""

import asyncio

import asyncpg

from bot.keywords import improve_prompt, suggest_keywords
from common import db
from common.log import setup
from common.textutils import normalize
from worker.classifier import classify
from worker.prefilter import _pattern_matches

log = setup("bot.tuning")

# автотюнинг: раз в столько новых реакций по интенту
AUTOTUNE_EVERY = 5
MAX_PREFILTER = 50
FEEDBACK_SAMPLE = 30
BACKTEST_CONCURRENCY = 5


async def _predict(text: str, intent: asyncpg.Record, prompt: str) -> bool:
    """Матчится ли сообщение под заданной версией критериев."""
    result = await classify(
        text,
        [{"code": intent["code"], "title": intent["title"], "llm_prompt": prompt}],
    )
    for item in result.get("results", []):
        if item.get("intent_code") == intent["code"]:
            return bool(item.get("match")) and (
                float(item.get("confidence", 0)) >= intent["threshold"]
            )
    return False


async def _backtest(
    intent: asyncpg.Record, prompt: str, confirmed: list[str], false_positives: list[str]
) -> dict:
    """Прогоняет размеченные сообщения через новую версию критериев.

    Все размеченные сообщения матчились СТАРОЙ версией (иначе не были бы
    опубликованы), поэтому старая точность известна без прогона:
    верны только лайки. Новую версию проверяем классификатором.
    """
    sem = asyncio.Semaphore(BACKTEST_CONCURRENCY)

    async def check(text: str) -> bool:
        async with sem:
            return await _predict(text, intent, prompt)

    preds_confirmed, preds_fp = await asyncio.gather(
        asyncio.gather(*(check(t) for t in confirmed)),
        asyncio.gather(*(check(t) for t in false_positives)),
    )
    kept = sum(preds_confirmed)          # 👍, которые всё ещё матчятся
    killed = sum(not p for p in preds_fp)  # 👎, которые перестали матчиться
    total = len(confirmed) + len(false_positives)
    return {
        "kept": kept,
        "lost": len(confirmed) - kept,
        "killed": killed,
        "remaining_fp": len(false_positives) - killed,
        "old_accuracy": len(confirmed) / total,
        "new_accuracy": (kept + killed) / total,
    }


async def run_tune(pool: asyncpg.Pool, intent: asyncpg.Record) -> dict:
    """Улучшает критерии, проверяет их бэктестом на размеченных сообщениях
    и применяет (вместе с расширением префильтра) только если не теряется
    ни одно подтверждённое совпадение. Возвращает сводку.
    """
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
    bt = await _backtest(intent, new_prompt, confirmed, false_positives)
    summary = {
        "prompt": new_prompt,
        "likes": len(confirmed),
        "dislikes": len(false_positives),
        "backtest": bt,
        "applied": bt["lost"] == 0,
        "added": [],
        "total_prefilter": len(intent["prefilter"] or []),
        "uncovered": 0,
    }
    if not summary["applied"]:
        # новая версия теряет подтверждённые совпадения — не применяем
        return summary

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
    summary.update(added=added, total_prefilter=len(merged), uncovered=len(uncovered))
    return summary


def backtest_report(res: dict) -> str:
    bt = res["backtest"]
    return (
        f"📊 Бэктест на размеченных (👍{res['likes']}/👎{res['dislikes']}):\n"
        f"👍 сохранено {bt['kept']}/{res['likes']}"
        + (f" (потеряно {bt['lost']}!)" if bt["lost"] else "")
        + f"\n👎 отсеяно {bt['killed']}/{res['dislikes']}"
        + (f" (осталось {bt['remaining_fp']})" if bt["remaining_fp"] else "")
        + f"\nТочность: {bt['old_accuracy']:.0%} → {bt['new_accuracy']:.0%}"
    )


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
        "SELECT id, code, title, llm_prompt, prefilter, threshold FROM intents WHERE id = $1",
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
    if res["applied"]:
        note = (
            f"🧠 Автотюнинг «{intent['title']}» (набралось {total} реакций): "
            f"критерии обновлены, префильтр +{len(res['added'])} "
            f"(всего {res['total_prefilter']}).\n{backtest_report(res)}"
        )
        if res["uncovered"]:
            note += f"\n⚠️ {res['uncovered']} подтверждённых сообщений вне префильтра!"
        note += f"\nПрименить ко всем сообщениям: /recalc (интент {intent['code']})"
    else:
        note = (
            f"🧠 Автотюнинг «{intent['title']}»: новая версия критериев теряет "
            f"{res['backtest']['lost']} подтверждённых совпадений — НЕ применена, "
            f"действующие критерии не тронуты.\n{backtest_report(res)}\n"
            f"Можно повторить вручную: /tune_prompt {intent['code']}"
        )
        await db.system_event(pool, note, level="warning")
        log.info(f"autotune for {intent['code']}: not applied (regression)")
        return
    await db.system_event(pool, note)
    log.info(f"autotune done for intent {intent['code']}")
