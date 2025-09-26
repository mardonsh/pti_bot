from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from aiogram import Bot
from app.db import Database
from app.keyboards import compliance_keyboard
from app.services import checks, roles
from app.services.checks import Checkin


logger = logging.getLogger(__name__)

REPORT_WINDOW = timedelta(hours=24)
PAUSE_TOKENS = {"inactive", "home", "home time"}
EXCEPTION_KEYWORDS = {
    "trailer not ready",
    "dropped",
    "drop yard",
    "at shop",
    "shop",
    "in shop",
    "no trailer",
    "waiting on trailer",
}
FLEET_MENTION = "@FleetOnDuty"


@dataclass(slots=True)
class DriverChatInfo:
    chat_id: int
    title: str
    link: Optional[str]


@dataclass(slots=True)
class ComplianceState:
    driver_id: int
    consecutive_reports: int
    last_report_at: Optional[datetime]
    last_driver_alert_at: Optional[datetime]
    last_dispatch_alert_at: Optional[datetime]
    last_status: Optional[str]
    last_comment_thread_id: Optional[int]


@dataclass(slots=True)
class ComplianceEntry:
    driver: checks.Driver
    checkin: Optional[checks.Checkin]
    status: str  # compliant, non_compliant, exception
    reason: Optional[str]
    target_date: date


async def send_hourly_report(
    *, bot: Bot, db: Database, group: roles.GroupSettings
) -> None:
    if group.compliance_topic_id is None:
        return

    timezone = ZoneInfo(group.tz)
    now = datetime.now(tz=timezone)

    drivers = await checks.list_active_drivers(db)
    latest_by_driver = await _fetch_latest_checkins(db, group.id)

    entries: List[ComplianceEntry] = []
    for driver in drivers:
        checkin = latest_by_driver.get(driver.id)
        status, reason, target_date = _evaluate_driver(driver, checkin, now)
        entries.append(ComplianceEntry(driver=driver, checkin=checkin, status=status, reason=reason, target_date=target_date))

    # Determine chat info for non-compliant drivers
    pending_entries = [entry for entry in entries if entry.status == "non_compliant"]
    chat_map = await _fetch_chat_info(bot, pending_entries)

    # Reclassify based on chat title pause tokens
    for entry in list(pending_entries):
        chat_info = chat_map.get(entry.driver.notify_chat_id) if entry.driver.notify_chat_id else None
        if chat_info and _is_paused_chat(chat_info.title):
            entry.status = "exception"
            entry.reason = "Chat inactive"
            pending_entries.remove(entry)

    compliant_total = sum(1 for e in entries if e.status == "compliant")
    exception_entries = [e for e in entries if e.status == "exception"]
    effective_total = max(len(drivers) - len(exception_entries), 0)
    pending_total = len(pending_entries)
    compliant_count = max(effective_total - pending_total, 0)

    # Update tracking and gather alert instructions
    alert_driver: List[ComplianceEntry] = []
    alert_dispatch: List[ComplianceEntry] = []

    for entry in entries:
        state = await _upsert_state(db, entry.driver.id, entry.status, now)
        if entry.status == "non_compliant":
            if entry.driver.notify_chat_id and _should_alert_driver(state, now):
                alert_driver.append(entry)
                await _mark_driver_alert(db, entry.driver.id, now)
            if _should_alert_dispatch(state, now):
                alert_dispatch.append(entry)
                await _mark_dispatch_alert(db, entry.driver.id, now)
        else:
            if state.consecutive_reports != 0:
                await _reset_state(db, entry.driver.id, now, entry.status)

    summary_lines = [
        "📊 PTI Compliance Report (Last 24h)",
        f"✅ {compliant_count}/{effective_total} drivers sent PTI photos.",
        f"❌ {pending_total} drivers pending",
    ]

    if exception_entries:
        summary_lines.append(f"🛠️ Exceptions: {len(exception_entries)}")

    await bot.send_message(
        chat_id=group.id,
        message_thread_id=group.compliance_topic_id,
        text="\n".join(summary_lines),
        disable_notification=True,
    )

    notes_map = await _fetch_latest_notes(db, [entry.driver.id for entry in pending_entries])

    for entry in pending_entries:
        chat_info = chat_map.get(entry.driver.notify_chat_id) if entry.driver.notify_chat_id else None
        chat_label = _format_chat_label(chat_info)
        mention = entry.driver.mention
        note_text = notes_map.get(entry.driver.id)
        note_line = f"\nNote: {html.escape(note_text)}" if note_text else ""
        detail_text = (
            f"🚨 Pending PTI\n"
            f"Driver: {mention}\n"
            f"Chat: {chat_label}\n"
            f"Since: {entry.target_date:%Y-%m-%d}{note_line}"
        )
        await bot.send_message(
            chat_id=group.id,
            message_thread_id=group.compliance_topic_id,
            text=detail_text,
            reply_markup=compliance_keyboard(entry.driver.id, entry.target_date.isoformat()),
            disable_notification=True,
        )

    # Driver reminders
    for entry in alert_driver:
        chat_id = entry.driver.notify_chat_id
        if not chat_id:
            continue
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "🚨 <b>PTI photos still missing.</b>\n"
                    "Please send now to avoid DOT violation & chargeback."
                ),
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to send driver reminder for %s", entry.driver.id)

    # Dispatch escalations
    for entry in alert_dispatch:
        try:
            await bot.send_message(
                chat_id=group.id,
                message_thread_id=group.compliance_topic_id,
                text=(
                    f"⚠️ 3 hours overdue – {FLEET_MENTION} please call {entry.driver.mention}."
                ),
                disable_notification=False,
            )
        except Exception:  # pragma: no cover
            logger.exception("Failed to post escalation for %s", entry.driver.id)


