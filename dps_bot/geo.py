"""Определение координат события.

Порядок (от точного к грубому):
1. GPS из Telegram (обрабатывается в handlers — сюда не попадает);
2. ориентиры, которые админ отметил точкой (/setplace);
3. «память» — бот запоминает, где реально находятся ориентиры из текстов
   («мост|маслянино», «ул ленина|маслянино»), по отчётам с GPS, и
   использует медиану этих точек для текстовых отчётов;
4. OpenStreetMap Nominatim (улица / заведение + населённый пункт);
5. центр населённого пункта (низкая точность — честно помечается).
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass

import aiohttp

from .config import Config
from .db import Database
from .gazetteer import VILLAGES, Place, norm
from .nlp import Analysis
from .utils import haversine, now_db

log = logging.getLogger(__name__)

SETTLEMENT_TYPES = {"village", "town", "hamlet", "city", "isolated_dwelling", "locality", "suburb", "neighbourhood"}


@dataclass
class GeoResult:
    lat: float
    lon: float
    accuracy_m: int
    method: str   # gps | place | memory | geocoder | village
    label: str


class Geo:
    def __init__(self, db: Database, cfg: Config):
        self.db = db
        self.cfg = cfg
        self.places: list[Place] = [Place(**vars(p)) for p in VILLAGES]
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._session: aiohttp.ClientSession | None = None
        self.online = True

    # ------------------------------------------------------------ справочник
    async def load_places(self) -> None:
        by_key = {p.key: Place(**vars(p)) for p in VILLAGES}
        for row in await self.db.all("SELECT * FROM places"):
            aliases = [a for a in (row["aliases"] or "").split(";") if a]
            if row["key"] in by_key:
                p = by_key[row["key"]]
                p.lat, p.lon, p.verified = row["lat"], row["lon"], bool(row["verified"])
                p.aliases = list(dict.fromkeys(p.aliases + aliases))
            else:
                by_key[row["key"]] = Place(row["key"], row["name"], row["lat"], row["lon"],
                                           aliases, row["kind"] or "landmark", bool(row["verified"]))
        self.places = list(by_key.values())

    def villages(self) -> list[Place]:
        return sorted((p for p in self.places if p.kind == "village"), key=lambda p: p.name)

    def place_by_key(self, key: str) -> Place | None:
        return next((p for p in self.places if p.key == key), None)

    def nearest_village(self, lat: float, lon: float) -> tuple[Place, float]:
        best = min(self.villages(), key=lambda p: haversine(lat, lon, p.lat, p.lon))
        return best, haversine(lat, lon, best.lat, best.lon)

    def in_bounds(self, lat: float, lon: float) -> bool:
        (s, w), (n, e) = self.cfg.bounds
        return s <= lat <= n and w <= lon <= e

    def describe(self, lat: float, lon: float) -> str:
        p, km = self.nearest_village(lat, lon)
        if km < 1.5:
            return p.name
        return f"{km:.1f} км от {p.name}"

    async def save_place(self, name: str, lat: float, lon: float, kind: str,
                         aliases: list[str] | None = None, verified: int = 2) -> Place:
        key = norm(name)
        existing = await self.db.one("SELECT aliases FROM places WHERE key=?", (key,))
        al = set((existing["aliases"] or "").split(";")) if existing else set()
        al.update(norm(a) for a in (aliases or []))
        al.discard("")
        await self.db.run(
            """INSERT OR REPLACE INTO places(key, name, lat, lon, kind, aliases, verified, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, name, lat, lon, kind, ";".join(sorted(al)), verified, now_db()),
        )
        await self.load_places()
        return self.place_by_key(key)

    # ------------------------------------------------------------ Nominatim
    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self.cfg.nominatim_user_agent},
                timeout=aiohttp.ClientTimeout(total=12),
                trust_env=True,  # учитывать HTTPS_PROXY / SSL_CERT_FILE окружения
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def nominatim(self, query: str, settlement_only: bool = False) -> tuple[float, float] | None:
        q = query.lower().strip()
        cached = await self.db.one(
            "SELECT lat, lon FROM geocache WHERE query=? AND created_at > datetime('now', '-30 days')", (q,)
        )
        if cached:
            return (cached["lat"], cached["lon"]) if cached["lat"] is not None else None

        (s, w), (n, e) = self.cfg.bounds
        params = {
            "q": query, "format": "jsonv2", "limit": "3", "countrycodes": "ru",
            "accept-language": "ru", "viewbox": f"{w},{n},{e},{s}", "bounded": "1",
        }
        result = None
        try:
            async with self._lock:  # политика Nominatim: не чаще 1 запроса в секунду
                wait = 1.1 - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                session = await self._http()
                async with session.get(self.cfg.nominatim_url, params=params) as resp:
                    self._last_call = time.monotonic()
                    data = await resp.json(content_type=None) if resp.status == 200 else []
            for item in data or []:
                lat, lon = float(item["lat"]), float(item["lon"])
                if not self.in_bounds(lat, lon):
                    continue
                if settlement_only and item.get("addresstype", item.get("type")) not in SETTLEMENT_TYPES:
                    continue
                result = (lat, lon)
                break
            self.online = True
        except Exception as e:  # сеть недоступна — работаем на справочнике
            log.warning("Nominatim недоступен: %s", e)
            self.online = False
            return None
        await self.db.run(
            "INSERT OR REPLACE INTO geocache(query, lat, lon, created_at) VALUES (?, ?, ?, ?)",
            (q, result[0] if result else None, result[1] if result else None, now_db()),
        )
        return result

    async def refine_villages(self) -> int:
        """Уточнить координаты сёл через OSM (однократно, в фоне)."""
        updated = 0
        for p in [p for p in self.villages() if not p.verified]:
            found = await self.nominatim(f"{p.name}, {self.cfg.region_query}", settlement_only=True)
            if not self.online:
                break
            if found:
                await self.db.run(
                    """INSERT INTO places(key, name, lat, lon, kind, verified, updated_at)
                       VALUES (?, ?, ?, ?, 'village', 1, ?)
                       ON CONFLICT(key) DO UPDATE SET lat=excluded.lat, lon=excluded.lon,
                       verified=1, updated_at=excluded.updated_at WHERE places.verified < 2""",
                    (p.key, p.name, found[0], found[1], now_db()),
                )
                updated += 1
        if updated:
            await self.load_places()
            log.info("📍 Уточнены координаты %s населённых пунктов", updated)
        return updated

    # ------------------------------------------------------------ память
    async def remember(self, a: Analysis, lat: float, lon: float, user_id: int | None) -> None:
        for key in a.memory_keys():
            await self.db.run(
                "INSERT INTO geo_memory(phrase, lat, lon, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, lat, lon, user_id, now_db()),
            )

    async def _from_memory(self, a: Analysis) -> GeoResult | None:
        for key in a.memory_keys():
            rows = await self.db.all(
                "SELECT lat, lon, user_id FROM geo_memory WHERE phrase=? ORDER BY id DESC LIMIT 60", (key,)
            )
            if not rows:
                continue
            mlat = statistics.median(r["lat"] for r in rows)
            mlon = statistics.median(r["lon"] for r in rows)
            close = [r for r in rows if haversine(mlat, mlon, r["lat"], r["lon"]) < 1.5]
            users = {r["user_id"] for r in close}
            if len(close) >= 2 and len(users) >= 1:
                lat = statistics.median(r["lat"] for r in close)
                lon = statistics.median(r["lon"] for r in close)
                spread = max(haversine(lat, lon, r["lat"], r["lon"]) for r in close) * 1000
                acc = int(min(max(80, spread), 800))
                return GeoResult(lat, lon, acc, "memory", key.replace("|_", "").replace("|", ", "))
        return None

    # ------------------------------------------------------------ главное
    async def resolve(self, a: Analysis) -> GeoResult | None:
        place = a.place
        # 2. ориентир, отмеченный админом
        if place and place.kind != "village":
            return GeoResult(place.lat, place.lon, 100, "place", place.name)

        # 3. память
        mem = await self._from_memory(a)
        if mem:
            return mem

        village = place if place and place.kind == "village" else None
        anchor = village or self.place_by_key("маслянино")

        # 4. геокодер: улица или именованный ориентир
        queries = []
        if a.street:
            queries.append(f"улица {a.street}, {anchor.name}, {self.cfg.region_query}")
        for lm in a.landmarks:
            if lm not in ("поворот", "въезд", "выезд", "перекресток", "развилка", "спуск", "подъём"):
                if village or lm[0].isupper():
                    queries.append(f"{lm}, {anchor.name}, {self.cfg.region_query}")
        for q in queries[:2]:
            found = await self.nominatim(q)
            if found and haversine(found[0], found[1], anchor.lat, anchor.lon) < 6:
                label = f"{a.summary() or anchor.name}"
                return GeoResult(found[0], found[1], 250, "geocoder", label)

        # 5. центр населённого пункта
        if village:
            acc = 2500 if a.place_role == "turn" else 1500
            label = ("поворот на " if a.place_role == "turn" else "") + village.name
            return GeoResult(village.lat, village.lon, acc, "village", label)
        return None
