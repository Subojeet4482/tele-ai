"""/multi: user -> message -> buttons -> tick the accounts to use -> delay -> when -> confirm -> run."""
from __future__ import annotations

from telegram_manager.bot.flows.bulk import _OPEN_INTRO
from telegram_manager.bot.flows.common import (
    accounts_step,
    buttons_step,
    buttons_text,
    confirm_step,
    delay_step,
    delay_text,
    describe_accounts,
    preview,
    target_step,
    text_step,
    when_step,
    when_text,
)
from telegram_manager.bot.wizard.spec import WizardContext, WizardSpec
from telegram_manager.constants import OPEN_CHAT_DELAY_PRESETS, OPEN_CHAT_MIN_DELAY


def build_multi_spec(deps) -> WizardSpec:
    def one_account(wc: WizardContext) -> bool:
        return len(wc.answers.get("accounts", [])) <= 1  # nothing to space out

    delay = delay_step(OPEN_CHAT_MIN_DELAY, OPEN_CHAT_DELAY_PRESETS, _OPEN_INTRO)
    delay.skip = one_account

    async def describe(wc: WizardContext) -> str:
        answers = wc.answers
        names = await describe_accounts(deps, wc.user_id, answers["accounts"])
        lines = [
            "🔎 Check and confirm",
            f"👤 {answers['target']}",
            f"✉️ {preview(answers['text'])}",
        ]
        if answers.get("buttons"):
            lines.append(f"🖱 Click: {buttons_text(answers['buttons'])}")
        lines.append(f"👥 {len(answers['accounts'])} account(s): {names}")
        if "delay" in answers:
            lines.append(f"⏱ {delay_text(answers['delay'])}")
        lines.append(f"⏰ {'now' if not answers.get('when') else 'in ' + when_text(answers['when'])}")
        lines.append("\nTap ✅ Confirm to send.")
        return "\n".join(lines)

    steps = [
        target_step("Enter the user name to message (like @mandal4482)."),
        text_step("✉️ Send the message text."),
        buttons_step(),
        accounts_step(
            deps,
            "👥 Choose the accounts to send from.\n\n"
            "Tap an account to tick it, then press Done. You can also type numbers (1 3 5) or all.",
        ),
        delay,
        when_step(),
        confirm_step("confirmed", describe, "✅ Confirm and send"),
    ]

    async def finish(wc: WizardContext) -> None:
        answers = wc.answers
        await deps.bulk.launch(
            wc.update,
            wc.context,
            wc.user_id,
            "multi",
            answers["target"],
            answers["text"],
            answers.get("delay", 0),
            account_ids=answers["accounts"],
            start_in=answers.get("when", 0) * 60,
            buttons=answers.get("buttons") or None,
        )

    return WizardSpec(name="multi", title="👥 Message from chosen accounts", steps=steps, finish=finish, back_to="menu")
