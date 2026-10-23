"""Daily broadcast screens (the questions themselves are in flows/schedule.py). Several can run at once."""
from __future__ import annotations

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.constants import MAX_DAILY_JOBS


class ScheduleHandlers(HandlerBase):
    async def home_screen(self, update, context) -> None:
        uid = self.uid(update)
        text = (
            "🕒 Daily broadcasts\n\nEach broadcast sends one message to one user every day, from all your accounts "
            f"or only the ones you pick. It can also press inline buttons of the bot's reply. "
            f"You can run up to {MAX_DAILY_JOBS} broadcasts at the same time.\n\n"
        )
        text += self.manager.daily_job_status(uid)
        await ui.show(update, text, keyboards.schedule_overview(self.manager, uid))

    async def home(self, update, context) -> None:
        if await self.entry(update):
            await self.home_screen(update, context)

    async def new(self, update, context) -> None:
        if not await self.entry(update):
            return
        if await self.accounts_or_notice(update) is None:
            return
        if self.manager.free_daily_slot(self.uid(update)) is None:
            await ui.show(
                update,
                f"You already have {MAX_DAILY_JOBS} daily broadcasts. Delete one to add another.",
                keyboards.schedule_overview(self.manager, self.uid(update)),
            )
            return
        await self.engine.start(update, context, "schedule")

    def _slot(self, update) -> int | None:
        try:
            return int(self.arg(update).rpartition("|")[2])  # sched|view|<slot> -> "view|<slot>" -> slot
        except ValueError:
            return None

    async def view(self, update, context) -> None:
        """Button sched|view|<slot>."""
        if not await self.entry(update):
            return
        uid, slot = self.uid(update), self._slot(update)
        if slot is None or self.manager.daily_job_for(uid, slot) is None:
            await self.home_screen(update, context)
            return
        text = f"⏰ Broadcast #{slot}\n\n" + self.manager.daily_job_status(uid, slot)
        await ui.show(update, text, keyboards.schedule_job(slot))

    async def edit(self, update, context) -> None:
        """Button sched|edit|<slot>: ask the questions again and replace that broadcast."""
        if not await self.entry(update):
            return
        uid, slot = self.uid(update), self._slot(update)
        if slot is None or self.manager.daily_job_for(uid, slot) is None:
            await self.home_screen(update, context)
            return
        if await self.accounts_or_notice(update) is None:
            return
        await self.engine.start(update, context, "schedule", {"slot": slot})

    async def delete(self, update, context) -> None:
        """Button sched|del|<slot>."""
        if not await self.entry(update):
            return
        slot = self._slot(update)
        if slot is not None:
            await self.manager.stop_daily_job(self.uid(update), slot)
        await self.home_screen(update, context)

    async def off(self, update, context) -> None:
        if not await self.entry(update):
            return
        was_on = await self.manager.stop_daily_job(self.uid(update))
        text = "🗑 All daily broadcasts are off." if was_on else "No daily broadcast was scheduled."
        await ui.show(update, text, keyboards.schedule_overview(self.manager, self.uid(update)))
