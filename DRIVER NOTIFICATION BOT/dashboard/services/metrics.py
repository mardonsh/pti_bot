from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db import Database
from app.services import checks


async def fetch_compliance_summary(db: Database) -> Dict[str, Any]:
    drivers = await checks.list_active_drivers(db)
    totals = await db.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'pass') AS pass_count,
            COUNT(*) FILTER (WHERE status IN ('pending', 'submitted')) AS pending_count
        FROM daily_checkins
        WHERE date = CURRENT_DATE
        """,
    )

    pass_count = int(totals["pass_count"] or 0) if totals else 0
    pending_count = int(totals["pending_count"] or 0) if totals else 0

    last_reset_row = await db.fetchrow(
        """
        SELECT performed_at
        FROM compliance_resets
        ORDER BY performed_at DESC
        LIMIT 1
        """,
    )
    last_reset_at: Optional[datetime] = None
    if last_reset_row:
        last_reset_at = last_reset_row["performed_at"]

    return {
        "total_drivers": len(drivers),
        "pass_count": pass_count,
        "pending_count": pending_count,
        "last_reset_at": last_reset_at,
        "generated_at": datetime.now(tz=timezone.utc),
    }


async def fetch_pending_drivers(db: Database) -> List[Dict[str, Any]]:
    records = await db.fetch(
        """
        WITH latest AS (
            SELECT DISTINCT ON (dc.driver_id)
                dc.driver_id,
                dc.date,
                dc.status,
                dc.sent_at
            FROM daily_checkins dc
            ORDER BY dc.driver_id, dc.date DESC
        ),
        pass_counts AS (
            SELECT driver_id, COUNT(*) AS pass_count
            FROM daily_checkins
            WHERE status = 'pass' AND date >= CURRENT_DATE - INTERVAL '6 days'
            GROUP BY driver_id
        )
        SELECT
            d.id AS driver_id,
            d.username,
            d.display_name,
            d.notify_chat_id,
            latest.date,
            latest.status,
            latest.sent_at,
            COALESCE(pass_counts.pass_count, 0) AS pass_count
        FROM latest
        JOIN drivers d ON d.id = latest.driver_id
        LEFT JOIN pass_counts ON pass_counts.driver_id = d.id
        WHERE latest.status IN ('pending', 'submitted') AND d.active = true
        ORDER BY latest.date ASC, d.username NULLS LAST, d.display_name
        """,
    )

    return [
        {
            "driver_id": record["driver_id"],
            "username": record["username"],
            "full_name": record["display_name"],
            "notify_chat_id": record["notify_chat_id"],
            "check_date": record["date"],
            "status": record["status"],
            "pass_count_7d": int(record["pass_count"] or 0),
            "last_notification": record["sent_at"],
        }
        for record in records
    ]


async def fetch_driver_checkins(db: Database, driver_id: int, days: int = 7) -> List[checks.Checkin]:
    return list(await checks.list_recent_checkins(db, driver_id=driver_id, days=days))
