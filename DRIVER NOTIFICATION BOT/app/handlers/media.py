from __future__ import annotations

from datetime import datetime
import html
import logging
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, Router, F
from aiogram.types import CallbackQuery, Message

from app.db import Database
from app.keyboards import DriverAction, DriverSkipChoice, SKIP_REASONS, driver_skip_keyboard
from app.services import checks, roles
from app.services.autosend import SchedulerService


logger = logging.getLogger(__name__)

router = Router(name="media")


@router.message(F.text.startswith("/"), F.chat.type == "private")
async def ignore_commands(message: Message) -> None:
    await message.answer("Commands are dispatcher-only. Send your check-in media here.")


@router.message()
async def handle_media_message(message: Message, bot: Bot, db: Database, scheduler: SchedulerService) -> None:
    if message.from_user is None or message.chat is None:
        return

    if not message.photo and not message.video:
        return

    logger.info(
        "media:update incoming", extra={
            "chat_id": message.chat.id if message.chat else None,
            "chat_type": message.chat.type if message.chat else None,
            "from_id": message.from_user.id if message.from_user else None,
            "has_photo": bool(message.photo),
            "has_video": bool(message.video),
        }
    )

    driver = await checks.ensure_driver(db, message.from_user)

    group = await _get_primary_group(db)
    if group is None:
        logger.warning("media:update group missing")
        if message.chat.type == "private":
            await message.answer("Dispatcher group not configured yet.")
        return

    if message.chat.id == group.id and message.message_thread_id == group.rolling_topic_id:
        logger.debug("media:update ignoring dispatcher thread")
        return

    expected_driver = None
    if message.chat.type != "private":
        expected_driver = await checks.find_driver_by_notify_chat(db, message.chat.id)
        if expected_driver is None:
            logger.info("media:update unlinked chat", extra={"chat_id": message.chat.id})
            await message.answer("Dispatcher hasn’t linked this chat yet. Ask them to run /notify here.")
            return
        if expected_driver.id != driver.id:
            logger.info(
                "media:update driver mismatch",
                extra={"chat_id": message.chat.id, "expected": expected_driver.id, "actual": driver.id},
            )
            return

    timezone = ZoneInfo(group.tz)
    today = datetime.now(tz=timezone).date()

    existing = await checks.fetch_checkin(db, driver_id=driver.id, group_id=group.id, check_date=today)
    prev_count = existing.media_count if existing else 0
    offthread_warned = existing and existing.reason == 'offthread_warning'

    bot_id = (await bot.get_me()).id
    is_reply = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.id == bot_id
    )

    logger.info(
        "media:update state",
        extra={
            "chat_id": message.chat.id,
            "driver_id": driver.id,
            "prev_count": prev_count,
            "is_reply": is_reply,
        },
    )

    offthread = message.chat.type != "private" and not is_reply
    if offthread and (prev_count >= 1 or offthread_warned):
        logger.info(
            "media:update blocked extra",
            extra={"chat_id": message.chat.id, "driver_id": driver.id, "prev_count": prev_count},
        )
        return

    notify_driver = offthread and prev_count == 0

    if notify_driver:
        checkin = existing or await checks.ensure_checkin(
            db,
            driver_id=driver.id,
            group_id=group.id,
            check_date=today,
        )
        await scheduler.cancel_followups(checkin.id)
        await checks.set_offthread_warning(db, checkin.id, True)
        group_name = message.chat.title or "Driver chat"
        group_name_html = html.escape(group_name.upper())
        logger.info(
            "media:update first_free_pass",
            extra={"chat_id": message.chat.id, "driver_id": driver.id, "group_name": group_name},
        )
        await message.answer("Please reply to the reminder to add more media.")
        await bot.send_message(
            chat_id=group.id,
            message_thread_id=group.rolling_topic_id,
            text=(
                f"{driver.mention} sent media in <b>{group_name_html}</b> without replying. "
                "Dispatch, please review the files and resolve when verified."
            ),
            disable_notification=True,
        )
        return

    file_id = message.photo[-1].file_id if message.photo else message.video.file_id  # type: ignore[union-attr]
    kind = "photo" if message.photo else "video"

    checkin, first_media = await checks.record_media(
        db,
        driver_id=driver.id,
        group_id=group.id,
        check_date=today,
        kind=kind,
        file_id=file_id,
        media_group_id=message.media_group_id,
    )

    await checks.set_offthread_warning(db, checkin.id, False)

    logger.info(
        "media:update stored",
        extra={
            "chat_id": message.chat.id,
            "driver_id": driver.id,
            "checkin_id": checkin.id,
            "first_media": first_media,
            "media_count": checkin.media_count,
        },
    )

    if message.chat.type == "private" and first_media:
        await message.answer("Submitted. Pending review.")

    if message.chat.type == "private" or is_reply:
        await scheduler.cancel_followups(checkin.id)

    await checks.sync_review_card(
        bot,
        db,
        group_id=group.id,
        thread_id=group.rolling_topic_id,
        driver=driver,
        checkin=checkin,
    )

    try:
        if kind == "photo":
            await bot.send_photo(
                chat_id=group.id,
                message_thread_id=group.rolling_topic_id,
                photo=file_id,
                caption=message.caption,
                disable_notification=True,
            )
        else:
            await bot.send_video(
                chat_id=group.id,
                message_thread_id=group.rolling_topic_id,
                video=file_id,
                caption=message.caption,
                disable_notification=True,
            )
    except Exception:
        logger.exception("media:update mirror_failed", extra={"chat_id": message.chat.id, "driver_id": driver.id})
        return

    group_label = ""
    if message.chat.type != "private":
        group_label = f"{html.escape((message.chat.title or 'Driver chat').upper())} "
    await bot.send_message(
        chat_id=group.id,
        message_thread_id=group.rolling_topic_id,
        text=f"Check-In update — {driver.mention} {group_label}media {checkin.media_count}/3",
        disable_notification=True,
    )
    logger.info(
        "media:update mirrored",
        extra={
            "chat_id": group.id,
            "driver_id": driver.id,
            "media_count": checkin.media_count,
            "group_label": group_label.strip(),
        },
    )


