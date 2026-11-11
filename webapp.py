"""Mini App: boshliq (admin) va nazoratchilar uchun boshqaruv paneli.

Huquq: admin — hamma xodim + nazoratchilar + sozlama; nazoratchi — faqat
admin unga biriktirgan xodimlar (savollarini tahrirlaydi, javoblarini ko'radi).
Boshqa xodimlarga 403."""

import hashlib
import hmac
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from sqlalchemy import case, delete, func, or_, select

from config import settings
from db import (ChecklistAnswer, ChecklistDay, ChecklistQuestion, ChecklistSettings, ChecklistViewer,
                Employee, async_session)
from jobs import send
from logic import (DEADLINE_TXT, MONTHS, POINTS, START_TXT, fmt_ball, fmt_day, is_locked, local_now, question_day,
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


def _require_admin(request) -> None:
    if request["me"].role != service.ADMIN:
        raise web.HTTPForbidden(text='{"error":"admin_only"}', content_type="application/json")


async def _body(request) -> dict:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text='{"error":"bad_json"}', content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text='{"error":"bad_json"}', content_type="application/json")
    return data


def _points(data: dict) -> float:
    try:
        p = round(float(data.get("points", POINTS[0])), 2)
    except (TypeError, ValueError):
        p = None
    if p not in POINTS:
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


@routes.get("/api/me")
async def me(request):
    m = request["me"]
    return web.json_response({"id": m.id, "full_name": m.full_name, "role": m.role,
                              "is_admin": m.role == service.ADMIN, "start": START_TXT, "deadline": DEADLINE_TXT,
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
            .where(ChecklistQuestion.is_active.is_(True), ChecklistQuestion.day == today)
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
         "started_bot": e.id in members,
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
            q = (select(ChecklistQuestion).where(ChecklistQuestion.day == day, ChecklistQuestion.is_active.is_(True))
                 .order_by(ChecklistQuestion.position, ChecklistQuestion.id))
            if allowed is not None:
                q = q.where(ChecklistQuestion.employee_id.in_(allowed))
            questions = list(await session.scalars(q))
            emp_ids = {x.employee_id for x in questions}
            answers = {(a.employee_id, a.question_id): a for a in await session.scalars(
                select(ChecklistAnswer).where(ChecklistAnswer.day == day, ChecklistAnswer.employee_id.in_(emp_ids)))}
            day_rows = {r.employee_id: r for r in await session.scalars(
                select(ChecklistDay).where(ChecklistDay.day == day, ChecklistDay.employee_id.in_(emp_ids)))}
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
    return web.json_response({
        "month": start.strftime("%Y-%m"), "label": f"{MONTHS[start.month]} {start.year}",
        "current": today.strftime("%Y-%m"),
        "rows": [{"id": i, "full_name": n, "days": d, "asked": int(a or 0), "answered": int(b or 0),
                  "penalty": round(float(p or 0), 2)} for i, n, d, a, b, p in rows],
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
            select(ChecklistQuestion).where(ChecklistQuestion.employee_id == emp_id,
                                            ChecklistQuestion.is_active.is_(True), ChecklistQuestion.day >= since)
            .order_by(ChecklistQuestion.day, ChecklistQuestion.position, ChecklistQuestion.id)))
        answers = {(a.day, a.question_id): a for a in await session.scalars(
            select(ChecklistAnswer).where(ChecklistAnswer.employee_id == emp_id, ChecklistAnswer.day >= since))}
        day_rows = {r.day: r for r in await session.scalars(
            select(ChecklistDay).where(ChecklistDay.employee_id == emp_id, ChecklistDay.day >= since))}
        started = emp_id in await service.member_ids(session)
    by_day: dict = {}
    for q in questions:
        by_day.setdefault(q.day, []).append(q)
    days = [_day_json(d, day_rows.get(d), by_day.get(d, []), lambda q, d=d: answers.get((d, q.id)))
            for d in sorted(set(by_day) | set(day_rows), reverse=True)
            if is_locked(d, now)]  # hali yuborilmagani "navbatdagi savollar"da
    queued = [{"id": q.id, "text": q.text, "points": q.points, "readers": len(q.reader_ids or [])} for q in by_day.get(target, []) if not is_locked(q.day, now)]
    return web.json_response({
        "id": emp.id, "full_name": emp.full_name, "has_telegram": emp.telegram_id is not None,
        "started_bot": started, "target_label": _day_label(target, today), "queued": queued, "days": days,
    })


@routes.post("/api/questions")
async def add_question(request):
    """Bitta savol bir yoki bir nechta xodimga. Keyingi 19:00 ga yoziladi:
    kunduzi — bugungi, kechqurun — ertangi."""
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
            .where(ChecklistQuestion.employee_id.in_(valid), ChecklistQuestion.day == day)
            .group_by(ChecklistQuestion.employee_id))).all())
        session.add_all(ChecklistQuestion(employee_id=e, day=day, text=text, position=(last.get(e) or 0) + 1,
                                          points=points, reader_ids=readers)
                        for e in valid)
        await session.commit()
    logger.info("checklist savol %s xodimga, %s (xodim %s)", len(valid), day, request["me"].id)
    return web.json_response({"count": len(valid), "day": fmt_day(day)})


