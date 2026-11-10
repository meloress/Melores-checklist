# Checklist bot

Har kuni 19:00 da xodimga uning shaxsiy savollari keladi, xodim chatda ketma-ket javob beradi. 19:30, 20:00 va 20:30 da eslatma yuboriladi. 21:00 da har bir javobsiz savolga jarima yoziladi (standart 0.2 ball, Mini App'da o'zgartiriladi). Shu paytda boshliqqa (admin) va nazoratchiga yig'ma hisobot boradi.

**Baza:** Trello botning Postgres'i ishlatiladi.
- `employees` jadvalidan xodimlar o'qiladi.
- `kpi_logs`ga jarima yoziladi, `reason` maydoni `Checklist:` bilan boshlanadi. Shuning uchun jarima umumiy KPI'ga kiradi.
- `employees.next_payment_date` Trello bot qoidasi bo'yicha qayta hisoblanadi: har N ball uchun oylik kuni 1 kun kechikadi.
- O'z jadvallari `checklist_*` nomli. Ular ishga tushishda avtomatik yaratiladi (`init_db`).

**Rollar:**
- Admin hamma narsani ko'radi va sozlaydi.
- Nazoratchi faqat admin unga biriktirgan xodimlarni ko'radi va ularning savollarini tahrirlaydi.
- Boshqa xodimlar faqat chatdan foydalanadi (/start, /ball).

## Ishga tushirish

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # BOT_TOKEN, DATABASE_URL, MINIAPP_BASE_URL
python main.py
python tests/test_logic.py  # bazasiz tekshiruv
```

## Railway

1. Shu loyihada yangi servis oching va shu repo'ni ulang.
2. Variables bo'limiga yozing: `BOT_TOKEN` (yangi bot), `DATABASE_URL` (Trello botniki bilan bir xil), `MINIAPP_BASE_URL` (shu servisning public domeni).
3. Settings → Networking bo'limida public domain yoqing.

## Bilish kerak

- **Jarima faqat savol yetib borgan bo'lsa yoziladi.** Xodim botni ochmagan yoki bloklagan bo'lsa, jarima yozilmaydi va yig'mada ⚠️ bilan ko'rinadi. Har bir xodim yangi botda bir marta /start bosishi shart.
- **19:00 dan keyin qo'shilgan savol** ertangi kundan boshlab so'raladi.
- **Kech javob:** 21:00 dan keyin ham yarim tungacha javob qabul qilinadi, lekin jarima bekor bo'lmaydi.
- **Restart:** 21:00 da bot o'chiq bo'lgan bo'lsa, yopilmagan kun keyingi ishga tushishda yopiladi (`catch_up`).
- **Oylik kuni hisobi:** `logic.payment_date` Trello botdagi `penalty_service.update_payment_date_if_needed` nusxasi. U yerda qoida o'zgarsa, bu yerni ham o'zgartiring.
