from __future__ import annotations

from datetime import date
from typing import Sequence

from aiogram import Bot

from app.db import Database
from app.services import checks


async def send_daily_digest(
    *,
    bot: Bot,
    db: Database,
    group_id: int,
    thread_id: int,
    check_date: date,
) -> None:
    stats = await checks.fetch_daily_stats(db, group_id=group_id, check_date=check_date)
    if stats.total > 0:
        percent = round((stats.done / stats.total) * 100)
    else:
        percent = 0

    pending_text = ", ".join(stats.pending_usernames) if stats.pending_usernames else "None"
    top_streaks_text = format_top_streaks(stats.top_streaks)

    message = (
        f"Daily Checks — Done {stats.done} / Total {stats.total} ({percent}%)\n"
        f"Pending: {pending_text}\n"
        f"Excused: {stats.excused}\n"
        f"Fails: {stats.fails}\n"
        f"Top streaks: {top_streaks_text}"
    )

    await bot.send_message(chat_id=group_id, text=message, message_thread_id=thread_id)


def format_top_streaks(entries: Sequence[tuple[str, int]]) -> str:
    if not entries:
        return "None"
    formatted = [f"{name} {streak}" for name, streak in entries]
    return ", ".join(formatted)
