"""Bot, job va Mini App ishlatadigan baza so'rovlari. Har biri chaqiruvchining
sessiyasida ishlaydi; commit'ni chaqiruvchi qiladi."""

from datetime import date, datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import (AppSetting, ChecklistAnswer, ChecklistDay, ChecklistMember, ChecklistQuestion,
                ChecklistSettings, ChecklistViewer, Employee, KpiLog)
from logic import REASON_PREFIX, month_bounds, payment_date

ADMIN = "admin"


async def employee_by_tg(session, telegram_id: int) -> Employee | None:
    return await session.scalar(
        select(Employee).where(Employee.telegram_id == telegram_id, Employee.is_active.is_(True))
    )


async def day_questions(session, employee_id: int, day: date) -> list[ChecklistQuestion]:
    """Shu kunga yozilgan (bir martalik) faol savollar."""
    return list(await session.scalars(
        select(ChecklistQuestion)
        .where(ChecklistQuestion.employee_id == employee_id,
               ChecklistQuestion.is_active.is_(True),
               ChecklistQuestion.day == day)
        .order_by(ChecklistQuestion.position, ChecklistQuestion.id)
    ))


async def day_answers(session, employee_id: int, day: date) -> dict[int, ChecklistAnswer]:
    rows = await session.scalars(
        select(ChecklistAnswer).where(ChecklistAnswer.employee_id == employee_id, ChecklistAnswer.day == day)
    )
    return {a.question_id: a for a in rows}


async def progress(session, employee_id: int, day: date):
    questions = await day_questions(session, employee_id, day)
    answers = await day_answers(session, employee_id, day)
    return questions, answers


def next_unanswered(questions, answers) -> ChecklistQuestion | None:
    return next((q for q in questions if q.id not in answers), None)


async def save_answer(session, employee_id: int, question: ChecklistQuestion, day: date, text: str) -> None:
    await session.execute(
        pg_insert(ChecklistAnswer).values(
            employee_id=employee_id, question_id=question.id, day=day,
            question_text=question.text, answer_text=text[:4000],
        ).on_conflict_do_nothing(constraint="uq_checklist_answer")
    )


async def reader_chats(session, question: ChecklistQuestion) -> list[int]:
    """Javobni o'qiydiganlar (savolda belgilangan), javob bergan xodimning o'zidan tashqari."""
    ids = [i for i in question.reader_ids or [] if i != question.employee_id]
    if not ids:
        return []
    return list(await session.scalars(select(Employee.telegram_id).where(
        Employee.id.in_(ids), Employee.is_active.is_(True), Employee.telegram_id.is_not(None))))


async def mark_delivered(session, employee_id: int, day: date, asking_question_id: int | None = None,
                         set_asking: bool = False) -> None:
    """Savol xabari yetib bordi (birinchi marta). `set_asking` — chatda hozir
    shu savolga javob kutilyapti (None = tugadi)."""
    stmt = pg_insert(ChecklistDay).values(employee_id=employee_id, day=day, delivered_at=datetime.now(timezone.utc),
                                          asking_question_id=asking_question_id)
    set_ = {"delivered_at": func.coalesce(ChecklistDay.delivered_at, stmt.excluded.delivered_at)}
    if set_asking:
        set_["asking_question_id"] = stmt.excluded.asking_question_id
    await session.execute(stmt.on_conflict_do_update(constraint="uq_checklist_day", set_=set_))


async def mark_blocked(session, employee_id: int, day: date) -> None:
    stmt = pg_insert(ChecklistDay).values(employee_id=employee_id, day=day, blocked_at=datetime.now(timezone.utc))
    await session.execute(stmt.on_conflict_do_update(constraint="uq_checklist_day",
                                                     set_={"blocked_at": stmt.excluded.blocked_at}))


async def asking(session, employee_id: int) -> ChecklistDay | None:
    """Xodim hozir qaysi kunning qaysi savoliga javob yozyapti (eng oxirgisi)."""
    return await session.scalar(
        select(ChecklistDay).where(ChecklistDay.employee_id == employee_id,
                                   ChecklistDay.asking_question_id.is_not(None))
        .order_by(ChecklistDay.day.desc()).limit(1)
    )


