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
    return f"{k.emoji} <b>{k.title}</b> · {esc(e['place'] or 'место не указано')}"


STATUS = {"pending": "⏳ <i>на проверке у модератора</i>", "closed": "✔️ <i>уже неактуально</i>",
          "fake": "🚫 <i>признано фейком</i>", "rejected": "❌ <i>отклонено</i>"}


def event_card(e, tz, user_lat: float | None = None, user_lon: float | None = None) -> str:
    lines = [event_title(e), ""]
    if e["direction"]:
        lines.append(f"➡️ в сторону <b>{esc(e['direction'])}</b>")
    if e["plate"]:
        lines.append(f"🚗 номер <code>{esc(e['plate'])}</code>")
    lines.append(f"🕐 {local(e['created_at'], tz, '%H:%M')} · {ago(e['created_at'])}")
    lines.append(f"🎯 {accuracy_line(e['geo_method'], e['accuracy_m'])}")
    if user_lat is not None and e["lat"] is not None:
        d = haversine(user_lat, user_lon, e["lat"], e["lon"])
        lines.append(f"{arrow(bearing(user_lat, user_lon, e['lat'], e['lon']))} <b>{dist_str(d)}</b> от вас")
    votes = f"👍 {e['confirms']} стоят · 👋 {e['gone']} уехали"
    if e["denies"]:
        votes += f" · 🚫 {e['denies']}"
    if e["reports"] > 1:
        votes += f" · 📣 ×{e['reports']}"
    lines += ["", votes]
    if e["status"] in STATUS:
        lines.append(STATUS[e["status"]])
    if e["text"]:
        text = e["text"] if len(e["text"]) < 400 else e["text"][:400] + "…"
        lines.append(f"<blockquote>{esc(text)}</blockquote>")
    lines.append(f"<i>#{e['id']} · {SOURCE.get(e['source'], e['source'])}</i>")
    return "\n".join(lines)


def event_line(e, tz, user_lat=None, user_lon=None) -> str:
    k = kind(e["kind"])
    extra = []
    if user_lat is not None and e["lat"] is not None:
        d = haversine(user_lat, user_lon, e["lat"], e["lon"])
        extra.append(f"{arrow(bearing(user_lat, user_lon, e['lat'], e['lon']))} {dist_str(d)}")
    extra.append(ago(e["created_at"]))
    if e["confirms"]:
        extra.append(f"👍 {e['confirms']}")
    return f"{k.emoji} <b>{esc(e['place'] or k.title)}</b> — {k.title.lower()}\n      <i>{' · '.join(extra)}</i>"


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


WELCOME = (
    "👋 <b>Привет! Я DPS Guardian — помощник водителей</b> · {region}\n\n"
    "Показываю, где сейчас стоят ДПС, камеры и радары, где ДТП и опасные участки. "
    "Всё это отмечают сами водители — и вы тоже можете помочь.\n\n"
    "<b>Как сообщить</b>\n"
    "✍️ Просто напишите мне: <i>«гайцы у моста в Бажинске»</i> — я пойму сленг и сокращения\n"
    "📍 Или пришлите геопозицию и выберите, что видите\n\n"
    "<b>Как не пропустить</b>\n"
    "🔔 Подпишитесь на свои сёла в «Уведомлениях»\n"
    "🛰 В дороге включите трансляцию геопозиции — предупрежу о ДПС <b>впереди</b>"
)

HELP = (
    "❓ <b>Справка</b>\n\n"
    "<b>✍️ Сообщить о событии</b>\n"
    "Пишите как удобно — я понимаю сленг, сокращения и опечатки:\n"
    "<blockquote>дпс на выезде из Маслянино в сторону Новосиба\n"
    "тренога на ул. Ленина\n"
    "ДТП на 45 км\n"
    "чисто у моста — это «отбой»</blockquote>\n"
    "Точнее всего — геопозиция: 📎 → Геопозиция, затем выбрать тип.\n\n"
    "<b>🗳 Голосование</b>\n"
    "✅ Стоят — продлевает · 👋 Уехали — закрывает · 🚫 Фейк — жалоба.\n"
    "За подтверждённые отчёты растёт репутация, и вас перестают проверять.\n\n"
    "<b>🔔 Уведомления</b>\n"
    "• по сёлам, на которые вы подписаны\n"
    "• в радиусе от вашей последней точки (3 часа)\n"
    "• 🛰 в дороге: 📎 → Геопозиция → <i>Транслировать</i> — предупрежу о том, что впереди\n\n"
    "<b>🔮 Прогноз</b> — где наряды обычно стоят в это время.\n\n"
    "<b>💬 В чате водителей</b> — добавьте меня в местный чат, я сам соберу отчёты из переписки.\n\n"
    "<b>Команды:</b> /start · /near · /feed · /map · /forecast · /settings · /me · /cancel"
)
