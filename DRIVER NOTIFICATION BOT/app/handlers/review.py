from __future__ import annotations

from typing import Optional
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.db import Database
from app.keyboards import FAIL_REASONS, FailReasonChoice, ReviewAction, fail_reason_keyboard
from app.services import checks, compliance, roles, streaks
from app.services.autosend import SchedulerService


router = Router(name="review")


@router.callback_query(ReviewAction.filter())
async def handle_review_action(
    callback: CallbackQuery,
    callback_data: ReviewAction,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    message = callback.message
    if not message or not message.chat or message.message_thread_id is None:
        await callback.answer()
        return

    group = await _guard_dispatcher_callback(
        callback,
        bot=bot,
        db=db,
        config=config,
        require_admin=config.admin_only_review,
    )
    if group is None:
        return

    driver = await checks.find_driver_by_id(db, callback_data.driver_id)
    if driver is None:
        await callback.answer("Driver missing", show_alert=True)
        return

    check_date = date.fromisoformat(callback_data.date)
    checkin = await checks.fetch_checkin(
        db,
        driver_id=driver.id,
        group_id=group.id,
        check_date=check_date,
    )
    if checkin is None:
        await callback.answer("No record for today.", show_alert=True)
        return

    timezone = ZoneInfo(group.tz)
    action = callback_data.action

    if group.paused and action in {"pass", "fail", "fix", "notify"}:
        await callback.answer("Paused for this group. Rename to resume.", show_alert=True)
        return

    if action == "pass":
        updated = await checks.update_review_status(
            db,
            driver_id=driver.id,
            group_id=group.id,
            check_date=check_date,
            status="pass",
            reviewer_user_id=callback.from_user.id,
            reason=None,
        )
        if updated:
            await streaks.update_after_pass(db, driver_id=driver.id, check_date=check_date)
            driver = await checks.find_driver_by_id(db, driver.id) or driver
            checkin = updated
            await scheduler.cancel_followups(checkin.id)
            await checks.sync_review_card(
                bot,
                db,
                group_id=group.id,
                thread_id=group.rolling_topic_id,
                driver=driver,
                checkin=checkin,
            )
            pass_time = checkin.reviewed_at or datetime.now(tz=ZoneInfo(group.tz))
            await compliance.handle_pass_event(
                bot=bot,
                db=db,
                group=group,
                driver=driver,
                reviewed_at=pass_time,
            )
            await callback.answer("Marked as Pass")
        else:
            await callback.answer("Unable to update.", show_alert=True)
        return

    if action == "fail":
        await callback.message.edit_reply_markup(
            reply_markup=fail_reason_keyboard(driver_id=driver.id, date=callback_data.date)
        )
        await callback.answer("Choose fail reason")
        return

    if action == "fix":
        updated = await checks.update_review_status(
            db,
            driver_id=driver.id,
            group_id=group.id,
            check_date=check_date,
            status="needs_fix",
            reviewer_user_id=callback.from_user.id,
            reason="Needs fix",
        )
        if updated:
            checkin = updated
            await _notify_driver(
                bot,
                driver.telegram_user_id,
                "Dispatcher needs you to address today’s check-in and resubmit.",
            )
            await scheduler.cancel_followups(checkin.id)
            await checks.sync_review_card(
                bot,
                db,
                group_id=group.id,
                thread_id=group.rolling_topic_id,
                driver=driver,
                checkin=checkin,
            )
            await callback.answer("Marked Needs Fix")
        else:
            await callback.answer("Unable to update.", show_alert=True)
        return

    if action == "notify":
        if checkin.sent_at and checkin.sent_at.astimezone(timezone).date() == check_date:
            await callback.answer("Already notified today.", show_alert=True)
            return
        checkin = await checks.mark_notified(db, checkin.id)
        target_chat = await checks.send_driver_notification(
            bot,
            driver=driver,
            checkin=checkin,
            check_date=check_date,
        )
        await checks.sync_review_card(
            bot,
            db,
            group_id=group.id,
            thread_id=group.rolling_topic_id,
            driver=driver,
            checkin=checkin,
        )
        await scheduler.schedule_followups(
            checkin_id=checkin.id,
            group=group,
            driver=driver,
            target_chat_id=target_chat,
        )
        await callback.answer("Reminder sent")
        return

    if action == "refresh":
        latest = await checks.fetch_checkin(
            db,
            driver_id=driver.id,
            group_id=group.id,
            check_date=check_date,
        )
        if latest:
            checkin = latest
        await checks.sync_review_card(
            bot,
            db,
            group_id=group.id,
            thread_id=group.rolling_topic_id,
            driver=driver,
            checkin=checkin,
        )
        await callback.answer("Refreshed")
        return

    await callback.answer()


@router.callback_query(FailReasonChoice.filter())
async def handle_fail_reason(
    callback: CallbackQuery,
    callback_data: FailReasonChoice,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    message = callback.message
    if not message or not message.chat or message.message_thread_id is None:
        await callback.answer()
        return

    group = await _guard_dispatcher_callback(
        callback,
        bot=bot,
        db=db,
        config=config,
        require_admin=config.admin_only_review,
    )
    if group is None:
        return
    if group.paused:
        await callback.answer("Paused for this group. Rename to resume.", show_alert=True)
        return

    driver = await checks.find_driver_by_id(db, callback_data.driver_id)
    if driver is None:
        await callback.answer("Driver missing", show_alert=True)
        return

    reason_label = FAIL_REASONS.get(callback_data.reason, "Other")
    check_date = date.fromisoformat(callback_data.date)

    updated = await checks.update_review_status(
        db,
        driver_id=driver.id,
        group_id=group.id,
        check_date=check_date,
        status="fail",
        reviewer_user_id=callback.from_user.id,
        reason=reason_label,
    )
    if updated is None:
        await callback.answer("Unable to update.", show_alert=True)
        return

    await _notify_driver(
        bot,
        driver.telegram_user_id,
        f"Daily Check failed: {reason_label}. Dispatcher will follow up.",
    )
    await scheduler.cancel_followups(updated.id)
    await checks.sync_review_card(
        bot,
        db,
        group_id=group.id,
        thread_id=group.rolling_topic_id,
        driver=driver,
        checkin=updated,
    )
    await callback.answer("Marked Fail")


async def _guard_dispatcher_callback(
    callback: CallbackQuery,
    *,
    bot: Bot,
    db: Database,
    config: Settings,
    require_admin: bool,
) -> Optional[roles.GroupSettings]:
    message = callback.message
    if message is None or message.chat is None or message.message_thread_id is None:
        await callback.answer()
        return None
    try:
        return await roles.ensure_dispatcher_context(
            bot=bot,
            db=db,
            chat_id=message.chat.id,
            message_thread_id=message.message_thread_id,
            user_id=callback.from_user.id,
            require_admin=require_admin,
        )
    except roles.DispatcherGroupNotConfigured:
        await callback.answer("Dispatcher group not configured.", show_alert=True)
    except roles.InvalidDispatcherContext:
        await callback.answer("Wrong topic.", show_alert=True)
    except roles.UnauthorizedDispatcher:
        await callback.answer("Access denied.", show_alert=True)
    return None


async def _notify_driver(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception:  # pragma: no cover - defensive
        pass
