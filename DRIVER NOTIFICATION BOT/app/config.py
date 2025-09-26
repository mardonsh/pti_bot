from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


DEFAULT_DIGEST_TIME = time(hour=10, minute=30)


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_time(value: Optional[str], default: time) -> time:
    if not value:
        return default
    try:
        dt = datetime.strptime(value.strip(), "%H:%M")
    except ValueError as exc:
        raise ValueError("Invalid time format. Expected HH:MM") from exc
    return time(hour=dt.hour, minute=dt.minute)


def _parse_timezone(value: Optional[str]) -> ZoneInfo:
    tz_name = (value or "UTC").strip()
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError(f"Unknown timezone: {tz_name}") from exc


@dataclass(slots=True)
class Settings:
    bot_token: str
    database_url: str
    admin_only_review: bool
    tz: ZoneInfo
    tz_name: str
    digest_time: time

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        bot_token = os.getenv("BOT_TOKEN")
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required")

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")

        tz_raw = os.getenv("TZ", "America/Chicago")
        tz = _parse_timezone(tz_raw)
        digest_time = _parse_time(os.getenv("DIGEST_TIME"), DEFAULT_DIGEST_TIME)

        return cls(
            bot_token=bot_token,
            database_url=database_url,
            admin_only_review=_parse_bool(os.getenv("ADMIN_ONLY_REVIEW"), default=False),
            tz=tz,
            tz_name=tz_raw,
            digest_time=digest_time,
        )