async def add_member(session, employee_id: int) -> None:
    await session.execute(pg_insert(ChecklistMember).values(employee_id=employee_id).on_conflict_do_nothing())


async def member_ids(session) -> set[int]:
    return set(await session.scalars(select(ChecklistMember.employee_id)))


async def penalty_rate(session) -> float:
    s = await session.get(ChecklistSettings, 1)
    return s.penalty_per_question if s else 0.2


async def participants(session, day: date) -> list[Employee]:
    """Shu kunga savol yozilgan, faol, Telegram'ga ulangan xodimlar."""
    has_q = (select(ChecklistQuestion.employee_id)
             .where(ChecklistQuestion.is_active.is_(True), ChecklistQuestion.day == day))
    return list(await session.scalars(
        select(Employee).where(Employee.is_active.is_(True), Employee.telegram_id.is_not(None),
                               Employee.id.in_(has_q)).order_by(Employee.full_name)
    ))


async def viewer_ids_of(session, viewer_id: int) -> set[int]:
    return set(await session.scalars(
        select(ChecklistViewer.employee_id).where(ChecklistViewer.viewer_id == viewer_id)
    ))


async def scope(session, employee: Employee) -> set[int] | None:
    """Mini App huquqi: None = hammasi (admin), to'plam = faqat shu xodimlar
    (nazoratchi), bo'sh to'plam = ruxsat yo'q."""
    if employee.role == ADMIN:
        return None
    return await viewer_ids_of(session, employee.id)


async def write_penalty(session, employee: Employee, score: float, reason: str) -> tuple[int, date]:
    """kpi_logs'ga minus ball yozadi, oylik kunini qayta hisoblaydi.
    (kpi_log id, yangi oylik kuni) qaytaradi."""
    log = KpiLog(employee_id=employee.id, score=-score, reason=f"{REASON_PREFIX} {reason}")
    session.add(log)
    await session.flush()
    return log.id, await recompute_payment_date(session, employee)


async def cancel_penalty(session, row: ChecklistDay, employee: Employee, by_employee_id: int) -> date:
    """Jarimani bekor qiladi: kpi_logs yozuvini o'chiradi, kim bekor qilganini
    yozadi, oylik kunini qayta hisoblaydi. `row.penalty` tarix uchun qoladi."""
    if row.kpi_log_id:
        await session.execute(delete(KpiLog).where(KpiLog.id == row.kpi_log_id, KpiLog.employee_id == employee.id))
    row.kpi_log_id = None
    row.cancelled_by, row.cancelled_at = by_employee_id, datetime.now(timezone.utc)
    await session.flush()
    return await recompute_payment_date(session, employee)


async def recompute_payment_date(session, employee: Employee) -> date:
    """Trello bot qoidasi bilan bir xil (`penalty_service.update_payment_date_if_needed`):
    joriy oyning minus ballari yig'indisidan oylik kunini noldan hisoblaydi."""
    start, end = month_bounds(datetime.now(timezone.utc).date())
    total_minus = await session.scalar(
        select(func.coalesce(func.sum(-KpiLog.score), 0))
        .where(KpiLog.employee_id == employee.id, KpiLog.score < 0,
               KpiLog.created_at >= start, KpiLog.created_at < end)
    )
    employee.next_payment_date = payment_date(start.date(), float(total_minus), await balls_per_day_shift(session))
    return employee.next_payment_date


async def month_breakdown(session, employee_id: int) -> tuple[float, float, float]:
    """Joriy oy: (checklist jarimasi, boshqa jarimalar, bonuslar) — musbat sonlar."""
    start, end = month_bounds(datetime.now(timezone.utc).date())
    rows = await session.execute(
        select(KpiLog.score, KpiLog.reason)
        .where(KpiLog.employee_id == employee_id, KpiLog.created_at >= start, KpiLog.created_at < end)
    )
    checklist = other = plus = 0.0
    for score, reason in rows:
        if score >= 0:
            plus += score
        elif reason.startswith(REASON_PREFIX):
            checklist -= score
        else:
            other -= score
    return round(checklist, 2), round(other, 2), round(plus, 2)


async def balls_per_day_shift(session) -> int:
    return await session.scalar(select(AppSetting.balls_per_day_shift).limit(1)) or 0
