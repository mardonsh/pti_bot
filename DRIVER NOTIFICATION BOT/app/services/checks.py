from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
from typing import Iterable, List, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ForceReply, User

from app.db import Database
from app.keyboards import driver_dm_keyboard, review_keyboard


@dataclass(slots=True)
class Driver:
    id: int
    telegram_user_id: int
    username: Optional[str]
    display_name: Optional[str]
    active: bool
    streak_current: int
    streak_best: int
    notify_chat_id: Optional[int]
    last_pass_at: Optional[datetime]
    last_congrats_at: Optional[datetime]

    @property
    def mention(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.display_name or f"Driver {self.telegram_user_id}"


@dataclass(slots=True)
class Checkin:
    id: int
    driver_id: int
    group_id: int
    date: date
    sent_at: Optional[datetime]
    responded_at: Optional[datetime]
    status: str
    reason: Optional[str]
    reviewer_user_id: Optional[int]
    reviewed_at: Optional[datetime]
    review_message_id: Optional[int]
    media_count: int
    updated_at: datetime

    def is_terminal(self) -> bool:
        return self.status in {"pass", "fail", "needs_fix", "excused"}


@dataclass(slots=True)
class DailyStats:
    done: int
    pending: int
    excused: int
    fails: int
    total: int
    pending_usernames: List[str]
    top_streaks: Sequence[tuple[str, int]]


async def ensure_driver(db: Database, user: User) -> Driver:
    display_name = user.full_name
    return await ensure_driver_profile(
        db,
        telegram_user_id=user.id,
        username=user.username,
        display_name=display_name,
    )


async def ensure_driver_profile(
    db: Database, *, telegram_user_id: Optional[int], username: Optional[str], display_name: Optional[str]
) -> Driver:
    return await _upsert_driver(
        db,
        telegram_user_id=telegram_user_id,
        username=username,
        display_name=display_name,
    )


async def _upsert_driver(
    db: Database, *, telegram_user_id: Optional[int], username: Optional[str], display_name: Optional[str]
) -> Driver:
    if username:
        existing = await db.fetchrow(
            "SELECT * FROM drivers WHERE lower(username) = lower($1)", username
        )
    else:
        existing = None

    if existing:
        new_id = telegram_user_id or existing["telegram_user_id"]
        record = await db.fetchrow(
            """
            UPDATE drivers
            SET telegram_user_id = $2,
                username = $3,
                display_name = $4,
                active = true,
                updated_at = now()
            WHERE id = $1
            RETURNING *
            """,
            existing["id"],
            new_id,
            username,
            display_name,
        )
        return _record_to_driver(record)

    if telegram_user_id is None:
        if not username:
            raise ValueError("Username required when telegram_user_id is missing")
        telegram_user_id = _virtual_user_id(username)

    record = await db.fetchrow(
        """
        INSERT INTO drivers (telegram_user_id, username, display_name, active)
        VALUES ($1, $2, $3, true)
        ON CONFLICT (telegram_user_id)
        DO UPDATE SET username = EXCLUDED.username,
                      display_name = EXCLUDED.display_name,
                      active = true,
                      updated_at = now()
        RETURNING *
        """,
        telegram_user_id,
        username,
        display_name,
    )
    return _record_to_driver(record)


def _virtual_user_id(username: str) -> int:
    """Return a stable negative ID for placeholder drivers until they start the bot."""
    digest = hashlib.sha256(username.lower().encode()).hexdigest()
    value = int(digest[:15], 16)
    return -(10**12 + value % (10**6))


async def find_driver_by_notify_chat(db: Database, chat_id: int) -> Optional[Driver]:
    record = await db.fetchrow(
        "SELECT * FROM drivers WHERE notify_chat_id = $1", chat_id
    )
    if record:
        return _record_to_driver(record)
    return None


async def find_driver_by_telegram_id(db: Database, telegram_user_id: int) -> Optional[Driver]:
    record = await db.fetchrow(
        "SELECT * FROM drivers WHERE telegram_user_id = $1", telegram_user_id
    )
    if record:
        return _record_to_driver(record)
    return None


async def find_driver_by_username(db: Database, username: str) -> Optional[Driver]:
    record = await db.fetchrow(
        "SELECT * FROM drivers WHERE lower(username) = lower($1)", username
    )
    if record:
        return _record_to_driver(record)
    return None


async def find_driver_by_id(db: Database, driver_id: int) -> Optional[Driver]:
    record = await db.fetchrow("SELECT * FROM drivers WHERE id=$1", driver_id)
    if record:
        return _record_to_driver(record)
    return None


async def set_driver_notify_chat(db: Database, *, driver_id: int, chat_id: int) -> None:
    await db.execute(
        """
        UPDATE drivers
        SET notify_chat_id = $2,
            updated_at = now()
        WHERE id = $1
        """,
        driver_id,
        chat_id,
    )


async def ensure_checkin(
    db: Database, *, driver_id: int, group_id: int, check_date: date
) -> Checkin:
    record = await db.fetchrow(
        """
        INSERT INTO daily_checkins (driver_id, group_id, date)
        VALUES ($1, $2, $3)
        ON CONFLICT (driver_id, date)
        DO UPDATE SET group_id = EXCLUDED.group_id,
                      updated_at = now()
        RETURNING *
        """,
        driver_id,
        group_id,
        check_date,
    )
    return _record_to_checkin(record)


async def mark_notified(db: Database, checkin_id: int) -> Checkin:
    record = await db.fetchrow(
        """
        UPDATE daily_checkins
        SET sent_at = COALESCE(sent_at, now()),
            updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        checkin_id,
    )
    return _record_to_checkin(record)


async def reset_checkin(db: Database, checkin_id: int) -> Checkin:
    record = await db.fetchrow(
        "SELECT driver_id FROM daily_checkins WHERE id=$1",
        checkin_id,
    )
    if record is None:
        raise ValueError("Checkin not found")
    driver_id = record["driver_id"]

    await db.execute("DELETE FROM media WHERE checkin_id=$1", checkin_id)
    record = await db.fetchrow(
        """
        UPDATE daily_checkins
        SET media_count = 0,
            responded_at = NULL,
            status = 'pending',
            reason = NULL,
            reviewer_user_id = NULL,
            reviewed_at = NULL,
            sent_at = NULL,
            review_message_id = NULL,
            updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        checkin_id,
    )

    await db.execute(
        "UPDATE drivers SET last_pass_at = NULL, updated_at = now() WHERE id = $1",
        driver_id,
    )
    return _record_to_checkin(record)


async def set_offthread_warning(db: Database, checkin_id: int, active: bool) -> None:
    reason = 'offthread_warning' if active else None
    await db.execute(
        "UPDATE daily_checkins SET reason = $2, updated_at = now() WHERE id = $1",
        checkin_id,
        reason,
    )


async def record_media(
    db: Database,
    *,
    driver_id: int,
    group_id: int,
    check_date: date,
    kind: str,
    file_id: str,
    media_group_id: Optional[str],
) -> tuple[Checkin, bool]:
    async with db.transaction() as conn:
        checkin_record = await conn.fetchrow(
            """
            INSERT INTO daily_checkins (driver_id, group_id, date)
            VALUES ($1, $2, $3)
            ON CONFLICT (driver_id, date)
            DO UPDATE SET group_id = EXCLUDED.group_id,
                          updated_at = now()
            RETURNING *
            """,
            driver_id,
            group_id,
            check_date,
        )
        first_media = checkin_record["media_count"] == 0
        checkin_id = checkin_record["id"]
        await conn.execute(
            """
            INSERT INTO media (checkin_id, kind, file_id, media_group_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT DO NOTHING
            """,
            checkin_id,
            kind,
            file_id,
            media_group_id,
        )
        updated = await conn.fetchrow(
            """
            UPDATE daily_checkins
            SET media_count = media_count + 1,
                responded_at = COALESCE(responded_at, now()),
                status = CASE WHEN status IN ('pending', 'submitted') THEN 'submitted' ELSE status END,
                reason = NULL,
                updated_at = now()
            WHERE id = $1
            RETURNING *
            """,
            checkin_id,
        )
    return _record_to_checkin(updated), first_media




async def set_review_message(
    db: Database, *, checkin_id: int, message_id: int
) -> None:
    await db.execute(
        """
        UPDATE daily_checkins
        SET review_message_id = $2,
            updated_at = now()
        WHERE id = $1
        """,
        checkin_id,
        message_id,
    )

async def set_excused(
    db: Database,
    *,
    driver_id: int,
    group_id: int,
    check_date: date,
    reason: str,
) -> Checkin:
    record = await db.fetchrow(
        """
        UPDATE daily_checkins
        SET status = 'excused',
            reason = $4,
            reviewer_user_id = NULL,
            reviewed_at = now(),
            updated_at = now()
        WHERE driver_id = $1 AND group_id = $2 AND date = $3
        RETURNING *
        """,
        driver_id,
        group_id,
        check_date,
        reason,
    )
    if record is None:
        record = await db.fetchrow(
            """
            INSERT INTO daily_checkins (driver_id, group_id, date, status, reason, reviewed_at)
            VALUES ($1, $2, $3, 'excused', $4, now())
            RETURNING *
            """,
            driver_id,
            group_id,
            check_date,
            reason,
        )
    return _record_to_checkin(record)


async def update_review_status(
    db: Database,
    *,
    driver_id: int,
    group_id: int,
    check_date: date,
    status: str,
    reviewer_user_id: int,
    reason: Optional[str],
) -> Optional[Checkin]:
    record = await db.fetchrow(
        """
        UPDATE daily_checkins
        SET status = $4,
            reason = $5,
            reviewer_user_id = $6,
            reviewed_at = now(),
            updated_at = now()
        WHERE driver_id = $1 AND group_id = $2 AND date = $3
        RETURNING *
        """,
        driver_id,
        group_id,
        check_date,
        status,
        reason,
        reviewer_user_id,
    )
    if record:
        if status == "pass":
            await db.execute(
                "UPDATE drivers SET last_pass_at = now(), updated_at = now() WHERE id = $1",
                driver_id,
            )
        elif status in {"fail", "needs_fix"}:
            await db.execute(
                "UPDATE drivers SET last_pass_at = NULL, updated_at = now() WHERE id = $1",
                driver_id,
            )
        return _record_to_checkin(record)
    return None


async def reopen_checkin(
    db: Database,
    *,
    driver_id: int,
    group_id: int,
    check_date: date,
) -> Optional[Checkin]:
    record = await db.fetchrow(
        """
        UPDATE daily_checkins
        SET status = 'submitted',
            reason = NULL,
            reviewer_user_id = NULL,
            reviewed_at = NULL,
            updated_at = now()
        WHERE driver_id = $1 AND group_id = $2 AND date = $3
        RETURNING *
        """,
        driver_id,
        group_id,
        check_date,
    )
    if record:
        await db.execute(
            "UPDATE drivers SET last_pass_at = NULL, updated_at = now() WHERE id = $1",
            driver_id,
        )
        return _record_to_checkin(record)
    return None


async def fetch_checkin_by_id(db: Database, checkin_id: int) -> Optional[Checkin]:
    record = await db.fetchrow(
        "SELECT * FROM daily_checkins WHERE id=$1",
        checkin_id,
    )
    if record:
        return _record_to_checkin(record)
    return None


async def fetch_checkin(
    db: Database, *, driver_id: int, group_id: int, check_date: date
) -> Optional[Checkin]:
    record = await db.fetchrow(
        """
        SELECT * FROM daily_checkins
        WHERE driver_id = $1 AND group_id = $2 AND date = $3
        """,
        driver_id,
        group_id,
        check_date,
    )
    if record:
        return _record_to_checkin(record)
    return None


async def fetch_latest_checkin(
    db: Database, *, driver_id: int, group_id: int
) -> Optional[Checkin]:
    record = await db.fetchrow(
        """
        SELECT *
        FROM daily_checkins
        WHERE driver_id = $1 AND group_id = $2
        ORDER BY date DESC
        LIMIT 1
        """,
        driver_id,
        group_id,
    )
    if record:
        return _record_to_checkin(record)
    return None


async def list_active_drivers(db: Database) -> Sequence[Driver]:
    records = await db.fetch("SELECT * FROM drivers WHERE active = true")
    return [_record_to_driver(r) for r in records]


async def list_recent_checkins(
    db: Database, *, driver_id: int, days: int = 7
) -> Sequence[Checkin]:
    if days <= 0:
        return []
    since = date.today() - timedelta(days=days - 1)
    records = await db.fetch(
        """
        SELECT *
        FROM daily_checkins
        WHERE driver_id = $1 AND date >= $2
        ORDER BY date DESC
        """,
        driver_id,
        since,
    )
    return [_record_to_checkin(record) for record in records]


async def update_driver_profile(db: Database, driver: Driver) -> None:
    await db.execute(
        """
        UPDATE drivers
        SET username = $2,
            display_name = $3,
            active = $4,
            updated_at = now()
        WHERE id = $1
        """,
        driver.id,
        driver.username,
        driver.display_name,
        driver.active,
    )


async def fetch_daily_stats(
    db: Database, *, group_id: int, check_date: date
) -> DailyStats:
    record = await db.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'pass') AS done,
            COUNT(*) FILTER (WHERE status IN ('pending','submitted')) AS pending,
            COUNT(*) FILTER (WHERE status = 'excused') AS excused,
            COUNT(*) FILTER (WHERE status = 'fail') AS fails,
            COUNT(*) AS total
        FROM daily_checkins
        WHERE group_id = $1 AND date = $2
        """,
        group_id,
        check_date,
    )

    pending_records = await db.fetch(
        """
        SELECT d.username, d.display_name
        FROM daily_checkins dc
        JOIN drivers d ON dc.driver_id = d.id
        WHERE dc.group_id = $1 AND dc.date = $2 AND dc.status IN ('pending', 'submitted')
        ORDER BY d.username NULLS LAST, d.display_name
        """,
        group_id,
        check_date,
    )

    top_streaks_records = await db.fetch(
        """
        SELECT d.username, d.display_name, d.streak_current
        FROM drivers d
        WHERE d.active = true AND d.streak_current > 0
        ORDER BY d.streak_current DESC, d.streak_best DESC, d.username
        LIMIT 3
        """,
    )

    pending_names = [
        f"@{r['username']}" if r["username"] else (r["display_name"] or "Driver")
        for r in pending_records
    ]
    top_streaks = [
        (f"@{r['username']}" if r["username"] else (r["display_name"] or "Driver"), r["streak_current"])
        for r in top_streaks_records
    ]

    return DailyStats(
        done=record["done"] or 0,
        pending=record["pending"] or 0,
        excused=record["excused"] or 0,
        fails=record["fails"] or 0,
        total=record["total"] or 0,
        pending_usernames=pending_names,
        top_streaks=top_streaks,
    )


async def sync_review_card(
    bot: Bot,
    db: Database,
    *,
    group_id: int,
    thread_id: int,
    driver: Driver,
    checkin: Checkin,
) -> None:
    text = render_review_card(driver, checkin)
    notified = checkin.sent_at is not None
    markup = review_keyboard(
        driver_id=driver.id,
        date=checkin.date.isoformat(),
        notified=notified,
        terminal=checkin.is_terminal(),
    )

    if checkin.review_message_id:
        try:
            await bot.edit_message_text(
                chat_id=group_id,
                message_id=checkin.review_message_id,
                text=text,
                reply_markup=markup,
            )
        except TelegramBadRequest as exc:
            lowered = (exc.message or "").lower()
            if "not modified" in lowered:
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=group_id,
                        message_id=checkin.review_message_id,
                        reply_markup=markup,
                    )
                except TelegramBadRequest as sub_exc:
                    sub_lower = (sub_exc.message or "").lower()
                    if "not modified" not in sub_lower:
                        raise
                return
            if "message to edit not found" in lowered or "message can't be edited" in lowered:
                message = await bot.send_message(
                    chat_id=group_id,
                    text=text,
                    message_thread_id=thread_id,
                    reply_markup=markup,
                )
                await set_review_message(db, checkin_id=checkin.id, message_id=message.message_id)
                checkin.review_message_id = message.message_id
                return
            raise
        else:
            return

    message = await bot.send_message(
        chat_id=group_id,
        text=text,
        message_thread_id=thread_id,
        reply_markup=markup,
    )
    await set_review_message(db, checkin_id=checkin.id, message_id=message.message_id)
    checkin.review_message_id = message.message_id


def render_review_card(driver: Driver, checkin: Checkin) -> str:
    status_map = {
        "pending": "Pending",
        "submitted": "Submitted",
        "pass": "Pass",
        "fail": "Fail",
        "needs_fix": "Needs Fix",
        "excused": "Excused",
    }
    status_text = status_map.get(checkin.status, checkin.status)
    streak_text = f"Streak current/best: {driver.streak_current}/{driver.streak_best}"
    media_text = f"Media: {checkin.media_count}/3"
    reason_text = f"Reason: {checkin.reason}" if checkin.reason else ""
    return "\n".join(
        part
        for part in [
            f"Daily Check — {checkin.date.isoformat()}",
            f"Driver: {driver.mention}",
            media_text,
            streak_text,
            f"Status: {status_text}",
            reason_text,
        ]
        if part
    )


async def send_driver_notification(
    bot: Bot,
    *,
    driver: Driver,
    checkin: Checkin,
    check_date: date,
    chat_id: Optional[int] = None,
) -> int:
    target_chat = chat_id or driver.notify_chat_id or driver.telegram_user_id
    streak_line = f"Streak current/best: {driver.streak_current}/{driver.streak_best}"
    header = "<b>Daily Safety Check (required)</b>"
    intro = f"Date: {check_date:%Y-%m-%d}\n{streak_line}\n\n"
    body = (
        "Upload 3–4 photos or a short video covering:\n"
        "• Trailer tires (both sides)\n"
        "• Glad-hands + pigtail\n"
        "• Trailer ABS lamp (key ON)\n"
        "Optional: Extinguisher + 3 triangles\n\n"
        "Confirm button is optional—you can just send the media.\n"
        "If you cannot complete today, pick a skip reason."
    )
    mention = f"{driver.mention}\n\n" if target_chat != driver.telegram_user_id else ""
    text = f"{header}\n{mention}{intro}{body}"
    await bot.send_message(
        chat_id=target_chat,
        text=text,
        reply_markup=driver_dm_keyboard(checkin.id),
    )

    force_reply = ForceReply(selective=True, input_field_placeholder="Send Daily Check media…")
    prefix = f"{driver.mention} " if target_chat != driver.telegram_user_id else ""
    prompt = f"{prefix}Reply here with today's photos/videos."
    await bot.send_message(
        chat_id=target_chat,
        text=prompt,
        reply_markup=force_reply,
    )

    return target_chat


def _record_to_driver(record) -> Driver:
    return Driver(
        id=record["id"],
        telegram_user_id=record["telegram_user_id"],
        username=record["username"],
        display_name=record["display_name"],
        active=record["active"],
        streak_current=record["streak_current"],
        streak_best=record["streak_best"],
        notify_chat_id=record["notify_chat_id"],
        last_pass_at=record.get("last_pass_at"),
        last_congrats_at=record.get("last_congrats_at"),
    )


def _record_to_checkin(record) -> Checkin:
    return Checkin(
        id=record["id"],
        driver_id=record["driver_id"],
        group_id=record["group_id"],
        date=record["date"],
        sent_at=record["sent_at"],
        responded_at=record["responded_at"],
        status=record["status"],
        reason=record["reason"],
        reviewer_user_id=record["reviewer_user_id"],
        reviewed_at=record["reviewed_at"],
        review_message_id=record["review_message_id"],
        media_count=record["media_count"],
        updated_at=record["updated_at"],
    )