async def send_daily_snapshot(
    *, bot: Bot, db: Database, group: roles.GroupSettings, target_date: date
) -> None:
    if group.compliance_topic_id is None:
        return

    drivers = await checks.list_active_drivers(db)
    total_drivers = len(drivers)

    stats = await db.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'pass') AS passed,
            COUNT(*) FILTER (WHERE status = 'fail') AS failed,
            COUNT(*) FILTER (WHERE status IN ('pending','submitted')) AS pending,
            COUNT(*) FILTER (WHERE status = 'needs_fix') AS needs_fix,
            COUNT(*) FILTER (WHERE status = 'excused') AS excused
        FROM daily_checkins
        WHERE group_id = $1 AND date = $2
        """,
        group.id,
        target_date,
    )

    passed = stats["passed"] if stats else 0
    missed = max(total_drivers - passed, 0)

    best_rows, worst_rows = await _weekly_rankings(db, group.id, target_date, limit=3)

    lines = [
        f"📅 Daily PTI Compliance ({target_date:%b %d})",
        f"✅ {passed}/{total_drivers} drivers completed PTI yesterday.",
        f"❌ {missed} missed",
    ]

    if best_rows:
        best_text = ", ".join(f"{row['label']} ({row['pct']}%)" for row in best_rows)
        lines.append(f"Top compliant drivers: {best_text}.")
    if worst_rows:
        worst_text = ", ".join(f"{row['label']} ({row['pct']}%)" for row in worst_rows)
        lines.append(f"Worst compliance: {worst_text}.")

    message_text = "\n".join(lines)

    await bot.send_message(
        chat_id=group.id,
        message_thread_id=group.compliance_topic_id,
        text=message_text,
        disable_notification=True,
    )


async def send_weekly_leaderboard(
    *, bot: Bot, db: Database, group: roles.GroupSettings, end_date: date
) -> None:
    if group.compliance_topic_id is None:
        return

    top_rows, worst_rows = await _weekly_rankings(db, group.id, end_date, limit=10)
    if not top_rows and not worst_rows:
        return

    lines = ["🏆 PTI Compliance Leaderboard"]
    if top_rows:
        lines.append("TOP 10")
        for idx, row in enumerate(top_rows, start=1):
            lines.append(f"{idx}. {row['label']} – {row['pct']}%")
    if worst_rows:
        lines.append("Worst TOP 10:")
        for idx, row in enumerate(worst_rows, start=1):
            lines.append(f"{idx}. {row['label']} – {row['pct']}%")

    message_text = "\n".join(lines)

    await bot.send_message(
        chat_id=group.id,
        message_thread_id=group.compliance_topic_id,
        text=message_text,
        disable_notification=True,
    )


async def handle_pass_event(
    *, bot: Bot, db: Database, group: roles.GroupSettings, driver: checks.Driver, reviewed_at: datetime
) -> None:
    timezone = ZoneInfo(group.tz)
    event_time = reviewed_at.astimezone(timezone)
    await _upsert_state(db, driver.id, "compliant", event_time)
    await _reset_state(db, driver.id, event_time, "compliant")

    week_start = event_time.date() - timedelta(days=event_time.weekday())
    week_end = week_start + timedelta(days=6)

    count_row = await db.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'pass') AS passes
        FROM daily_checkins
        WHERE driver_id = $1 AND group_id = $2 AND date BETWEEN $3 AND $4
        """,
        driver.id,
        group.id,
        week_start,
        week_end,
    )
    passes = count_row["passes"] if count_row else 0

    if passes >= 5:
        if not driver.last_congrats_at or driver.last_congrats_at.date() < week_start:
            chat_id = driver.notify_chat_id or driver.telegram_user_id
            text = (
                "🎉 Great job keeping compliant 7/7 days this week!\n"
                "You’ve submitted more than four PTI passes – keep it up!"
            )
            try:
                await bot.send_message(chat_id=chat_id, text=text)
            except Exception:  # pragma: no cover
                logger.exception("Failed to send congrats to %s", driver.id)
            else:
                await db.execute(
                    "UPDATE drivers SET last_congrats_at = $2, updated_at = now() WHERE id = $1",
                    driver.id,
                    event_time,
                )


