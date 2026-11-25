"""API мини-аппа: подпись Telegram, живые данные, отчёт и голос."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from dps_bot.webserver import WebServer

from .test_e2e import make_app, run

TOKEN = "123456:TEST"


def init_data(uid: int, token: str = TOKEN, auth_date: int | None = None) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAA",
              "user": json.dumps({"id": uid, "first_name": f"U{uid}", "username": f"u{uid}"}, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_api(tmp_path, fake_bot):
    bot, s = fake_bot

    async def scenario():
        app = await make_app(tmp_path, bot)
        app.cfg.__dict__["bot_token"] = TOKEN  # frozen dataclass — подменяем для теста
        client = TestClient(TestServer(WebServer(app, 0).make_app()))
        await client.start_server()
        try:
            h = {"X-Init-Data": init_data(31)}

            # без подписи и с чужой подписью — отказ
            assert (await client.get("/api/state")).status == 401
            assert (await client.get("/api/state", headers={"X-Init-Data": init_data(31, "999:OTHER")})).status == 401
            old = init_data(31, auth_date=int(time.time()) - 3 * 86400)
            assert (await client.get("/api/state", headers={"X-Init-Data": old})).status == 401

            r = await client.get("/api/state", headers=h)
            j = await r.json()
            assert r.status == 200 and j["ok"] and j["e"] == [] and r.headers["Access-Control-Allow-Origin"] == "*"

            # отчёт с комментарием прямо из карты
            r = await client.post("/api/report", headers=h, json={
                "k": "r", "lat": 54.3726, "lon": 84.2764, "acc": 25, "text": "фен у моста, в сторону Новосиба"})
            j = await r.json()
            assert r.status == 200 and j["status"] == "active", j
            e = await app.events.get(j["id"])
            assert e["kind"] == "radar" and e["direction"] == "Новосибирск" and e["place"] == "Бажинск"
            assert any("Опубликовано из карты" in t for t in s.texts(31)), "бот прислал карточку в чат"

            # за пределами района и мусор — отказ
            r = await client.post("/api/report", headers=h, json={"k": "p", "lat": 55.03, "lon": 82.92})
            assert r.status == 422
            assert (await client.post("/api/report", headers=h, json={"k": "zzz"})).status == 400

            # голос другого пользователя
            r = await client.post("/api/vote", headers={"X-Init-Data": init_data(32)}, json={"id": j["id"], "v": "h"})
            assert (await r.json())["ok"] and (await app.events.get(j["id"]))["confirms"] == 1

            # страница отдаётся сервером
            r = await client.get("/")
            assert r.status == 200 and "DPS Guardian" in await r.text()
        finally:
            await client.close()

    run(scenario())
