# Checklist bot

Rahbar yoki nazoratchi kun davomida kerakli xodimlarga savol yozadi (bir martalik, doimiy ro'yxat emas). Savollar o'sha kuni 19:00 da yuboriladi, xodim chatda ketma-ket javob beradi. 19:30, 20:00 va 20:30 da eslatma keladi. 21:00 da har bir javobsiz savolga jarima yoziladi (standart 0.2 ball, Mini App'da o'zgartiriladi), boshliqqa (admin) va nazoratchiga yig'ma hisobot boradi. Yig'ma bitta qisqa xabar: necha kishi to'liq javob berdi, jami jarima, kim javob bermadi. Javoblar sig'sa, shu xabarning o'zida keladi, aks holda «Javoblarni ko'rish» tugmasi Mini App'dagi kunlik javoblar ekranini ochadi. 19:00 dan keyin yozilgan savol ertaga ketadi. Savol yozilmagan xodimga hech narsa bormaydi.

**Baza:** Trello botning Postgres'i ishlatiladi.
- `employees` jadvalidan xodimlar o'qiladi.
- `kpi_logs`ga jarima yoziladi, `reason` maydoni `Checklist:` bilan boshlanadi. Shuning uchun jarima umumiy KPI'ga kiradi.
- `employees.next_payment_date` Trello bot qoidasi bo'yicha qayta hisoblanadi: har N ball uchun oylik kuni 1 kun kechikadi.
- O'z jadvallari `checklist_*` nomli. Ular ishga tushishda avtomatik yaratiladi (`init_db`).

**Rollar:**
- Admin hamma narsani ko'radi va sozlaydi.
- Nazoratchi faqat admin unga biriktirgan xodimlarni ko'radi va ularning savollarini tahrirlaydi.
- Boshqa xodimlar faqat chatdan foydalanadi (/start, /ball).

**Savol yozilganda belgilanadi:** muhimligi (javobsiz qolsa 0.2 / 0.5 / 0.7 / 1 ball jarima; ballsiz eski savollarga Sozlamalardagi umumiy stavka), kim javob beradi va javobni kim ko'radi. Xodimlar ro'yxati rol bo'yicha guruhlangan. Xodim javob yozishi bilan javob belgilangan o'quvchilarga darhol boradi.

**Sozlamalar (faqat admin, Mini App):** yuborish vaqti, javob muddati, eslatma oralig'i, ball variantlari va eski savollar stavkasi. Hammasi bazada (`checklist_settings`), saqlanganda bot qayta ishga tushmasdan jadvalni yangilaydi. Yuborish va muddat oralig'ida jadvalni o'zgartirib bo'lmaydi. Yuqoridagi 19:00/21:00/30 — standart qiymatlar.

**Huquqlar:** Panel tugmasi faqat admin, rahbar va nazoratchiga ko'rinadi. Rahbarni admin tayinlaydi; rahbar hamma xodimni ko'radi va to'liq yig'ma oladi, sozlama va jarimani bekor qilish faqat adminda.

**Mini App:** bitta savolni bir nechta xodimga birdaniga yozish, kunlik javoblar (kun bo'yicha) va oylik statistika (javob foizi, jarima; faqat savol yetib borgan kunlar).

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

- **Ikki marta jarima yo'q:** kun yopilishi `pg_advisory_xact_lock` bilan qulflangan (deploy paytida ikki nusxa bo'lsa ham).
- **Bloklash:** botni ochib keyin bloklagan xodimga jarima yoziladi; hech ochmaganiga yozilmaydi (yig'mada ⚠️).
- **Jarimani bekor qilish:** admin Mini App'da (xodim kartasi → tarix) — kpi_logs'dan o'chadi, oylik kuni qayta hisoblanadi.
- **Yuborilgan savol qulflanadi:** 19:00 dan keyin uni tahrirlab/o'chirib bo'lmaydi.
- **Har bir xodim** yangi botda bir marta /start bosishi shart, aks holda savol unga bormaydi.
- **Kech javob:** 21:00 dan keyin ham yarim tungacha javob qabul qilinadi, lekin jarima bekor bo'lmaydi.
- **Restart:** 21:00 da bot o'chiq bo'lgan bo'lsa, yopilmagan kun keyingi ishga tushishda yopiladi (`catch_up`).
- **Oylik kuni hisobi:** `logic.payment_date` Trello botdagi `penalty_service.update_payment_date_if_needed` nusxasi. U yerda qoida o'zgarsa, bu yerni ham o'zgartiring.
