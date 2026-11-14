"""Bot, job va Mini App ishlatadigan baza so'rovlari. Har biri chaqiruvchining
sessiyasida ishlaydi; commit'ni chaqiruvchi qiladi."""

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import (AppSetting, ChecklistAnswer, ChecklistDay, ChecklistMember, ChecklistQuestion,
                ChecklistSettings, ChecklistViewer, Employee, KpiLog, TrelloEmployee)
import logic
from logic import REASON_PREFIX, month_bounds, payment_date


async def load_schedule(session) -> None:
    """Bazadagi kun jadvali va ball variantlarini logic'ga yozadi."""
    s = await session.get(ChecklistSettings, 1)
    logic.configure(time.fromisoformat(s.start_time), time.fromisoformat(s.deadline_time),
                    s.remind_every_min, s.points)

ADMIN = "admin"


async def employee_by_tg(session, telegram_id: int) -> Employee | None:
    return await session.scalar(
        select(Employee).where(Employee.telegram_id == telegram_id, Employee.is_active.is_(True))
    )


def on_day(day: date):
    """Shu kuni yuboriladigan savollar sharti: savol har kuni takrorlanadi,
    `day`..`until_day` oralig'ida (logic.applies bilan bir xil qoida)."""
    q = ChecklistQuestion
    return and_(q.is_active.is_(True), q.day <= day, or_(q.until_day.is_(None), q.until_day >= day))


async def day_questions(session, employee_id: int, day: date) -> list[ChecklistQuestion]:
    """Shu kuni xodimga yuboriladigan savollar (har kungi ro'yxat)."""
    return list(await session.scalars(
        select(ChecklistQuestion)
        .where(ChecklistQuestion.employee_id == employee_id, on_day(day))
        .order_by(ChecklistQuestion.position, ChecklistQuestion.id)
    ))


async def end_question(session, q: ChecklistQuestion) -> None:
    """Savolni to'xtatadi. Hali bir marta ham yuborilmagan — butunlay o'chadi;
    yuborilgan — tarix va bugun ochiq oynadagi hisob saqlanib, keyingi kundan to'xtaydi."""
    last = logic.question_day() - timedelta(days=1)
    if q.day > last:
        q.is_active = False
    else:
        q.until_day = last
    await session.flush()


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
    """Shu kuni savoli bor, faol, Telegram'ga ulangan xodimlar."""
    has_q = select(ChecklistQuestion.employee_id).where(on_day(day))
    return list(await session.scalars(
        select(Employee).where(Employee.is_active.is_(True), Employee.telegram_id.is_not(None),
                               Employee.id.in_(has_q)).order_by(Employee.full_name)
    ))


async def viewer_ids_of(session, viewer_id: int) -> set[int]:
    return set(await session.scalars(
        select(ChecklistViewer.employee_id).where(ChecklistViewer.viewer_id == viewer_id)
    ))


async def attach_to_viewers(session, employee_ids) -> None:
    """Yangi xodim hamma nazoratchining ro'yxatiga tushadi — aks holda admin uni
    qo'shgani bilan nazoratchida ko'rinmaydi. Kerakmasini admin keyin olib tashlaydi."""
    viewers = set(await session.scalars(select(ChecklistViewer.viewer_id)))
    rows = [{"viewer_id": v, "employee_id": e} for v in viewers for e in employee_ids if v != e]
    if rows:
        await session.execute(pg_insert(ChecklistViewer).values(rows).on_conflict_do_nothing())


async def leader_ids(session) -> set[int]:
    s = await session.get(ChecklistSettings, 1)
    return set(s.leader_ids or []) if s else set()


async def scope(session, employee: Employee) -> set[int] | None:
    """Mini App huquqi: None = hammasi, bo'sh to'plam = ruxsat yo'q (Mini App ham
    ko'rinmaydi). Admin, rahbar va nazoratchi — bir xil to'liq huquq (2026-11-14, user:
    "adminnikidek, hech narsa qolmasin"); nazoratchi = kamida bitta xodim biriktirilgan."""
    if employee.role == ADMIN or employee.id in await leader_ids(session):
        return None
    return None if await viewer_ids_of(session, employee.id) else set()


async def panel_chats(session) -> list[tuple[int, bool]]:
    """(telegram_id, Panel ko'rinsinmi) — Telegram'ga ulangan hamma faol xodim."""
    emps = list(await session.scalars(select(Employee).where(
        Employee.is_active.is_(True), Employee.telegram_id.is_not(None))))
    leaders = await leader_ids(session)
    viewers = set(await session.scalars(select(ChecklistViewer.viewer_id)))
    return [(e.telegram_id, e.role == ADMIN or e.id in leaders or e.id in viewers) for e in emps]


