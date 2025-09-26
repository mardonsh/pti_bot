from __future__ import annotations

from datetime import date

from app.db import Database


async def update_after_pass(db: Database, driver_id: int, check_date: date) -> None:
    await db.execute(
        """
        UPDATE drivers
        SET streak_current = streak_current + 1,
            streak_best = GREATEST(streak_best, streak_current + 1),
            last_check_date = $2,
            updated_at = now()
        WHERE id = $1
        """,
        driver_id,
        check_date,
    )


async def reset_missed_checks(db: Database, group_id: int, check_date: date) -> None:
    await db.execute(
        """
        UPDATE drivers AS d
        SET streak_current = 0,
            updated_at = now()
        WHERE d.active = true
          AND NOT EXISTS (
            SELECT 1 FROM daily_checkins AS dc
            WHERE dc.driver_id = d.id
              AND dc.group_id = $1
              AND dc.date = $2
          )
        """,
        group_id,
        check_date,
    )
