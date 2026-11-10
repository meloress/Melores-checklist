"""Bazasiz, sof funksiyalar — tests/test_logic.py shularni tekshiradi."""

from datetime import date, datetime, time, timedelta, timezone
from html import escape

from config import settings

TZ = timezone(timedelta(hours=5))  # Toshkent, DST yo'q
WINDOW_START = time.fromisoformat(settings.checklist_start)  # savollar keladi
DEADLINE = time.fromisoformat(settings.checklist_deadline)  # shundan keyin jarima
assert WINDOW_START < DEADLINE, "CHECKLIST_START CHECKLIST_DEADLINE'dan oldin bo'lishi kerak"
START_TXT, DEADLINE_TXT = WINDOW_START.strftime("%H:%M"), DEADLINE.strftime("%H:%M")
REASON_PREFIX = "Checklist:"
BASE_PAYMENT_DAY = 15  # Trello bot: penalty_service.BASE_PAYMENT_DAY
MESSAGE_LIMIT = 3800  # Telegram 4096, zaxira bilan

MONTHS = ["", "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun", "Iyul",
          "Avgust", "Sentyabr", "Oktyabr", "Noyabr", "Dekabr"]


def reminder_times(start: time = WINDOW_START, deadline: time = DEADLINE,
                   every_min: int = settings.checklist_remind_every_min) -> list[time]:
    """Boshlanishdan muddatgacha har `every_min` daqiqada (birinchisi — savollar xabari)."""
    t = datetime.combine(date.min, start)
    end = datetime.combine(date.min, deadline)
    out = []
    while t < end:
        out.append(t.time())
        t += timedelta(minutes=max(every_min, 1))
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


def penalty_for(unanswered: int, rate: float) -> float:
    return round(max(unanswered, 0) * rate, 2)


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


def digest_block(name: str, qa: list[tuple[str, str | None]], penalty: float, status: str) -> str:
    answered = sum(1 for _, a in qa if a)
    head = f"👤 <b>{escape(name)}</b> — {answered}/{len(qa)}"
    if status == NEVER_STARTED:
        head += " ⚠️ botni hali ochmagan — savollar yetib bormadi"
    elif status == BLOCKED:
        head += " ⛔ botni bloklagan"
    elif status == NOT_SENT:
        head += " ⚠️ savollar yetib bormadi"
    elif answered == len(qa):
        head += " ✅"
    if penalty:
        head += f" · {fmt_ball(-penalty)} ball"
    lines = [head]
    for i, (q, a) in enumerate(qa, 1):
        lines.append(f"{i}. {escape(q)}\n— {escape(a) if a else '<i>javob yo‘q</i>'}")
    return "\n".join(lines)


def split_messages(header: str, blocks: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Bloklarni Telegram limitiga sig'adigan xabarlarga bo'ladi; bitta blok
    limitdan uzun bo'lsa, o'zi kesiladi (javoblar 4000 belgigacha bo'lishi mumkin)."""
    messages, current = [], header
    for block in blocks:
        block = block if len(block) <= limit else block[: limit - 1] + "…"
        if len(current) + 2 + len(block) > limit:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        messages.append(current)
    return messages
