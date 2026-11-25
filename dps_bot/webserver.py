"""Встроенный веб-сервер: мини-апп + API для живых данных.

Включается переменной WEBAPP_PORT. Наружу его нужно опубликовать по HTTPS
(Caddy/nginx — см. deploy/), тогда WEBAPP_URL=https://ваш-домен/ .

Каждый запрос к API подписан Telegram (initData) — подделать пользователя нельзя.
Без сервера мини-апп продолжает работать в режиме «снимок в ссылке + deep-link».
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data

from .app import App
from .events import NewEvent
from .keyboards import event_kb, with_home
from .texts import event_card
from .utils import from_db, now_db, utcnow
from .webapp import CODE, KIND_BY_CODE

log = logging.getLogger(__name__)
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "index.html")
MAX_AGE = timedelta(hours=24)


def _cors(resp: web.StreamResponse) -> web.StreamResponse:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _err(status: int, msg: str) -> web.Response:
    return _cors(web.json_response({"ok": False, "error": msg}, status=status))


def _event_json(e) -> list:
    return [e["id"], CODE.get(e["kind"], "p"), round(e["lat"], 5), round(e["lon"], 5),
            int(from_db(e["created_at"]).timestamp()), e["confirms"], e["gone"],
            e["place"] or "", e["direction"] or "", e["accuracy_m"] or 0]


class WebServer:
    def __init__(self, app: App, port: int, host: str = "0.0.0.0"):
        self.app, self.port, self.host = app, port, host
        self.runner: web.AppRunner | None = None

    # ------------------------------------------------------------- auth
    async def _auth(self, request: web.Request):
        init = request.headers.get("X-Init-Data") or request.query.get("init", "")
        if not init:
            raise web.HTTPUnauthorized(text="no init data")
        try:
            data = safe_parse_webapp_init_data(self.app.cfg.bot_token, init)
        except ValueError:
            raise web.HTTPUnauthorized(text="bad signature")
        if not data.user or utcnow() - data.auth_date > MAX_AGE:
            raise web.HTTPUnauthorized(text="expired")
        u = data.user
        user = await self.app.db.upsert_user(u.id, u.username, " ".join(filter(None, [u.first_name, u.last_name])))
        if user["banned"]:
            raise web.HTTPForbidden(text="banned")
        return user

    # ------------------------------------------------------------- handlers
    async def index(self, request: web.Request) -> web.StreamResponse:
        resp = web.FileResponse(STATIC)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    async def options(self, request: web.Request) -> web.Response:
        return _cors(web.Response())

    async def state(self, request: web.Request) -> web.Response:
        try:
            await self._auth(request)
        except web.HTTPException as e:
            return _err(e.status, e.text or "auth")
        me = await self.app.bot.me()
        events = [e for e in await self.app.events.active(300) if e["lat"] is not None]
        return _cors(web.json_response({"ok": True, "v": 1, "t": int(utcnow().timestamp()), "b": me.username,
                                        "e": [_event_json(e) for e in events]}))

    async def report(self, request: web.Request) -> web.Response:
        try:
            user = await self._auth(request)
            body = await request.json()
            kind = KIND_BY_CODE[body["k"]]
            lat, lon = float(body["lat"]), float(body["lon"])
            text = (str(body.get("text") or "").strip()[:500]) or None
        except web.HTTPException as e:
            return _err(e.status, e.text or "auth")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return _err(400, "bad request")

        app = self.app
        uid = user["user_id"]
        is_admin = await app.is_admin(uid)
        if not app.geo.in_bounds(lat, lon):
            return _err(422, "Точка за пределами района")
        if not is_admin and await app.events.rate_limited(uid):
            return _err(429, "Слишком много отчётов за час")
        a = app.analyze(text) if text else None
        ev = NewEvent(kind=kind, lat=lat, lon=lon, accuracy_m=int(body.get("acc") or 60), geo_method="gps",
                      place=app.geo.describe(lat, lon), direction=a.direction if a else None,
                      plate=a.plate if a else None, text=text,
                      source="admin" if is_admin else "user", author_id=uid)
        e, merged = await app.submit(ev, app.events.initial_status(user, is_admin), a, gps=True)
        status = "merged" if merged else e["status"]
        head = {"merged": "🤝 <b>Такое событие уже было — засчитали как подтверждение!</b>",
                "pending": "📩 <b>Отчёт из карты отправлен на проверку.</b>",
                "active": "✅ <b>Опубликовано из карты. Спасибо!</b>"}.get(status, "✅")
        await app.notifier.send(uid, head + "\n\n" + event_card(e, app.cfg.tz), with_home(event_kb(e)))
        return _cors(web.json_response({"ok": True, "status": status, "id": e["id"]}))

    async def vote(self, request: web.Request) -> web.Response:
        try:
            user = await self._auth(request)
            body = await request.json()
            eid = int(body["id"])
            value = {"h": "here", "g": "gone", "f": "fake"}[body["v"]]
        except web.HTTPException as e:
            return _err(e.status, e.text or "auth")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return _err(400, "bad request")
        is_admin = await self.app.is_admin(user["user_id"])
        msg, e = await self.app.events.vote(eid, user["user_id"], value, is_admin)
        return _cors(web.json_response({"ok": True, "message": msg.replace("<b>", "").replace("</b>", ""),
                                        "event": _event_json(e) if e and e["lat"] is not None else None,
                                        "active": bool(e and e["status"] in ("active", "pending"))}))

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "time": now_db()})

    # ------------------------------------------------------------- lifecycle
    def make_app(self) -> web.Application:
        w = web.Application(client_max_size=64 * 1024)
        w.router.add_get("/", self.index)
        w.router.add_get("/index.html", self.index)
        w.router.add_get("/health", self.health)
        w.router.add_get("/api/state", self.state)
        w.router.add_post("/api/report", self.report)
        w.router.add_post("/api/vote", self.vote)
        w.router.add_route("OPTIONS", "/api/{tail:.*}", self.options)
        return w

    async def start(self) -> None:
        self.runner = web.AppRunner(self.make_app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()
        log.info("🌐 Мини-апп и API слушают порт %s", self.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
