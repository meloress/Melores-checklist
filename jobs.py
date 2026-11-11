"""Boshlanishdan (19:00) har 30 daqiqada eslatma, muddatda (21:00) jarima + yig'ma hisobot."""

import asyncio
import logging
import re
from datetime import date, datetime, timezone
from html import unescape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonDefault, MenuButtonWebApp,
                           WebAppInfo)
from sqlalchemy import func, or_, select

from config import settings
from db import ChecklistDay, ChecklistViewer, Employee, async_session
from logic import (BLOCKED, DEADLINE_TXT, DELIVERED, MESSAGE_LIMIT, MONTHS, NEVER_STARTED, NOT_SENT, digest_block,
                   digest_summary, fmt_ball, fmt_day, is_past_deadline, local_now, penalty_for)
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


async def set_menu(bot: Bot, chat_id: int, on: bool) -> None:
    """"Panel" tugmasi faqat admin/rahbar/nazoratchiga; qolganlarga oddiy menyu."""
    if not settings.miniapp_base_url:
        return
    button = (MenuButtonWebApp(text="Panel", web_app=WebAppInfo(url=settings.miniapp_base_url)) if on
              else MenuButtonDefault())
    try:
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=button)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass  # botni hali ochmagan — /start bosganda o'rnatiladi
    await asyncio.sleep(SEND_GAP)


async def sync_menus(bot: Bot) -> None:
    """Hamma xodimning menyu tugmasini huquqiga moslaydi (ishga tushishda va
    admin rahbar/nazoratchini o'zgartirganda)."""
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())  # umumiy: Panel yo'q
        async with async_session() as session:
            chats = await service.panel_chats(session)
        for chat_id, on in chats:
            await set_menu(bot, chat_id, on)
    except Exception:
        logger.exception("menyu tugmalari yangilanmadi")


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
                at_risk = penalty_for(questions, answers, rate)
                text = (f"📝 <b>Bugungi savollar</b>: {len(questions)} ta.\n"
                        f"Soat {DEADLINE_TXT} gacha javob bering — javob bermasangiz jami {fmt_ball(-at_risk)} ball.")
            else:
                text = f"⏰ Eslatma: {done}/{len(questions)} savolga javob berdingiz. {DEADLINE_TXT} gacha tugating."
            try:
                if await send(bot, emp.telegram_id, text, reply_markup=START_KB):
                    await service.mark_delivered(session, emp.id, day)
                else:
                    await service.mark_blocked(session, emp.id, day)
                    logger.info("checklist: %s ga yetib bormadi (bloklagan/ochmagan)", emp.full_name)
                await session.commit()
            except Exception:
                logger.exception("checklist eslatma: xodim %s", emp.id)
                await session.rollback()


