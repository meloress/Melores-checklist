"""Mini App: boshliq (admin) va nazoratchilar uchun boshqaruv paneli.

Huquq: admin, rahbar va nazoratchi — bir xil, to'liq (2026-11-14, user):
hamma xodim, tahrirlash, sozlama, jarimani bekor qilish, rahbar/nazoratchi tayinlash.
Boshqa xodimlarga 403."""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from sqlalchemy import case, delete, func, or_, select

from config import settings
from db import (ChecklistAnswer, ChecklistDay, ChecklistQuestion, ChecklistSettings, ChecklistViewer,
                Employee, KpiLog, TrelloEmployee, async_session)
import jobs
from jobs import send, sync_menus
import logic
from logic import (MONTHS, applies, fmt_ball, fmt_day, is_locked, local_now, question_day,
                   reminder_times)
import service

logger = logging.getLogger(__name__)
PUBLIC = Path(__file__).resolve().parent / "public"
INIT_DATA_MAX_AGE = 24 * 3600
HISTORY_DAYS = 7


def verify_init_data(init_data: str, bot_token: str, now: float | None = None) -> int:
    """Telegram WebApp initData imzosi (Trello bot: miniapp/auth.py). telegram_id qaytaradi."""
    pairs = dict(parse_qsl(init_data, strict_parsing=True))
    received = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    if not received or not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), received):
        raise ValueError("hash mos emas")
    if (now or time.time()) - int(pairs.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
        raise ValueError("initData eskirgan")
    return int(json.loads(pairs["user"])["id"])


@web.middleware
async def auth(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    try:
        telegram_id = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""), settings.bot_token)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        logger.info("Mini App rad etildi: %s", exc)
        return web.json_response({"error": "unauthorized"}, status=401)
    async with async_session() as session:
        me = await service.employee_by_tg(session, telegram_id)
        allowed = None if me is None else await service.scope(session, me)
    if me is None:
        return web.json_response({"error": "not_registered"}, status=403)
    if allowed is not None and not allowed:
        return web.json_response({"error": "forbidden"}, status=403)
    request["me"], request["scope"] = me, allowed
    return await handler(request)


@web.middleware
async def no_cache(request: web.Request, handler):
    # Telegram WebView statik fayllarni qattiq keshlaydi (Trello botda kuzatilgan).
    response = await handler(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


def _in_scope(request, employee_id: int) -> None:
    allowed = request["scope"]
    if allowed is not None and employee_id not in allowed:
        raise web.HTTPForbidden(text='{"error":"forbidden"}', content_type="application/json")


async def _body(request) -> dict:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text='{"error":"bad_json"}', content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text='{"error":"bad_json"}', content_type="application/json")
    return data


def _points(data: dict, current: float | None = None) -> float:
    """Sozlamadagi variantlardan biri; tahrirda savolning hozirgi balli ham (variant olib tashlangan bo'lsa)."""
    try:
        p = round(float(data.get("points", logic.POINTS[0])), 2)
    except (TypeError, ValueError):
        p = None
    if p not in logic.POINTS and (p is None or p != current):
        raise web.HTTPBadRequest(text='{"error":"points"}', content_type="application/json")
    return p


