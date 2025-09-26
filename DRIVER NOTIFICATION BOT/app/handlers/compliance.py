from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Router, F
from aiogram.types import CallbackQuery, ForceReply, Message
from aiogram.exceptions import TelegramBadRequest

from app.config import Settings
from app.db import Database
from app.keyboards import ComplianceAction
from app.services import checks, compliance, roles, streaks
from app.services.autosend import SchedulerService


router = Router(name="compliance")


@router.callback_query(ComplianceAction.filter())
async def handle_compliance_action(
    callback: CallbackQuery,
    callback_data: ComplianceAction,
    bot: Bot,
    db: Database,
    config: Settings,
    scheduler: SchedulerService,
) -> None:
    group = await roles.fetch_default_group(db)
    if group is None:
        await callback.answer("Dispatcher group not configured.", show_alert=True)
        return

    try:
        await roles.ensure_dispatcher_user(
            bot=bot,
            group=group,
            user_id=callback.from_user.id,
            require_admin=config.admin_only_review,
        )
    except roles.UnauthorizedDispatcher:
        await callback.answer("Dispatcher permissions required.", show_alert=True)
        return

    if callback_data.action == "pass":
        await _handle_compliance_pass(
            callback=callback,
            callback_data=callback_data,
            bot=bot,
            db=db,
            scheduler=scheduler,
            group=group,
        )
        return

    if callback_data.action == "comment":
        await _handle_compliance_comment(
            callback=callback,
            callback_data=callback_data,
            bot=bot,
            db=db,
            group=group,
        )
        return

    await callback.answer()


async def _handle_compliance_pass(
    *,
    callback: CallbackQuery,
    callback_data: ComplianceAction,
    bot: Bot,
    db: Database,
    scheduler: SchedulerService,
    group: roles.GroupSettings,
) -> None:
    driver = await checks.find_driver_by_id(db, callback_data.driver_id)
    if driver is None:
        await callback.answer("Driver not found.", show_alert=True)
        return

    check_date = date.fromisoformat(callback_data.date)
    timezone = ZoneInfo(group.tz)

    detail_message = callback.message

    await checks.ensure_checkin(
        db,
        driver_id=driver.id,
        group_id=group.id,
        check_date=check_date,
    )

    updated = await checks.update_review_status(
        db,
        driver_id=driver.id,
        group_id=group.id,
        check_date=check_date,
        status="pass",
        reviewer_user_id=callback.from_user.id,
        reason=None,
    )

    if updated is None:
        await callback.answer("No record to update.", show_alert=True)
        return

    await streaks.update_after_pass(db, driver_id=driver.id, check_date=check_date)

    driver = await checks.find_driver_by_id(db, driver.id) or driver
    await scheduler.cancel_followups(updated.id)

    if detail_message and detail_message.chat:
        base_lines = (detail_message.text or "").splitlines()
        if base_lines:
            base_lines[0] = "✅ Completed PTI"
            if base_lines[-1].strip().lower() != "(reviewed)":
                base_lines.append("(reviewed)")
            new_text = "\n".join(base_lines)
        else:
            new_text = "✅ Completed PTI\n(reviewed)"
        try:
            await bot.edit_message_text(
                chat_id=detail_message.chat.id,
                message_id=detail_message.message_id,
                text=new_text,
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass

    await checks.sync_review_card(
        bot,
        db,
        group_id=group.id,
        thread_id=group.rolling_topic_id,
        driver=driver,
        checkin=updated,
    )

    reviewed_at = updated.reviewed_at or datetime.now(tz=timezone)
    await compliance.handle_pass_event(
        bot=bot,
        db=db,
        group=group,
        driver=driver,
        reviewed_at=reviewed_at,
    )

    await callback.answer("Marked as Pass")


async def _handle_compliance_comment(
    *,
    callback: CallbackQuery,
    callback_data: ComplianceAction,
    bot: Bot,
    db: Database,
    group: roles.GroupSettings,
) -> None:
    driver = await checks.find_driver_by_id(db, callback_data.driver_id)
    if driver is None:
        await callback.answer("Driver not found.", show_alert=True)
        return

    message = callback.message
    if not message or not message.chat:
        await callback.answer()
        return

    prompt = await bot.send_message(
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
        text=f"Comment for {driver.mention} — reply with details.",
        reply_markup=ForceReply(selective=True),
    )
    await compliance.store_comment_prompt(db, driver_id=driver.id, message_id=prompt.message_id)
    await callback.answer("Reply with your comment.")


@router.message(F.reply_to_message)
async def handle_comment_reply(
    message: Message,
    db: Database,
) -> None:
    if not message.reply_to_message:
        return
    group = await roles.fetch_default_group(db)
    if group is None or not message.chat or message.chat.id != group.id:
        return
    driver_id = await compliance.resolve_comment_prompt(db, message_id=message.reply_to_message.message_id)
    if driver_id is None:
        return
    driver = await checks.find_driver_by_id(db, driver_id)
    if driver is None:
        return
    note = message.text or message.caption
    if not note:
        await message.reply("Only text comments are supported.")
        return

    await compliance.record_comment(
        db,
        driver_id=driver.id,
        author_id=message.from_user.id if message.from_user else 0,
        note=note,
    )
    await message.reply("Comment saved.")
