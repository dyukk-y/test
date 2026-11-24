"""Асинхронная работа с SQLite (aiosqlite, WAL)."""
from __future__ import annotations

import os
from typing import Any, Iterable

import aiosqlite

from .utils import now_db

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    full_name      TEXT,
    created_at     TEXT NOT NULL,
    last_seen      TEXT,
    lat            REAL,
    lon            REAL,
    heading        REAL,
    loc_time       TEXT,
    live_until     TEXT,
    alerts         INTEGER DEFAULT 1,
    radius_km      REAL DEFAULT 2,
    alert_kinds    TEXT DEFAULT 'police,radar,camera,raid,accident,hazard',
    quiet_from     INTEGER,
    quiet_to       INTEGER,
    premium_until  TEXT,
    reputation     REAL DEFAULT 0,
    reports        INTEGER DEFAULT 0,
    reports_ok     INTEGER DEFAULT 0,
    reports_fake   INTEGER DEFAULT 0,
    votes          INTEGER DEFAULT 0,
    trusted        INTEGER DEFAULT 0,
    banned         INTEGER DEFAULT 0,
    ban_reason     TEXT,
    blocked_bot    INTEGER DEFAULT 0,
    premium_warned INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admins (
    user_id  INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',  -- pending | active | closed | rejected | fake
    lat         REAL,
    lon         REAL,
    accuracy_m  INTEGER,
    geo_method  TEXT,                            -- gps | memory | place | geocoder | village | none
    place       TEXT,
    direction   TEXT,
    plate       TEXT,
    text        TEXT,
    photo_id    TEXT,
    source      TEXT NOT NULL DEFAULT 'user',    -- user | channel | group | admin
    source_ref  TEXT,
    author_id   INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    closed_at   TEXT,
    confirms    INTEGER DEFAULT 0,
    denies      INTEGER DEFAULT 0,
    gone        INTEGER DEFAULT 0,
    reports     INTEGER DEFAULT 1,
    text_hash   TEXT,
    scored      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_hash ON events(text_hash);

CREATE TABLE IF NOT EXISTS votes (
    event_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    value      TEXT NOT NULL,                    -- here | gone | fake
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    event_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS zone_subs (
    user_id   INTEGER NOT NULL,
    place_key TEXT NOT NULL,
    PRIMARY KEY (user_id, place_key)
);

CREATE TABLE IF NOT EXISTS places (
    key        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    kind       TEXT DEFAULT 'village',          -- village | landmark
    aliases    TEXT DEFAULT '',
    verified   INTEGER DEFAULT 0,               -- 0 приблизительно, 1 геокодер, 2 задано админом
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS geo_memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase     TEXT NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    user_id    INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_geo_memory ON geo_memory(phrase);

CREATE TABLE IF NOT EXISTS geocache (
    query      TEXT PRIMARY KEY,
    lat        REAL,
    lon        REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    link        TEXT UNIQUE NOT NULL,
    title       TEXT,
    chat_id     INTEGER,
    is_active   INTEGER DEFAULT 1,
    last_msg_id INTEGER DEFAULT 0,
    added_by    INTEGER,
    added_at    TEXT,
    last_parse  TEXT,
    found       INTEGER DEFAULT 0,
    last_error  TEXT
);

CREATE TABLE IF NOT EXISTS parser_accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone      TEXT UNIQUE NOT NULL,
    session    TEXT NOT NULL,
    status     TEXT DEFAULT 'new',               -- new | active | disabled | error
    added_by   INTEGER,
    added_at   TEXT,
    last_used  TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    days        INTEGER NOT NULL,
    charge_id   TEXT UNIQUE,
    status      TEXT DEFAULT 'paid',             -- paid | refunded | gift
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    enabled   INTEGER DEFAULT 1,
    added_at  TEXT,
    found     INTEGER DEFAULT 0
);
"""

DEFAULT_SETTINGS = {
    "moderation": "new",        # off | new | all
    "ttl_factor": "1.0",
    "max_reports_hour": "6",
    "trust_threshold": "5",
    "group_parsing": "1",
    "channel_min_conf": "0.7",
    "free_zones": "2",
}


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._settings: dict[str, str] = {}

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            await self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        await self.conn.commit()
        async with self.conn.execute("SELECT key, value FROM settings") as cur:
            self._settings = {r["key"]: r["value"] for r in await cur.fetchall()}

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ----------------------------------------------------------- низкий уровень
    async def one(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def all(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def val(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = await self.one(sql, params)
        return row[0] if row and row[0] is not None else default

    async def run(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Выполнить запрос с коммитом. Возвращает число затронутых строк."""
        async with self.conn.execute(sql, tuple(params)) as cur:
            result = cur.rowcount
        await self.conn.commit()
        return result

    async def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        """INSERT с коммитом. Возвращает id новой строки."""
        async with self.conn.execute(sql, tuple(params)) as cur:
            result = cur.lastrowid
        await self.conn.commit()
        return result

    async def backup(self, target_path: str) -> None:
        async with aiosqlite.connect(target_path) as dst:
            await self.conn.backup(dst)

    # ----------------------------------------------------------- настройки
    def setting(self, key: str, default: str | None = None) -> str | None:
        return self._settings.get(key, DEFAULT_SETTINGS.get(key, default))

    def setting_float(self, key: str) -> float:
        try:
            return float(self.setting(key) or 0)
        except ValueError:
            return float(DEFAULT_SETTINGS.get(key, 0))

    async def set_setting(self, key: str, value: str) -> None:
        self._settings[key] = value
        await self.run("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))

    # ----------------------------------------------------------- пользователи
    async def upsert_user(self, user_id: int, username: str | None, full_name: str | None) -> aiosqlite.Row:
        now = now_db()
        await self.conn.execute(
            """INSERT INTO users(user_id, username, full_name, created_at, last_seen) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
               full_name=excluded.full_name, last_seen=excluded.last_seen, blocked_bot=0""",
            (user_id, username, full_name, now, now),
        )
        await self.conn.commit()
        return await self.get_user(user_id)

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        return await self.one("SELECT * FROM users WHERE user_id=?", (user_id,))

    async def is_admin(self, user_id: int, owners: list[int]) -> bool:
        if user_id in owners:
            return True
        return await self.one("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) is not None

    async def is_premium(self, user_id: int) -> bool:
        return await self.one(
            "SELECT 1 FROM users WHERE user_id=? AND premium_until > ?", (user_id, now_db())
        ) is not None

    async def set_location(self, user_id: int, lat: float, lon: float,
                           heading: float | None = None, live_until: str | None = None) -> None:
        if live_until is not None:
            await self.run(
                "UPDATE users SET lat=?, lon=?, heading=?, loc_time=?, live_until=? WHERE user_id=?",
                (lat, lon, heading, now_db(), live_until, user_id),
            )
        else:
            await self.run(
                "UPDATE users SET lat=?, lon=?, heading=?, loc_time=? WHERE user_id=?",
                (lat, lon, heading, now_db(), user_id),
            )
