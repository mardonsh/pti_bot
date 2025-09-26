from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.db import Database
from app.handlers import announce, commands, compliance, media, review
from app.middlewares.context import ContextMiddleware
from app.services.autosend import SchedulerService


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = Settings.load()
    database = Database(config.database_url)
    await database.connect()

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)

    scheduler = AsyncIOScheduler(timezone=config.tz)
    scheduler_service = SchedulerService(scheduler=scheduler, bot=bot, db=database)

    dispatcher.include_router(commands.router)
    dispatcher.include_router(review.router)
    dispatcher.include_router(announce.router)
    dispatcher.include_router(compliance.router)
    dispatcher.include_router(media.router)

    context_middleware = ContextMiddleware(db=database, config=config, scheduler=scheduler_service)
    dispatcher.message.middleware(context_middleware)
    dispatcher.callback_query.middleware(context_middleware)

    await scheduler_service.initialize()
    scheduler.start()

    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await database.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
