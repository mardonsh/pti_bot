from __future__ import annotations

from pathlib import Path
from typing import Annotated, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasicCredentials

from app.db import Database
from app.services import checks

from .auth import require_basic_auth
from .config import DashboardSettings, load_settings
from .dependencies import get_db
from .models import (
    ComplianceSummaryModel,
    DriverCheckinModel,
    DriverCheckinsResponse,
    PendingDriverModel,
)
from .services import (
    fetch_compliance_summary,
    fetch_driver_checkins,
    fetch_pending_drivers,
)


TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


DatabaseFactory = Callable[[DashboardSettings], Database]


def create_app(database_factory: DatabaseFactory | None = None) -> FastAPI:
    settings = load_settings()
    db_factory = database_factory or (lambda cfg: Database(cfg.database_url))

    app = FastAPI(
        title=settings.title,
        version="0.1.0",
        description="Lightweight dashboard exposing driver compliance metrics.",
    )

    @app.on_event("startup")
    async def startup() -> None:
        settings = load_settings()
        database = db_factory(settings)
        await database.connect()
        app.state.db = database
        app.state.settings = settings

    @app.on_event("shutdown")
    async def shutdown() -> None:
        db: Database | None = getattr(app.state, "db", None)
        if db is not None:
            await db.close()

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_home(
        request: Request,
        _: Annotated[HTTPBasicCredentials, Depends(require_basic_auth)],
        db: Annotated[Database, Depends(get_db)],
    ) -> HTMLResponse:
        summary_data = await fetch_compliance_summary(db)
        pending_data = await fetch_pending_drivers(db)

        summary = ComplianceSummaryModel(**summary_data)
        pending = [PendingDriverModel(**item) for item in pending_data]

        settings = load_settings()
        return TEMPLATES.TemplateResponse(
            "index.html",
            {
                "request": request,
                "summary": summary,
                "pending": pending,
                "settings": settings,
            },
        )

    @app.get(
        "/api/compliance/summary",
        response_model=ComplianceSummaryModel,
    )
    async def api_compliance_summary(
        _: Annotated[HTTPBasicCredentials, Depends(require_basic_auth)],
        db: Annotated[Database, Depends(get_db)],
    ) -> ComplianceSummaryModel:
        data = await fetch_compliance_summary(db)
        return ComplianceSummaryModel(**data)

    @app.get(
        "/api/compliance/pending",
        response_model=list[PendingDriverModel],
    )
    async def api_compliance_pending(
        _: Annotated[HTTPBasicCredentials, Depends(require_basic_auth)],
        db: Annotated[Database, Depends(get_db)],
    ) -> list[PendingDriverModel]:
        data = await fetch_pending_drivers(db)
        return [PendingDriverModel(**item) for item in data]

    @app.get(
        "/api/drivers/{driver_id}/checkins",
        response_model=DriverCheckinsResponse,
    )
    async def api_driver_checkins(
        driver_id: int,
        _: Annotated[HTTPBasicCredentials, Depends(require_basic_auth)],
        db: Annotated[Database, Depends(get_db)],
    ) -> DriverCheckinsResponse:
        driver = await checks.find_driver_by_id(db, driver_id)
        if driver is None:
            raise HTTPException(status_code=404, detail="Driver not found")

        checkins = await fetch_driver_checkins(db, driver_id=driver_id, days=7)
        items = [
            DriverCheckinModel(
                id=checkin.id,
                date=checkin.date,
                status=checkin.status,
                sent_at=checkin.sent_at,
                responded_at=checkin.responded_at,
                reviewed_at=checkin.reviewed_at,
                reason=checkin.reason,
                group_id=checkin.group_id,
                updated_at=checkin.updated_at,
            )
            for checkin in checkins
        ]

        return DriverCheckinsResponse(
            driver_id=driver.id,
            username=driver.username,
            full_name=driver.display_name,
            checkins=items,
        )

    return app


app = create_app()
