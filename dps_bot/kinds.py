"""Типы дорожных событий."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Kind:
    code: str
    emoji: str
    title: str
    ttl_min: int          # сколько живёт событие без подтверждений
    color: str            # цвет маркера folium
    hex: str              # цвет для PNG


KINDS: dict[str, Kind] = {
    "police":   Kind("police",   "🚔", "ДПС",               90,  "red",       "#d63031"),
    "radar":    Kind("radar",    "📡", "Радар / скорость",   60,  "purple",    "#8e44ad"),
    "camera":   Kind("camera",   "📷", "Камера / тренога",   240, "blue",      "#0984e3"),
    "raid":     Kind("raid",     "🚧", "Рейд / проверка",    120, "darkred",   "#8e0000"),
    "accident": Kind("accident", "💥", "ДТП",               90,  "orange",    "#e67e22"),
    "hazard":   Kind("hazard",   "⚠️", "Опасность на дороге", 240, "cadetblue", "#16a085"),
}

# Какие типы «закрываются» сообщениями вида «чисто / уехали»
CLEARABLE = {"police", "radar", "raid", "camera"}
# Какие типы считаются «полицейскими» для прогноза
ENFORCEMENT = {"police", "radar", "raid", "camera"}


def kind(code: str | None) -> Kind:
    return KINDS.get(code or "police", KINDS["police"])
