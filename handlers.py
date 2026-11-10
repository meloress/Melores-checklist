"""Chat: /start, ▶️ Boshlash, ketma-ket savol-javob, /ball.

Holat (qaysi savolga javob kutilyapti) aiogram FSM'da (xotirada). Restartda
yo'qolsa zarari yo'q: keyingi matn javob deb saqlanmaydi, bot joriy savolni
qayta beradi."""

from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from db import ChecklistQuestion, async_session
from jobs import START_KB
from logic import DEADLINE_TXT, MONTHS, START_TXT, fmt_ball, fmt_day, local_now, open_day
import service

router = Router()

NOT_REGISTERED = ("Siz tizimda topilmadingiz. Avval asosiy Melores botida ro'yxatdan o'ting, "
                  "keyin bu yerda /start bosing.")


class Answering(StatesGroup):
    question = State()


async def _ask_next(message: Message, telegram_id: int, state: FSMContext, day: date | None = None) -> None:
    day = day or open_day()
    async with async_session() as session:
        emp = await service.employee_by_tg(session, telegram_id)
        if emp is None:
            await state.clear()
            await message.answer(NOT_REGISTERED)
            return
        if day is None:
            await state.clear()
            await message.answer(f"Savollar har kuni soat {START_TXT} da keladi.")
            return
        questions, answers = await service.progress(session, emp.id, day)
        nxt = service.next_unanswered(questions, answers)
        if not questions:
            await state.clear()
            await message.answer("Bugun siz uchun savol yo'q.")
            return
        if nxt is None:
            await state.clear()
            await message.answer("✅ Rahmat! Bugungi barcha savollarga javob berdingiz.")
            return
        await service.mark_delivered(session, emp.id, day)
        await session.commit()
    await state.set_state(Answering.question)
    await state.update_data(question_id=nxt.id, day=day.isoformat())
    idx = questions.index(nxt) + 1
    await message.answer(f"Savol {idx}/{len(questions)}:\n\n<b>{escape(nxt.text)}</b>", parse_mode="HTML")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session() as session:
        emp = await service.employee_by_tg(session, message.from_user.id)
        if emp is None:
            await message.answer(NOT_REGISTERED)
            return
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
        f"Har kuni soat {START_TXT} da savollar keladi, {DEADLINE_TXT} gacha javob berasiz.\n"
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
async def begin(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _ask_next(callback.message, callback.from_user.id, state)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext) -> None:
    if await state.get_state() != Answering.question.state:
        await _ask_next(message, message.from_user.id, state)
        return
    data = await state.get_data()
    day = date.fromisoformat(data["day"])
    async with async_session() as session:
        emp = await service.employee_by_tg(session, message.from_user.id)
        question = await session.get(ChecklistQuestion, data["question_id"])
        if emp is not None and question is not None and question.employee_id == emp.id:
            await service.save_answer(session, emp.id, question, day, message.text)
            await session.commit()
    # Kecha boshlangan suhbat yarim tundan keyin davom etsa — o'sha kun savollari.
    await _ask_next(message, message.from_user.id, state, day)


@router.message()
async def other(message: Message) -> None:
    await message.answer("Iltimos, javobni matn bilan yozing.")
