from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db import Database
from app.services import checks, compliance, digest, streaks
from app.services import roles


logger = logging.getLogger(__name__)

FOLLOWUP_DELAYS = (timedelta(minutes=15), timedelta(minutes=50))


class SchedulerService:
    def __init__(self, *, scheduler: AsyncIOScheduler, bot: Bot, db: Database) -> None:
        self.scheduler = scheduler
        self.bot = bot
        self.db = db
        self._followup_jobs: Dict[int, List[str]] = {}

    async def initialize(self) -> None:
        records = await self.db.fetch("SELECT * FROM groups")
        for record in records:
            group = roles.GroupSettings.from_record(record)
            self._schedule_group(group)

    async def refresh_group(self, group_id: int) -> None:
        await self._remove_group_jobs(group_id)
        group = await roles.fetch_group(self.db, group_id)
        if group:
            self._schedule_group(group)

    def _schedule_group(self, group: roles.GroupSettings) -> None:
        timezone = ZoneInfo(group.tz)
        digest_trigger = CronTrigger(
            hour=group.digest_time.hour,
            minute=group.digest_time.minute,
            timezone=timezone,
        )
        self.scheduler.add_job(
            self._run_digest_job,
            trigger=digest_trigger,
            id=_job_id("digest", group.id),
            args=[group.id],
            replace_existing=True,
        )

        reset_trigger = CronTrigger(hour=0, minute=5, timezone=timezone)
        self.scheduler.add_job(
            self._run_midnight_reset,
            trigger=reset_trigger,
            id=_job_id("reset", group.id),
            args=[group.id],
            replace_existing=True,
        )

        if group.autosend_enabled and group.autosend_time:
            trigger = CronTrigger(
                hour=group.autosend_time.hour,
                minute=group.autosend_time.minute,
                timezone=timezone,
            )
            self.scheduler.add_job(
                self._run_autosend_job,
                trigger=trigger,
                id=_job_id("autosend", group.id),
                args=[group.id],
                replace_existing=True,
            )

        if group.compliance_topic_id:
            compliance_trigger = CronTrigger(minute=0, hour='*/2', timezone=timezone)
            self.scheduler.add_job(
                self._run_compliance_job,
                trigger=compliance_trigger,
                id=_job_id("compliance", group.id),
                args=[group.id],
                replace_existing=True,
            )

            weekly_trigger = CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=timezone)
            self.scheduler.add_job(
                self._run_weekly_leaderboard,
                trigger=weekly_trigger,
                id=_job_id("weekly", group.id),
                args=[group.id],
                replace_existing=True,
            )

    async def schedule_followups(
        self,
        *,
        checkin_id: int,
        group: roles.GroupSettings,
        driver: checks.Driver,
        target_chat_id: int,
    ) -> None:
        await self.cancel_followups(checkin_id)
        timezone = ZoneInfo(group.tz)
        now = datetime.now(tz=timezone)
        jobs: List[str] = []
        for idx, delay in enumerate(FOLLOWUP_DELAYS, start=1):
            run_date = now + delay
            job_id = f"followup:{checkin_id}:{idx}"
            self.scheduler.add_job(
                self._run_followup_job,
                trigger='date',
                run_date=run_date,
                id=job_id,
                args=[checkin_id, group.id, group.rolling_topic_id, driver.id, target_chat_id, idx, group.tz],
                replace_existing=True,
            )
            jobs.append(job_id)
        if jobs:
            self._followup_jobs[checkin_id] = jobs

    async def cancel_followups(self, checkin_id: int) -> None:
        job_ids = self._followup_jobs.pop(checkin_id, [])
        for job_id in job_ids:
            job = self.scheduler.get_job(job_id)
            if job:
                job.remove()

    async def _run_followup_job(
        self,
        checkin_id: int,
        group_id: int,
        thread_id: int,
        driver_id: int,
        target_chat_id: int,
        slot: int,
        tz_name: str,
    ) -> None:
        job_id = f"followup:{checkin_id}:{slot}"
        jobs = self._followup_jobs.get(checkin_id)
        if jobs and job_id in jobs:
            jobs.remove(job_id)
            if not jobs:
                self._followup_jobs.pop(checkin_id, None)
        try:
            checkin = await checks.fetch_checkin_by_id(self.db, checkin_id)
            if not checkin:
                await self.cancel_followups(checkin_id)
                return
            if checkin.status not in {"pending", "submitted"} or checkin.responded_at:
                await self.cancel_followups(checkin_id)
                return
            driver = await checks.find_driver_by_id(self.db, driver_id)
            group = await roles.fetch_group(self.db, group_id)
            if not driver or not group:
                await self.cancel_followups(checkin_id)
                return
            if target_chat_id < 0:
                try:
                    chat = await self.bot.get_chat(target_chat_id)
                    if roles.is_driver_chat_paused(chat):
                        logger.info(
                            "Follow-up skipped for driver %s (chat paused)", driver_id
                        )
                        await self.cancel_followups(checkin_id)
                        return
                except Exception:  # pragma: no cover - defensive
                    logger.exception("Failed to inspect chat %s", target_chat_id)
                    await self.cancel_followups(checkin_id)
                    return
            timezone = ZoneInfo(tz_name)
            await checks.send_driver_notification(
                self.bot,
                driver=driver,
                checkin=checkin,
                check_date=checkin.date,
                chat_id=target_chat_id,
            )
            await self.bot.send_message(
                chat_id=group.id,
                message_thread_id=thread_id,
                text=(f"Follow-up {slot}/2: {driver.mention} still pending. Reminder sent."),
                disable_notification=True,
            )
            await checks.sync_review_card(
                self.bot,
                self.db,
                group_id=group.id,
                thread_id=thread_id,
                driver=driver,
                checkin=checkin,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Follow-up job failed for checkin %s slot %s", checkin_id, slot)

    async def _remove_group_jobs(self, group_id: int) -> None:
        for kind in ("digest", "reset", "autosend", "compliance", "weekly"):
            job_id = _job_id(kind, group_id)
            job = self.scheduler.get_job(job_id)
            if job:
                job.remove()

    async def _run_autosend_job(self, group_id: int) -> None:
        group = await roles.fetch_group(self.db, group_id)
        if not group or not group.autosend_enabled or not group.autosend_time:
            return
        group = await roles.refresh_group_pause(bot=self.bot, db=self.db, group=group)
        if group.paused:
            logger.info("Autosend skipped for group %s (paused)", group_id)
            return

        timezone = ZoneInfo(group.tz)
        today = datetime.now(tz=timezone).date()
        drivers = await checks.list_active_drivers(self.db)

        for driver in drivers:
            try:
                if driver.notify_chat_id:
                    try:
                        chat = await self.bot.get_chat(driver.notify_chat_id)
                        if roles.is_driver_chat_paused(chat):
                            logger.info(
                                "Autosend skipped for driver %s (chat paused)", driver.id
                            )
                            continue
                    except Exception:  # pragma: no cover - defensive
                        logger.exception(
                            "Failed to inspect chat %s", driver.notify_chat_id
                        )
                checkin = await checks.ensure_checkin(
                    self.db,
                    driver_id=driver.id,
                    group_id=group.id,
                    check_date=today,
                )
                if checkin.sent_at and checkin.sent_at.astimezone(timezone).date() == today:
                    continue
                if checkin.media_count > 0 or checkin.status not in {"pending", "submitted"}:
                    await self.cancel_followups(checkin.id)
                    checkin = await checks.reset_checkin(self.db, checkin.id)
                checkin = await checks.mark_notified(self.db, checkin.id)
                target_chat = await checks.send_driver_notification(
                    self.bot,
                    driver=driver,
                    checkin=checkin,
                    check_date=today,
                )
                await checks.sync_review_card(
                    self.bot,
                    self.db,
                    group_id=group.id,
                    thread_id=group.rolling_topic_id,
                    driver=driver,
                    checkin=checkin,
                )
                await self.schedule_followups(
                    checkin_id=checkin.id,
                    group=group,
                    driver=driver,
                    target_chat_id=target_chat,
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to autosend check for driver %s", driver.id)

    async def _run_digest_job(self, group_id: int) -> None:
        group = await roles.fetch_group(self.db, group_id)
        if not group:
            return
        group = await roles.refresh_group_pause(bot=self.bot, db=self.db, group=group)
        if group.paused:
            logger.info("Digest skipped for group %s (paused)", group_id)
            return

        timezone = ZoneInfo(group.tz)
        today = datetime.now(tz=timezone).date()
        try:
            await digest.send_daily_digest(
                bot=self.bot,
                db=self.db,
                group_id=group.id,
                thread_id=group.rolling_topic_id,
                check_date=today,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to post digest for group %s", group.id)

    async def _run_midnight_reset(self, group_id: int) -> None:
        group = await roles.fetch_group(self.db, group_id)
        if not group:
            return
        timezone = ZoneInfo(group.tz)
        today = datetime.now(tz=timezone).date()
        target_date = today - timedelta(days=1)
        try:
            await streaks.reset_missed_checks(self.db, group_id=group.id, check_date=target_date)
            if group.compliance_topic_id:
                try:
                    await compliance.send_daily_snapshot(
                        bot=self.bot,
                        db=self.db,
                        group=group,
                        target_date=target_date,
                    )
                except Exception:  # pragma: no cover
                    logger.exception("Failed compliance snapshot for group %s", group.id)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed midnight reset for group %s", group.id)

    async def _run_compliance_job(self, group_id: int) -> None:
        group = await roles.fetch_group(self.db, group_id)
        if not group or not group.compliance_topic_id:
            return
        group = await roles.refresh_group_pause(bot=self.bot, db=self.db, group=group)
        if group.paused:
            logger.info("Compliance report skipped for group %s (paused)", group_id)
            return
        try:
            await compliance.send_hourly_report(bot=self.bot, db=self.db, group=group)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed compliance report for group %s", group.id)

    async def _run_weekly_leaderboard(self, group_id: int) -> None:
        group = await roles.fetch_group(self.db, group_id)
        if not group or not group.compliance_topic_id:
            return
        group = await roles.refresh_group_pause(bot=self.bot, db=self.db, group=group)
        if group.paused:
            return
        timezone = ZoneInfo(group.tz)
        today = datetime.now(tz=timezone).date()
        try:
            await compliance.send_weekly_leaderboard(
                bot=self.bot,
                db=self.db,
                group=group,
                end_date=today,
            )
        except Exception:  # pragma: no cover
            logger.exception("Failed weekly leaderboard for group %s", group.id)


def _job_id(kind: str, group_id: int) -> str:
    return f"{kind}:{group_id}"
