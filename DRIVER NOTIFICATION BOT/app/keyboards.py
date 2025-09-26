from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

from typing import Optional, Sequence, Tuple


class DriverAction(CallbackData, prefix="drv"):
    action: str
    checkin_id: int


class DriverSkipChoice(CallbackData, prefix="drs"):
    reason: str
    checkin_id: int


class ReviewAction(CallbackData, prefix="rev"):
    action: str
    driver_id: int
    date: str  # YYYY-MM-DD


class FailReasonChoice(CallbackData, prefix="frc"):
    reason: str
    driver_id: int
    date: str


class AnnounceAction(CallbackData, prefix="ann"):
    step: str
    value: Optional[str] = None


class ComplianceAction(CallbackData, prefix="cmp"):
    action: str
    driver_id: int
    date: str


SKIP_REASONS = {
    "off": "Off today",
    "shop": "In shop",
    "no_trailer": "No trailer",
    "shipper": "Already at shipper",
    "other": "Other",
}


FAIL_REASONS = {
    "low_tire": "Low tire",
    "abs_lamp": "ABS lamp",
    "air_leak": "Air leak",
    "lights": "Lights",
    "equipment": "Missing extinguisher/triangles",
    "other": "Other",
}


def driver_dm_keyboard(checkin_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Confirm",
        callback_data=DriverAction(action="confirm", checkin_id=checkin_id).pack(),
    )
    builder.button(
        text="Skip (reason)",
        callback_data=DriverAction(action="skip", checkin_id=checkin_id).pack(),
    )
    builder.adjust(1, 1)
    return builder.as_markup()


def driver_skip_keyboard(checkin_id: int):
    builder = InlineKeyboardBuilder()
    for key, label in SKIP_REASONS.items():
        builder.button(
            text=label,
            callback_data=DriverSkipChoice(reason=key, checkin_id=checkin_id).pack(),
        )
    builder.adjust(1)
    return builder.as_markup()


def review_keyboard(driver_id: int, date: str, *, notified: bool, terminal: bool):
    builder = InlineKeyboardBuilder()
    if not terminal:
        builder.button(
            text="Pass ✅",
            callback_data=ReviewAction(action="pass", driver_id=driver_id, date=date).pack(),
        )
        builder.button(
            text="Fail ❌",
            callback_data=ReviewAction(action="fail", driver_id=driver_id, date=date).pack(),
        )
        builder.button(
            text="Needs Fix 🛠️",
            callback_data=ReviewAction(action="fix", driver_id=driver_id, date=date).pack(),
        )
        builder.adjust(3)
    if not notified:
        builder.button(
            text="Notify Today",
            callback_data=ReviewAction(action="notify", driver_id=driver_id, date=date).pack(),
        )
        builder.adjust(1)
    builder.button(
        text="Refresh",
        callback_data=ReviewAction(action="refresh", driver_id=driver_id, date=date).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


def fail_reason_keyboard(driver_id: int, date: str):
    builder = InlineKeyboardBuilder()
    for key, label in FAIL_REASONS.items():
        builder.button(
            text=label,
            callback_data=FailReasonChoice(reason=key, driver_id=driver_id, date=date).pack(),
        )
    builder.adjust(1)
    return builder.as_markup()


def announce_audience_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="All", callback_data=AnnounceAction(step="audience", value="all").pack())
    builder.button(text="Drivers", callback_data=AnnounceAction(step="audience", value="drivers").pack())
    builder.button(text="Dispatch", callback_data=AnnounceAction(step="audience", value="dispatch").pack())
    builder.adjust(1)
    return builder.as_markup()


def announce_confirm_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Confirm", callback_data=AnnounceAction(step="confirm", value="yes").pack())
    builder.button(text="Cancel", callback_data=AnnounceAction(step="confirm", value="no").pack())
    builder.adjust(2)
    return builder.as_markup()


def compliance_keyboard(driver_id: int, date_str: str):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Pass ✅",
        callback_data=ComplianceAction(action="pass", driver_id=driver_id, date=date_str).pack(),
    )
    builder.button(
        text="Comment 📝",
        callback_data=ComplianceAction(action="comment", driver_id=driver_id, date=date_str).pack(),
    )
    builder.adjust(2)
    return builder.as_markup()
