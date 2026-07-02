"""Управляющий бот: long polling + фоновые корутины публикации и мониторинга."""

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.handlers import router
from bot.publisher import publish_loop, system_events_loop, watchdog_loop
from common import db
from common.config import settings
from common.log import setup

log = setup("bot")


async def main() -> None:
    pool = await db.create_pool()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(publish_loop(bot, pool))
    asyncio.create_task(system_events_loop(bot, pool))
    asyncio.create_task(watchdog_loop(pool))

    await db.system_event(pool, "🟢 Управляющий бот запущен")
    log.info("bot started")
    # pool прокидывается во все хендлеры через workflow data
    await dp.start_polling(bot, pool=pool)


if __name__ == "__main__":
    asyncio.run(main())