async def record_comment(
    db: Database, *, driver_id: int, author_id: int, note: str
) -> None:
    await db.execute(
        "INSERT INTO compliance_notes (driver_id, author_id, note) VALUES ($1, $2, $3)",
        driver_id,
        author_id,
        note,
    )


async def store_comment_prompt(
    db: Database, *, driver_id: int, message_id: int
) -> None:
    await db.execute(
        """
        INSERT INTO compliance_tracking (driver_id, last_comment_thread_id)
        VALUES ($1, $2)
        ON CONFLICT (driver_id)
        DO UPDATE SET last_comment_thread_id = EXCLUDED.last_comment_thread_id, updated_at = now()
        """,
        driver_id,
        message_id,
    )


async def resolve_comment_prompt(db: Database, *, message_id: int) -> Optional[int]:
    record = await db.fetchrow(
        "SELECT driver_id FROM compliance_tracking WHERE last_comment_thread_id = $1",
        message_id,
    )
    if record:
        await db.execute(
            "UPDATE compliance_tracking SET last_comment_thread_id = NULL, updated_at = now() WHERE driver_id = $1",
            record["driver_id"],
        )
        return record["driver_id"]
    return None


async def clear_tracking(db: Database) -> None:
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO compliance_resets DEFAULT VALUES")
        await conn.execute("DELETE FROM compliance_tracking")


def _evaluate_driver(
    driver: checks.Driver,
    checkin: Optional[checks.Checkin],
    now: datetime,
) -> tuple[str, Optional[str], date]:
    if checkin:
        reason_text = (checkin.reason or "").lower()
        if reason_text and any(keyword in reason_text for keyword in EXCEPTION_KEYWORDS):
            return "exception", checkin.reason, checkin.date
        if checkin.status == "pass" and checkin.reviewed_at and now - checkin.reviewed_at <= REPORT_WINDOW:
            return "compliant", None, checkin.date
        if checkin.status == "excused":
            return "exception", checkin.reason, checkin.date
        if checkin.status == "needs_fix":
            return "exception", "Needs fix", checkin.date
        if checkin.status == "fail":
            return "non_compliant", checkin.reason, checkin.date
        if checkin.status in {"pending", "submitted"} and driver.last_pass_at and now - driver.last_pass_at <= REPORT_WINDOW:
            return "compliant", None, checkin.date
        return "non_compliant", checkin.reason, checkin.date

    if driver.last_pass_at and now - driver.last_pass_at <= REPORT_WINDOW:
        return "compliant", None, now.date()

    return "non_compliant", None, now.date()


async def _fetch_latest_checkins(db: Database, group_id: int) -> Dict[int, Checkin]:
    records = await db.fetch(
        """
        SELECT DISTINCT ON (driver_id) *
        FROM daily_checkins
        WHERE group_id = $1
        ORDER BY driver_id, date DESC
        """,
        group_id,
    )
    return {record["driver_id"]: _checkin_from_record(record) for record in records}


def _checkin_from_record(record) -> Checkin:
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


async def _fetch_chat_info(
    bot: Bot, entries: Sequence[ComplianceEntry]
) -> Dict[Optional[int], DriverChatInfo]:
    result: Dict[Optional[int], DriverChatInfo] = {}
    seen: set[int] = set()
    for entry in entries:
        chat_id = entry.driver.notify_chat_id
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        try:
            chat = await bot.get_chat(chat_id)
        except Exception:  # pragma: no cover
            logger.exception("Failed to fetch chat %s", chat_id)
            continue
        title = chat.title or chat.full_name or f"Chat {chat.id}"
        link = None
        if chat.username:
            link = f"https://t.me/{chat.username}"
        elif chat.type in {"group", "supergroup"} and chat.id < 0:
            raw = str(-chat.id)
            if raw.startswith("100"):
                raw = raw[3:]
            link = f"https://t.me/c/{raw}"
        result[chat_id] = DriverChatInfo(chat_id=chat_id, title=title, link=link)
    return result


def _format_chat_label(info: Optional[DriverChatInfo]) -> str:
    if info is None:
        return "Direct"
    title = html.escape(info.title)
    if info.link:
        return f'<a href="{html.escape(info.link)}">{title}</a>'
    return title


def _is_paused_chat(title: str) -> bool:
    lowered = title.lower()
    return any(token in lowered for token in PAUSE_TOKENS)


