"""Подбор ключевых слов префильтра через LLM по критериям интента."""

import json

import anthropic

from common.config import settings
from common.textutils import normalize

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["keywords"],
    "additionalProperties": False,
}

_SYSTEM = (
    "Ты помогаешь настроить префильтр для мониторинга Telegram-чатов. По названию "
    "и критериям интента подбери 10–15 шаблонов для первичного отбора сообщений. "
    "Каждый шаблон ищется как ПОДСТРОКА в нормализованном тексте (нижний регистр, "
    "е вместо ё), поэтому давай ОСНОВЫ слов без окончаний: «передержк», а не "
    "«передержка» — так одна основа ловит все словоформы. Включай синонимы, "
    "разговорные и англоязычные варианты, если они встречаются в русскоязычных "
    "чатах. Префильтр должен быть широким: лучше лишний кандидат, чем пропущенное "
    "сообщение — точную фильтрацию делает следующий этап (LLM-классификатор). "
    "Не включай слишком общие слова, которые встречаются в любом сообщении "
    "(например «нужно», «ищу», «срочно»)."
)


_TUNE_SCHEMA = {
    "type": "object",
    "properties": {"prompt": {"type": "string"}},
    "required": ["prompt"],
    "additionalProperties": False,
}

_TUNE_SYSTEM = (
    "Ты улучшаешь критерии интента для классификатора сообщений из Telegram-чатов. "
    "Тебе дают текущие критерии и размеченные админом примеры: ложные срабатывания "
    "(классификатор ошибочно посчитал сообщение подходящим) и подтверждённые "
    "совпадения. Перепиши критерии так, чтобы исключить подобные ложные "
    "срабатывания, но сохранить все подтверждённые. Добавь явные исключения "
    "(«Не подходит: ...») по мотивам ошибок. Критерии — сжатый текст на русском, "
    "без вступлений и пояснений, не длиннее ~120 слов."
)


async def improve_prompt(
    title: str, current: str, false_positives: list[str], confirmed: list[str]
) -> str:
    def block(items: list[str]) -> str:
        return "\n".join(f"- {t[:300]}" for t in items) or "- (нет примеров)"

    user = (
        f"Интент: {title}\n\nТекущие критерии:\n{current}\n\n"
        f"Ложные срабатывания (дизлайки):\n{block(false_positives)}\n\n"
        f"Подтверждённые совпадения (лайки):\n{block(confirmed)}\n\n"
        "Верни улучшенные критерии."
    )
    response = await _client.messages.create(
        model=settings.llm_model,
        max_tokens=800,
        system=_TUNE_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _TUNE_SCHEMA}},
    )
    raw = next(b.text for b in response.content if b.type == "text")
    return json.loads(raw)["prompt"].strip()


async def suggest_keywords(title: str, criteria: str) -> list[str]:
    response = await _client.messages.create(
        model=settings.llm_model,
        max_tokens=500,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"Интент: {title}\nКритерии: {criteria}\n\nПодбери шаблоны префильтра.",
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    raw = next(b.text for b in response.content if b.type == "text")
    keywords = json.loads(raw)["keywords"]
    # нормализуем так же, как текст сообщений при матчинге, и убираем дубли
    seen: dict[str, None] = {}
    for kw in keywords:
        kw = normalize(kw)
        if kw and kw not in seen:
            seen[kw] = None
    return list(seen)[:20]
