"""Boshlanishdan (19:00) har 30 daqiqada eslatma, muddatda (21:00) jarima + yig'ma hisobot."""

import asyncio
import logging
import re
from datetime import date, datetime, timezone
from html import unescape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from db import ChecklistDay, ChecklistViewer, Employee, async_session
from logic import (DEADLINE_TXT, MONTHS, digest_block, fmt_ball, fmt_day, is_past_deadline, local_now,
                   penalty_for, split_messages)
import service

logger = logging.getLogger(__name__)
SEND_GAP = 0.05  # Telegram: ~30 xabar/soniya

START_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Boshlash", callback_data="cl:start")]])


async def send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """HTML xabar; kesilgan blok HTML'ni buzsa — oddiy matn bo'lib ketadi.
    Bloklagan/ochmagan bo'lsa False."""
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
        return True
    except TelegramForbiddenError:
        return False
    except TelegramBadRequest as exc:
        if "chat not found" in str(exc).lower():
            return False
        await bot.send_message(chat_id, unescape(re.sub(r"<[^>]+>", "", text)), **kwargs)
        return True
    finally:
        await asyncio.sleep(SEND_GAP)


async def remind(bot: Bot) -> None:
    day = local_now().date()
    async with async_session() as session:
        rate = await service.penalty_rate(session)
        for emp in await service.participants(session, day):
            questions, answers = await service.progress(session, emp.id, day)
            done = sum(1 for q in questions if q.id in answers)
            if done == len(questions):
                continue
            if done == 0:
                text = (f"📝 <b>Kunlik checklist</b>: {len(questions)} ta savol.\n"
                        f"Soat {DEADLINE_TXT} gacha javob bering — javobsiz har savolga {fmt_ball(-rate)} ball.")
            else:
                text = f"⏰ Eslatma: {done}/{len(questions)} savolga javob berdingiz. {DEADLINE_TXT} gacha tugating."
            try:
                if await send(bot, emp.telegram_id, text, reply_markup=START_KB):
                    await service.mark_delivered(session, emp.id, day)
                    await session.commit()
                else:
                    logger.info("checklist: %s ga yetib bormadi", emp.full_name)
            except Exception:
                logger.exception("checklist eslatma: xodim %s", emp.id)
                await session.rollback()


async def close_day(bot: Bot, day: date) -> None:
    """Kunni yopadi: jarima → kpi_logs, xodimga xabar, rahbar/nazoratchiga yig'ma.
    Idempotent: allaqachon yopilgan xodim qayta hisoblanmaydi."""
    blocks: dict[int, str] = {}
    notices: list[tuple[int, str]] = []
    async with async_session() as session:
        rate = await service.penalty_rate(session)
        bps = await service.balls_per_day_shift(session)
        rows = {r.employee_id: r for r in await session.scalars(select(ChecklistDay).where(ChecklistDay.day == day))}
        for emp in await service.participants(session, day):
            row = rows.get(emp.id)
            if row is not None and row.closed_at is not None:
                continue
            if row is None:
                row = ChecklistDay(employee_id=emp.id, day=day)
                session.add(row)
            questions, answers = await service.progress(session, emp.id, day)
            answered = sum(1 for q in questions if q.id in answers)
            delivered = row.delivered_at is not None
            # Savollar umuman yetib bormagan bo'lsa — jarima yo'q, yig'mada ⚠️ bilan ko'rinadi.
            penalty = penalty_for(len(questions) - answered, rate) if delivered else 0.0
            row.asked, row.answered, row.penalty = len(questions), answered, penalty
            row.closed_at = datetime.now(timezone.utc)
            if penalty:
                pay_day = await service.write_penalty(
                    session, emp, penalty, f"{len(questions) - answered} ta savolga javob berilmadi ({fmt_day(day)})"
                )
                cl, other, _ = await service.month_breakdown(session, emp.id)
                notices.append((emp.telegram_id,
                    f"⚠️ Sizga jarima yozildi: <b>{fmt_ball(-penalty)} ball</b>\n"
                    f"Sabab: kunlik checklistdagi {len(questions) - answered} ta savolga {DEADLINE_TXT} gacha javob berilmadi.\n"
                    + (f"Har {bps} ball uchun oylik kuni 1 kun kechikadi.\n" if bps else "")
                    + f"\n{MONTHS[local_now().month]}: checklist {fmt_ball(-cl)}, muddat va boshqa {fmt_ball(-other)}."
                    + (f"\nOylik kuni: {fmt_day(pay_day)}" if pay_day else "")))
            blocks[emp.id] = digest_block(
                emp.full_name, [(q.text, answers[q.id].answer_text if q.id in answers else None) for q in questions],
                penalty, delivered,
            )
        await session.commit()

        if not blocks:
            return
        admins = list(await session.scalars(select(Employee).where(
            Employee.role == service.ADMIN, Employee.is_active.is_(True), Employee.telegram_id.is_not(None))))
        viewer_rows = list(await session.execute(select(ChecklistViewer.viewer_id, ChecklistViewer.employee_id)))
        viewers = {v.id: v for v in await session.scalars(select(Employee).where(
            Employee.id.in_({r.viewer_id for r in viewer_rows}), Employee.is_active.is_(True),
            Employee.telegram_id.is_not(None)))}

    for chat_id, text in notices:
        await send(bot, chat_id, text)

    header = f"📋 <b>Kunlik checklist — {fmt_day(day)}</b>"
    recipients: dict[int, list[int]] = {a.telegram_id: list(blocks) for a in admins}
    for viewer_id, employee_id in viewer_rows:
        v = viewers.get(viewer_id)
        if v and v.telegram_id not in {a.telegram_id for a in admins} and employee_id in blocks:
            recipients.setdefault(v.telegram_id, []).append(employee_id)
    for chat_id, emp_ids in recipients.items():
        wanted = set(emp_ids)
        for part in split_messages(header, [b for i, b in blocks.items() if i in wanted]):
            await send(bot, chat_id, part)
    logger.info("checklist %s yopildi: %s xodim, %s jarima", day, len(blocks), len(notices))


async def catch_up(bot: Bot) -> None:
    """Jarayon 21:00 da ishlamay qolgan bo'lsa (deploy/restart): yopilmagan
    kunlarni yopadi. Faqat savol YETIB BORGAN kunlar — aks holda bot o'chiq
    turgan kechqurun hammaga jarima yozilmasin (yetmaganlarga jarima yo'q)."""
    async with async_session() as session:
        days = set(await session.scalars(
            select(ChecklistDay.day).where(ChecklistDay.closed_at.is_(None)).distinct()))
    for day in sorted(days):
        if is_past_deadline(day):
            await close_day(bot, day)


async def close_today(bot: Bot) -> None:
    await close_day(bot, local_now().date())
