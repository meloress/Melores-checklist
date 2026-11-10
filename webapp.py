"""Mini App: boshliq (admin) va nazoratchilar uchun boshqaruv paneli.

Huquq: admin — hamma xodim + nazoratchilar + sozlama; nazoratchi — faqat
admin unga biriktirgan xodimlar (savollarini tahrirlaydi, javoblarini ko'radi).
Boshqa xodimlarga 403."""

import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from sqlalchemy import delete, func, select

from config import settings
from db import (ChecklistAnswer, ChecklistDay, ChecklistQuestion, ChecklistSettings, ChecklistViewer,
                Employee, async_session)
from jobs import send
from logic import (DEADLINE_TXT, START_TXT, fmt_ball, fmt_day, is_locked, local_now, question_day,
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
    days = []
    for d in sorted(set(by_day) | set(day_rows), reverse=True):
        if not is_locked(d, now):
            continue  # hali yuborilmagan — "navbatdagi savollar"da ko'rinadi
        r = day_rows.get(d)
        items = []
        for q in by_day.get(d, []):
            a = answers.get((d, q.id))
            items.append({"q": q.text, "a": a.answer_text if a else None,
                          "at": a.answered_at.astimezone(now.tzinfo).strftime("%H:%M") if a else None})
        days.append({
            "day_id": r.id if r else None, "day": fmt_day(d), "label": _day_label(d, today), "items": items,
            "penalty": r.penalty if r else 0, "closed": bool(r and r.closed_at),
            "delivered": bool(r and r.delivered_at), "blocked": bool(r and r.blocked_at and not r.delivered_at),
            "cancelled": bool(r and r.cancelled_at),
        })
    queued = [{"id": q.id, "text": q.text} for q in by_day.get(target, []) if not is_locked(q.day, now)]
    return web.json_response({
        "id": emp.id, "full_name": emp.full_name, "has_telegram": emp.telegram_id is not None,
        "started_bot": started, "target_label": _day_label(target, today), "queued": queued, "days": days,
    })


@routes.post("/api/employees/{id:\\d+}/questions")
async def add_question(request):
    """Savol keyingi 19:00 ga yoziladi: kunduzi — bugungi, kechqurun — ertangi."""
    emp_id = int(request.match_info["id"])
    _in_scope(request, emp_id)
    text = _text(await _body(request))
    day = question_day()
    async with async_session() as session:
        if await session.get(Employee, emp_id) is None:
            raise web.HTTPNotFound()
        pos = await session.scalar(select(func.coalesce(func.max(ChecklistQuestion.position), 0))
                                   .where(ChecklistQuestion.employee_id == emp_id, ChecklistQuestion.day == day))
        q = ChecklistQuestion(employee_id=emp_id, day=day, text=text, position=pos + 1)
        session.add(q)
        await session.commit()
    return web.json_response({"id": q.id, "text": q.text, "day": fmt_day(day)})


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
    text = _text(await _body(request))
    async with async_session() as session:
        q = await _own_question(request, session)
        q.text = text
        await session.commit()
    return web.json_response({"id": q.id, "text": q.text})


@routes.delete("/api/questions/{id:\\d+}")
async def delete_question(request):
    async with async_session() as session:
        q = await _own_question(request, session)
        q.is_active = False
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
