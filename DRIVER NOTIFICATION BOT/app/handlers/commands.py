from __future__ import annotations

from datetime import date, datetime, time
import html
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Chat, Message

from app.config import Settings
from app.db import Database
from app.services import checks, compliance, roles
from app.services.roles import GroupSettings
from app.services.autosend import SchedulerService


router = Router(name="commands")


@router.message(Command("set_topic"))
async def handle_set_topic(
    message: Message,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    if message.chat is None or message.chat.type not in {"supergroup", "group"}:
        return
    if message.message_thread_id is None:
        await message.reply("Run /set_topic inside the rolling topic thread.")
        return

    title = message.chat.title or "Dispatcher"
    group_id = message.chat.id
    thread_id = message.message_thread_id
    await db.execute(
        """
        INSERT INTO groups (id, title, rolling_topic_id, tz, paused)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (id)
        DO UPDATE SET title = EXCLUDED.title,
                      rolling_topic_id = EXCLUDED.rolling_topic_id,
                      paused = EXCLUDED.paused,
                      updated_at = now()
        """,
        group_id,
        title,
        thread_id,
        config.tz_name,
        False,
    )

    await scheduler.refresh_group(group_id)
    await message.reply("Dispatcher topic saved. Daily Check workflow is ready.")


@router.message(Command("set_timezone"))
async def handle_set_timezone(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    if not message.from_user or not message.chat:
        return

    group = await _guard_dispatcher_or_compliance_message(
        message,
        bot=bot,
        db=db,
        config=config,
        require_admin=config.admin_only_review,
    )
    if group is None:
        return

    args = (command.args or "").strip()
    if not args:
        await message.reply("Usage: /set_timezone <IANA timezone>, e.g. America/Chicago")
        return

    try:
        ZoneInfo(args)
    except Exception:
        await message.reply("Invalid timezone. Provide a valid IANA name like America/Chicago.")
        return

    await db.execute(
        "UPDATE groups SET tz = $1, updated_at = now() WHERE id = $2",
        args,
        group.id,
    )
    await scheduler.refresh_group(group.id)
    await message.reply(f"Timezone updated to {args}.")


@router.message(Command("set_compliance_topic"))
async def handle_set_compliance_topic(
    message: Message,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    if message.chat is None or message.message_thread_id is None:
        await message.reply("Run /set_compliance_topic inside the topic you want to use.")
        return

    group = await roles.fetch_group(db, message.chat.id)
    if group is None:
        await message.reply("Dispatcher group not configured. Run /set_topic first.")
        return

    try:
        await roles.ensure_dispatcher_user(
            bot=bot,
            group=group,
            user_id=message.from_user.id,
            require_admin=config.admin_only_review,
        )
    except roles.UnauthorizedDispatcher:
        await message.reply("Dispatcher permissions required.")
        return

    await db.execute(
        "UPDATE groups SET compliance_topic_id=$1, updated_at=now() WHERE id=$2",
        message.message_thread_id,
        group.id,
    )
    await scheduler.refresh_group(group.id)
    await message.reply("Compliance topic saved. Hourly reports will post here.")


@router.message(Command("set_trailer_topic"))
async def handle_set_trailer_topic(
    message: Message,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    if message.chat is None or message.message_thread_id is None:
        await message.reply("Run /set_trailer_topic inside the topic you want to use.")
        return

    group = await roles.fetch_group(db, message.chat.id)
    if group is None:
        await message.reply("Dispatcher group not configured. Run /set_topic first.")
        return

    try:
        await roles.ensure_dispatcher_user(
            bot=bot,
            group=group,
            user_id=message.from_user.id,
            require_admin=config.admin_only_review,
        )
    except roles.UnauthorizedDispatcher:
        await message.reply("Dispatcher permissions required.")
        return

    await db.execute(
        "UPDATE groups SET trailer_topic_id=$1, updated_at=now() WHERE id=$2",
        message.message_thread_id,
        group.id,
    )
    await scheduler.refresh_group(group.id)
    await message.reply("Trailer topic saved. Future trailer alerts will post here.")


@router.message(Command("compliance_report"))
async def handle_compliance_report(
    message: Message,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    group = await _guard_dispatcher_or_compliance_message(
        message,
        bot=bot,
        db=db,
        config=config,
        require_admin=config.admin_only_review,
    )
    if group is None:
        return

    if group.compliance_topic_id is None:
        await message.reply("Compliance topic not configured. Run /set_compliance_topic inside the target thread.")
        return

    await compliance.send_hourly_report(bot=bot, db=db, group=group)
    await message.reply("Compliance snapshot sent.")


@router.message(Command("compliance_reset"))
async def handle_compliance_reset(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    group = await _guard_dispatcher_message(
        message,
        bot=bot,
        db=db,
        config=config,
        require_admin=config.admin_only_review,
    )
    if group is None:
        return

    args = (command.args or "").strip()
    if args:
        try:
            target_date = datetime.strptime(args, "%Y-%m-%d").date()
        except ValueError:
            await message.reply("Invalid date format. Use YYYY-MM-DD")
            return
    else:
        target_date = datetime.now(tz=ZoneInfo(group.tz)).date()

    records = await db.fetch(
        """
        SELECT id, driver_id
        FROM daily_checkins
        WHERE group_id = $1 AND date = $2
        """,
        group.id,
        target_date,
    )

    reset_ids: list[int] = []
    reset_count = 0
    for record in records:
        driver = await checks.find_driver_by_id(db, record["driver_id"])
        if driver is None:
            continue
        await scheduler.cancel_followups(record["id"])
        checkin = await checks.reset_checkin(db, record["id"])
        await checks.sync_review_card(
            bot,
            db,
            group_id=group.id,
            thread_id=group.rolling_topic_id,
            driver=driver,
            checkin=checkin,
        )
        reset_ids.append(driver.id)
        reset_count += 1

    if reset_ids:
        await db.execute(
            "UPDATE drivers SET last_pass_at = NULL, updated_at = now() WHERE id = ANY($1::int[])",
            reset_ids,
        )

    await compliance.clear_tracking(db)
    await message.reply(
        f"Compliance data reset for {reset_count} drivers on {target_date:%Y-%m-%d}."
    )


@router.message(Command("status"))
async def handle_status(
    message: Message,
    bot: Bot,
    db: Database,
    config: Settings,
) -> None:
    if message.chat is None:
        return
    group = await roles.fetch_group(db, message.chat.id)
    if not group:
        await message.reply("Dispatcher group not configured. Run /set_topic inside the rolling topic.")
        return

    if message.message_thread_id is not None and group.rolling_topic_id != message.message_thread_id:
        await message.reply("Use /status inside the saved rolling topic.")
        return

    group = await roles.refresh_group_pause(bot=bot, db=db, group=group)
    timezone = ZoneInfo(group.tz)
    today = datetime.now(tz=timezone).date()
    stats = await checks.fetch_daily_stats(db, group_id=group.id, check_date=today)

    autosend_text = "Off"
    if group.autosend_enabled and group.autosend_time:
        autosend_text = f"On @ {group.autosend_time.strftime('%H:%M')}"

    paused_text = "Yes" if group.paused else "No"
    digest_text = group.digest_time.strftime("%H:%M")
    compliance_text = "Configured" if group.compliance_topic_id else "Not set"

    message_text = (
        f"Dispatcher group: {group.id}\n"
        f"Rolling topic: {group.rolling_topic_id}\n"
        f"Paused: {paused_text}\n"
        f"Autosend: {autosend_text}\n"
        f"Digest time: {digest_text}\n"
        f"Compliance topic: {compliance_text}\n"
        f"Admin-only review: {'Yes' if config.admin_only_review else 'No'}\n\n"
        f"Today — Done {stats.done} / Pending {stats.pending} / Excused {stats.excused} / Fails {stats.fails}"
    )
    await message.reply(message_text)


@router.message(Command("notify"))
async def handle_notify(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    if not message.from_user or not message.chat:
        return

    dispatcher_group = await roles.fetch_default_group(db)
    if dispatcher_group is None:
        await message.reply("Dispatcher group not configured. Run /set_topic inside the rolling topic.")
        return

    in_dispatcher_topic = (
        message.chat.id == dispatcher_group.id
        and message.message_thread_id == dispatcher_group.rolling_topic_id
    )

    target_chat_id = None if in_dispatcher_topic else message.chat.id

    await _process_notify(
        bot=bot,
        db=db,
        config=config,
        scheduler=scheduler,
        dispatcher_group=dispatcher_group,
        origin_chat=message.chat,
        origin_thread=message.message_thread_id,
        actor_id=message.from_user.id,
        command=command,
        message=message,
        target_chat_id=target_chat_id,
    )


@router.message(Command("autosend"))
async def handle_autosend(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    if not message.from_user or not message.chat:
        return

    group = await _guard_dispatcher_message(
        message,
        bot=bot,
        db=db,
        config=config,
        require_admin=config.admin_only_review,
    )
    if group is None:
        return

    args = (command.args or "").strip().split()
    if not args:
        await message.reply("Usage: /autosend on HH:MM or /autosend off")
        return

    action = args[0].lower()
    if action == "on":
        if len(args) < 2:
            await message.reply("Provide time as HH:MM.")
            return
        if group.paused:
            await message.reply("Paused for this group. Rename to resume.")
            return
        try:
            time_value = datetime.strptime(args[1], "%H:%M").time()
        except ValueError:
            await message.reply("Invalid time format. Use HH:MM")
            return
        await db.execute(
            """
            UPDATE groups
            SET autosend_enabled = true,
                autosend_time = $2,
                updated_at = now()
            WHERE id = $1
            """,
            group.id,
            time_value,
        )
        await scheduler.refresh_group(group.id)
        await message.reply(f"Autosend enabled for {time_value.strftime('%H:%M')}.")
        return

    if action == "off":
        await db.execute(
            """
            UPDATE groups
            SET autosend_enabled = false,
                autosend_time = NULL,
                updated_at = now()
            WHERE id = $1
            """,
            group.id,
        )
        await scheduler.refresh_group(group.id)
        await message.reply("Autosend disabled.")
        return

    await message.reply("Usage: /autosend on HH:MM or /autosend off")


@router.message(Command("trailer"))
async def handle_trailer(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    config: Settings,
) -> None:
    if message.chat is None:
        return

    dispatcher_group = await roles.fetch_default_group(db)
    if dispatcher_group is None:
        await message.reply("Dispatcher group not configured. Run /set_topic inside the rolling topic.")
        return

    if message.chat.id == dispatcher_group.id and message.message_thread_id == dispatcher_group.rolling_topic_id:
        await message.reply("Run /trailer inside the driver chat where you need the reminder.")
        return

    raw_args = command.args or ""
    if not raw_args.strip():
        await message.reply("Usage: /trailer TRAILER_ID [ACTION NOTE]")
        return

    lines = [line.strip() for line in raw_args.splitlines() if line.strip()]
    first_line = lines[0]
    parts = first_line.split()
    trailer_id = parts[0].upper()
    action_note = " ".join(parts[1:]).strip()
    action_display = action_note.upper() if action_note else "CHECK"
    location = lines[1] if len(lines) > 1 else ""

    if not location and len(parts) >= 3:
        location = " ".join(parts[2:])

    location_line = f"Location: {location}\n\n" if location else ""
    driver_prompt = (
        "📸 TRAILER PTI Photo Reminder\n\n"
        f"Please send your Pre-Trip Inspection photos for trailer {trailer_id} ({action_display}).\n\n"
        f"{location_line}"
        "Required shots:\n"
        "• Front & both sides\n"
        "• Rear doors & seal (if any)\n"
        "• All tires & brakes\n"
        "• Mudflaps\n"
        "• Trailer registration & annual inspection sticker\n"
        "• Lights (with lights turned on)\n\n"
        "⚠️ Important:\n\n"
        "Send BEFORE leaving the yard.\n"
        "Failure to provide PTI photos may result in charges and a possible DOT violation.\n\n"
        "Double-check that all trailer tires are properly inflated and in good condition.\n"
        "👉 80% of DOT violations come from tire issues — let’s avoid them!"
    )

    if roles.is_driver_chat_paused(message.chat):
        await message.reply("Paused for this driver chat. Rename to resume.")
        return

    await message.reply(driver_prompt)

    timezone = ZoneInfo(dispatcher_group.tz)
    now_local = datetime.now(tz=timezone)
    now_utc = now_local.astimezone(ZoneInfo("UTC"))
    local_time = now_local.strftime("%Y-%m-%d %H:%M")
    utc_time = now_utc.strftime("%Y-%m-%d %H:%M")

    target_thread = dispatcher_group.trailer_topic_id or dispatcher_group.rolling_topic_id

    chat_anchor = _format_chat_anchor(message.chat)
    dispatcher_text = (
        "<b>🚛 Trailer Check</b>\n"
        f"Trailer: {html.escape(trailer_id)}\n"
        f"Action: {html.escape(action_display)}\n"
        f"Location: {html.escape(location)}\n"
        f"From chat: {chat_anchor}\n"
        f"When: {local_time} ({dispatcher_group.tz})\n\n"
        "FLEET TEAM PLEASE CHECK AND VERIFY"
    )

    await bot.send_message(
        chat_id=dispatcher_group.id,
        message_thread_id=target_thread,
        text=dispatcher_text,
        disable_notification=True,
    )

@router.message(Command("reopen"))
async def handle_reopen(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    config: Settings,
) -> None:
    if not message.from_user or not message.chat:
        return

    group = await _guard_dispatcher_message(
        message,
        bot=bot,
        db=db,
        config=config,
        require_admin=config.admin_only_review,
    )
    if group is None:
        return

    args = (command.args or "").strip().split()
    if not args:
        await message.reply("Usage: /reopen @driver [YYYY-MM-DD]")
        return

    driver_ref = args[0]
    driver = await _resolve_driver(message, command, db, fallback=driver_ref)
    if driver is None:
        await message.reply("Driver not found.")
        return

    if len(args) > 1:
        try:
            target_date = datetime.strptime(args[1], "%Y-%m-%d").date()
        except ValueError:
            await message.reply("Invalid date format. Use YYYY-MM-DD")
            return
    else:
        timezone = ZoneInfo(group.tz)
        target_date = datetime.now(tz=timezone).date()

    checkin = await checks.reopen_checkin(
        db,
        driver_id=driver.id,
        group_id=group.id,
        check_date=target_date,
    )
    if checkin is None:
        await message.reply("No check-in to reopen.")
        return

    await checks.sync_review_card(
        bot,
        db,
        group_id=group.id,
        thread_id=group.rolling_topic_id,
        driver=driver,
        checkin=checkin,
    )
    await message.reply(f"Reopened {driver.mention} for {target_date:%Y-%m-%d}.")


@router.message(Command("reset"))
async def handle_reset(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    if not message.from_user or not message.chat:
        return

    group = await _guard_dispatcher_message(
        message,
        bot=bot,
        db=db,
        config=config,
        require_admin=config.admin_only_review,
    )
    if group is None:
        return

    args = (command.args or "").strip().split()
    if not args:
        await message.reply("Usage: /reset @driver [YYYY-MM-DD] | /reset all")
        return

    if args[0].lower() == "all":
        if len(args) > 1:
            await message.reply("Usage: /reset all")
            return
        timezone = ZoneInfo(group.tz)
        target_date = datetime.now(tz=timezone).date()
        drivers = await checks.list_active_drivers(db)
        reset_count = 0
        reset_ids = []
        for driver in drivers:
            await _reset_single_driver(
                bot=bot,
                db=db,
                scheduler=scheduler,
                group=group,
                driver=driver,
                target_date=target_date,
            )
            reset_count += 1
            reset_ids.append(driver.id)
        if reset_ids:
            await db.execute(
                "UPDATE drivers SET last_pass_at = NULL, updated_at = now() WHERE id = ANY($1::int[])",
                reset_ids,
            )
        await message.reply(
            f"Reset reminder state for {reset_count} active drivers on {target_date:%Y-%m-%d}."
        )
        return

    driver_ref = args[0]
    driver = await _resolve_driver(message, command, db, fallback=driver_ref)
    if driver is None:
        await message.reply("Driver not found.")
        return

    if len(args) > 1:
        try:
            target_date = datetime.strptime(args[1], "%Y-%m-%d").date()
        except ValueError:
            await message.reply("Invalid date format. Use YYYY-MM-DD")
            return
    else:
        timezone = ZoneInfo(group.tz)
        target_date = datetime.now(tz=timezone).date()

    await _reset_single_driver(
        bot=bot,
        db=db,
        scheduler=scheduler,
        group=group,
        driver=driver,
        target_date=target_date,
    )
    await message.reply(
        f"Reset reminder state for {driver.mention} on {target_date:%Y-%m-%d}."
    )


async def _reset_single_driver(
    *,
    bot: Bot,
    db: Database,
    scheduler: SchedulerService,
    group: roles.GroupSettings,
    driver: checks.Driver,
    target_date: date,
) -> None:
    checkin = await checks.ensure_checkin(
        db,
        driver_id=driver.id,
        group_id=group.id,
        check_date=target_date,
    )
    await scheduler.cancel_followups(checkin.id)
    checkin = await checks.reset_checkin(db, checkin.id)
    await checks.sync_review_card(
        bot,
        db,
        group_id=group.id,
        thread_id=group.rolling_topic_id,
        driver=driver,
        checkin=checkin,
    )


async def _guard_dispatcher_message(
    message: Message,
    *,
    bot: Bot,
    db: Database,
    config: Settings,
    require_admin: bool,
) -> Optional[roles.GroupSettings]:
    if message.chat is None or message.message_thread_id is None:
        await message.reply("This command must be used inside the dispatcher rolling topic.")
        return None
    try:
        return await roles.ensure_dispatcher_context(
            bot=bot,
            db=db,
            chat_id=message.chat.id,
            message_thread_id=message.message_thread_id,
            user_id=message.from_user.id,
            require_admin=require_admin,
        )
    except roles.DispatcherGroupNotConfigured:
        await message.reply("Dispatcher group not configured. Run /set_topic inside the rolling topic.")
    except roles.InvalidDispatcherContext:
        await message.reply("This command must be used in the saved rolling topic.")
    except roles.UnauthorizedDispatcher:
        await message.reply("Dispatcher permissions required.")
    return None


async def _guard_dispatcher_or_compliance_message(
    message: Message,
    *,
    bot: Bot,
    db: Database,
    config: Settings,
    require_admin: bool,
) -> Optional[roles.GroupSettings]:
    if message.chat is None or message.message_thread_id is None:
        await message.reply("This command must be used inside the dispatcher rolling or compliance topic.")
        return None

    group = await roles.fetch_group(db, message.chat.id)
    if group is None:
        await message.reply("Dispatcher group not configured. Run /set_topic inside the rolling topic.")
        return None

    allowed_threads = {group.rolling_topic_id}
    if group.compliance_topic_id:
        allowed_threads.add(group.compliance_topic_id)

    if message.message_thread_id not in allowed_threads:
        await message.reply("Use this command inside the dispatcher rolling or compliance topic.")
        return None

    try:
        await roles.ensure_dispatcher_user(
            bot=bot,
            group=group,
            user_id=message.from_user.id,
            require_admin=require_admin,
        )
    except roles.UnauthorizedDispatcher:
        await message.reply("Dispatcher permissions required.")
        return None

    return await roles.refresh_group_pause(bot=bot, db=db, group=group)


async def _resolve_driver(
    bot: Bot,
    message: Message,
    command: CommandObject,
    db: Database,
    fallback: Optional[str] = None,
) -> Optional[checks.Driver]:
    async def ensure_from_username(username: str) -> Optional[checks.Driver]:
        if not username:
            return None
        cleaned = username.lstrip("@")
        display_name = cleaned
        return await checks.ensure_driver_profile(
            db,
            telegram_user_id=None,
            username=cleaned,
            display_name=display_name,
        )

    entities = message.entities or []
    text = message.text or ""
    for entity in entities:
        if entity.type == "text_mention" and entity.user:
            return await checks.ensure_driver(db, entity.user)
        if entity.type == "mention":
            username = text[entity.offset + 1 : entity.offset + entity.length]
            driver = await checks.find_driver_by_username(db, username)
            if driver:
                return driver
            driver = await ensure_from_username(username)
            if driver:
                return driver

    args = (command.args or "").strip()
    if args.startswith("@"):
        username = args[1:]
        driver = await checks.find_driver_by_username(db, username)
        if driver:
            return driver
        driver = await ensure_from_username(username)
        if driver:
            return driver

    if fallback and fallback.startswith("@"):
        username = fallback[1:]
        driver = await checks.find_driver_by_username(db, username)
        if driver:
            return driver
        return await ensure_from_username(username)

    return None


def _format_chat_anchor(chat: Chat) -> str:
    title = html.escape(chat.title or "Driver chat")
    link = _build_chat_link(chat)
    if link:
        return f'<a href="{html.escape(link)}">{title}</a>'
    return title


def _build_chat_link(chat: Chat) -> Optional[str]:
    if chat.username:
        return f"https://t.me/{chat.username}"
    if chat.type in {"supergroup", "group"} and chat.id < 0:
        chat_id_value = -chat.id
        chat_id_str = str(chat_id_value)
        if chat_id_str.startswith("100"):
            chat_id_str = chat_id_str[3:]
        return f"https://t.me/c/{chat_id_str}"
    return None


async def _process_notify(
    *,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
    dispatcher_group: GroupSettings,
    origin_chat: Chat,
    origin_thread: Optional[int],
    actor_id: int,
    command: CommandObject,
    message: Message,
    target_chat_id: Optional[int],
) -> None:
    dispatcher_group = await roles.refresh_group_pause(bot=bot, db=db, group=dispatcher_group)
    if dispatcher_group.paused:
        await message.reply("Paused for this group. Rename to resume.")
        return

    if target_chat_id is not None:
        try:
            await roles.ensure_dispatcher_user(
                bot=bot,
                group=dispatcher_group,
                user_id=actor_id,
                require_admin=config.admin_only_review,
            )
        except roles.UnauthorizedDispatcher:
            await message.reply("Dispatcher permissions required.")
            return

        if target_chat_id == dispatcher_group.id:
            await message.reply("Use /notify in the dispatcher topic only to trigger DMs.")
            return

    driver = await _resolve_driver(bot, message, command, db)

    if driver is None and message.reply_to_message and message.reply_to_message.from_user:
        driver = await checks.ensure_driver(db, message.reply_to_message.from_user)

    if driver is None and target_chat_id is not None:
        driver = await checks.find_driver_by_notify_chat(db, target_chat_id)

    if driver is None and origin_chat.type == "private" and message.from_user is not None:
        driver = await checks.ensure_driver(db, message.from_user)

    if driver is None:
        if target_chat_id is None:
            await message.reply("Driver not found. Mention them or reply to their message.")
        else:
            await message.reply("Driver not found. Reply to their message once so I can link this chat.")
        return

    driver_chat = None
    if target_chat_id is not None:
        await checks.set_driver_notify_chat(db, driver_id=driver.id, chat_id=target_chat_id)
        driver.notify_chat_id = target_chat_id
    elif driver.notify_chat_id:
        target_chat_id = driver.notify_chat_id

    if target_chat_id is None and driver.telegram_user_id < 0:
        await message.reply("Driver not linked yet. Run /notify in their group chat once first.")
        return

    if target_chat_id is not None:
        if origin_chat.id == target_chat_id:
            driver_chat = origin_chat
        else:
            driver_chat = await bot.get_chat(target_chat_id)
        if roles.is_driver_chat_paused(driver_chat):
            await message.reply("Paused for this driver chat. Rename to resume.")
            return

    timezone = ZoneInfo(dispatcher_group.tz)
    today = datetime.now(tz=timezone).date()

    checkin = await checks.ensure_checkin(
        db,
        driver_id=driver.id,
        group_id=dispatcher_group.id,
        check_date=today,
    )
    needs_reset = (
        checkin.media_count > 0
        or checkin.status not in {"pending", "submitted"}
        or checkin.reason is not None
        or checkin.review_message_id is not None
    )
    if needs_reset:
        await scheduler.cancel_followups(checkin.id)
        checkin = await checks.reset_checkin(db, checkin.id)
    checkin = await checks.mark_notified(db, checkin.id)

    try:
        actual_target_chat = await checks.send_driver_notification(
            bot,
            driver=driver,
            checkin=checkin,
            check_date=today,
            chat_id=target_chat_id,
        )
    except TelegramBadRequest as exc:
        target = "driver chat" if target_chat_id else "driver DM"
        await message.reply(f"Failed to send reminder to {target}: {exc.message}")
        return

    await checks.sync_review_card(
        bot,
        db,
        group_id=dispatcher_group.id,
        thread_id=dispatcher_group.rolling_topic_id,
        driver=driver,
        checkin=checkin,
    )
    await scheduler.schedule_followups(
        checkin_id=checkin.id,
        group=dispatcher_group,
        driver=driver,
        target_chat_id=actual_target_chat,
    )
    target_chat_id = target_chat_id or actual_target_chat

    if target_chat_id is None:
        destination = "their saved chat" if driver.notify_chat_id else "DM"
        await message.reply(f"Reminder sent to {driver.mention} via {destination}.")
    elif origin_chat.id == target_chat_id:
        group_label = html.escape(origin_chat.title or "Driver chat")
        await message.reply("Reminder posted here.")
        await bot.send_message(
            chat_id=dispatcher_group.id,
            message_thread_id=dispatcher_group.rolling_topic_id,
            text=(
                f"Manual notify sent to {driver.mention} in <b>{group_label}</b>. "
                "Awaiting response."
            ),
            disable_notification=True,
        )
    else:
        await message.reply("Reminder sent.")