async def close_day(bot: Bot, day: date) -> None:
    """Kunni yopadi: jarima → kpi_logs, xodimga xabar, rahbar/nazoratchiga yig'ma.
    Idempotent: allaqachon yopilgan xodim qayta hisoblanmaydi."""
    blocks: dict[int, str] = {}
    summary: dict[int, tuple] = {}
    notices: list[tuple[int, str]] = []
    async with async_session() as session:
        # Ikki nusxa bir vaqtda yopmasin (21:00 da deploy: eski + yangi jarayon,
        # yoki catch_up + cron) — aks holda bitta xodimga 2 marta jarima yoziladi.
        # Ikkinchisi birinchisi commit qilguncha kutadi, keyin hammasini "yopilgan" ko'radi.
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"checklist-close:{day}"))))
        rate = await service.penalty_rate(session)
        members = await service.member_ids(session)
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
            # Yetib bordi → jarima. Yetmadi, lekin botni avval ochgan (demak keyin
            # bloklagan) → baribir jarima, aks holda bloklab qochish mumkin. Hech
            # ochmagan → jarima yo'q (u savolni ko'rmagan), yig'mada ⚠️.
            if row.delivered_at is not None:
                status = DELIVERED
            elif emp.id not in members:
                status = NEVER_STARTED
            else:
                # Telegram haqiqatan "bloklangan" degan bo'lsa — jarima; yuborishga
                # umuman urinilmagan bo'lsa (bot o'chiq edi) — yo'q.
                status = BLOCKED if row.blocked_at is not None else NOT_SENT
            penalty = penalty_for(questions, answers, rate) if status in (DELIVERED, BLOCKED) else 0.0
            row.asked, row.answered, row.penalty = len(questions), answered, penalty
            row.closed_at = datetime.now(timezone.utc)
            row.asking_question_id = None
            if penalty:
                row.kpi_log_id, pay_day = await service.write_penalty(
                    session, emp, penalty, f"{len(questions) - answered} ta savolga javob berilmadi ({fmt_day(day)})"
                )
                cl, other, _ = await service.month_breakdown(session, emp.id)
                notices.append((emp.telegram_id,
                    f"⚠️ Sizga jarima yozildi: <b>{fmt_ball(-penalty)} ball</b>\n"
                    f"Sabab: {fmt_day(day)} dagi {len(questions) - answered} ta savolga {DEADLINE_TXT} gacha javob berilmadi.\n"
                    + (f"Har {bps} ball uchun oylik kuni 1 kun kechikadi.\n" if bps else "")
                    + f"\n{MONTHS[local_now().month]}: checklist {fmt_ball(-cl)}, muddat va boshqa {fmt_ball(-other)}."
                    + (f"\nOylik kuni: {fmt_day(pay_day)}" if pay_day else "")))
            blocks[emp.id] = digest_block(
                emp.full_name, [(q.text, answers[q.id].answer_text if q.id in answers else None) for q in questions],
                penalty, status,
            )
            summary[emp.id] = (emp.full_name, answered, len(questions), penalty, status)
        await session.commit()

        if not blocks:
            return
        # Admin va rahbarlar — hamma xodimning yig'masi.
        leaders = await service.leader_ids(session)
        admins = list(await session.scalars(select(Employee).where(
            or_(Employee.role == service.ADMIN, Employee.id.in_(leaders)),
            Employee.is_active.is_(True), Employee.telegram_id.is_not(None))))
        viewer_rows = list(await session.execute(select(ChecklistViewer.viewer_id, ChecklistViewer.employee_id)))
        viewers = {v.id: v for v in await session.scalars(select(Employee).where(
            Employee.id.in_({r.viewer_id for r in viewer_rows}), Employee.is_active.is_(True),
            Employee.telegram_id.is_not(None)))}

    for chat_id, text in notices:
        await send(bot, chat_id, text)

    # Bitta qisqa xabar (kim javob bermadi, jami jarima). Javoblar ham sig'sa —
    # shu xabarning o'zida, aks holda Panel tugmasi (50 xodimga 15 ta xabar emas).
    header = f"📋 <b>Kunlik checklist — {fmt_day(day)}</b>"
    recipients: dict[int, list[int]] = {a.telegram_id: list(blocks) for a in admins}
    for viewer_id, employee_id in viewer_rows:
        v = viewers.get(viewer_id)
        if v and v.telegram_id not in {a.telegram_id for a in admins} and employee_id in blocks:
            recipients.setdefault(v.telegram_id, []).append(employee_id)
    kb = None
    if settings.miniapp_base_url:
        url = f"{settings.miniapp_base_url.rstrip('/')}/?screen=answers&day={day.isoformat()}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Javoblarni ko'rish",
                                                                         web_app=WebAppInfo(url=url))]])
    for chat_id, emp_ids in recipients.items():
        wanted = [i for i in blocks if i in set(emp_ids)]
        short = digest_summary(header, [summary[i] for i in wanted])
        full = short + "\n\n" + "\n\n".join(blocks[i] for i in wanted)
        await send(bot, chat_id, full if len(full) <= MESSAGE_LIMIT else short, reply_markup=kb)
    logger.info("checklist %s yopildi: %s xodim, %s jarima", day, len(blocks), len(notices))


async def catch_up(bot: Bot) -> None:
    """Jarayon 21:00 da ishlamay qolgan bo'lsa (deploy/restart): yopilmagan
    kunlarni yopadi. Faqat kamida bir xabar YETIB BORGAN kunlar (checklist_days
    qatori bor) — bot butun kechqurun o'chiq bo'lsa, hech kimga jarima yo'q."""
    async with async_session() as session:
        days = set(await session.scalars(
            select(ChecklistDay.day).where(ChecklistDay.closed_at.is_(None)).distinct()))
    for day in sorted(days):
        if is_past_deadline(day):
            await close_day(bot, day)


async def close_today(bot: Bot) -> None:
    await close_day(bot, local_now().date())
