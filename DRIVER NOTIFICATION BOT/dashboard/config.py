from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv


@dataclass
class DashboardSettings:
    database_url: str
    basic_auth_user: str
    basic_auth_password: str
    title: str = "Driver Compliance Dashboard"


def _require_env(name: str, value: Optional[str]) -> str:
    if value:
        return value
    raise RuntimeError(f"{name} environment variable is required for the dashboard")


@lru_cache(maxsize=1)
def load_settings() -> DashboardSettings:
    load_dotenv()

    database_url = os.getenv("DATABASE_READONLY_URL")
    if not database_url:
        database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_READONLY_URL (or DATABASE_URL as fallback) is required for the dashboard"
        )

    basic_auth_user = _require_env("DASHBOARD_BASIC_USER", os.getenv("DASHBOARD_BASIC_USER"))
    basic_auth_password = _require_env("DASHBOARD_BASIC_PASSWORD", os.getenv("DASHBOARD_BASIC_PASSWORD"))

    title = os.getenv("DASHBOARD_TITLE", "Driver Compliance Dashboard")

    return DashboardSettings(
        database_url=database_url,
        basic_auth_user=basic_auth_user,
        basic_auth_password=basic_auth_password,
        title=title,
    )
