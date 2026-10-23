"""/all, /alll and /allblock: user -> confirm -> delay between accounts -> message -> run."""
from __future__ import annotations

from telegram_manager.bot.flows.common import (
    buttons_step,
    buttons_text,
    confirm_step,
    delay_text,
    delay_step,
    preview,
    target_step,
    text_step,
    when_step,
    when_text,
)
from telegram_manager.bot.wizard.spec import WizardContext, WizardSpec
from telegram_manager.constants import (
    BULK_DELAY_MAX,
    BULK_DELAY_MIN,
    DELAY_PRESETS,
    OPEN_CHAT_DELAY_PRESETS,
    OPEN_CHAT_MIN_DELAY,
)

# kind -> (title, needs a message, minimum delay, presets)
KINDS = {
    "all": ("📨 Message all accounts", True, BULK_DELAY_MIN, DELAY_PRESETS),
    "alll": ("📤 Message all (opens chat)", True, OPEN_CHAT_MIN_DELAY, OPEN_CHAT_DELAY_PRESETS),
    "block": ("🚫 Block on all accounts", False, BULK_DELAY_MIN, DELAY_PRESETS),
    "click": ("🖱 Click buttons on all accounts", False, BULK_DELAY_MIN, DELAY_PRESETS),
}

_NORMAL_INTRO = (
    f"⏱ Delay between accounts ({BULK_DELAY_MIN}–{BULK_DELAY_MAX} sec)\n\n"
    "⚡ 0 = instant: every account acts at the same time.\n"
    "Any other number: one account acts, the bot waits that many seconds, then the next account acts.\n\n"
    "Tap a value or type a number."
)
_OPEN_INTRO = (
    f"⏱ Delay between accounts ({OPEN_CHAT_MIN_DELAY}–{BULK_DELAY_MAX} sec)\n\n"
    "This opens new chats, so accounts go one after another with at least "
    f"{OPEN_CHAT_MIN_DELAY} seconds in between: one account sends, the bot waits, then the next one sends.\n\n"
    "Tap a value or type a number."
)


def build_bulk_specs(deps) -> list[WizardSpec]:
    return [_build(deps, kind) for kind in KINDS]


def _build(deps, kind: str) -> WizardSpec:
    title, needs_text, minimum, presets = KINDS[kind]

    async def describe(wc: WizardContext) -> str:
        answers = wc.answers
        lines = ["🔎 Confirm the user", f"👤 {answers['target']}"]
        if answers.get("text"):
            lines.append(f"✉️ {preview(answers['text'])}")
        if answers.get("buttons"):
            lines.append(f"🖱 Click: {buttons_text(answers['buttons'])}")
        if "delay" in answers:
            lines.append(f"⏱ {delay_text(answers['delay'])}")
        if answers.get("when"):
            lines.append(f"⏰ in {when_text(answers['when'])}")
        lines.append("\nTap ✅ Confirm to continue.")
        return "\n".join(lines)

    ask = "Enter the user name to block (like @mandal4482)." if kind == "block" else \
        "Enter the user name (or bot) to message (like @mandal4482)."
    steps = [
        target_step(ask),
        confirm_step("confirmed", describe),
        delay_step(minimum, presets, _OPEN_INTRO if kind == "alll" else _NORMAL_INTRO),
    ]
    if needs_text:
        steps.append(text_step("✉️ Send the message text."))
    if kind != "block":
        steps.append(buttons_step(required=kind == "click"))
        steps.append(when_step())

    async def finish(wc: WizardContext) -> None:
        answers = wc.answers
        await deps.bulk.launch(
            wc.update,
            wc.context,
            wc.user_id,
            kind,
            answers["target"],
            answers.get("text"),
            answers["delay"],
            start_in=answers.get("when", 0) * 60,
            buttons=answers.get("buttons") or None,
        )

    return WizardSpec(name=f"bulk_{kind}", title=title, steps=steps, finish=finish, back_to="menu")
