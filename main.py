import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import jobs
import webapp
from config import settings
import service
from db import async_session, init_db
from handlers import router
from logic import TZ

TRELLO_BOTS = {"melorestrellobot"}  # production Trello bot — bu token bilan ishlamaymiz


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(token=settings.bot_token)
    # Trello botning tokeni adashib qo'yilsa — u botning menyu tugmasi va
    # buyruqlarini buzib qo'ymasin (2026-11-09 da shunday bo'lgan).
    me = await bot.get_me()
    if me.username.lower() in TRELLO_BOTS:
        raise SystemExit(f"BOT_TOKEN @{me.username} — bu Trello bot! Checklist uchun alohida bot tokeni kerak.")
    logging.info("Checklist bot: @%s", me.username)

    await init_db()
    async with async_session() as session:
        await service.load_schedule(session)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([BotCommand(command="start", description="Boshlash"),
                               BotCommand(command="ball", description="Oylik ball va jarimalar")])
    if settings.miniapp_base_url:
        menu_task = asyncio.create_task(jobs.sync_menus(bot))  # noqa: F841 (havola: GC o'chirmasin). Panel faqat admin/rahbar/nazoratchiga

    jobs.SCHEDULER = AsyncIOScheduler(timezone=TZ)
    jobs.reschedule(bot)
    jobs.SCHEDULER.start()

    try:
        await jobs.catch_up(bot)
    except Exception:
        logging.exception("catch_up xatosi")

    await webapp.run(bot, me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
