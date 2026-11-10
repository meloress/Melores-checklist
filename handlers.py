"""Chat: /start, ▶️ Boshlash, ketma-ket savol-javob, /ball.

Qaysi savolga javob kutilayotgani bazada (`checklist_days.asking_question_id`),
xotirada emas — bot 19:00–21:00 oralig'ida qayta ishga tushsa (deploy) ham
xodim yozgan javob yo'qolmaydi."""

from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from db import ChecklistQuestion, async_session
from jobs import START_KB
from logic import DEADLINE_TXT, MONTHS, START_TXT, fmt_ball, fmt_day, local_now, open_day
import service

router = Router()

NOT_REGISTERED = ("Siz tizimda topilmadingiz. Avval asosiy Melores botida ro'yxatdan o'ting, "
                  "keyin bu yerda /start bosing.")


async def _ask_next(message: Message, telegram_id: int, day: date | None = None) -> None:
    """Shu kunning keyingi javobsiz savolini beradi va uni "kutilmoqda" deb belgilaydi."""
    day = day or open_day()
    async with async_session() as session:
        emp = await service.employee_by_tg(session, telegram_id)
        if emp is None:
            await message.answer(NOT_REGISTERED)
            return
        if day is None:
            await message.answer(f"Savollar bo'lsa, soat {START_TXT} da keladi.")
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
    await message.answer(f"Savol {idx}/{len(questions)}:\n\n<b>{escape(nxt.text)}</b>", parse_mode="HTML")


@router.message(CommandStart())
async def start(message: Message) -> None:
    async with async_session() as session:
        emp = await service.employee_by_tg(session, message.from_user.id)
        if emp is None:
            await message.answer(NOT_REGISTERED)
            return
        await service.add_member(session, emp.id)
        day = open_day()
        pending = False
        if day:
            questions, answers = await service.progress(session, emp.id, day)
            pending = service.next_unanswered(questions, answers) is not None
            if pending:
                await service.mark_delivered(session, emp.id, day)
        await session.commit()
    await message.answer(
        f"Assalomu alaykum, {escape(emp.full_name)}!\n"
        f"Rahbar savol yozsa, u soat {START_TXT} da keladi — {DEADLINE_TXT} gacha javob berasiz.\n"
        "Balingizni ko'rish: /ball",
        parse_mode="HTML",
        reply_markup=START_KB if pending else None,
    )


@router.message(Command("ball"))
async def ball(message: Message) -> None:
    async with async_session() as session:
        emp = await service.employee_by_tg(session, message.from_user.id)
        if emp is None:
            await message.answer(NOT_REGISTERED)
            return
        checklist, other, plus = await service.month_breakdown(session, emp.id)
    lines = [f"📊 <b>{MONTHS[local_now().month]}</b>",
             f"Checklist jarimasi: {fmt_ball(-checklist)}",
             f"Muddat va boshqa jarimalar: {fmt_ball(-other)}",
             f"Bonus: {fmt_ball(plus)}",
             f"Jami: {fmt_ball(plus - checklist - other)}"]
    if emp.next_payment_date:
        lines.append(f"Oylik kuni: {fmt_day(emp.next_payment_date)}")
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
        if answering:
            await service.save_answer(session, emp.id, question, row.day, message.text)
            await session.commit()
    # Hech qaysi savol so'ralmagan bo'lsa — joriy savol beriladi (javob sifatida
    # saqlanmaydi). Yarim tundan keyin kechagi savollarga yozilsa — o'sha kun davom etadi.
    await _ask_next(message, message.from_user.id, row.day if answering else None)


@router.message()
async def other(message: Message) -> None:
    await message.answer("Iltimos, javobni matn bilan yozing.")
