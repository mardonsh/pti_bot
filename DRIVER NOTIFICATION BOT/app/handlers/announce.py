from __future__ import annotations

import re
from typing import Optional

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.db import Database
from app.keyboards import AnnounceAction, announce_audience_keyboard, announce_confirm_keyboard
from app.services import checks, roles


router = Router(name="announce")

AUDIENCE_VALUES = {"all", "drivers", "dispatch"}


class AnnounceStates(StatesGroup):
    choosing_audience = State()
    waiting_text = State()


@router.message(Command("announce"))
async def handle_announce(
    message: Message,
    bot: Bot,
    db: Database,
    config: Settings,
    state: FSMContext,
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

    args = (message.text or "").split(maxsplit=1)
    inline_args = ""
    if len(args) > 1:
        inline_args = args[1]

    if inline_args:
        parsed = _parse_inline_announce(inline_args)
        if not parsed:
            await message.reply(
                "Inline usage: /announce audience:<all|drivers|dispatch> text: Your message"
            )
            return
        audience, text_value = parsed
        if group.paused and audience in {"all", "drivers"}:
            await message.reply("Paused—driver DMs disabled.")
            return
        await _send_announcement(
            bot=bot,
            db=db,
            group=group,
            author=message.from_user.full_name,
            audience=audience,
            text=text_value,
        )
        await message.reply("Announcement sent.")
        return

    await state.set_state(AnnounceStates.choosing_audience)
    await state.update_data(group_id=group.id, thread_id=group.rolling_topic_id, initiator=message.from_user.id)
    await message.reply("Choose audience:", reply_markup=announce_audience_keyboard())


@router.callback_query(AnnounceAction.filter())
async def handle_announce_callbacks(
    callback: CallbackQuery,
    callback_data: AnnounceAction,
    bot: Bot,
    db: Database,
    config: Settings,
    state: FSMContext,
) -> None:
    message = callback.message
    if not message or not callback.from_user or not message.chat:
        await callback.answer()
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

    current_state = await state.get_state()
    data = await state.get_data()

    initiator = data.get("initiator")
    if initiator and initiator != callback.from_user.id:
        await callback.answer("Another dispatcher is editing this announcement.", show_alert=True)
        return

    if callback_data.step == "audience":
        audience = callback_data.value or ""
        if audience not in AUDIENCE_VALUES:
            await callback.answer()
            return
        if group.paused and audience in {"all", "drivers"}:
            await callback.answer("Paused—driver DMs disabled.", show_alert=True)
            return
        await state.update_data(audience=audience)
        await state.set_state(AnnounceStates.waiting_text)
        await callback.message.answer("Send announcement text.")
        await callback.answer()
        return

    if callback_data.step == "confirm" and current_state == AnnounceStates.waiting_text:
        if callback_data.value == "yes":
            audience = data.get("audience")
            text_value = data.get("text")
            if not audience or not text_value:
                await callback.answer("Missing data", show_alert=True)
                return
            if group.paused and audience in {"all", "drivers"}:
                await callback.answer("Paused—driver DMs disabled.", show_alert=True)
                return
            await _send_announcement(
                bot=bot,
                db=db,
                group=group,
                author=callback.from_user.full_name,
                audience=audience,
                text=text_value,
            )
            await callback.answer("Announcement sent")
        else:
            await callback.answer("Cancelled")
        await state.clear()
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    await callback.answer()


@router.message(AnnounceStates.waiting_text)
async def capture_announcement_text(
    message: Message,
    bot: Bot,
    db: Database,
    config: Settings,
    state: FSMContext,
) -> None:
    group = await _guard_dispatcher_message(
        message,
        bot=bot,
        db=db,
        config=config,
        require_admin=config.admin_only_review,
    )
    if group is None:
        await state.clear()
        return
    data = await state.get_data()
    initiator = data.get("initiator")
    if initiator and initiator != message.from_user.id:
        await message.reply("Another dispatcher is completing this announcement.")
        return
    if message.chat.id != group.id or message.message_thread_id != group.rolling_topic_id:
        await message.reply("Use this inside the dispatcher topic.")
        return
    text_value = (message.text or "").strip()
    if not text_value:
        await message.reply("Please send text for the announcement.")
        return
    await state.update_data(text=text_value)
    preview = f"Preview:\n{text_value}\n\nSend?"
    await message.reply(preview, reply_markup=announce_confirm_keyboard())


async def _send_announcement(
    *,
    bot: Bot,
    db: Database,
    group: roles.GroupSettings,
    author: str,
    audience: str,
    text: str,
) -> None:
    dispatch_text = f"Announcement from {author}:\n{text}"
    if audience in {"all", "dispatch"}:
        await bot.send_message(
            chat_id=group.id,
            text=dispatch_text,
            message_thread_id=group.rolling_topic_id,
        )

    if audience in {"all", "drivers"}:
        drivers = await checks.list_active_drivers(db)
        for driver in drivers:
            try:
                await bot.send_message(driver.telegram_user_id, text)
            except Exception:  # pragma: no cover - defensive
                continue


def _parse_inline_announce(args: str) -> Optional[tuple[str, str]]:
    match = re.search(r"audience:(\w+)\s+text:(.+)", args, flags=re.IGNORECASE)
    if not match:
        return None
    audience = match.group(1).lower()
    if audience not in AUDIENCE_VALUES:
        return None
    text_value = match.group(2).strip()
    if not text_value:
        return None
    return audience, text_value


async def _guard_dispatcher_message(
    message: Message,
    *,
    bot: Bot,
    db: Database,
    config: Settings,
    require_admin: bool,
) -> Optional[roles.GroupSettings]:
    if message.chat is None or message.message_thread_id is None:
        await message.reply("Use this in the dispatcher topic.")
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
        await message.reply("Dispatcher group not configured.")
    except roles.InvalidDispatcherContext:
        await message.reply("Wrong topic.")
    except roles.UnauthorizedDispatcher:
        await message.reply("Dispatcher permissions required.")
    return None
