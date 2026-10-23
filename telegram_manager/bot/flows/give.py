"""Hand accounts over: the owner gives to an admin (picked from a list), an admin gives to the owner."""
from __future__ import annotations

import logging

from telegram_manager.bot import keyboards
from telegram_manager.bot.flows.common import accounts_step, confirm_step, describe_accounts, static
from telegram_manager.bot.wizard.spec import Choice, Step, WizardContext, WizardSpec
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)


def build_give_spec(deps) -> WizardSpec:
    access, manager, store = deps.access, deps.manager, deps.store

    def holder(wc: WizardContext) -> bool:
        return access.is_owner(wc.user_id)

    def who(user_id: int) -> str:
        if access.is_owner(user_id):
            return "👑 the owner"
        row = access.admin(user_id) or {}
        return f"🛡 {row.get('name') or 'Admin'} ({user_id})"

    async def to_choices(wc: WizardContext) -> list[Choice]:
        return [Choice(f"🛡 {row.get('name') or 'Admin'} — {row['id']}"[:40], str(row["id"])) for row in access.admins()]

    async def parse_to(wc: WizardContext, raw: str) -> int:
        try:
            target = int(raw.strip())
        except ValueError:
            target = 0
        if not access.is_admin(target) or access.is_owner(target):
            raise ValueError("Pick one of the admins in the list.")
        return target

    async def describe(wc: WizardContext) -> str:
        to = wc.answers.get("to") or access.owner_id
        accounts = await describe_accounts(deps, wc.user_id, wc.answers["accounts"])
        return (
            f"🎁 Give {len(wc.answers['accounts'])} account(s) to {who(to)}?\n\n{accounts}\n\n"
            "They get full control of these accounts (messages, profile, session). "
            "The accounts disappear from your list."
        )

    async def finish(wc: WizardContext) -> None:
        to = wc.answers.get("to") or access.owner_id
        moved, failed = [], []
        for account_id in wc.answers["accounts"]:
            try:
                row = await manager.transfer_account(account_id, wc.user_id, to)
                moved.append(row)
            except Exception as exc:
                logger.warning("Could not give an account: %s", describe_error(exc))
                failed.append(describe_error(exc))
        text = f"✅ {len(moved)} account(s) given to {who(to)}."
        if failed:
            text += f"\n⚠️ {len(failed)} could not be moved: {failed[0]}"
        if moved:
            try:
                names = ", ".join(manager.label_for_account(row) for row in moved)
                sender = who(wc.user_id)
                await wc.context.bot.send_message(
                    to, f"🎁 {sender} gave you {len(moved)} account(s): {names}\nOpen 📋 My accounts to see them."
                )
            except Exception:
                text += "\n(I could not message them; ask them to open the bot.)"
        await wc.show(text, keyboards.after_action("🎁 Give more", "give|start"))

    steps = [
        Step(
            "to",
            static("🎁 Who should get the accounts?"),
            parse_to,
            to_choices,
            skip=lambda wc: not holder(wc),
        ),
        accounts_step(
            deps,
            "🎁 Which accounts do you want to give?\n\nTap accounts to tick them, then Done — or Select all.",
        ),
        confirm_step("confirmed", describe, "✅ Give"),
    ]
    return WizardSpec(name="give", title="🎁 Give accounts", steps=steps, finish=finish, back_to="home")
