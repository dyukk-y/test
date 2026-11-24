"""Клавиатуры и callback-фабрики (никакого ручного split('_') — данные типизированы)."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .gazetteer import Place
from .kinds import KINDS
from .utils import maps_links


class EvCb(CallbackData, prefix="ev"):
    act: str          # card | here | gone | fake | loc | ok | no | ban
    id: int


class RepCb(CallbackData, prefix="rp"):
    act: str          # kind | pub | cancel | village | needloc
    val: str = ""
    page: int = 0


class NavCb(CallbackData, prefix="nav"):
    to: str
    page: int = 0


class SetCb(CallbackData, prefix="st"):
    key: str
    val: str = ""


class ZoneCb(CallbackData, prefix="zn"):
    idx: int
    page: int = 0


class PremCb(CallbackData, prefix="pr"):
    days: int


class AdmCb(CallbackData, prefix="adm"):
    sec: str
    act: str = ""
    id: int = 0
    page: int = 0


# ------------------------------------------------------------------ главное меню
BTN_LOCATION = "📍 Что рядом"
BTN_REPORT = "🚨 Сообщить"
BTN_MAP = "🗺 Карта"
BTN_FEED = "📋 Сейчас на дорогах"
BTN_FORECAST = "🔮 Прогноз"
BTN_SETTINGS = "⚙️ Настройки"
BTN_PREMIUM = "⭐ Премиум"
BTN_HELP = "❓ Помощь"
BTN_HERE = "📍 Отправить мою точку"


def main_menu() -> ReplyKeyboardRemove:
    """Меню теперь на inline-кнопках; этим убираем старую клавиатуру снизу."""
    return ReplyKeyboardRemove()


def home_kb(webapp_url: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if webapp_url:
        b.row(InlineKeyboardButton(text="🗺 Открыть карту", web_app=WebAppInfo(url=webapp_url)))
    b.row(InlineKeyboardButton(text="📋 Сейчас на дорогах", callback_data=NavCb(to="feed").pack()),
          InlineKeyboardButton(text="🚨 Сообщить", callback_data=NavCb(to="report").pack()))
    b.row(InlineKeyboardButton(text="📍 Что рядом", callback_data=NavCb(to="near").pack()),
          InlineKeyboardButton(text="🔮 Прогноз", callback_data=NavCb(to="fc").pack()))
    b.row(InlineKeyboardButton(text="⚙️ Уведомления", callback_data=NavCb(to="settings").pack()),
          InlineKeyboardButton(text="👤 Профиль", callback_data=NavCb(to="me").pack()))
    b.row(InlineKeyboardButton(text="⭐ Премиум", callback_data=NavCb(to="premium").pack()),
          InlineKeyboardButton(text="❓ Помощь", callback_data=NavCb(to="help").pack()))
    return b.as_markup()


HOME_BTN = InlineKeyboardButton(text="◀️ В меню", callback_data=NavCb(to="home").pack())


def with_home(kb: InlineKeyboardMarkup | None = None) -> InlineKeyboardMarkup:
    rows = list(kb.inline_keyboard) if kb else []
    return InlineKeyboardMarkup(inline_keyboard=rows + [[HOME_BTN]])


def location_request() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_HERE, request_location=True)], [KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True, one_time_keyboard=True,
    )


# ------------------------------------------------------------------ события
def kinds_kb(prefix_text: str = "") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in KINDS.values():
        b.button(text=f"{k.emoji} {k.title}", callback_data=RepCb(act="kind", val=k.code))
    b.button(text="❌ Отмена", callback_data=RepCb(act="cancel"))
    b.adjust(2)
    return b.as_markup()


def event_kb(e, show_card_btn: bool = False, is_admin: bool = False, map_url: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    eid = e["id"]
    b.row(
        InlineKeyboardButton(text=f"✅ Стоят ({e['confirms']})", callback_data=EvCb(act="here", id=eid).pack()),
        InlineKeyboardButton(text=f"👋 Уехали ({e['gone']})", callback_data=EvCb(act="gone", id=eid).pack()),
        InlineKeyboardButton(text="🚫 Фейк", callback_data=EvCb(act="fake", id=eid).pack()),
    )
    if e["lat"] is not None:
        ya, go = maps_links(e["lat"], e["lon"])
        b.row(
            InlineKeyboardButton(text="📍 Точка", callback_data=EvCb(act="loc", id=eid).pack()),
            InlineKeyboardButton(text="Яндекс", url=ya),
            InlineKeyboardButton(text="Google", url=go),
        )
    if map_url:
        b.row(InlineKeyboardButton(text="🗺 Открыть на карте", web_app=WebAppInfo(url=map_url)))
    if show_card_btn:
        b.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=EvCb(act="card", id=eid).pack()))
    if is_admin:
        b.row(InlineKeyboardButton(text="🛡 Модерировать", callback_data=AdmCb(sec="ev", id=eid).pack()))
    return b.as_markup()


def report_preview_kb(has_location: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Опубликовать", callback_data=RepCb(act="pub").pack()))
    b.row(InlineKeyboardButton(text="📍 Указать точно (геопозиция)", callback_data=RepCb(act="needloc").pack()))
    b.row(
        InlineKeyboardButton(text="🏘 Выбрать село", callback_data=RepCb(act="village").pack()),
        InlineKeyboardButton(text="🔁 Тип", callback_data=RepCb(act="retype").pack()),
    )
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data=RepCb(act="cancel").pack()))
    return b.as_markup()


def villages_kb(villages: list[Place], page: int, act: str = "pickv", per_page: int = 12) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    pages = max(1, (len(villages) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    for i, p in enumerate(villages[page * per_page:(page + 1) * per_page], start=page * per_page):
        b.button(text=p.name, callback_data=RepCb(act=act, val=str(i)))
    b.adjust(3)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=RepCb(act="village", page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=RepCb(act="village", page=page + 1).pack()))
    b.row(*nav)
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data=RepCb(act="cancel").pack()))
    return b.as_markup()


def feed_kb(events: list, page: int, pages: int, map_url: str | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for e in events:
        from .kinds import kind
        b.button(text=f"{kind(e['kind']).emoji} #{e['id']}", callback_data=EvCb(act="card", id=e["id"]))
    b.adjust(4)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=NavCb(to="feed", page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f"🔄 {page + 1}/{pages}", callback_data=NavCb(to="feed", page=page).pack()))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=NavCb(to="feed", page=page + 1).pack()))
    b.row(*nav)
    if map_url:
        b.row(InlineKeyboardButton(text="🗺 Открыть карту", web_app=WebAppInfo(url=map_url)))
    b.row(
        InlineKeyboardButton(text="🖼 Картинка", callback_data=NavCb(to="map_png").pack()),
        InlineKeyboardButton(text="📄 HTML-файл", callback_data=NavCb(to="map_html").pack()),
    )
    b.row(HOME_BTN)
    return b.as_markup()


def map_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🗺 Интерактивная (HTML)", callback_data=NavCb(to="map_html").pack()),
        InlineKeyboardButton(text="🖼 Картинка", callback_data=NavCb(to="map_png").pack()),
    )
    b.row(InlineKeyboardButton(text="🔥 Карта «горячих точек» за 30 дней", callback_data=NavCb(to="map_heat").pack()))
    b.row(HOME_BTN)
    return b.as_markup()


def nearby_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in KINDS.values():
        b.button(text=f"{k.emoji} Тут {k.title.split(' /')[0].lower()}", callback_data=RepCb(act="here", val=k.code))
    b.adjust(2)
    b.row(HOME_BTN)
    return b.as_markup()


# ------------------------------------------------------------------ настройки
RADII = [1, 2, 3, 5, 10]
QUIET = [("off", "выкл"), ("23-7", "23:00–07:00"), ("0-6", "00:00–06:00"), ("22-8", "22:00–08:00")]


def settings_kb(u, zones_cnt: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"🔔 Уведомления: {'вкл' if u['alerts'] else 'выкл'}",
                               callback_data=SetCb(key="alerts").pack()))
    b.row(InlineKeyboardButton(text=f"📏 Радиус: {u['radius_km']:g} км", callback_data=SetCb(key="radius").pack()))
    quiet = "выкл" if u["quiet_from"] is None else f"{u['quiet_from']:02d}:00–{u['quiet_to']:02d}:00"
    b.row(InlineKeyboardButton(text=f"🌙 Тихие часы: {quiet}", callback_data=SetCb(key="quiet").pack()))
    b.row(InlineKeyboardButton(text="🎚 Какие события присылать", callback_data=SetCb(key="kinds").pack()))
    b.row(InlineKeyboardButton(text=f"🏘 Мои сёла ({zones_cnt})", callback_data=ZoneCb(idx=-1).pack()))
    b.row(InlineKeyboardButton(text="🛰 Режим «в дороге» (live)", callback_data=SetCb(key="live").pack()))
    b.row(HOME_BTN)
    return b.as_markup()


def radius_kb(current: float, premium: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for r in RADII:
        lock = "" if premium or r <= 3 else " ⭐"
        mark = "• " if r == current else ""
        b.button(text=f"{mark}{r} км{lock}", callback_data=SetCb(key="radius", val=str(r)))
    b.adjust(5)
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data=NavCb(to="settings").pack()))
    return b.as_markup()


def quiet_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, title in QUIET:
        b.button(text=title, callback_data=SetCb(key="quiet", val=code))
    b.adjust(2)
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data=NavCb(to="settings").pack()))
    return b.as_markup()


def alert_kinds_kb(enabled: set[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in KINDS.values():
        mark = "✅" if k.code in enabled else "▫️"
        b.button(text=f"{mark} {k.emoji} {k.title}", callback_data=SetCb(key="kind", val=k.code))
    b.adjust(1)
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data=NavCb(to="settings").pack()))
    return b.as_markup()


def zones_kb(villages: list[Place], subs: set[str], page: int, per_page: int = 12) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    pages = max(1, (len(villages) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    for i, p in enumerate(villages[page * per_page:(page + 1) * per_page], start=page * per_page):
        mark = "✅ " if p.key in subs else ""
        b.button(text=f"{mark}{p.name}", callback_data=ZoneCb(idx=i, page=page))
    b.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=ZoneCb(idx=-1, page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=ZoneCb(idx=-1, page=page + 1).pack()))
    b.row(*nav)
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data=NavCb(to="settings").pack()))
    return b.as_markup()


def premium_kb(plans: list[tuple[int, int]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for days, stars in plans:
        b.button(text=f"⭐ {stars} — {days} дн.", callback_data=PremCb(days=days))
    b.adjust(1)
    b.row(HOME_BTN)
    return b.as_markup()


def back_kb(to: str, text: str = "◀️ Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=NavCb(to=to).pack())]])
