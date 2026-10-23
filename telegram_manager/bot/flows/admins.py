"""Owner only: add and remove admins."""
from __future__ import annotations

import logging

from telegram_manager.bot import keyboards
from telegram_manager.bot.flows.common import confirm_step, static
from telegram_manager.bot.wizard.spec import Choice, Step, WizardContext, WizardSpec
from telegram_manager.errors import describe_error
from telegram_manager.parsing import parse_int

logger = logging.getLogger(__name__)


def _admin_label(row: dict) -> str:
    return f"{row.get('name') or 'Admin'} — {row['id']}"


def build_admin_specs(deps) -> list[WizardSpec]:
    access, manager, store = deps.access, deps.manager, deps.store

    # --- add ----------------------------------------------------------------------------
    async def parse_user_id(wc: WizardContext, raw: str) -> int:
        user_id = parse_int(raw, "Telegram user ID")
        if access.is_owner(user_id):
            raise ValueError("That is you, the owner.")
        if access.is_admin(user_id):
            raise ValueError("That user is already an admin.")
        return user_id

    async def describe_add(wc: WizardContext) -> str:
        return (
            f"➕ Add {wc.answers['user_id']} as an admin?\n\n"
            "They connect their own accounts and only ever see those. They cannot add or remove admins."
        )

    async def finish_add(wc: WizardContext) -> None:
        user_id = wc.answers["user_id"]
        bot = wc.context.bot
        name = ""
        try:
            chat = await bot.get_chat(user_id)
            name = getattr(chat, "full_name", None) or getattr(chat, "username", None) or ""
        except Exception:
            logger.debug("Could not look up the new admin's name")
        try:
            await access.add(user_id, wc.user_id, name)
        except Exception as exc:
            await wc.show(f"❌ Could not add the admin: {describe_error(exc)}", keyboards.back("adm|home"))
            return
        note = ""
        try:
            await bot.send_message(user_id, "👋 You were added as an admin. Send /start to begin.")
        except Exception:
            note = "\n\nI could not message them yet. Ask them to open this bot and press Start."
        await wc.show(f"✅ {name or user_id} is now an admin.{note}", keyboards.back("adm|home"))

    add = WizardSpec(
        name="adm_add",
        title="➕ Add admin",
        steps=[
            Step(
                "user_id",
                static("Send the Telegram numeric user ID of the new admin.\n(They can get it from @userinfobot.)"),
                parse_user_id,
            ),
            confirm_step("confirmed", describe_add, "✅ Add admin"),
        ],
        finish=finish_add,
        owner_only=True,
        back_to="adm|home",
    )

    # --- remove -------------------------------------------------------------------------
    async def admin_choices(wc: WizardContext) -> list[Choice]:
        return [Choice(_admin_label(row)[:40], str(row["id"])) for row in access.admins()]

    async def parse_admin(wc: WizardContext, raw: str) -> int:
        try:
            row = access.admin(int(raw.strip()))
        except ValueError:
            row = None
        if row is None:
            raise ValueError("Pick one of the admins in the list.")
        return int(row["id"])

    async def describe_remove(wc: WizardContext) -> str:
        count = len(await store.accounts.list(wc.answers["admin"]))
        return (
            f"➖ Remove admin {wc.answers['admin']}?\n\n"
            f"Their {count} connected account(s) will be logged out and deleted, and their daily broadcast stops."
        )

    async def finish_remove(wc: WizardContext) -> None:
        user_id = wc.answers["admin"]
        failed = 0
        for account in await store.accounts.list(user_id):
            try:
                await manager.remove_account(str(account["id"]))
            except Exception:
                logger.exception("Could not remove an account while removing an admin")
                failed += 1
        if failed:
            await wc.show(
                f"❌ {failed} account(s) could not be logged out, so the admin was not removed. Try again.",
                keyboards.back("adm|home"),
            )
            return
        await manager.stop_daily_job(user_id)
        await access.remove(user_id)
        try:
            await wc.context.bot.send_message(user_id, "You are no longer an admin of this bot.")
        except Exception:
            logger.debug("Could not tell the removed admin")
        await wc.show(f"✅ Admin {user_id} was removed.", keyboards.back("adm|home"))

    remove = WizardSpec(
        name="adm_rm",
        title="➖ Remove admin",
        steps=[
            Step("admin", static("Choose the admin to remove."), parse_admin, admin_choices),
            confirm_step("confirmed", describe_remove, "✅ Remove admin"),
        ],
        finish=finish_remove,
        owner_only=True,
        back_to="adm|home",
    )
    return [add, remove]
