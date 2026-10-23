"""The 2-step password form. Passwords are typed here (the message is deleted at once) and go straight to
Telegram: they never reach the AI, the logs or the database. Used by the buttons AND by the AI's two_factor tool."""
from __future__ import annotations

from telegram_manager.bot import keyboards
from telegram_manager.bot.flows.common import confirm_step, describe_accounts, static
from telegram_manager.bot.wizard.spec import Step, WizardContext, WizardSpec
from telegram_manager.errors import describe_error

ACTION_TEXT = {"enable": "turn 2-step verification ON", "change": "change the 2-step password", "disable": "turn 2-step verification OFF"}
HINT_MAX = 100


def build_security_specs(deps) -> list[WizardSpec]:
    manager = deps.manager

    def action(wc: WizardContext) -> str:
        return str(wc.answers.get("action") or "")

    async def parse_secret(wc: WizardContext, raw: str) -> str:
        value = raw.strip("\n")
        if not value.strip():
            raise ValueError("The password cannot be empty.")
        if len(value) > 120:
            raise ValueError("That password is too long (max 120 characters).")
        return value

    async def parse_hint(wc: WizardContext, raw: str) -> str:
        value = raw.strip()
        if value in ("-", ""):
            return ""
        if len(value) > HINT_MAX:
            raise ValueError(f"A hint can have at most {HINT_MAX} characters.")
        return value

    async def describe(wc: WizardContext) -> str:
        accounts = await describe_accounts(deps, wc.user_id, wc.answers["accounts"])
        return f"🔐 {ACTION_TEXT[action(wc)].capitalize()} on {len(wc.answers['accounts'])} account(s)?\n\n{accounts}"

    async def finish(wc: WizardContext) -> None:
        results, errors = [], []
        for account_id in wc.answers["accounts"]:
            try:
                results.append(
                    await manager.set_two_fa(
                        account_id, wc.user_id, action(wc), wc.answers.get("password", ""), wc.answers.get("current", ""), wc.answers.get("hint", "")
                    )
                )
            except Exception as exc:
                errors.append(describe_error(exc))
        text = f"✅ {results[0] if results else 'Done'} on {len(results)} of {len(wc.answers['accounts'])} account(s)." if results else "❌ Nothing changed."
        if errors:
            text += "\n⚠️ " + "\n⚠️ ".join(list(dict.fromkeys(errors))[:4])
        if results and action(wc) != "disable":
            text += "\n\nℹ️ Keep the password safe: without it the account cannot be recovered or have 2-step turned off."
        await wc.show(text, keyboards.back("sec|home", "🔐 Account security"))

    steps = [
        Step("current", static("🔑 Send the CURRENT 2-step password (your message is deleted right away)."), parse_secret,
             sensitive=True, skip=lambda wc: action(wc) == "enable"),
        Step("password", static("🆕 Send the NEW 2-step password (your message is deleted right away)."), parse_secret,
             sensitive=True, skip=lambda wc: action(wc) == "disable"),
        Step("hint", static("💡 Send a password hint, or - for none."), parse_hint, skip=lambda wc: action(wc) == "disable"),
        confirm_step("confirmed", describe, "✅ Do it"),
    ]
    return [WizardSpec(name="tfa", title="🔐 2-step verification", steps=steps, finish=finish, back_to="sec|home")]
