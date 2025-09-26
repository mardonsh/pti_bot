from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timezone
from typing import Callable
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

CHECKS_BEHAVIOR: dict[str, Callable[..., object]] = {}


def _ensure_checks_stub() -> None:
    if "app.services.checks" in sys.modules:
        return

    checks_module = ModuleType("app.services.checks")

    async def _list_active_drivers(db):
        func = CHECKS_BEHAVIOR.get("list_active_drivers")
        if func is None:
            return []
        result = func(db)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _list_recent_checkins(db, *, driver_id: int, days: int = 7):
        func = CHECKS_BEHAVIOR.get("list_recent_checkins")
        if func is None:
            return []
        result = func(db, driver_id=driver_id, days=days)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _find_driver_by_id(db, driver_id: int):
        func = CHECKS_BEHAVIOR.get("find_driver_by_id")
        if func is None:
            return None
        result = func(db, driver_id)
        if asyncio.iscoroutine(result):
            return await result
        return result

    checks_module.list_active_drivers = _list_active_drivers
    checks_module.list_recent_checkins = _list_recent_checkins
    checks_module.find_driver_by_id = _find_driver_by_id

    services_module = sys.modules.setdefault("app.services", ModuleType("app.services"))
    services_module.checks = checks_module
    sys.modules["app.services.checks"] = checks_module


_ensure_checks_stub()

os.environ.setdefault("DATABASE_READONLY_URL", "postgresql://stub:stub@db:5432/postgres")
os.environ.setdefault("DASHBOARD_BASIC_USER", "test-user")
os.environ.setdefault("DASHBOARD_BASIC_PASSWORD", "test-pass")
os.environ.setdefault("DASHBOARD_TITLE", "Test Dashboard")

from dashboard import config
from dashboard.main import create_app


class StubDatabase:
    async def connect(self) -> None:  # pragma: no cover - simple stub
        return None

    async def close(self) -> None:  # pragma: no cover - simple stub
        return None


@pytest.fixture(autouse=True)
def dashboard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_READONLY_URL", "postgresql://stub:stub@db:5432/postgres")
    monkeypatch.setenv("DASHBOARD_BASIC_USER", "test-user")
    monkeypatch.setenv("DASHBOARD_BASIC_PASSWORD", "test-pass")
    monkeypatch.setenv("DASHBOARD_TITLE", "Test Dashboard")
    config.load_settings.cache_clear()
    CHECKS_BEHAVIOR.clear()


def _build_client(monkeypatch: pytest.MonkeyPatch, patches: Callable[[pytest.MonkeyPatch], None]) -> TestClient:
    stub_db = StubDatabase()

    app = create_app(database_factory=lambda _settings: stub_db)
    patches(monkeypatch)

    return TestClient(app)


def test_summary_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    generated_at = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)

    async def fake_fetch_compliance_summary(_db):
        return {
            "total_drivers": 7,
            "pass_count": 5,
            "pending_count": 2,
            "last_reset_at": None,
            "generated_at": generated_at,
        }

    async def fake_pending(_db):
        return []

    def apply_patches(mp: pytest.MonkeyPatch) -> None:
        mp.setattr("dashboard.main.fetch_compliance_summary", fake_fetch_compliance_summary)
        mp.setattr("dashboard.main.fetch_pending_drivers", fake_pending)

    with _build_client(monkeypatch, apply_patches) as client:
        response = client.get("/api/compliance/summary", auth=("test-user", "test-pass"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_drivers"] == 7
    assert payload["pending_count"] == 2
    assert payload["generated_at"].replace("Z", "+00:00") == generated_at.isoformat()


def test_pending_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    pending_payload = [
        {
            "driver_id": 1,
            "username": "alpha",
            "full_name": "Alpha Tester",
            "notify_chat_id": -1001,
            "check_date": date(2024, 5, 1),
            "status": "pending",
            "pass_count_7d": 3,
            "last_notification": datetime(2024, 5, 1, 11, 30, tzinfo=timezone.utc),
        }
    ]

    async def fake_fetch_summary(_db):
        return {
            "total_drivers": 1,
            "pass_count": 0,
            "pending_count": 1,
            "last_reset_at": None,
            "generated_at": datetime.now(tz=timezone.utc),
        }

    async def fake_pending(_db):
        return pending_payload

    def apply_patches(mp: pytest.MonkeyPatch) -> None:
        mp.setattr("dashboard.main.fetch_compliance_summary", fake_fetch_summary)
        mp.setattr("dashboard.main.fetch_pending_drivers", fake_pending)

    with _build_client(monkeypatch, apply_patches) as client:
        response = client.get("/api/compliance/pending", auth=("test-user", "test-pass"))

    assert response.status_code == 200
    data = response.json()
    assert data[0]["driver_id"] == 1
    assert data[0]["pass_count_7d"] == 3


def test_driver_checkins_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = SimpleNamespace(
        id=42,
        username="driver42",
        display_name="Driver Forty Two",
    )

    checkin = SimpleNamespace(
        id=5,
        driver_id=42,
        group_id=200,
        date=date(2024, 5, 1),
        sent_at=datetime(2024, 5, 1, 10, 0, tzinfo=timezone.utc),
        responded_at=None,
        status="pending",
        reason=None,
        reviewed_at=None,
        media_count=0,
        updated_at=datetime(2024, 5, 1, 10, 5, tzinfo=timezone.utc),
    )

    async def fake_fetch_summary(_db):
        return {
            "total_drivers": 1,
            "pass_count": 0,
            "pending_count": 1,
            "last_reset_at": None,
            "generated_at": datetime.now(tz=timezone.utc),
        }

    async def fake_pending(_db):
        return []

    async def fake_checkins(_db, driver_id, days=7):
        return [checkin]

    async def fake_find_driver(_db, driver_id):
        return driver

    def apply_patches(mp: pytest.MonkeyPatch) -> None:
        CHECKS_BEHAVIOR["find_driver_by_id"] = fake_find_driver
        mp.setattr("dashboard.main.fetch_compliance_summary", fake_fetch_summary)
        mp.setattr("dashboard.main.fetch_pending_drivers", fake_pending)
        mp.setattr("dashboard.main.fetch_driver_checkins", fake_checkins)

    with _build_client(monkeypatch, apply_patches) as client:
        response = client.get("/api/drivers/42/checkins", auth=("test-user", "test-pass"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["driver_id"] == 42
    assert payload["checkins"][0]["id"] == 5
    assert payload["checkins"][0]["status"] == "pending"
