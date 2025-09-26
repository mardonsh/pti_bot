from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class ComplianceSummaryModel(BaseModel):
    total_drivers: int
    pass_count: int
    pending_count: int
    last_reset_at: Optional[datetime]
    generated_at: datetime


class PendingDriverModel(BaseModel):
    driver_id: int
    username: Optional[str]
    full_name: Optional[str]
    notify_chat_id: Optional[int]
    check_date: date
    status: str
    pass_count_7d: int
    last_notification: Optional[datetime]


class DriverCheckinModel(BaseModel):
    id: int
    date: date
    status: str
    sent_at: Optional[datetime]
    responded_at: Optional[datetime]
    reviewed_at: Optional[datetime]
    reason: Optional[str]
    group_id: int
    updated_at: datetime


class DriverCheckinsResponse(BaseModel):
    driver_id: int
    username: Optional[str]
    full_name: Optional[str]
    checkins: list[DriverCheckinModel]
