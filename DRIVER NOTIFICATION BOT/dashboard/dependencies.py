from __future__ import annotations

from fastapi import Request

from app.db import Database


async def get_db(request: Request) -> Database:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise RuntimeError("Database connection is not initialized")
    return db
