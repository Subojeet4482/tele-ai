"""Daily broadcast, asked one question at a time (this is only reachable from the menu button)."""
from __future__ import annotations

from datetime import time as dtime

from telegram_manager.bot import keyboards
from telegram_manager.bot.flows.common import (
    accounts_step,
    buttons_step,
    buttons_text,
    confirm_step,
    describe_accounts,
    delay_step,
    preview,
    static,
    target_step,
    text_step,
)
from telegram_manager.bot.wizard.spec import Choice, Step, WizardContext, WizardSpec
from telegram_manager.constants import (
    BULK_DELAY_MAX,
    COUNT_PRESETS,
    DRIFT_MAX,
    DRIFT_PRESETS,
    OPEN_CHAT_DELAY_PRESETS,
    OPEN_CHAT_MIN_DELAY,
    TIME_PRESETS,
)
from telegram_manager.core.models import DailyJob
from telegram_manager.parsing import format_clock, parse_count, parse_shift_range, parse_time

_DELAY_INTRO = (
    f"⏱ Delay between accounts ({OPEN_CHAT_MIN_DELAY}–{BULK_DELAY_MAX} sec)\n\n"
    f"The broadcast opens chats, so accounts go one after another, at least {OPEN_CHAT_MIN_DELAY} seconds apart.\n\n"
    "Tap a value or type a number."
)


def _shift_text(low: int, high: int) -> str:
    if high > low:
        return f"Random +{low}-{high} sec later every day"
    return f"+{low} sec later every day" if low else "Same time every day"


def _shift(value, seconds: int):
    """The clock time `seconds` later (wraps around midnight)."""
    total = (value.hour * 3600 + value.minute * 60 + value.second + seconds) % 86400
    return dtime(total // 3600, total % 3600 // 60, total % 60)


def build_schedule_spec(deps) -> WizardSpec:
    manager, store = deps.manager, deps.store
    tz = manager.tz.key

    async def describe_target(wc: WizardContext) -> str:
        return f"🔎 Confirm the user\n👤 {wc.answers['target']}\n\nTap ✅ Confirm to continue."

    async def time_prompt(wc: WizardContext) -> str:
        return (
            f"🕐 At what time should the first broadcast go out? (timezone {tz})\n\n"
            "Tap a time or type one, like 10:30am, 22:15 or 6:42:30pm (seconds are allowed)."
        )

    async def time_choices(wc: WizardContext) -> list[Choice]:
        return [Choice(format_clock(parse_time(value)), value) for value in TIME_PRESETS]

    async def parse_clock(wc: WizardContext, raw: str):
        return parse_time(raw)

    async def drift_prompt(wc: WizardContext) -> str:
        return (
            "⏩ Should the time move later every day?\n\n"
            "Tap \"Same time\" to send at the same time every day, or choose / type how many seconds "
            f"to add each day (1–{DRIFT_MAX}), or a random range like 10-60 (a new random value each day).\n\n"
            f"Example: first run {format_clock(wc.answers['time'])} with 30 sec →\n"
            f"Day 1: {format_clock(wc.answers['time'])}\n"
            f"Day 2: {format_clock(_shift(wc.answers['time'], 30))}\n"
            f"Day 3: {format_clock(_shift(wc.answers['time'], 60))}"
        )

    async def drift_choices(wc: WizardContext) -> list[Choice]:
        return [Choice("🔁 Same time daily" if n == 0 else f"+{n} sec", str(n)) for n in DRIFT_PRESETS]

    async def parse_shift(wc: WizardContext, raw: str) -> int:
        low, high = parse_shift_range(raw)
        wc.answers["drift_max"] = high  # high > low means: a random shift between the two, every day
        return low

    async def count_choices(wc: WizardContext) -> list[Choice]:
        return [Choice(f"{n}×", str(n)) for n in COUNT_PRESETS]

    async def parse_times(wc: WizardContext, raw: str) -> int:
        return parse_count(raw)

    async def describe_review(wc: WizardContext) -> str:
        a = wc.answers
        every = await _covers_all(wc)
        who = "all of your connected accounts (accounts you add later too)" if every else \
            f"{len(a['accounts'])} chosen account(s): {await describe_accounts(deps, wc.user_id, a['accounts'])}"
        buttons = f"🖱 Click: {buttons_text(a['buttons'])}\n" if a.get("buttons") else ""
        return (
            "📋 Review the daily broadcast\n\n"
            f"👤 To: {a['target']}\n"
            f"✉️ Message: {preview(a['text'], 120)}\n"
            f"{buttons}"
            f"👥 From: {who}\n"
            f"⏱ {a['delay']} sec between accounts\n"
            f"🕐 First run: {format_clock(a['time'])} ({tz})\n"
            f"⏩ {_shift_text(a['drift'], a.get('drift_max', 0))}\n"
            f"🔁 Runs per day: {a['count']}"
        )

    async def _covers_all(wc: WizardContext) -> bool:
        """True when every connected account is ticked: the job then follows the account list."""
        everyone = {str(x["id"]) for x in await store.accounts.list(wc.user_id) if x.get("enabled", True)}
        return everyone <= set(wc.answers["accounts"])

    async def finish(wc: WizardContext) -> None:
        a = wc.answers
        picked = None if await _covers_all(wc) else list(a["accounts"])
        job = DailyJob(
            a["target"], a["text"], a["delay"], a["time"], a["count"],
            account_ids=picked, buttons=list(a.get("buttons") or []), drift_seconds=a.get("drift", 0),
            drift_max_seconds=a.get("drift_max", 0),
        )
        try:
            slot = await manager.start_daily_job(wc.user_id, job, a.get("slot"))
        except ValueError as exc:
            await wc.show(f"❌ {exc}", keyboards.schedule_overview(manager, wc.user_id))
            return
        accounts = [x for x in await store.accounts.list(wc.user_id) if x.get("enabled", True)]
        if picked is not None:
            accounts = [x for x in accounts if str(x["id"]) in picked]
        note = ""
        gap = 86400 / job.times_per_day
        if accounts and (len(accounts) - 1) * job.delay_seconds > gap:
            note = "\n\n⚠️ One run takes longer than the gap between runs, so some runs will be skipped."
        await wc.show(
            f"✅ Daily broadcast #{slot} scheduled.\n\n" + manager.daily_job_status(wc.user_id, slot) + note,
            keyboards.schedule_overview(manager, wc.user_id),
        )

    steps = [
        target_step("Enter the user name to message every day (like @mandal4482)."),
        confirm_step("confirmed", describe_target),
        text_step("✉️ Send the message text."),
        buttons_step(),
        accounts_step(
            deps,
            "👥 Which accounts should send the broadcast?\n\n"
            "Tap accounts to tick them, then Done — or Select all. You can also type numbers (1 3 5) or all.",
        ),
        delay_step(OPEN_CHAT_MIN_DELAY, OPEN_CHAT_DELAY_PRESETS, _DELAY_INTRO),
        Step("time", time_prompt, parse_clock, time_choices, columns=2),
        Step("drift", drift_prompt, parse_shift, drift_choices, columns=3),
        Step(
            "count",
            static("🔁 How many times per day? Tap a number or type one."),
            parse_times,
            count_choices,
            columns=3,
        ),
        confirm_step("review", describe_review, "✅ Schedule it"),
    ]
    return WizardSpec(name="schedule", title="🕒 Daily broadcast", steps=steps, finish=finish, back_to="sched|home")
