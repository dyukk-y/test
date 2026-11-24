"""Конфигурация. Все секреты — только из переменных окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv необязателен
    pass


def _ints(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


def _plans(value: str) -> list[tuple[int, int]]:
    """"7:50,30:150" -> [(7, 50), (30, 150)] — (дней, звёзд)."""
    plans = []
    for chunk in value.replace(" ", "").split(","):
        if ":" in chunk:
            days, stars = chunk.split(":", 1)
            plans.append((int(days), int(stars)))
    return plans


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_ids: list[int]
    data_dir: str = "data"
    timezone: str = "Asia/Novosibirsk"

    # Регион
    region_name: str = "Маслянинский район"
    region_query: str = "Маслянинский район, Новосибирская область, Россия"
    center: tuple[float, float] = (54.3437, 84.2134)
    # (юг, запад), (север, восток) — с запасом вокруг района
    bounds: tuple[tuple[float, float], tuple[float, float]] = ((54.00, 83.60), (54.70, 84.90))

    # Геокодер
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    nominatim_user_agent: str = "dps-guardian-bot/3.0"

    # Telethon (парсер каналов) — необязательно
    api_id: int | None = None
    api_hash: str | None = None

    # Премиум за Telegram Stars
    premium_enabled: bool = True
    premium_plans: list[tuple[int, int]] = field(default_factory=lambda: [(7, 50), (30, 150), (365, 990)])

    backup_chat_id: str | None = None
    webapp_url: str | None = None  # HTTPS-адрес мини-аппа (docs/index.html)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "dps.db")


def load_config() -> Config:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("❌ Не задан BOT_TOKEN. Скопируйте .env.example в .env и заполните.")
    owners = _ints(os.environ.get("OWNER_IDS", ""))
    if not owners:
        raise SystemExit("❌ Не задан OWNER_IDS (Telegram ID владельца бота).")

    api_id = os.environ.get("TG_API_ID", "").strip()
    kwargs = dict(
        bot_token=token,
        owner_ids=owners,
        data_dir=os.environ.get("DATA_DIR", "data"),
        timezone=os.environ.get("TIMEZONE", "Asia/Novosibirsk"),
        api_id=int(api_id) if api_id.isdigit() else None,
        api_hash=os.environ.get("TG_API_HASH", "").strip() or None,
        premium_enabled=os.environ.get("PREMIUM_ENABLED", "1").strip() not in ("0", "false", "no", ""),
        backup_chat_id=os.environ.get("BACKUP_CHAT_ID", "").strip() or None,
        webapp_url=os.environ.get("WEBAPP_URL", "").strip() or None,
        nominatim_user_agent=os.environ.get("NOMINATIM_USER_AGENT", "dps-guardian-bot/3.0"),
    )
    if os.environ.get("PREMIUM_PLANS"):
        kwargs["premium_plans"] = _plans(os.environ["PREMIUM_PLANS"])
    os.makedirs(kwargs["data_dir"], exist_ok=True)
    return Config(**kwargs)
