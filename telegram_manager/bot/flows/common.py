"""Steps that several flows share."""
from __future__ import annotations

import re
from typing import Awaitable, Callable

from telegram_manager.bot.wizard.spec import Choice, Step, WizardAbort, WizardContext
from telegram_manager.constants import BULK_DELAY_MAX, WHEN_PRESETS
from telegram_manager.parsing import (
    looks_like_selector,
    parse_button_names,
    parse_delay,
    parse_minutes,
    parse_selector,
    parse_target,
)


def static(text: str) -> Callable[[WizardContext], Awaitable[str]]:
    async def prompt(wc: WizardContext) -> str:
        return text

    return prompt


def preview(text: str, limit: int = 80) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def delay_text(seconds: int) -> str:
    return "instant (all accounts at the same time)" if seconds == 0 else f"{seconds} sec between accounts"


def target_step(ask: str) -> Step:
    async def parse(wc: WizardContext, raw: str) -> str:
        return parse_target(raw)

    return Step("target", static(ask), parse)


def confirm_step(key: str, describe: Callable[[WizardContext], Awaitable[str]], label: str = "✅ Confirm") -> Step:
    async def parse(wc: WizardContext, raw: str) -> bool:
        value = raw.strip().lower()
        if value in ("y", "yes", "confirm", "ok"):
            return True
        if value in ("n", "no", "cancel"):
            raise WizardAbort("❌ Cancelled.")
        raise ValueError("Tap the button, or send Y to confirm or N to cancel.")

    async def choices(wc: WizardContext) -> list[Choice]:
        return [Choice(label, "yes")]

    return Step(key, describe, parse, choices)


def delay_step(minimum: int, presets: tuple[int, ...], intro: str) -> Step:
    async def parse(wc: WizardContext, raw: str) -> int:
        return parse_delay(raw, minimum, BULK_DELAY_MAX)

    async def choices(wc: WizardContext) -> list[Choice]:
        return [Choice("⚡ 0 · instant" if n == 0 else f"{n} sec", str(n)) for n in presets]

    return Step("delay", static(intro), parse, choices, columns=3)


def text_step(ask: str) -> Step:
    async def parse(wc: WizardContext, raw: str) -> str:
        text = raw.strip()
        if not text:
            raise ValueError("The message cannot be empty.")
        return text

    return Step("text", static(ask), parse)


def when_text(minutes: int) -> str:
    if not minutes:
        return "now"
    hours, rest = divmod(minutes, 60)
    return " ".join(part for part in (f"{hours} h" if hours else "", f"{rest} min" if rest else "") if part)


def when_step() -> Step:
    """When to send: now, or after a wait. The wait lives in the running bot, so a restart cancels it."""

    async def parse(wc: WizardContext, raw: str) -> int:
        return parse_minutes(raw)

    async def choices(wc: WizardContext) -> list[Choice]:
        return [Choice("⚡ Now" if n == 0 else (f"{n} min" if n < 60 else f"{n // 60} hour"), str(n)) for n in WHEN_PRESETS]

    intro = (
        "⏰ When should it be sent?\n\n"
        "Tap a value, or type the wait: 10 (minutes), 30m, 2h (up to 24 hours).\n"
        "A timed message waits inside the bot: if the bot restarts before it is sent, it is cancelled."
    )
    return Step("when", static(intro), parse, choices, columns=3)


def buttons_text(buttons: list[str]) -> str:
    return " → ".join(buttons)


def buttons_step(required: bool = False) -> Step:
    """Names of inline buttons to press after the message (the bot's reply is searched for them)."""

    async def parse(wc: WizardContext, raw: str) -> list[str]:
        if not required and raw.strip().lower() in ("-", "skip", "no", "none"):
            return []
        if not wc.answers.get("text") and not required and raw.strip() == "-":
            raise ValueError("There is no message to send, so name at least one button to click.")
        return parse_button_names(raw)

    async def choices(wc: WizardContext) -> list[Choice]:
        return [] if required else [Choice("⏭ Skip — no buttons", "-")]

    intro = (
        "🖱 Which inline buttons should be clicked?\n\n"
        "If the bot answers with buttons, name the ones to press, in order. Write them like "
        "[follow] (join) or follow, join.\n"
        "A name matches any button that contains it: join also presses \"✅ Joined\". "
        "A link button to a channel makes the account join that channel."
    )
    return Step("buttons", static(intro), parse, None if required else choices)


def accounts_step(deps, intro: str | None = None) -> Step:
    """Tick the accounts to use (buttons, or type numbers like 1 3 5, or all). Answer: list of account ids."""
    store, manager = deps.store, deps.manager

    async def choices(wc: WizardContext) -> list[Choice]:
        rows = await store.accounts.list(wc.user_id)
        return [
            Choice(
                f"{a.get('number')}. {str(a.get('display_name') or 'Unnamed')[:24]} "
                f"{'🟢' if manager.is_online(str(a['id'])) else '🔴'}",
                str(a["id"]),
            )
            for a in rows
        ]

    async def parse(wc: WizardContext, raw: str) -> list[str]:
        """The Done button sends ticked ids; typing works too: account numbers (1 3 5) or "all"."""
        rows = await store.accounts.list(wc.user_id)
        by_id = {str(a["id"]): str(a["id"]) for a in rows}
        by_number = {str(a.get("number")): str(a["id"]) for a in rows}
        if raw.strip().lower() == "all":
            chosen = [str(a["id"]) for a in rows]
        elif "-" in raw and looks_like_selector(raw):  # 1-5, 1,3,7-9
            numbers = parse_selector(raw, [a.get("number") for a in rows if a.get("number")])
            chosen = [by_number[str(n)] for n in numbers]
        else:
            chosen = []
            for token in filter(None, re.split(r"[,\s]+", raw.strip())):
                account_id = by_id.get(token) or by_number.get(token)
                if account_id is None:
                    raise ValueError(f"There is no account '{token}'. Tap the buttons, or type numbers like 1 3 5.")
                if account_id not in chosen:
                    chosen.append(account_id)
        if not chosen:
            raise ValueError("Pick at least one account.")
        return chosen

    text = intro or (
        "👥 Choose the accounts to use.\n\n"
        "Tap an account to tick it, then press Done. You can also type numbers (1 3 5) or all."
    )
    return Step("accounts", static(text), parse, choices, paged=True, multi=True)


async def describe_accounts(deps, user_id: int, ids: list[str]) -> str:
    rows = {str(a["id"]): a for a in await deps.store.accounts.list(user_id)}
    return ", ".join(
        f"{rows[i].get('number')}. {str(rows[i].get('display_name') or 'Unnamed')[:20]}" for i in ids if i in rows
    )
