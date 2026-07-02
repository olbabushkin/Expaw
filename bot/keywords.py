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
