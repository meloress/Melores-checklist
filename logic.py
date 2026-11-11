"""Bazasiz, sof funksiyalar — tests/test_logic.py shularni tekshiradi."""

from datetime import date, datetime, time, timedelta, timezone
from html import escape

TZ = timezone(timedelta(hours=5))  # Toshkent, DST yo'q

# Kun jadvali va ball variantlari — admin Sozlamalarda o'zgartiradi (checklist_settings),
# ishga tushishda va har o'zgarishda configure() bilan shu yerga yoziladi. Boshqa
# modullar `logic.START_TXT` deb o'qiydi (from-import qilsa eski qiymat qotib qoladi).
WINDOW_START, DEADLINE, REMIND_EVERY_MIN = time(19), time(21), 30  # savollar keladi / jarima / eslatma
POINTS = (0.2, 0.5, 0.7, 1.0)  # savol muhimligi: javobsiz qolsa shuncha jarima
START_TXT, DEADLINE_TXT = "19:00", "21:00"


def check_schedule(start: time, deadline: time, every_min: int, points) -> tuple:
    """Tekshiradi va (start, deadline, every_min, points) qaytaradi; noto'g'ri — ValueError(kalit)."""
    if not start < deadline:
        raise ValueError("schedule_order")
    if not 0 <= every_min <= 240:
        raise ValueError("remind_every")
    pts = tuple(sorted({round(float(p), 2) for p in points}))
    if not 1 <= len(pts) <= 6 or not all(0.05 <= p <= 10 for p in pts):
        raise ValueError("points_list")
    return start, deadline, every_min, pts


def configure(start: time, deadline: time, every_min: int, points) -> None:
    global WINDOW_START, DEADLINE, REMIND_EVERY_MIN, POINTS, START_TXT, DEADLINE_TXT
    WINDOW_START, DEADLINE, REMIND_EVERY_MIN, POINTS = check_schedule(start, deadline, every_min, points)
    START_TXT, DEADLINE_TXT = WINDOW_START.strftime("%H:%M"), DEADLINE.strftime("%H:%M")


REASON_PREFIX = "Checklist:"
BASE_PAYMENT_DAY = 15  # Trello bot: penalty_service.BASE_PAYMENT_DAY
MESSAGE_LIMIT = 3800  # Telegram 4096, zaxira bilan

MONTHS = ["", "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun", "Iyul",
          "Avgust", "Sentyabr", "Oktyabr", "Noyabr", "Dekabr"]


def reminder_times(start: time | None = None, deadline: time | None = None,
                   every_min: int | None = None) -> list[time]:
    """Boshlanishdan muddatgacha har `every_min` daqiqada (birinchisi — savollar xabari).
    0 — eslatma yo'q, faqat savollar xabari."""
    start, deadline = start or WINDOW_START, deadline or DEADLINE
    every_min = REMIND_EVERY_MIN if every_min is None else every_min
    if every_min <= 0:
        return [start]
    t = datetime.combine(date.min, start)
    end = datetime.combine(date.min, deadline)
    out = []
    while t < end:
        out.append(t.time())
        t += timedelta(minutes=every_min)
    return out


def local_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(TZ)


def window_start_utc(day: date) -> datetime:
    """Shu kungi boshlanish vaqti (UTC'da). Bundan keyin qo'shilgan savol ertangi kundan."""
    return datetime.combine(day, WINDOW_START, TZ).astimezone(timezone.utc)


def open_day(now: datetime | None = None) -> date | None:
    """Boshlanish–23:59 oralig'ida javob berish mumkin bo'lgan kun, aks holda None."""
    local = local_now(now)
    return local.date() if local.time() >= WINDOW_START else None


def is_past_deadline(day: date, now: datetime | None = None) -> bool:
    return local_now(now) >= datetime.combine(day, DEADLINE, TZ)


def question_day(now: datetime | None = None) -> date:
    """Yangi savol qaysi kuni yuboriladi: boshlanishgacha — bugun, keyin — ertaga.
    Savollar doimiy emas: rahbar kun davomida yozadi, o'sha kuni bir marta ketadi."""
    local = local_now(now)
    return local.date() if local.time() < WINDOW_START else local.date() + timedelta(days=1)


def is_locked(day: date, now: datetime | None = None) -> bool:
    """Savol yuborilgan (oyna ochilgan) — endi tahrirlab/o'chirib bo'lmaydi,
    aks holda jarima hisobi o'zgarib qoladi."""
    return local_now(now) >= datetime.combine(day, WINDOW_START, TZ)