def _ids(data: dict, key: str, allow_empty: bool) -> list[int]:
    ids = data.get(key, [])
    if (not isinstance(ids, list) or len(ids) > 1000 or (not ids and not allow_empty)
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        raise web.HTTPBadRequest(text=f'{{"error":"{key}"}}', content_type="application/json")
    return ids


def _text(data: dict) -> str:
    text = str(data.get("text", "")).strip()
    if not text or len(text) > 1000:
        raise web.HTTPBadRequest(text='{"error":"text_length"}', content_type="application/json")
    return text


routes = web.RouteTableDef()


async def _leaders() -> set[int]:
    async with async_session() as session:
        return await service.leader_ids(session)


@routes.get("/api/me")
async def me(request):
    m = request["me"]
    return web.json_response({"id": m.id, "full_name": m.full_name, "role": m.role,
                              # Panelga kirgan har kim — admin huquqida (2026-11-14); unvon faqat Profil uchun.
                              "is_admin": True,
                              "panel_role": "admin" if m.role == service.ADMIN else
                              "rahbar" if m.id in await _leaders() else "nazoratchi", "start": logic.START_TXT, "deadline": logic.DEADLINE_TXT, "points": list(logic.POINTS),
                              "bot_username": request.config_dict.get("bot_username", ""),
                              "reminders": [t.strftime("%H:%M") for t in reminder_times()[1:]]})


@routes.get("/api/employees")
async def employees(request):
    today = local_now().date()
    allowed = request["scope"]
    async with async_session() as session:
        q = select(Employee).where(Employee.is_active.is_(True)).order_by(Employee.full_name)
        if allowed is not None:
            q = q.where(Employee.id.in_(allowed))
        emps = list(await session.scalars(q))
        counts = dict((await session.execute(
            select(ChecklistQuestion.employee_id, func.count())
            .where(service.on_day(today))
            .group_by(ChecklistQuestion.employee_id))).all())
        answered = dict((await session.execute(
            select(ChecklistAnswer.employee_id, func.count())
            .where(ChecklistAnswer.day == today).group_by(ChecklistAnswer.employee_id))).all())
        month_penalty = dict((await session.execute(
            select(ChecklistDay.employee_id, func.sum(ChecklistDay.penalty))
            .where(ChecklistDay.day >= today.replace(day=1), ChecklistDay.cancelled_at.is_(None))
            .group_by(ChecklistDay.employee_id))).all())
        members = await service.member_ids(session)
    return web.json_response([
        {"id": e.id, "full_name": e.full_name, "role": e.role, "has_telegram": e.telegram_id is not None,
         "started_bot": e.id in members, "linked": e.trello_employee_id is not None,
         "questions": counts.get(e.id, 0), "answered_today": answered.get(e.id, 0),
         "month_penalty": round(float(month_penalty.get(e.id) or 0), 2)}
        for e in emps
    ])


def _day_label(day, today) -> str:
    return "bugun" if day == today else "ertaga" if day == today + timedelta(days=1) else fmt_day(day)


def _day_json(d, r, questions, answer_of) -> dict:
    """Bir xodimning bir kuni: savol-javoblar va holat (xodim kartasi va kunlik javoblar ekrani)."""
    items = []
    for q in questions:
        a = answer_of(q)
        items.append({"q": q.text, "a": a.answer_text if a else None, "points": q.points,
                      "at": local_now(a.answered_at).strftime("%H:%M") if a else None})
    return {
        "day_id": r.id if r else None, "day": fmt_day(d), "items": items,
        "penalty": r.penalty if r else 0, "closed": bool(r and r.closed_at),
        "delivered": bool(r and r.delivered_at), "blocked": bool(r and r.blocked_at and not r.delivered_at),
        "cancelled": bool(r and r.cancelled_at),
    }


def _query_date(request, key: str, fmt_len: int):
    raw = request.query.get(key)
    try:
        return None if not raw else date.fromisoformat(raw if fmt_len == 10 else raw + "-01")
    except ValueError:
        raise web.HTTPBadRequest(text=f'{{"error":"{key}"}}', content_type="application/json")


@routes.get("/api/answers")
async def day_answers(request):
    """Bir kunda savol yuborilgan hamma xodimning javoblari (21:00 yig'ma tugmasi shu yerga ochadi)."""
    now = local_now()
    day = _query_date(request, "day", 10) or now.date()
    allowed = request["scope"]
    rows = []
    if is_locked(day, now):
        async with async_session() as session:
            # Faqat shu kuni savol haqiqatan yuborilgan xodimlar (checklist_days qatori bor) —
            # savollar har kuni takrorlanadi, yuborilmagan kun tarixda ko'rinmasin.
            dq = select(ChecklistDay).where(ChecklistDay.day == day)
            if allowed is not None:
                dq = dq.where(ChecklistDay.employee_id.in_(allowed))
            day_rows = {r.employee_id: r for r in await session.scalars(dq)}
            emp_ids = set(day_rows)
            questions = list(await session.scalars(
                select(ChecklistQuestion).where(service.on_day(day), ChecklistQuestion.employee_id.in_(emp_ids))
                .order_by(ChecklistQuestion.position, ChecklistQuestion.id)))
            answers = {(a.employee_id, a.question_id): a for a in await session.scalars(
                select(ChecklistAnswer).where(ChecklistAnswer.day == day, ChecklistAnswer.employee_id.in_(emp_ids)))}
            names = dict((await session.execute(
                select(Employee.id, Employee.full_name).where(Employee.id.in_(emp_ids)))).all())
        for emp_id in sorted(emp_ids, key=lambda i: names.get(i, "")):
            own = [x for x in questions if x.employee_id == emp_id]
            rows.append({"id": emp_id, "full_name": names.get(emp_id, "?"),
                         **_day_json(day, day_rows.get(emp_id), own, lambda q, e=emp_id: answers.get((e, q.id)))})
    return web.json_response({"day": day.isoformat(), "label": fmt_day(day), "today": now.date().isoformat(),
                              "rows": rows})


@routes.get("/api/stats")
async def month_stats(request):
    """Oylik intizom: yopilgan, savol YETIB BORGAN (yoki bloklagan) kunlar bo'yicha
    javob foizi va jarima. Yetib bormagan kun xodim aybi emas — hisobga kirmaydi."""
    today = local_now().date()
    start = _query_date(request, "month", 7) or today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    allowed = request["scope"]
    counted = or_(ChecklistDay.delivered_at.is_not(None), ChecklistDay.blocked_at.is_not(None))
    async with async_session() as session:
        q = (select(Employee.id, Employee.full_name, func.count(), func.sum(ChecklistDay.asked),
                    func.sum(ChecklistDay.answered),
                    func.sum(case((ChecklistDay.cancelled_at.is_(None), ChecklistDay.penalty), else_=0)))
             .join(Employee, Employee.id == ChecklistDay.employee_id)
             .where(ChecklistDay.day >= start, ChecklistDay.day < end, ChecklistDay.closed_at.is_not(None), counted)
             .group_by(Employee.id, Employee.full_name))
        if allowed is not None:
            q = q.where(ChecklistDay.employee_id.in_(allowed))
        rows = (await session.execute(q)).all()
        # Umumiy KPI ball (Trello'dagi hamma jarima+bonus) — faqat Trello'ga bog'langanlarda.
        link = dict((await session.execute(select(Employee.id, Employee.trello_employee_id).where(
            Employee.id.in_([r[0] for r in rows]), Employee.trello_employee_id.is_not(None)))).all())
        m_start = datetime.combine(start, dtime(), timezone.utc)
        kpi = dict((await session.execute(
            select(KpiLog.employee_id, func.sum(KpiLog.score))
            .where(KpiLog.employee_id.in_(link.values()), KpiLog.created_at >= m_start,
                   KpiLog.created_at < datetime.combine(end, dtime(), timezone.utc))
            .group_by(KpiLog.employee_id))).all()) if link else {}
    return web.json_response({
        "month": start.strftime("%Y-%m"), "label": f"{MONTHS[start.month]} {start.year}",
        "current": today.strftime("%Y-%m"),
        "rows": [{"id": i, "full_name": n, "days": d, "asked": int(a or 0), "answered": int(b or 0),
                  "penalty": round(float(p or 0), 2),
                  "kpi_total": round(float(kpi.get(link[i]) or 0), 2) if i in link else None}
                 for i, n, d, a, b, p in rows],
    })


@routes.get("/api/employees/{id:\\d+}")
async def employee_detail(request):
    emp_id = int(request.match_info["id"])
    _in_scope(request, emp_id)
    now = local_now()
    today, target = now.date(), question_day()
    since = today - timedelta(days=HISTORY_DAYS - 1)
    async with async_session() as session:
        emp = await session.get(Employee, emp_id)
        if emp is None:
            raise web.HTTPNotFound()
        questions = list(await session.scalars(
            select(ChecklistQuestion).where(
                ChecklistQuestion.employee_id == emp_id, ChecklistQuestion.is_active.is_(True),
                ChecklistQuestion.day <= target,
                or_(ChecklistQuestion.until_day.is_(None), ChecklistQuestion.until_day >= since))
            .order_by(ChecklistQuestion.position, ChecklistQuestion.id)))
        answers = {(a.day, a.question_id): a for a in await session.scalars(
            select(ChecklistAnswer).where(ChecklistAnswer.employee_id == emp_id, ChecklistAnswer.day >= since))}
        day_rows = {r.day: r for r in await session.scalars(
            select(ChecklistDay).where(ChecklistDay.employee_id == emp_id, ChecklistDay.day >= since))}
        started = emp_id in await service.member_ids(session)
        trello = None
        if emp.trello_employee_id is not None:  # umumiy ball Trello botdan (faqat o'qiladi)
            cl, other, plus = await service.month_breakdown(session, emp)
            pay = await service.payment_day(session, emp)
            trello = {"checklist": cl, "other": other, "plus": plus, "total": round(plus - cl - other, 2),
                      "pay_day": fmt_day(pay) if pay else None}
    def on(d):
        return [q for q in questions if applies(q.day, q.until_day, d)]

    # Tarix — faqat savol haqiqatan yuborilgan kunlar (checklist_days qatori bor).
    days = [_day_json(d, day_rows[d], on(d), lambda q, d=d: answers.get((d, q.id)))
            for d in sorted(day_rows, reverse=True) if is_locked(d, now)]
    queued = [{"id": q.id, "text": q.text, "points": q.points, "readers": len(q.reader_ids or [])}
              for q in on(target)]
    return web.json_response({
        "id": emp.id, "full_name": emp.full_name, "has_telegram": emp.telegram_id is not None,
        "started_bot": started, "target_label": _day_label(target, today), "queued": queued, "days": days,
        "role": emp.role, "trello": trello, "linked": emp.trello_employee_id is not None,
        # Telefon — faqat admin (xodim qo'shish/tahrirlash faqat unda).
        "phone": emp.phone,
    })


ROLES = ("worker", "brigadier", "seller", "supervisor", "observer", "admin")


def _employee_fields(data: dict) -> tuple[str, str, str]:
    name = str(data.get("full_name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    role = data.get("role")
    if not 2 <= len(name) <= 150:
        raise _bad("full_name")
    if len(logic.normalize_phone(phone)) < 9 or len(phone) > 20:
        raise _bad("phone")
    if role not in ROLES:
        raise _bad("role")
    return name, phone, role


@routes.post("/api/employees")
async def add_employee(request):
    """Yangi xodim — faqat checklist'da (Trello botga tushmaydi). Admin, rahbar va
    nazoratchi (yangisi hamma nazoratchiga biriktiriladi — qo'shgan odam ham ko'radi).
    Xodim botda /start bosib, shu telefonni ulashganda Telegram'iga bog'lanadi."""
    name, phone, role = _employee_fields(await _body(request))
    async with async_session() as session:
        if await service.employee_by_phone(session, phone):
            raise web.HTTPConflict(text='{"error":"phone_taken"}', content_type="application/json")
        emp = Employee(full_name=name, phone=phone, role=role)
        session.add(emp)
        await session.flush()
        await service.attach_to_viewers(session, [emp.id])
        await session.commit()
    logger.info("checklist xodim qo'shildi: %s (qo'shgan %s)", emp.id, request["me"].id)
    return web.json_response({"id": emp.id})


@routes.put("/api/employees/{id:\\d+}")
async def edit_employee(request):
    """Ism/telefon/rol/faollik — faqat checklist'dagi yozuv (Trello'dagi o'zgarmaydi). Faqat admin."""
    data = await _body(request)
    name, phone, role = _employee_fields(data)
    active = data.get("is_active", True)
    if not isinstance(active, bool):
        raise _bad("bad_json")
    emp_id = int(request.match_info["id"])
    if emp_id == request["me"].id and (role != request["me"].role or not active):
        raise web.HTTPConflict(text='{"error":"self_demote"}', content_type="application/json")
    async with async_session() as session:
        emp = await session.get(Employee, emp_id)
        if emp is None:
            raise web.HTTPNotFound()
        if await service.employee_by_phone(session, phone, exclude_id=emp_id):
            raise web.HTTPConflict(text='{"error":"phone_taken"}', content_type="application/json")
        if logic.normalize_phone(phone) != logic.normalize_phone(emp.phone):
            emp.telegram_id = None  # boshqa raqam — yangi egasi /start bilan qayta ulanadi
        emp.full_name, emp.phone, emp.role, emp.is_active = name, phone, role, active
        await session.commit()
    logger.info("checklist xodim %s tahrirlandi (admin %s)", emp_id, request["me"].id)
    _refresh_menus(request)
    return web.json_response({"ok": True})


@routes.get("/api/trello-import")
async def trello_candidates(request):
    """Trello'dagi faol, checklist'da hali yo'q xodimlar (keraklilari import qilinadi).
    Admin bo'lmagan — Trello'dagi adminlarni ko'rmaydi (admin huquqini faqat admin beradi)."""
    async with async_session() as session:
        rows = list(await session.scalars(
            select(TrelloEmployee).where(TrelloEmployee.is_active.is_(True),
                                         TrelloEmployee.id.not_in(select(Employee.trello_employee_id).where(
                                             Employee.trello_employee_id.is_not(None))))
            .order_by(TrelloEmployee.full_name)))
    return web.json_response([{"id": t.id, "full_name": t.full_name, "role": t.role} for t in rows])


@routes.post("/api/trello-import")
async def trello_import(request):
    ids = _ids(await _body(request), "trello_ids", allow_empty=False)
    new = []
    async with async_session() as session:
        taken = set(await session.scalars(select(Employee.trello_employee_id).where(
            Employee.trello_employee_id.in_(ids))))
        tg_taken = set(await session.scalars(select(Employee.telegram_id).where(Employee.telegram_id.is_not(None))))
        for t in await session.scalars(select(TrelloEmployee).where(TrelloEmployee.id.in_(ids),
                                                                   TrelloEmployee.is_active.is_(True))):
            if t.id in taken:
                continue
            new.append(Employee(trello_employee_id=t.id, full_name=t.full_name, phone=t.phone_number,
                                role=t.role if t.role in ROLES else "worker",
                                telegram_id=t.telegram_id if t.telegram_id not in tg_taken else None))
        session.add_all(new)
        await session.flush()
        await service.attach_to_viewers(session, [e.id for e in new])
        await session.commit()
    logger.info("checklist: Trello'dan %s xodim import qilindi (qo'shgan %s)", len(new), request["me"].id)
    _refresh_menus(request)
    return web.json_response({"count": len(new)})


@routes.post("/api/questions")
async def add_question(request):
    """Bitta savol bir yoki bir nechta xodimga. Keyingi 19:00 dan boshlab har kuni
    yuboriladi (kunduzi yozilgani — bugundan, kechqurun — ertadan), o'chirilguncha."""
    data = await _body(request)
    text, points = _text(data), _points(data)
    ids = _ids(data, "employee_ids", allow_empty=False)
    readers = sorted(set(_ids(data, "reader_ids", allow_empty=True)))
    for i in ids + readers:
        _in_scope(request, i)
    day = question_day()
    async with async_session() as session:
        valid = set(await session.scalars(select(Employee.id).where(Employee.id.in_(ids),
                                                                     Employee.is_active.is_(True))))
        if not valid:
            raise web.HTTPNotFound()
        last = dict((await session.execute(
            select(ChecklistQuestion.employee_id, func.max(ChecklistQuestion.position))
            .where(ChecklistQuestion.employee_id.in_(valid), service.on_day(day))
            .group_by(ChecklistQuestion.employee_id))).all())
        session.add_all(ChecklistQuestion(employee_id=e, day=day, text=text, position=(last.get(e) or 0) + 1,
                                          points=points, reader_ids=readers)
                        for e in valid)
        await session.commit()
    logger.info("checklist savol %s xodimga, %s (xodim %s)", len(valid), day, request["me"].id)
    return web.json_response({"count": len(valid), "day": fmt_day(day)})


async def _own_question(request, session) -> ChecklistQuestion:
    """O'z xodimiga tegishli, hali davom etayotgan (to'xtatilmagan) savol."""
    q = await session.get(ChecklistQuestion, int(request.match_info["id"]))
    if q is None or not q.is_active or q.until_day is not None:
        raise web.HTTPNotFound()
    _in_scope(request, q.employee_id)
    return q


@routes.put("/api/questions/{id:\\d+}")
async def edit_question(request):
    """Hali yuborilmagan savol joyida o'zgaradi. Yuborilgani esa eskisi to'xtatilib,
    yangisi navbatdagi kundan boshlanadi — o'tgan kunlar tarixi va bugungi
    ochiq oynadagi jarima hisobi o'zgarmaydi."""
    data = await _body(request)
    text = _text(data)
    start = question_day()
    async with async_session() as session:
        q = await _own_question(request, session)
        points = _points(data, q.points)
        if q.day >= start:
            q.text, q.points = text, points
        else:
            await service.end_question(session, q)
            q = ChecklistQuestion(employee_id=q.employee_id, day=start, text=text, points=points,
                                  position=q.position, reader_ids=list(q.reader_ids or []))
            session.add(q)
        await session.commit()
    return web.json_response({"id": q.id, "text": q.text, "points": q.points})


@routes.delete("/api/questions/{id:\\d+}")
async def delete_question(request):
    """Keyingi kundan boshlab yuborilmaydi (bugun ochiq oynadagisi hisobda qoladi)."""
    async with async_session() as session:
        q = await _own_question(request, session)
        await service.end_question(session, q)
        await session.commit()
    return web.json_response({"ok": True})


@routes.post("/api/questions/{id:\\d+}/up")
async def move_question_up(request):
    """Savolni bitta yuqoriga suradi (xodimning har kungi ro'yxatida)."""
    async with async_session() as session:
        q = await _own_question(request, session)
        qs = await service.day_questions(session, q.employee_id, question_day())
        if q not in qs:
            raise web.HTTPNotFound()
        i = qs.index(q)
        if i > 0:
            qs[i - 1], qs[i] = qs[i], qs[i - 1]
            for pos, x in enumerate(qs, 1):  # eski bir xil position'lar ham tartibga tushadi
                x.position = pos
            await session.commit()
    return web.json_response({"ok": True})


@routes.post("/api/days/{id:\\d+}/cancel-penalty")
async def cancel_penalty(request):
    """Noto'g'ri jarimani bekor qilish (kasallik, texnik nosozlik). Pul masalasi —
    faqat admin. kpi_logs'dan o'chiriladi, oylik kuni qayta hisoblanadi, xodimga xabar."""
    me_ = request["me"]
    async with async_session() as session:
        row = await session.get(ChecklistDay, int(request.match_info["id"]))
        if row is None:
            raise web.HTTPNotFound()
        if not row.penalty or row.cancelled_at is not None:
            raise web.HTTPConflict(text='{"error":"nothing_to_cancel"}', content_type="application/json")
        emp = await session.get(Employee, row.employee_id)
        pay_day = await service.cancel_penalty(session, row, emp, me_.id)
        await session.commit()
    logger.info("checklist jarima bekor qilindi: xodim %s, %s, %s ball (admin %s)",
                emp.id, row.day, row.penalty, me_.id)
    bot = request.config_dict.get("bot")
    if bot and emp.telegram_id:
        try:
            await send(bot, emp.telegram_id,
                       f"✅ {fmt_day(row.day)} dagi checklist jarimangiz ({fmt_ball(-row.penalty)} ball) bekor qilindi."
                       + (f"\nOylik kuni: {fmt_day(pay_day)}" if pay_day else ""))
        except Exception:
            logger.exception("bekor qilish xabari yuborilmadi (xodim %s)", emp.id)
    return web.json_response({"ok": True})


@routes.get("/api/viewers")
async def viewers(request):
    """Nazoratchilar. Admin/rahbar — hammasi; nazoratchi — faqat o'zi ko'radigan
    xodimlar doirasida (2026-11-14, user: nazoratchi qo'shishni ular ham qiladi)."""
    allowed = request["scope"]
    async with async_session() as session:
        rows = (await session.execute(select(ChecklistViewer.viewer_id, ChecklistViewer.employee_id))).all()
        emps = list(await session.scalars(
            select(Employee).where(Employee.is_active.is_(True)).order_by(Employee.full_name)))
        leaders = await service.leader_ids(session)
    if allowed is not None:
        emps = [e for e in emps if e.id in allowed]
        rows = [(v, e) for v, e in rows if v in allowed and e in allowed]
    by_viewer: dict[int, list[int]] = {}
    for v, e in rows:
        by_viewer.setdefault(v, []).append(e)
    names = {e.id: e.full_name for e in emps}
    return web.json_response({
        "viewers": [{"id": v, "full_name": names.get(v, "?"), "employee_ids": ids}
                    for v, ids in by_viewer.items() if v in names],
        "leaders": [{"id": i, "full_name": names[i]} for i in sorted(leaders, key=lambda i: names.get(i, ""))
                    if i in names],
        "employees": [{"id": e.id, "full_name": e.full_name, "role": e.role} for e in emps],
    })


@routes.put("/api/viewers/{id:\\d+}")
async def set_viewer(request):
    """Nazoratchining ko'radigan xodimlari ro'yxatini to'liq almashtiradi (bo'sh = olib tashlash).
    Nazoratchi buni faqat o'z doirasida qiladi: o'zi ko'rmaydigan xodimlarga tegmaydi."""
    allowed = request["scope"]
    viewer_id = int(request.match_info["id"])
    _in_scope(request, viewer_id)
    ids = (await _body(request)).get("employee_ids", [])
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise web.HTTPBadRequest(text='{"error":"employee_ids"}', content_type="application/json")
    for i in ids:
        _in_scope(request, i)
    async with async_session() as session:
        target = await session.get(Employee, viewer_id)
        if target is None:
            raise web.HTTPNotFound()
        valid = set(await session.scalars(select(Employee.id).where(Employee.id.in_(ids))))
        mine = delete(ChecklistViewer).where(ChecklistViewer.viewer_id == viewer_id)
        if allowed is not None:
            mine = mine.where(ChecklistViewer.employee_id.in_(allowed))
        await session.execute(mine)
        session.add_all(ChecklistViewer(viewer_id=viewer_id, employee_id=e) for e in valid if e != viewer_id)
        await session.commit()
    _refresh_menus(request)
    return web.json_response({"ok": True})


@routes.put("/api/leaders/{id:\\d+}")
async def set_leader(request):
    """Rahbar qilish / olib tashlash: {"on": true|false}. Faqat admin."""
    emp_id = int(request.match_info["id"])
    on = (await _body(request)).get("on")
    if not isinstance(on, bool):
        raise web.HTTPBadRequest(text='{"error":"bad_json"}', content_type="application/json")
    async with async_session() as session:
        emp = await session.get(Employee, emp_id)
        if emp is None or not emp.is_active:
            raise web.HTTPNotFound()
        s = await session.get(ChecklistSettings, 1)
        ids = set(s.leader_ids or [])
        s.leader_ids = sorted(ids | {emp_id} if on else ids - {emp_id})
        await session.commit()
    logger.info("checklist rahbar %s: %s (admin %s)", "qo'shildi" if on else "olindi", emp_id, request["me"].id)
    _refresh_menus(request)
    return web.json_response({"ok": True})


def _refresh_menus(request) -> None:
    """Panel tugmasini kimga ko'rsatish o'zgardi — fonda hammasini moslaymiz."""
    bot = request.config_dict.get("bot")
    if bot is not None:
        task = asyncio.create_task(sync_menus(bot))
        _BG.add(task)
        task.add_done_callback(_BG.discard)


_BG: set = set()  # fon vazifalariga havola (GC o'chirmasin)


def _settings_json(s: ChecklistSettings) -> dict:
    return {"penalty_per_question": s.penalty_per_question, "start_time": s.start_time,
            "deadline_time": s.deadline_time, "remind_every_min": s.remind_every_min, "points": list(s.points)}


def _bad(key: str):
    return web.HTTPBadRequest(text=f'{{"error":"{key}"}}', content_type="application/json")


@routes.get("/api/settings")
async def get_settings(request):
    async with async_session() as session:
        return web.json_response(_settings_json(await session.get(ChecklistSettings, 1)))


@routes.put("/api/settings")
async def put_settings(request):
    """Hamma sozlama bitta formadan; yuborilmagan maydon o'zgarmaydi."""
    data = await _body(request)
    async with async_session() as session:
        s = await session.get(ChecklistSettings, 1)
        try:
            rate = round(float(data.get("penalty_per_question", s.penalty_per_question)), 2)
        except (TypeError, ValueError):
            raise _bad("penalty")
        if not 0 <= rate <= 10:
            raise _bad("penalty_range")
        try:
            start = dtime.fromisoformat(str(data.get("start_time", s.start_time)))
            deadline = dtime.fromisoformat(str(data.get("deadline_time", s.deadline_time)))
            every = data.get("remind_every_min", s.remind_every_min)
            points = data.get("points", s.points)
            if not isinstance(every, int) or isinstance(every, bool) or not isinstance(points, list):
                raise ValueError("bad_json")
            start, deadline, every, points = logic.check_schedule(start.replace(second=0, microsecond=0),
                                                            deadline.replace(second=0, microsecond=0), every, points)
        except ValueError as exc:
            raise _bad(str(exc) if str(exc) in ("schedule_order", "remind_every", "points_list") else "time")
        new_times = (start.strftime("%H:%M"), deadline.strftime("%H:%M"), every)
        if new_times != (s.start_time, s.deadline_time, s.remind_every_min):
            # Savollar ketib, javob kutilayotgan paytda jadvalni o'zgartirish bugungi kunni buzadi.
            now_t = local_now().time()
            if logic.WINDOW_START <= now_t < logic.DEADLINE:
                raise web.HTTPConflict(text='{"error":"window_open"}', content_type="application/json")
        s.penalty_per_question = rate
        s.start_time, s.deadline_time, s.remind_every_min = new_times
        s.points = list(points)
        await session.commit()
        await service.load_schedule(session)
        result = _settings_json(s)
    logger.info("checklist sozlamalari o'zgardi: %s (xodim %s)", result, request["me"].id)
    bot = request.config_dict.get("bot")
    if bot is not None and jobs.SCHEDULER is not None:
        jobs.reschedule(bot)
        task = asyncio.create_task(jobs.catch_up(bot))  # muddat oldinga surilgan bo'lsa — kunni yopadi
        _BG.add(task)
        task.add_done_callback(_BG.discard)
    return web.json_response(result)


async def _index(request):
    return web.FileResponse(PUBLIC / "index.html")


def create_app(bot=None, bot_username: str = "") -> web.Application:
    app = web.Application(middlewares=[no_cache, auth])
    app["bot"] = bot  # xodimga xabar (jarima bekor qilindi); testda None
    app["bot_username"] = bot_username
    app.add_routes(routes)
    app.router.add_get("/", _index)  # add_static "/" ga index.html bermaydi
    app.router.add_static("/", PUBLIC, show_index=False)
    return app


async def run(bot, bot_username: str) -> None:
    runner = web.AppRunner(create_app(bot, bot_username))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.port).start()
    logger.info("Mini App :%s da ishlayapti", settings.port)