async def _fetch_latest_notes(db: Database, driver_ids: Sequence[int]) -> Dict[int, str]:
    if not driver_ids:
        return {}
    records = await db.fetch(
        """
        SELECT DISTINCT ON (driver_id) driver_id, note
        FROM compliance_notes
        WHERE driver_id = ANY($1::int[])
        ORDER BY driver_id, created_at DESC
        """,
        list(driver_ids),
    )
    return {record["driver_id"]: record["note"] for record in records}


async def _upsert_state(
    db: Database, driver_id: int, status: str, now: datetime
) -> ComplianceState:
    record = await db.fetchrow(
        """
        INSERT INTO compliance_tracking (driver_id, last_status, last_report_at, consecutive_reports)
        VALUES ($1, $2, $3, CASE WHEN $2 = 'non_compliant' THEN 1 ELSE 0 END)
        ON CONFLICT (driver_id)
        DO UPDATE SET
            last_status = EXCLUDED.last_status,
            last_report_at = EXCLUDED.last_report_at,
            consecutive_reports = CASE
                WHEN EXCLUDED.last_status = 'non_compliant' AND compliance_tracking.last_status = 'non_compliant'
                    THEN compliance_tracking.consecutive_reports + 1
                WHEN EXCLUDED.last_status = 'non_compliant' THEN 1
                ELSE 0
            END,
            updated_at = now()
        RETURNING *
        """,
        driver_id,
        status,
        now,
    )
    return _record_to_state(record)


async def _reset_state(
    db: Database, driver_id: int, now: datetime, status: str
) -> None:
    await db.execute(
        """
        UPDATE compliance_tracking
        SET consecutive_reports = 0,
            last_status = $3,
            last_report_at = $2,
            updated_at = now()
        WHERE driver_id = $1
        """,
        driver_id,
        now,
        status,
    )


def _record_to_state(record) -> ComplianceState:
    return ComplianceState(
        driver_id=record["driver_id"],
        consecutive_reports=record["consecutive_reports"],
        last_report_at=record["last_report_at"],
        last_driver_alert_at=record["last_driver_alert_at"],
        last_dispatch_alert_at=record["last_dispatch_alert_at"],
        last_status=record["last_status"],
        last_comment_thread_id=record["last_comment_thread_id"],
    )


def _should_alert_driver(state: ComplianceState, now: datetime) -> bool:
    if state.consecutive_reports < 2:
        return False
    if state.last_driver_alert_at and now - state.last_driver_alert_at < REPORT_WINDOW:
        return False
    return True


def _should_alert_dispatch(state: ComplianceState, now: datetime) -> bool:
    if state.consecutive_reports < 3:
        return False
    if state.last_dispatch_alert_at and now - state.last_dispatch_alert_at < REPORT_WINDOW:
        return False
    return True


async def _mark_driver_alert(db: Database, driver_id: int, when: datetime) -> None:
    await db.execute(
        "UPDATE compliance_tracking SET last_driver_alert_at = $2, updated_at = now() WHERE driver_id = $1",
        driver_id,
        when,
    )


async def _mark_dispatch_alert(db: Database, driver_id: int, when: datetime) -> None:
    await db.execute(
        "UPDATE compliance_tracking SET last_dispatch_alert_at = $2, updated_at = now() WHERE driver_id = $1",
        driver_id,
        when,
    )


async def _weekly_rankings(
    db: Database, group_id: int, end_date: date, limit: int
) -> tuple[List[dict], List[dict]]:
    start_date = end_date - timedelta(days=6)
    records = await db.fetch(
        """
        SELECT d.id, d.username, d.display_name,
               COUNT(*) FILTER (WHERE dc.status = 'pass') AS passes,
               COUNT(*) FILTER (WHERE dc.status <> 'excused') AS total
        FROM drivers d
        LEFT JOIN daily_checkins dc
            ON dc.driver_id = d.id AND dc.group_id = $1 AND dc.date BETWEEN $2 AND $3
        WHERE d.active = true
        GROUP BY d.id
        HAVING COUNT(*) FILTER (WHERE dc.status <> 'excused') > 0
        """,
        group_id,
        start_date,
        end_date,
    )

    def to_label(record) -> str:
        if record["username"]:
            return f"@{record['username']}"
        return record["display_name"] or f"Driver {record['id']}"

    rows = []
    for record in records:
        total = record["total"]
        passes = record["passes"]
        pct = 0
        if total:
            pct = int(round((passes / total) * 100))
        rows.append({
            "driver_id": record["id"],
            "label": to_label(record),
            "pct": pct,
        })

    top = sorted(rows, key=lambda r: (-r["pct"], r["label"]))[:limit]
    worst = sorted(rows, key=lambda r: (r["pct"], r["label"]))[:limit]
    return top, worst