def is_real_answer(text: str) -> bool:
    """Kamida bitta harf yoki raqam: "ha", "5" o'tadi; ".", "-", "👍" o'tmaydi."""
    return any(c.isalnum() for c in text)


def penalty_for(questions, answered_ids, rate: float) -> float:
    """Javobsiz savollar balli yig'indisi; balli yo'q (eski) savol — umumiy stavka."""
    return round(sum(rate if q.points is None else q.points for q in questions if q.id not in answered_ids), 2)


def month_bounds(reference: date) -> tuple[datetime, datetime]:
    """Trello bot: penalty_service.month_bounds bilan bir xil (UTC)."""
    start = datetime(reference.year, reference.month, 1, tzinfo=timezone.utc)
    if reference.month == 12:
        return start, datetime(reference.year + 1, 1, 1, tzinfo=timezone.utc)
    return start, datetime(reference.year, reference.month + 1, 1, tzinfo=timezone.utc)


def payment_date(month_start: date, total_minus: float, balls_per_day_shift: int) -> date:
    """Trello bot: penalty_service.update_payment_date_if_needed bilan bir xil —
    har `balls_per_day_shift` minus ball uchun oylik kuni 1 kun suriladi.
    Ikki joyda yashaydi: u yerda o'zgarsa, bu yerni ham o'zgartiring."""
    shift = int(round(total_minus, 2) // balls_per_day_shift) if balls_per_day_shift > 0 else 0
    return date(month_start.year, month_start.month, BASE_PAYMENT_DAY) + timedelta(days=shift)


def fmt_ball(score: float) -> str:
    """"+3", "-1,5", "0" — Trello bot: utils.formatters.fmt_ball."""
    if round(score, 2) == 0:
        return "0"
    return f"{score:+.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_day(day: date) -> str:
    return day.strftime("%d.%m.%Y")


# Yig'mada xodim holati: savollar yetdi / botni ochib keyin bloklagan (jarima
# yoziladi) / botni hech ochmagan yoki yuborilmay qolgan (jarima yo'q — savolni ko'rmagan).
DELIVERED, BLOCKED, NEVER_STARTED, NOT_SENT = "delivered", "blocked", "never_started", "not_sent"


def _marks(answered: int, asked: int, penalty: float, status: str) -> str:
    out = ""
    if status == NEVER_STARTED:
        out += " ⚠️ botni hali ochmagan — savollar yetib bormadi"
    elif status == BLOCKED:
        out += " ⛔ botni bloklagan"
    elif status == NOT_SENT:
        out += " ⚠️ savollar yetib bormadi"
    elif answered == asked:
        out += " ✅"
    if penalty:
        out += f" · {fmt_ball(-penalty)} ball"
    return out


def digest_block(name: str, qa: list[tuple[str, str | None]], penalty: float, status: str) -> str:
    answered = sum(1 for _, a in qa if a)
    lines = [f"👤 <b>{escape(name)}</b> — {answered}/{len(qa)}" + _marks(answered, len(qa), penalty, status)]
    for i, (q, a) in enumerate(qa, 1):
        lines.append(f"{i}. {escape(q)}\n— {escape(a) if a else '<i>javob yo‘q</i>'}")
    return "\n".join(lines)


SUMMARY_NAMES = 30  # undan ko'p bo'lsa "va yana N kishi" — xabar limitga sig'sin


def digest_summary(header: str, items: list[tuple[str, int, int, float, str]]) -> str:
    """Bitta qisqa xabar: necha kishi to'liq javob berdi, jami jarima, kim
    javob bermadi. items: (ism, javob, savol, jarima, holat)."""
    full = sum(1 for _, a, q, _, _ in items if a >= q)
    total = sum(p for *_, p, _ in items)
    lines = [header, f"✅ To'liq javob berdi: <b>{full}/{len(items)}</b>"]
    if total:
        lines.append(f"Jami jarima: <b>{fmt_ball(-total)} ball</b>")
    missing = [it for it in items if it[1] < it[2]]
    if missing:
        lines.append("\nJavob to'liq emas:")
        lines += [f"• {escape(n)} — {a}/{q}" + _marks(a, q, p, s) for n, a, q, p, s in missing[:SUMMARY_NAMES]]
        if len(missing) > SUMMARY_NAMES:
            lines.append(f"… va yana {len(missing) - SUMMARY_NAMES} kishi")
    return "\n".join(lines)
