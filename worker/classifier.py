"""Ступень 2: классификация кандидатов через Claude (structured outputs).

Один запрос на сообщение со всеми интентами-кандидатами. Ответ — строгий JSON
по схеме (output_config.format гарантирует валидность). Ретраи 429/5xx делает
сам SDK (max_retries), после исчерпания — исключение наружу, воркер пометит
сообщение как error.
"""

import anthropic

from common.config import settings

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=3)

_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "intent_code": {"type": "string"},
                    "match": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["intent_code", "match", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_SYSTEM = (
    "Ты — классификатор сообщений из Telegram-чатов. Тебе дают текст сообщения "
    "и список интентов с критериями. Для КАЖДОГО интента реши, соответствует ли "
    "сообщение критериям, и оцени уверенность от 0 до 1. Сообщение соответствует "
    "интенту, только если автор явно выражает описанную в критериях потребность "
    "или предложение, а не просто упоминает тему. reason — одно короткое "
    "предложение на русском."
)


def build_user_prompt(text: str, intents: list[dict]) -> str:
    blocks = []
    for i in intents:
        blocks.append(f"### Интент `{i['code']}` — {i['title']}\nКритерии: {i['llm_prompt']}")
    intents_block = "\n\n".join(blocks)
    return (
        f"Интенты:\n\n{intents_block}\n\n"
        f"Сообщение:\n<<<\n{text}\n>>>\n\n"
        "Верни results с записью для каждого интента."
    )


async def classify(text: str, intents: list[dict]) -> dict:
    """intents: [{code, title, llm_prompt}] → {'results': [...]} (валидный JSON)."""
    import json

    response = await _client.messages.create(
        model=settings.llm_model,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[{"role": "user", "content": build_user_prompt(text, intents)}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    raw = next(b.text for b in response.content if b.type == "text")
    return json.loads(raw)