async def _own_question(request, session) -> ChecklistQuestion:
    """Tahrirlanadigan savol: o'z xodimiga tegishli va hali YUBORILMAGAN —
    yuborilgandan keyin o'zgartirish jarima hisobini buzadi."""
    q = await session.get(ChecklistQuestion, int(request.match_info["id"]))
    if q is None or not q.is_active:
        raise web.HTTPNotFound()
    _in_scope(request, q.employee_id)
    if is_locked(q.day):
        raise web.HTTPConflict(text='{"error":"question_sent"}', content_type="application/json")
    return q


@routes.put("/api/questions/{id:\\d+}")
async def edit_question(request):
    data = await _body(request)
    text, points = _text(data), _points(data)
    async with async_session() as session:
        q = await _own_question(request, session)
        q.text, q.points = text, points
        await session.commit()
    return web.json_response({"id": q.id, "text": q.text, "points": q.points})


@routes.delete("/api/questions/{id:\\d+}")
async def delete_question(request):
    async with async_session() as session:
        q = await _own_question(request, session)
        q.is_active = False
        await session.commit()
    return web.json_response({"ok": True})


@routes.post("/api/questions/{id:\\d+}/up")
async def move_question_up(request):
    """Savolni bitta yuqoriga suradi (yuborilmagan savollar ichida)."""
    async with async_session() as session:
        q = await _own_question(request, session)
        qs = await service.day_questions(session, q.employee_id, q.day)
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
    _require_admin(request)
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
                       f"✅ {fmt_day(row.day)} dagi checklist jarimangiz ({fmt_ball(-row.penalty)} ball) bekor qilindi.\n"
                       f"Oylik kuni: {fmt_day(pay_day)}")
        except Exception:
            logger.exception("bekor qilish xabari yuborilmadi (xodim %s)", emp.id)
    return web.json_response({"ok": True})


@routes.get("/api/viewers")
async def viewers(request):
    _require_admin(request)
    async with async_session() as session:
        rows = (await session.execute(select(ChecklistViewer.viewer_id, ChecklistViewer.employee_id))).all()
        emps = list(await session.scalars(
            select(Employee).where(Employee.is_active.is_(True)).order_by(Employee.full_name)))
    by_viewer: dict[int, list[int]] = {}
    for v, e in rows:
        by_viewer.setdefault(v, []).append(e)
    names = {e.id: e.full_name for e in emps}
    return web.json_response({
        "viewers": [{"id": v, "full_name": names.get(v, "?"), "employee_ids": ids}
                    for v, ids in by_viewer.items() if v in names],
        "employees": [{"id": e.id, "full_name": e.full_name, "role": e.role} for e in emps],
    })


@routes.put("/api/viewers/{id:\\d+}")
async def set_viewer(request):
    """Nazoratchining ko'radigan xodimlari ro'yxatini to'liq almashtiradi (bo'sh = olib tashlash)."""
    _require_admin(request)
    viewer_id = int(request.match_info["id"])
    ids = (await _body(request)).get("employee_ids", [])
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise web.HTTPBadRequest(text='{"error":"employee_ids"}', content_type="application/json")
    async with async_session() as session:
        if await session.get(Employee, viewer_id) is None:
            raise web.HTTPNotFound()
        valid = set(await session.scalars(select(Employee.id).where(Employee.id.in_(ids))))
        await session.execute(delete(ChecklistViewer).where(ChecklistViewer.viewer_id == viewer_id))
        session.add_all(ChecklistViewer(viewer_id=viewer_id, employee_id=e) for e in valid if e != viewer_id)
        await session.commit()
    return web.json_response({"ok": True})


@routes.get("/api/settings")
async def get_settings(request):
    _require_admin(request)
    async with async_session() as session:
        return web.json_response({"penalty_per_question": await service.penalty_rate(session)})


@routes.put("/api/settings")
async def put_settings(request):
    _require_admin(request)
    try:
        rate = round(float((await _body(request))["penalty_per_question"]), 2)
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text='{"error":"penalty"}', content_type="application/json")
    if not 0 <= rate <= 10:
        raise web.HTTPBadRequest(text='{"error":"penalty_range"}', content_type="application/json")
    async with async_session() as session:
        s = await session.get(ChecklistSettings, 1)
        s.penalty_per_question = rate
        await session.commit()
    logger.info("checklist jarima %s ga o'zgartirildi (xodim %s)", rate, request["me"].id)
    return web.json_response({"penalty_per_question": rate})


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
