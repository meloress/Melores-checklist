import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import jobs
import webapp
from config import settings
from db import init_db
from handlers import router
from logic import DEADLINE, TZ, reminder_times

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
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([BotCommand(command="start", description="Boshlash"),
                               BotCommand(command="ball", description="Oylik ball va jarimalar")])
    if settings.miniapp_base_url:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="Panel", web_app=WebAppInfo(url=settings.miniapp_base_url)))

    scheduler = AsyncIOScheduler(timezone=TZ)
    for t in reminder_times():
        scheduler.add_job(jobs.remind, "cron", hour=t.hour, minute=t.minute, args=[bot], misfire_grace_time=600)
    scheduler.add_job(jobs.close_today, "cron", hour=DEADLINE.hour, minute=DEADLINE.minute, args=[bot],
                      misfire_grace_time=3600)
    scheduler.start()

    try:
        await jobs.catch_up(bot)
    except Exception:
        logging.exception("catch_up xatosi")

    await webapp.run(bot, me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
