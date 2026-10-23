"""Daily broadcasts: each admin can have several recurring jobs."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

from telegram_manager.constants import (
    BULK_DELAY_MAX,
    DRIFT_MAX,
    MAX_DAILY_JOBS,
    MAX_TIMES_PER_DAY,
    OPEN_CHAT_MIN_DELAY,
)
from telegram_manager.core.models import DailyJob
from telegram_manager.parsing import format_clock

logger = logging.getLogger(__name__)


class SchedulerMixin:
    """Every owner/admin can run several daily broadcasts at once (slots 1..MAX_DAILY_JOBS)."""

    def daily_jobs_for(self, owner_id: int) -> list[tuple[int, DailyJob]]:
        """[(slot, job)] of this admin, slot 1 first."""
        return sorted(((slot, entry[0]) for (owner, slot), entry in self._daily.items() if owner == owner_id),
                      key=lambda item: item[0])

    def daily_job_for(self, owner_id: int, slot: int | None = None) -> DailyJob | None:
        """One job: the given slot, or (no slot) the admin's first one."""
        if slot is not None:
            entry = self._daily.get((owner_id, slot))
            return entry[0] if entry else None
        jobs = self.daily_jobs_for(owner_id)
        return jobs[0][1] if jobs else None

    def _describe_job(self, job: DailyJob) -> str:
        accounts = "all accounts" if job.account_ids is None else f"{len(job.account_ids)} chosen account(s)"
        buttons = f"Buttons to click: {' → '.join(job.buttons)}\n" if job.buttons else ""
        if job.drift_max_seconds > job.drift_seconds:
            drift = f"random +{job.drift_seconds}-{job.drift_max_seconds} sec later every day"
        else:
            drift = f"+{job.drift_seconds} sec later every day" if job.drift_seconds else "same time every day"
        return (
            f"Target: {job.target}\n"
            f"Message: {job.text}\n"
            f"{buttons}"
            f"Accounts: {accounts}\n"
            f"Delay between accounts: {job.delay_seconds}s\n"
            f"Starts daily at: {format_clock(job.start_time)} ({self.tz.key})\n"
            f"Daily shift: {drift}\n"
            f"Times per day: {job.times_per_day}"
        )

    def daily_job_status(self, owner_id: int, slot: int | None = None) -> str:
        if slot is not None:
            job = self.daily_job_for(owner_id, slot)
            return self._describe_job(job) if job else "That daily broadcast does not exist."
        jobs = self.daily_jobs_for(owner_id)
        if not jobs:
            return "No daily broadcast is scheduled."
        if len(jobs) == 1:
            return self._describe_job(jobs[0][1])
        return "\n\n".join(f"⏰ Broadcast #{number}\n{self._describe_job(job)}" for number, job in jobs)

    def free_daily_slot(self, owner_id: int) -> int | None:
        used = {number for number, _ in self.daily_jobs_for(owner_id)}
        return next((n for n in range(1, MAX_DAILY_JOBS + 1) if n not in used), None)

    async def start_daily_job(self, owner_id: int, job: DailyJob, slot: int | None = None) -> int:
        """Schedule a recurring broadcast. slot=None takes the next free slot; a given slot is
        replaced. Saved to Firestore so it survives restarts. Returns the slot used."""
        if slot is None:
            slot = self.free_daily_slot(owner_id)
            if slot is None:
                raise ValueError(f"You already have {MAX_DAILY_JOBS} daily broadcasts. Delete one first.")
        elif not 1 <= slot <= MAX_DAILY_JOBS:
            raise ValueError("Unknown broadcast number")
        job.delay_seconds = max(OPEN_CHAT_MIN_DELAY, min(int(job.delay_seconds), BULK_DELAY_MAX))
        job.times_per_day = max(1, min(int(job.times_per_day), MAX_TIMES_PER_DAY))
        job.drift_seconds = max(0, min(int(job.drift_seconds), DRIFT_MAX))
        job.drift_max_seconds = max(job.drift_seconds, min(int(job.drift_max_seconds or 0), DRIFT_MAX))
        shifting = bool(job.drift_seconds or job.drift_max_seconds)
        job.anchor_date = self._anchor_for(job) if shifting else None  # day 0 = the first real run
        # Save first: if Firestore fails, the job that is already running is left alone.
        await self.store.jobs.save(owner_id, job.to_dict(), slot)
        self._run_daily_job(owner_id, slot, job)
        return slot

    async def stop_daily_job(self, owner_id: int, slot: int | None = None) -> bool:
        """Cancel and delete one broadcast, or (slot=None) all of this admin's. True if any was scheduled."""
        keys = [key for key in self._daily if key[0] == owner_id and (slot is None or key[1] == slot)]
        for key in keys:
            self._cancel_daily(key)
        await self.store.jobs.delete(owner_id, slot)
        return bool(keys)

    async def detach_account_from_jobs(self, owner_id: int, account_id: str) -> None:
        """An account left this admin: drop it from their chosen-account broadcasts (and stop
        a broadcast that would be left with no account)."""
        for slot, job in self.daily_jobs_for(owner_id):
            if job.account_ids is None or account_id not in job.account_ids:
                continue
            remaining = [item for item in job.account_ids if item != account_id]
            if remaining:
                job.account_ids = remaining
                await self.store.jobs.save(owner_id, job.to_dict(), slot)
            else:
                await self.stop_daily_job(owner_id, slot)
                await self.notify(owner_id, f"⚠️ Daily broadcast #{slot} was stopped: its only account moved to someone else.")

    async def restore_daily_jobs(self) -> None:
        try:
            rows = await self.store.jobs.list_all()
        except Exception:
            logger.exception("Could not restore the daily jobs")
            return
        for owner_id, slot, data in rows:
            try:
                job = DailyJob.from_dict(data)
                if (job.drift_seconds or job.drift_max_seconds) and not job.anchor_date:
                    job.anchor_date = datetime.now(self.tz).date().isoformat()
                self._run_daily_job(owner_id, slot, job)
                logger.info("Restored a daily job from Firestore")
            except Exception:
                logger.exception("Could not restore one daily job")

    def _run_daily_job(self, owner_id: int, slot: int, job: DailyJob) -> None:
        key = (owner_id, slot)
        self._cancel_daily(key)
        self._daily[key] = (job, asyncio.create_task(self._daily_job_loop(owner_id, slot, job)))

    def _cancel_daily(self, key: tuple[int, int]) -> None:
        entry = self._daily.pop(key, None)
        if entry:
            entry[1].cancel()

    def _cancel_all_daily(self) -> None:
        for key in list(self._daily):
            self._cancel_daily(key)

    def _anchor_for(self, job: DailyJob) -> str:
        """Day 0 of the drift = the day of the first real run: today if a run is still ahead today, else tomorrow."""
        now = datetime.now(self.tz)
        job.anchor_date = now.date().isoformat()
        first = self._next_run(job, now)
        return (now.date() if first.date() == now.date() else now.date() + timedelta(days=1)).isoformat()

    def _next_run(self, job: DailyJob, now: datetime) -> datetime:
        """Next scheduled run after `now`. A day's runs start at job.start_time, plus drift_seconds for every
        day since the first one, and are spaced 24h / times_per_day apart. Times wrap inside the calendar day,
        so the broadcast still goes out once per day (times_per_day times) however large the drift grows."""
        step = 86400 / job.times_per_day
        base = job.start_time.hour * 3600 + job.start_time.minute * 60 + job.start_time.second
        anchor = date.fromisoformat(job.anchor_date) if job.anchor_date else None
        candidates = []
        for day_offset in (-1, 0, 1):
            day = now.date() + timedelta(days=day_offset)
            days_since = max(0, (day - anchor).days) if anchor else 0
            start = base + job.shift_total(days_since)
            midnight = datetime.combine(day, dtime(0, 0), tzinfo=self.tz)
            for slot in range(job.times_per_day):
                offset = (start + slot * step) % 86400 if (job.drift_seconds or job.drift_max_seconds or anchor) else base + slot * step
                candidates.append(midnight + timedelta(seconds=offset))
        return min(c for c in candidates if c.timestamp() > now.timestamp())

    async def _broadcast(self, owner_id: int, job: DailyJob):
        """One round: from every account, or only from the ones picked for this broadcast."""
        extra = {"buttons": job.buttons} if job.buttons else {}
        if job.account_ids is None:
            return await self.send_all_any(owner_id, job.target, job.text, job.delay_seconds, **extra)
        return await self.send_selected(owner_id, job.account_ids, job.target, job.text, job.delay_seconds, True, **extra)

    async def _daily_job_loop(self, owner_id: int, slot: int, job: DailyJob) -> None:
        while True:
            try:
                now = datetime.now(self.tz)
                next_run = self._next_run(job, now)
                logger.info("Daily job #%s: next broadcast at %s", slot, next_run.isoformat(timespec="minutes"))
                await asyncio.sleep(max(0.0, next_run.timestamp() - now.timestamp()))
                logger.info("Daily job: broadcasting")
                result = await self._broadcast(owner_id, job)
                if result.total == 0:
                    await self.notify(owner_id, f"⚠️ Daily broadcast #{slot}: none of its accounts is connected any more, nothing was sent.")
                if result.errors:
                    await self.notify(owner_id, f"⚠️ Daily broadcast #{slot} finished with issues:\n" + "\n".join(result.errors))
                interval = 86400 / job.times_per_day
                skipped = int((time.time() - next_run.timestamp()) // interval)
                if skipped > 0:
                    logger.warning("Daily job: broadcast took longer than %ds, skipped %d slot(s)", interval, skipped)
                    await self.notify(
                        owner_id,
                        f"⚠️ Daily broadcast #{slot} took longer than the {int(interval)}s between slots, so "
                        f"{skipped} scheduled run(s) were skipped. Use a smaller delay or fewer runs per day.",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Daily job round failed")
                await asyncio.sleep(30)  # avoid a tight error loop
            await asyncio.sleep(1)  # never run the same slot twice if we woke a hair early