async def write_penalty(session, employee: Employee, score: float, reason: str) -> tuple[int | None, date | None]:
    """Trello'ga bog'langan xodim: kpi_logs'ga minus ball yozadi, oylik kunini qayta
    hisoblaydi — (kpi_log id, yangi oylik kuni). Bog'lanmagan (checklist'ning o'zida
    qo'shilgan) xodim: jarima faqat checklist_days.penalty'da qoladi — (None, None)."""
    if employee.trello_employee_id is None:
        return None, None
    log = KpiLog(employee_id=employee.trello_employee_id, score=-score, reason=f"{REASON_PREFIX} {reason}")
    session.add(log)
    await session.flush()
    return log.id, await recompute_payment_date(session, employee)


async def cancel_penalty(session, row: ChecklistDay, employee: Employee, by_employee_id: int) -> date | None:
    """Jarimani bekor qiladi: kpi_logs yozuvini o'chiradi, kim bekor qilganini
    yozadi, oylik kunini qayta hisoblaydi. `row.penalty` tarix uchun qoladi."""
    if row.kpi_log_id and employee.trello_employee_id is not None:
        await session.execute(delete(KpiLog).where(KpiLog.id == row.kpi_log_id,
                                                   KpiLog.employee_id == employee.trello_employee_id))
    row.kpi_log_id = None
    row.cancelled_by, row.cancelled_at = by_employee_id, datetime.now(timezone.utc)
    await session.flush()
    return await recompute_payment_date(session, employee)


async def recompute_payment_date(session, employee: Employee) -> date | None:
    """Trello bot qoidasi bilan bir xil (`penalty_service.update_payment_date_if_needed`):
    joriy oyning minus ballari yig'indisidan oylik kunini noldan hisoblaydi.
    Faqat Trello'ga bog'langan xodim (oylik kuni Trello'da yashaydi)."""
    te = await session.get(TrelloEmployee, employee.trello_employee_id) if employee.trello_employee_id else None
    if te is None:
        return None
    start, end = month_bounds(datetime.now(timezone.utc).date())
    total_minus = await session.scalar(
        select(func.coalesce(func.sum(-KpiLog.score), 0))
        .where(KpiLog.employee_id == te.id, KpiLog.score < 0,
               KpiLog.created_at >= start, KpiLog.created_at < end)
    )
    te.next_payment_date = payment_date(start.date(), float(total_minus), await balls_per_day_shift(session))
    return te.next_payment_date


async def payment_day(session, employee: Employee) -> date | None:
    te = await session.get(TrelloEmployee, employee.trello_employee_id) if employee.trello_employee_id else None
    return te.next_payment_date if te else None


async def month_breakdown(session, employee: Employee) -> tuple[float, float, float]:
    """Joriy oy: (checklist jarimasi, boshqa jarimalar, bonuslar) — musbat sonlar.
    Bog'langan xodim — Trello kpi_logs'dan (umumiy ball); bog'lanmagan — faqat checklist."""
    if employee.trello_employee_id is None:
        total = await session.scalar(
            select(func.coalesce(func.sum(ChecklistDay.penalty), 0))
            .where(ChecklistDay.employee_id == employee.id, ChecklistDay.cancelled_at.is_(None),
                   ChecklistDay.day >= logic.local_now().date().replace(day=1)))
        return round(float(total), 2), 0.0, 0.0
    start, end = month_bounds(datetime.now(timezone.utc).date())
    rows = await session.execute(
        select(KpiLog.score, KpiLog.reason)
        .where(KpiLog.employee_id == employee.trello_employee_id, KpiLog.created_at >= start, KpiLog.created_at < end)
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


def _phone_digits(col):
    return func.right(func.regexp_replace(col, r"\D", "", "g"), 9)


async def employee_by_phone(session, phone: str, exclude_id: int | None = None) -> Employee | None:
    """Faol xodim telefon bo'yicha (oxirgi 9 raqam — +998/bo'shliq farqisiz)."""
    norm = logic.normalize_phone(phone)
    if len(norm) < 9:
        return None
    q = select(Employee).where(Employee.is_active.is_(True), _phone_digits(Employee.phone) == norm)
    if exclude_id is not None:
        q = q.where(Employee.id != exclude_id)
    return await session.scalar(q.limit(1))


async def balls_per_day_shift(session) -> int:
    return await session.scalar(select(AppSetting.balls_per_day_shift).limit(1)) or 0
