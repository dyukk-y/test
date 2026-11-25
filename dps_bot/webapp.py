"""Мини-апп (Telegram Web App).

Статическая страница (docs/index.html, хостится на GitHub Pages или где угодно по HTTPS)
получает снимок активных событий прямо в ссылке: #d=base64url(JSON).
Отчёты и голоса возвращаются в бота через deep-link t.me/<bot>?start=r_… / v_…,
поэтому отдельный сервер с API не нужен.
"""
from __future__ import annotations

import base64
import json
import re

from aiogram.types import InlineKeyboardButton, WebAppInfo

from .utils import from_db

CODE = {"police": "p", "radar": "r", "camera": "c", "raid": "x", "accident": "a", "hazard": "h"}
KIND_BY_CODE = {v: k for k, v in CODE.items()}
MAX_URL = 3500  # запас под лимит длины ссылки web_app


def _pack(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def build_url(base: str, bot_username: str, events: list, user_loc=None, focus: int = 0,
              tab: str | None = None, api: str | None = None) -> str:
    items = []
    for e in events:
        if e["lat"] is None:
            continue
        items.append([e["id"], CODE.get(e["kind"], "p"), round(e["lat"], 5), round(e["lon"], 5),
                      int(from_db(e["created_at"]).timestamp()), e["confirms"], e["gone"],
                      (e["place"] or "")[:28], (e["direction"] or "")[:16], e["accuracy_m"] or 0])
    from .utils import utcnow

    data = {"v": 1, "t": int(utcnow().timestamp()), "b": bot_username, "e": items}
    if user_loc:
        data["u"] = [round(user_loc[0], 4), round(user_loc[1], 4)]
    if focus:
        data["f"] = focus
    if api:
        data["a"] = api
    suffix = "&tab=report" if tab == "report" else ""
    url = f"{base.rstrip('/')}/#d={_pack(data)}{suffix}"
    while len(url) > MAX_URL and data["e"]:  # оставляем самые свежие
        data["e"] = data["e"][: max(1, len(data["e"]) * 3 // 4)] if len(data["e"]) > 1 else []
        url = f"{base.rstrip('/')}/#d={_pack(data)}{suffix}"
    return url


def button(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))


_REPORT = re.compile(r"^r_([prcxah])_(\d{6,7})_(\d{6,8})$")
_VOTE = re.compile(r"^v_(\d+)_([hgf])$")


def parse_start(arg: str | None):
    """Разобрать deep-link из мини-аппа."""
    if not arg:
        return None
    m = _REPORT.match(arg)
    if m:
        return ("report", KIND_BY_CODE[m.group(1)], int(m.group(2)) / 1e5, int(m.group(3)) / 1e5)
    m = _VOTE.match(arg)
    if m:
        return ("vote", int(m.group(1)), {"h": "here", "g": "gone", "f": "fake"}[m.group(2)])
    if arg == "m":
        return ("menu",)
    return None
