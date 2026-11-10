"""Bazasiz tekshiruv: `python tests/test_logic.py` (pytest shart emas)."""

import hashlib
import hmac
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
# .env'da sinov vaqtlari bo'lsa ham testlar standart jadvalni tekshirsin
os.environ.update(CHECKLIST_START="19:00", CHECKLIST_DEADLINE="21:00", CHECKLIST_REMIND_EVERY_MIN="30")

from datetime import time  # noqa: E402

from logic import (BLOCKED, DELIVERED, NEVER_STARTED, NOT_SENT, digest_block, digest_summary,  # noqa: E402
                   fmt_ball, is_locked, is_past_deadline, open_day, payment_date, penalty_for, question_day,
                   reminder_times, window_start_utc)
from webapp import verify_init_data  # noqa: E402


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# Jarima: javobsiz savol × stavka, kasr aniq
assert penalty_for(3, 0.2) == 0.6
assert penalty_for(0, 0.2) == 0
assert penalty_for(-1, 0.2) == 0
assert penalty_for(7, 0.15) == 1.05

# Oylik kuni: Trello bot qoidasi — har 5 minus ball = 1 kun (15-sanadan)
assert payment_date(date(2026, 11, 1), 4.99, 5) == date(2026, 11, 15)
assert payment_date(date(2026, 11, 1), 5.0, 5) == date(2026, 11, 16)
assert payment_date(date(2026, 11, 1), 2.9999999, 1) == date(2026, 11, 18)  # float qoldig'i kun yemasin
assert payment_date(date(2026, 11, 1), 12, 0) == date(2026, 11, 15)  # 0 = o'chiq, bo'linish xatosi yo'q

# Vaqt oynasi: Toshkent UTC+5. 19:00 Toshkent = 14:00 UTC
assert window_start_utc(date(2026, 11, 9)) == utc(2026, 11, 9, 14)
assert open_day(utc(2026, 11, 9, 13, 59)) is None  # 18:59
assert open_day(utc(2026, 11, 9, 14)) == date(2026, 11, 9)  # 19:00
assert open_day(utc(2026, 11, 9, 18, 59)) == date(2026, 11, 9)  # 23:59 — kech javob mumkin
assert open_day(utc(2026, 11, 9, 19)) is None  # 00:00 ertasi — yangi kun, hali 19:00 emas
assert not is_past_deadline(date(2026, 11, 9), utc(2026, 11, 9, 15, 59))  # 20:59
assert is_past_deadline(date(2026, 11, 9), utc(2026, 11, 9, 16))  # 21:00

# Eslatmalar: 19:00 (savollar), 19:30, 20:00, 20:30 — 21:00 da emas (u jarima)
assert reminder_times() == [time(19), time(19, 30), time(20), time(20, 30)]
assert reminder_times(time(13, 0), time(13, 10), 5) == [time(13), time(13, 5)]

assert fmt_ball(-0.6) == "-0,6" and fmt_ball(-1.0) == "-1" and fmt_ball(0.001) == "0"

# Yig'ma: HTML escape, javobsiz savol belgilanadi, yetib bormaganlik ko'rinadi
block = digest_block("Ali <b>", [("Bugun?", "ish <ok>"), ("Ertaga?", None)], 0.2, DELIVERED)
assert "Ali &lt;b&gt;" in block and "ish &lt;ok&gt;" in block
assert "1/2" in block and "javob yo" in block and "-0,2 ball" in block
assert "ochmagan" in digest_block("X", [("Q", None)], 0, NEVER_STARTED)
assert "bloklagan" in digest_block("X", [("Q", None)], 0.2, BLOCKED)
assert "yetib bormadi" in digest_block("X", [("Q", None)], 0, NOT_SENT)
assert "✅" in digest_block("X", [("Q", "A")], 0, DELIVERED)

# Bir martalik savollar: 19:00 gacha yozilgani — bugungi, keyin — ertangi
assert question_day(utc(2026, 11, 10, 13, 59)) == date(2026, 11, 10)  # 18:59
assert question_day(utc(2026, 11, 10, 14)) == date(2026, 11, 11)  # 19:00 — endi ertaga
assert question_day(utc(2026, 11, 10, 19, 30)) == date(2026, 11, 11)  # 00:30 ertasi — o'sha kun
# Yuborilgan savol qulflanadi (tahrir/o'chirish jarima hisobini buzmasin)
assert not is_locked(date(2026, 11, 10), utc(2026, 11, 10, 13, 59))
assert is_locked(date(2026, 11, 10), utc(2026, 11, 10, 14))
assert not is_locked(date(2026, 11, 11), utc(2026, 11, 10, 15))  # ertangi savol hali ochiq

# Qisqa yig'ma: to'liq javob berganlar soni, jami jarima, faqat to'liq emaslar ro'yxatda
s = digest_summary("H", [("Ali", 2, 2, 0, DELIVERED), ("Vali <x>", 1, 3, 0.4, DELIVERED),
                         ("Soli", 0, 1, 0, NEVER_STARTED)])
assert s.startswith("H") and "1/3" in s.split("\n")[1] and "-0,4 ball" in s
assert "Ali" not in s and "Vali &lt;x&gt;" in s and "ochmagan" in s
big = digest_summary("H", [(f"X{i}", 0, 1, 0.2, DELIVERED) for i in range(100)])
assert "yana 70 kishi" in big and len(big) < 3800  # 100 kishi ham bitta xabarga sig'adi
assert "to'liq emas" not in digest_summary("H", [("A", 1, 1, 0, DELIVERED)])

# initData imzosi (Telegram algoritmi)
def signed(token, user_id, auth_date):
    pairs = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)

assert verify_init_data(signed("123:test", 42, 1000), "123:test", now=1000) == 42
for bad in (signed("boshqa:token", 42, 1000), signed("123:test", 42, 1000).replace("42", "43")):
    try:
        verify_init_data(bad, "123:test", now=1000)
        raise AssertionError("soxta initData o'tib ketdi")
    except ValueError:
        pass
try:
    verify_init_data(signed("123:test", 42, 1000), "123:test", now=1000 + 2 * 86400)
    raise AssertionError("eskirgan initData o'tib ketdi")
except ValueError:
    pass

print("OK")
