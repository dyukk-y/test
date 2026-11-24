"""Тексты сообщений (HTML)."""
from __future__ import annotations

from .kinds import kind
from .nlp import Analysis
from .utils import ago, arrow, bearing, dist_str, esc, haversine, local

ACCURACY = {
    "gps": "📡 GPS",
    "place": "📌 ориентир",
    "memory": "🧠 по прошлым отчётам",
    "geocoder": "🗺 по адресу",
    "village": "🏘 примерно (центр села)",
}
SOURCE = {"user": "водитель", "channel": "канал", "group": "чат", "admin": "администратор"}


def accuracy_line(method: str | None, acc: int | None) -> str:
    if not method:
        return "📍 место не определено"
    label = ACCURACY.get(method, method)
    return f"{label}, ±{dist_str((acc or 0) / 1000)}" if acc else label


def event_title(e) -> str:
    k = kind(e["kind"])
    place = e["place"] or "место не указано"
    return f"{k.emoji} <b>{k.title}</b> — {esc(place)}"


def event_card(e, tz, user_lat: float | None = None, user_lon: float | None = None) -> str:
    lines = [event_title(e)]
    if e["direction"]:
        lines.append(f"➡️ Направление: в сторону {esc(e['direction'])}")
    if e["plate"]:
        lines.append(f"🚗 Номер: <code>{esc(e['plate'])}</code>")
    lines.append(f"🕐 {local(e['created_at'], tz, '%H:%M')} ({ago(e['created_at'])}), "
                 f"обновлено {ago(e['updated_at'])}")
    lines.append(accuracy_line(e["geo_method"], e["accuracy_m"]))
    if user_lat is not None and e["lat"] is not None:
        d = haversine(user_lat, user_lon, e["lat"], e["lon"])
        lines.append(f"{arrow(bearing(user_lat, user_lon, e['lat'], e['lon']))} {dist_str(d)} от вас")
    votes = f"👍 {e['confirms']}  👋 {e['gone']}  🚫 {e['denies']}"
    if e["reports"] > 1:
        votes += f"  📣 сообщили {e['reports']} раз"
    lines.append(votes)
    status = {"pending": "⏳ на проверке", "closed": "✔️ неактуально", "fake": "🚫 фейк",
              "rejected": "❌ отклонено"}.get(e["status"])
    if status:
        lines.append(status)
    if e["text"]:
        text = e["text"] if len(e["text"]) < 300 else e["text"][:300] + "…"
        lines.append(f"\n💬 <i>{esc(text)}</i>")
    lines.append(f"\n<code>#{e['id']}</code> · источник: {SOURCE.get(e['source'], e['source'])}")
    return "\n".join(lines)


def event_line(e, tz, user_lat=None, user_lon=None) -> str:
    k = kind(e["kind"])
    dist = ""
    if user_lat is not None and e["lat"] is not None:
        d = haversine(user_lat, user_lon, e["lat"], e["lon"])
        dist = f" · {arrow(bearing(user_lat, user_lon, e['lat'], e['lon']))} {dist_str(d)}"
    conf = f" · 👍{e['confirms']}" if e["confirms"] else ""
    return (f"{k.emoji} <b>{esc(e['place'] or k.title)}</b>{dist}\n"
            f"    {k.title}, {ago(e['created_at'])}{conf} · #{e['id']}")


def analysis_preview(a: Analysis, geo_label: str | None, method: str | None, acc: int | None, kind_code: str) -> str:
    k = kind(kind_code)
    lines = ["🧐 <b>Вот что я понял:</b>", "", f"{k.emoji} Тип: <b>{k.title}</b>"]
    lines.append(f"📍 Место: <b>{esc(geo_label)}</b>" if geo_label else "📍 Место: <b>не понял</b> ❗")
    lines.append(f"    {accuracy_line(method, acc)}" if method else "    уточните точку кнопкой ниже")
    if a.direction:
        lines.append(f"➡️ В сторону: {esc(a.direction)}")
    if a.plate:
        lines.append(f"🚗 Номер: <code>{esc(a.plate)}</code>")
    if a.road_km is not None:
        lines.append(f"🛣 {a.road_km} км трассы")
    lines.append("")
    lines.append("Всё верно? Можно уточнить точку — тогда метка будет точной до метров.")
    return "\n".join(lines)


START = (
    "🚔 <b>DPS Guardian — {region}</b>\n\n"
    "Помогаю водителям знать, где стоят ДПС, камеры и радары, где ДТП и опасные участки.\n\n"
    "<b>Как пользоваться:</b>\n"
    "• <b>Просто напишите</b>, что видите: «гайцы у моста в Бажинске» — я пойму сокращения, сленг и опечатки\n"
    "• Нажмите <b>{loc}</b> — покажу, что рядом с вами, и можно отметить событие в 2 касания\n"
    "• Включите <b>трансляцию геопозиции</b> в дороге — предупрежу о ДПС <i>впереди по курсу</i>\n"
    "• Подпишитесь на свои сёла в ⚙️ Настройках — пришлю уведомление, если там появятся ДПС\n\n"
    "Чем больше подтверждений — тем точнее карта. Спасибо, что помогаете! 🤝"
)

HELP = (
    "📚 <b>Справка</b>\n\n"
    "<b>Сообщить о событии</b>\n"
    "Пишите как удобно: «дпс на выезде из Маслянино в сторону Новосиба», «тренога на ул. Ленина», "
    "«ДТП на 45 км», «чисто у моста» (отбой). Можно приложить фото и подпись.\n"
    "Самое точное — отправить геопозицию и выбрать тип события.\n\n"
    "<b>Голосование</b>\n"
    "✅ Стоят — продлевает событие · 👋 Уехали — закрывает · 🚫 Фейк — жалоба.\n"
    "За подтверждённые отчёты растёт репутация, доверенные пользователи публикуют без проверки.\n\n"
    "<b>Уведомления</b>\n"
    "• по подписке на сёла (⚙️ Настройки → Мои сёла)\n"
    "• по вашей точке в радиусе N км\n"
    "• 🛰 в дороге: поделитесь <i>трансляцией геопозиции</i> (📎 → Геопозиция → Транслировать) — "
    "бот предупредит о событиях впереди по ходу движения\n\n"
    "<b>Прогноз</b> — где наряды обычно стоят в это время по статистике.\n\n"
    "<b>Команды</b>\n"
    "/start — меню · /near — что рядом · /feed — сейчас на дорогах · /map — карта\n"
    "/forecast — прогноз · /me — моя статистика · /settings — настройки · /cancel — отмена\n\n"
    "Бот можно добавить в местный чат водителей — он будет сам собирать отчёты из переписки "
    "(нужно выключить privacy mode у бота в @BotFather или сделать его админом чата)."
)
