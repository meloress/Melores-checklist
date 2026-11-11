"""Chat: /start, ▶️ Boshlash, ketma-ket savol-javob, /ball.

Qaysi savolga javob kutilayotgani bazada (`checklist_days.asking_question_id`),
xotirada emas — bot 19:00–21:00 oralig'ida qayta ishga tushsa (deploy) ham
xodim yozgan javob yo'qolmaydi."""

import logging
from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy.exc import IntegrityError

from db import ChecklistQuestion, async_session
from jobs import START_KB, send, set_menu
import logic
from logic import MONTHS, fmt_ball, fmt_day, is_real_answer, local_now, open_day
import service

router = Router()
logger = logging.getLogger(__name__)

NOT_REGISTERED = "Siz hali ro'yxatdan o'tmagansiz. /start bosing va telefon raqamingizni ulashing."
CONTACT_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Raqamni ulashish", request_contact=True)]],
                                 resize_keyboard=True, one_time_keyboard=True)


async def _ask_contact(message: Message) -> None:
    await message.answer("Assalomu alaykum! Ro'yxatdan o'tish uchun pastdagi tugma bilan telefon "
                         "raqamingizni ulashing — admin sizni shu raqam bilan qo'shgan bo'lishi kerak.",
                         reply_markup=CONTACT_KB)


async def _ask_next(message: Message, telegram_id: int, day: date | None = None) -> None:
    """Shu kunning keyingi javobsiz savolini beradi va uni "kutilmoqda" deb belgilaydi."""
    day = day or open_day()
    async with async_session() as session:
        emp = await service.employee_by_tg(session, telegram_id)
        if emp is None:
            await message.answer(NOT_REGISTERED)
            return
        if day is None:
            await message.answer(f"Savollar bo'lsa, soat {logic.START_TXT} da keladi.")
            return
        questions, answers = await service.progress(session, emp.id, day)
        nxt = service.next_unanswered(questions, answers)
        if not questions:
            await message.answer("Bugun sizga savol yo'q.")
            return
        await service.mark_delivered(session, emp.id, day, nxt.id if nxt else None, set_asking=True)
        await session.commit()
    if nxt is None:
        await message.answer("✅ Rahmat! Barcha savollarga javob berdingiz.")
        return
    idx = questions.index(nxt) + 1
    ball = f" · javobsiz {fmt_ball(-nxt.points)} ball" if nxt.points else ""
    await message.answer(f"Savol {idx}/{len(questions)}{ball}:\n\n<b>{escape(nxt.text)}</b>", parse_mode="HTML")


@router.message(CommandStart())
async def start(message: Message) -> None:
    async with async_session() as session:
        emp = await service.employee_by_tg(session, message.from_user.id)
        if emp is None:
            await _ask_contact(message)
            return
        await service.add_member(session, emp.id)
        day = open_day()
        pending = False
        if day:
            questions, answers = await service.progress(session, emp.id, day)
            pending = service.next_unanswered(questions, answers) is not None
            if pending:
                await service.mark_delivered(session, emp.id, day)
        allowed = await service.scope(session, emp)
        await session.commit()
    await set_menu(message.bot, message.chat.id, allowed is None or bool(allowed))
    await message.answer(
        f"Assalomu alaykum, {escape(emp.full_name)}!\n"
        f"Rahbar savol yozsa, u soat {logic.START_TXT} da keladi — {logic.DEADLINE_TXT} gacha javob berasiz.\n"
        "Balingizni ko'rish: /ball",
        parse_mode="HTML",
        reply_markup=START_KB if pending else None,
    )


@router.message(F.contact)
async def on_contact(message: Message) -> None:
    """Ro'yxatdan o'tish: admin qo'shgan xodim telefoni bo'yicha topiladi va Telegram'iga bog'lanadi.
    Faqat O'Z raqami (boshqaning kontakt kartasini yuborib, o'rniga o'tib bo'lmasin)."""
    if message.contact.user_id != message.from_user.id:
        await message.answer("Iltimos, tugma bilan o'zingizning raqamingizni ulashing.", reply_markup=CONTACT_KB)
        return
    async with async_session() as session:
        if await service.employee_by_tg(session, message.from_user.id) is None:
            emp = await service.employee_by_phone(session, message.contact.phone_number)
            if emp is None:
                await message.answer("Bu raqam ro'yxatda yo'q. Adminga murojaat qiling — u sizni shu raqam "
                                     "bilan qo'shsin, keyin /start bosing.", reply_markup=ReplyKeyboardRemove())
                return
            if emp.telegram_id is not None and emp.telegram_id != message.from_user.id:
                await message.answer("Bu raqam boshqa Telegram akkauntga ulangan. Adminga murojaat qiling.",
                                     reply_markup=ReplyKeyboardRemove())
                return
            emp.telegram_id = message.from_user.id
            try:
                await session.commit()
            except IntegrityError:
                await message.answer("Bu Telegram akkaunt boshqa xodimga ulangan. Adminga murojaat qiling.",
                                     reply_markup=ReplyKeyboardRemove())
                return
            logger.info("checklist: xodim %s Telegram'ga ulandi", emp.id)
    await message.answer("✅ Ro'yxatdan o'tdingiz.", reply_markup=ReplyKeyboardRemove())
    await start(message)


@router.message(Command("ball"))
async def ball(message: Message) -> None:
    async with async_session() as session:
        emp = await service.employee_by_tg(session, message.from_user.id)
        if emp is None:
            await message.answer(NOT_REGISTERED)
            return
        checklist, other, plus = await service.month_breakdown(session, emp)
        pay_day = await service.payment_day(session, emp)
    lines = [f"📊 <b>{MONTHS[local_now().month]}</b>", f"Checklist jarimasi: {fmt_ball(-checklist)}"]
    if emp.trello_employee_id is not None:  # umumiy ball — Trello botdan
        lines += [f"Muddat va boshqa jarimalar: {fmt_ball(-other)}",
                  f"Bonus: {fmt_ball(plus)}",
                  f"Jami: {fmt_ball(plus - checklist - other)}"]
    if pay_day:
        lines.append(f"Oylik kuni: {fmt_day(pay_day)}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "cl:start")
async def begin(callback: CallbackQuery) -> None:
    await callback.answer()
    await _ask_next(callback.message, callback.from_user.id)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    async with async_session() as session:
        emp = await service.employee_by_tg(session, message.from_user.id)
        row = await service.asking(session, emp.id) if emp else None
        question = await session.get(ChecklistQuestion, row.asking_question_id) if row else None
        answering = question is not None and question.employee_id == emp.id
        if answering and not is_real_answer(message.text):
            await message.answer("Javob bo'sh — iltimos, so'z yoki raqam bilan yozing.")
            return
        readers = []
        if answering:
            await service.save_answer(session, emp.id, question, row.day, message.text)
            await session.commit()
            readers = await service.reader_chats(session, question)
    for chat_id in readers:
        try:
            await send(message.bot, chat_id, f"📩 <b>{escape(emp.full_name)}</b> javob berdi\n\n"
                                             f"<b>{escape(question.text)}</b>\n— {escape(message.text[:3000])}")
        except Exception:
            logger.exception("checklist: javob o'quvchiga ketmadi (%s)", chat_id)
    # Hech qaysi savol so'ralmagan bo'lsa — joriy savol beriladi (javob sifatida
    # saqlanmaydi). Yarim tundan keyin kechagi savollarga yozilsa — o'sha kun davom etadi.
    await _ask_next(message, message.from_user.id, row.day if answering else None)


@router.message()
async def other(message: Message) -> None:
    await message.answer("Iltimos, javobni matn bilan yozing.")
