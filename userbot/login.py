"""Интерактивная генерация session-файла юзербота.

Запускать локально (нужен ввод кода из Telegram):
    TG_API_ID=... TG_API_HASH=... TG_SESSION=./data/session/userbot python -m userbot.login
Полученный файл монтируется в контейнер volume'ом, права 600.
"""

import asyncio
import pathlib

from telethon import TelegramClient

from common.config import settings


async def main() -> None:
    pathlib.Path(settings.tg_session).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(settings.tg_session, settings.tg_api_id, settings.tg_api_hash)
    await client.start()
    me = await client.get_me()
    print(f"logged in as {me.first_name} (id={me.id}), session: {settings.tg_session}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
