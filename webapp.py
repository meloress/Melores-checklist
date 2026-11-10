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
from logic import DEADLINE_TXT, START_TXT, local_now, reminder_times
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
                              "reminders": [t.strftime("%H:%M") for t in reminder_times()[1:]]})


@routes.get("/api/employees")
async def employees(request):
    day = local_now().date()
    allowed = request["scope"]
    async with async_session() as session:
        q = select(Employee).where(Employee.is_active.is_(True)).order_by(Employee.full_name)
        if allowed is not None:
            q = q.where(Employee.id.in_(allowed))
        emps = list(await session.scalars(q))
        counts = dict((await session.execute(
            select(ChecklistQuestion.employee_id, func.count())
            .where(ChecklistQuestion.is_active.is_(True)).group_by(ChecklistQuestion.employee_id))).all())
        answered = dict((await session.execute(
            select(ChecklistAnswer.employee_id, func.count())
            .where(ChecklistAnswer.day == day).group_by(ChecklistAnswer.employee_id))).all())
        month_penalty = dict((await session.execute(
            select(ChecklistDay.employee_id, func.sum(ChecklistDay.penalty))
            .where(ChecklistDay.day >= day.replace(day=1)).group_by(ChecklistDay.employee_id))).all())
    return web.json_response([
        {"id": e.id, "full_name": e.full_name, "role": e.role, "has_telegram": e.telegram_id is not None,
         "questions": counts.get(e.id, 0), "answered_today": answered.get(e.id, 0),
         "month_penalty": round(float(month_penalty.get(e.id) or 0), 2)}
        for e in emps
    ])


@routes.get("/api/employees/{id:\\d+}")
async def employee_detail(request):
    emp_id = int(request.match_info["id"])
    _in_scope(request, emp_id)
    since = local_now().date() - timedelta(days=HISTORY_DAYS - 1)
    async with async_session() as session:
        emp = await session.get(Employee, emp_id)
        if emp is None:
            raise web.HTTPNotFound()
        questions = await session.scalars(
            select(ChecklistQuestion).where(ChecklistQuestion.employee_id == emp_id, ChecklistQuestion.is_active.is_(True))
            .order_by(ChecklistQuestion.position, ChecklistQuestion.id))
        answers = await session.scalars(
            select(ChecklistAnswer).where(ChecklistAnswer.employee_id == emp_id, ChecklistAnswer.day >= since)
            .order_by(ChecklistAnswer.day.desc(), ChecklistAnswer.id))
        day_rows = {r.day: r for r in await session.scalars(
            select(ChecklistDay).where(ChecklistDay.employee_id == emp_id, ChecklistDay.day >= since))}
    days: dict = {}
    for a in answers:
        days.setdefault(a.day, []).append({"q": a.question_text, "a": a.answer_text,
                                           "at": a.answered_at.astimezone(local_now().tzinfo).strftime("%H:%M")})
    for d in day_rows:
        days.setdefault(d, [])
    return web.json_response({
        "id": emp.id, "full_name": emp.full_name, "has_telegram": emp.telegram_id is not None,
        "questions": [{"id": q.id, "text": q.text} for q in questions],
        "days": [{
            "day": d.strftime("%d.%m.%Y"), "items": items,
            "asked": day_rows[d].asked if d in day_rows else None,
            "penalty": day_rows[d].penalty if d in day_rows else 0,
            "delivered": d in day_rows and day_rows[d].delivered_at is not None,
            "closed": d in day_rows and day_rows[d].closed_at is not None,
        } for d, items in sorted(days.items(), reverse=True)],
    })


@routes.post("/api/employees/{id:\\d+}/questions")
async def add_question(request):
    emp_id = int(request.match_info["id"])
    _in_scope(request, emp_id)
    text = _text(await _body(request))
    async with async_session() as session:
        if await session.get(Employee, emp_id) is None:
            raise web.HTTPNotFound()
        pos = await session.scalar(select(func.coalesce(func.max(ChecklistQuestion.position), 0))
                                   .where(ChecklistQuestion.employee_id == emp_id))
        q = ChecklistQuestion(employee_id=emp_id, text=text, position=pos + 1)
        session.add(q)
        await session.commit()
    return web.json_response({"id": q.id, "text": q.text})


async def _own_question(request, session) -> ChecklistQuestion:
    q = await session.get(ChecklistQuestion, int(request.match_info["id"]))
    if q is None or not q.is_active:
        raise web.HTTPNotFound()
    _in_scope(request, q.employee_id)
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
        q.is_active = False  # javoblar tarixi qolsin
        await session.commit()
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


def create_app() -> web.Application:
    app = web.Application(middlewares=[no_cache, auth])
    app.add_routes(routes)
    app.router.add_get("/", _index)  # add_static "/" ga index.html bermaydi
    app.router.add_static("/", PUBLIC, show_index=False)
    return app


async def run() -> None:
    runner = web.AppRunner(create_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.port).start()
    logger.info("Mini App :%s da ishlayapti", settings.port)
