from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Optional

from aiogram import Bot
from aiogram.types import Chat, ChatMember

from app.db import Database


DRIVER_PAUSE_TOKENS = {"inactive", "home", "home time"}


class DispatcherError(RuntimeError):
    """Base class for dispatcher validation errors."""


class DispatcherGroupNotConfigured(DispatcherError):
    pass


class InvalidDispatcherContext(DispatcherError):
    pass


class UnauthorizedDispatcher(DispatcherError):
    pass


@dataclass(slots=True)
class GroupSettings:
    id: int
    title: str
    rolling_topic_id: int
    compliance_topic_id: Optional[int]
    trailer_topic_id: Optional[int]
    tz: str
    paused: bool
    autosend_enabled: bool
    autosend_time: Optional[time]
    digest_time: time

    @classmethod
    def from_record(cls, record) -> "GroupSettings":
        return cls(
            id=record["id"],
            title=record["title"],
            rolling_topic_id=record["rolling_topic_id"],
            compliance_topic_id=record["compliance_topic_id"],
            trailer_topic_id=record.get("trailer_topic_id"),
            tz=record["tz"],
            paused=record["paused"],
            autosend_enabled=record["autosend_enabled"],
            autosend_time=record["autosend_time"],
            digest_time=record["digest_time"],
        )


async def fetch_group(db: Database, chat_id: int) -> Optional[GroupSettings]:
    record = await db.fetchrow("SELECT * FROM groups WHERE id=$1", chat_id)
    if not record:
        return None
    return GroupSettings.from_record(record)

async def fetch_default_group(db: Database) -> Optional[GroupSettings]:
    record = await db.fetchrow("SELECT * FROM groups ORDER BY created_at LIMIT 1")
    if not record:
        return None
    return GroupSettings.from_record(record)


async def ensure_dispatcher_context(
    *,
    bot: Bot,
    db: Database,
    chat_id: int,
    message_thread_id: Optional[int],
    user_id: int,
    require_admin: bool,
) -> GroupSettings:
    group = await fetch_group(db, chat_id)
    if group is None:
        raise DispatcherGroupNotConfigured("Dispatcher group is not configured. Run /set_topic inside the topic.")

    if group.rolling_topic_id != message_thread_id:
        raise InvalidDispatcherContext("This command must be used inside the saved rolling topic.")

    member = await bot.get_chat_member(chat_id, user_id)
    _validate_membership(member, require_admin=require_admin)

    group = await refresh_group_pause(bot=bot, db=db, group=group)
    return group


async def ensure_dispatcher_user(
    *, bot: Bot, group: GroupSettings, user_id: int, require_admin: bool
) -> None:
    member = await bot.get_chat_member(group.id, user_id)
    _validate_membership(member, require_admin=require_admin)


async def refresh_group_pause(*, bot: Bot, db: Database, group: GroupSettings) -> GroupSettings:
    chat = await bot.get_chat(group.id)
    title = chat.title or group.title
    paused = False

    if title != group.title or group.paused:
        await db.execute(
            "UPDATE groups SET title=$1, paused=$2, updated_at=now() WHERE id=$3",
            title,
            paused,
            group.id,
        )
        group = GroupSettings(
            id=group.id,
            title=title,
            rolling_topic_id=group.rolling_topic_id,
            compliance_topic_id=group.compliance_topic_id,
            trailer_topic_id=group.trailer_topic_id,
            tz=group.tz,
            paused=paused,
            autosend_enabled=group.autosend_enabled,
            autosend_time=group.autosend_time,
            digest_time=group.digest_time,
        )
    return group


def is_driver_chat_paused(chat: Optional[Chat]) -> bool:
    if chat is None:
        return False
    title = chat.title or chat.full_name or ""
    lowered = title.lower()
    return any(token in lowered for token in DRIVER_PAUSE_TOKENS)


def _validate_membership(member: ChatMember, *, require_admin: bool) -> None:
    status = member.status
    if status in {"left", "kicked"}:
        raise UnauthorizedDispatcher("You must be a member of the dispatcher group to use this command.")
    if require_admin and status not in {"administrator", "creator"}:
        raise UnauthorizedDispatcher("Admin privileges required for this action.")