@router.callback_query(DriverAction.filter())
async def handle_driver_action(
    callback: CallbackQuery,
    callback_data: DriverAction,
    db: Database,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return

    checkin = await _get_checkin_by_id(db, callback_data.checkin_id)
    if checkin is None:
        await callback.answer("No check-in found.", show_alert=True)
        return

    if callback_data.action == "confirm":
        await callback.answer("Thanks. Upload your media when ready.")
        return

    if callback_data.action == "skip":
        await callback.message.edit_reply_markup(
            reply_markup=driver_skip_keyboard(callback_data.checkin_id)
        )
        await callback.answer("Choose a reason")
        return

    await callback.answer()


@router.callback_query(DriverSkipChoice.filter())
async def handle_skip_choice(
    callback: CallbackQuery,
    callback_data: DriverSkipChoice,
    bot: Bot,
    db: Database,
    scheduler: SchedulerService,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return

    group = await _get_primary_group(db)
    if group is None:
        await callback.answer()
        return

    driver = await checks.ensure_driver(db, callback.from_user)
    checkin = await _get_checkin_by_id(db, callback_data.checkin_id)
    if checkin is None:
        await callback.answer("No check-in found.", show_alert=True)
        return

    reason_label = SKIP_REASONS.get(callback_data.reason, "Other")

    checkin = await checks.set_excused(
        db,
        driver_id=driver.id,
        group_id=group.id,
        check_date=checkin.date,
        reason=reason_label,
    )
    await checks.sync_review_card(
        bot,
        db,
        group_id=group.id,
        thread_id=group.rolling_topic_id,
        driver=driver,
        checkin=checkin,
    )
    await scheduler.cancel_followups(checkin.id)
    await callback.answer("Marked excused")
    await callback.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(
        chat_id=group.id,
        message_thread_id=group.rolling_topic_id,
        text=f"Excused — {driver.mention}: {reason_label}",
        disable_notification=True,
    )


async def _get_primary_group(db: Database) -> Optional[roles.GroupSettings]:
    record = await db.fetchrow("SELECT * FROM groups ORDER BY created_at LIMIT 1")
    if not record:
        return None
    return roles.GroupSettings.from_record(record)


async def _get_checkin_by_id(db: Database, checkin_id: int) -> Optional[checks.Checkin]:
    return await checks.fetch_checkin_by_id(db, checkin_id)
