"""Full control of one account (username, session export, giving it away), the start screen
settings, the help screen and the public session export."""
from __future__ import annotations

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.flows.export import send_session
from telegram_manager.bot.flows.startcfg import startcfg_view
from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.errors import describe_error


class ControlHandlers(HandlerBase):
    # --- username: un|set|<id>, un|rm|<id> ----------------------------------------------------
    async def username_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        action, _, account_id = self.arg(update).partition("|")
        account = await self.own_account(update, account_id)
        if account is None or action not in ("set", "rm"):
            return
        back = keyboards.back(f"acc|{account_id}", "⬅️ Back to account")
        if not self.manager.is_online(str(account["id"])):
            await ui.show(update, "🔴 This account is offline right now, so its username cannot be changed.", back)
            return
        if action == "set":
            await self.engine.start(update, context, "pf_username", {"account": str(account["id"])})
            return
        try:
            await self.manager.set_username(str(account["id"]), self.uid(update), "")
        except Exception as exc:
            await ui.show(update, f"❌ Could not remove it: {describe_error(exc)}", back)
            return
        await ui.show(update, "✅ Username removed.", back)

    # --- export a session of an own account: exp|pick, exp|ask|<id>, exp|go|<id>, exp|multi -----
    async def export_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        action, _, account_id = self.arg(update).partition("|")
        if action == "multi":
            if await self.accounts_or_notice(update) is None:
                return
            await self.engine.start(update, context, "exp_multi")
            return
        if action == "pick":
            accounts = await self.accounts_or_notice(update)
            if accounts is None:
                return
            await ui.show(
                update,
                "📤 Which account's session string do you want?",
                keyboards.accounts_picker(accounts, "exp|ask", self.manager.is_online, "home", "📤"),
            )
            return
        account = await self.own_account(update, account_id)
        if account is None:
            return
        label = self.manager.label_for_account(account)
        if action == "ask":
            await ui.show(
                update,
                f"📤 Export the session of {label}?\n\n"
                "⚠️ A session string is a full login: whoever holds it can use this account without a code. "
                "I will send it to this chat only, and the message deletes itself after a few minutes.",
                keyboards.export_confirm(str(account["id"])),
            )
            return
        if action == "go":
            back = keyboards.back(f"acc|{account['id']}", "⬅️ Back to account")
            try:
                session = await self.manager.export_session(str(account["id"]), self.uid(update))
                await send_session(context, update.effective_chat.id, session)
            except Exception as exc:
                await ui.show(update, f"❌ Could not export it: {describe_error(exc)}", back)
                return
            await ui.show(update, f"✅ The session of {label} is in the message below. Copy it now.", back)

    # --- revoke a session that was just exported (public /export): expr|<token> ------------------
    async def revoke_export_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        from telegram_manager.bot.flows.export import pop_revoke

        token = self.arg(update)
        pending = pop_revoke(token)
        if pending is None:
            await ui.show(update, "⌛ This Revoke button expired or was already used.", None)
            return
        try:
            revoked = await self.manager.revoke_exported_session(
                pending["session"], pending["api_id"], pending["api_hash"]
            )
        except Exception as exc:
            await ui.show(update, f"❌ Could not revoke it: {describe_error(exc)}", None)
            return
        label = pending.get("label") or "That account"
        text = (
            f"✅ {label}'s exported session was revoked — it no longer works."
            if revoked
            else f"⚠️ Telegram did not confirm the revoke for {label} (it may already be invalid)."
        )
        await ui.show(update, text, None)

    # --- give accounts: give|start, give|acc|<id> --------------------------------------------------
    async def _give(self, update, context, answers: dict | None) -> None:
        access = self.deps.access
        if access.is_owner(self.uid(update)) and not access.admins():
            await ui.show(
                update,
                "🎁 There is no admin to give accounts to yet. Add one first.",
                keyboards.back("adm|home", "👑 Manage admins"),
            )
            return
        await self.engine.start(update, context, "give", answers)

    async def give_start(self, update, context) -> None:
        if not await self.entry(update):
            return
        if await self.accounts_or_notice(update) is None:
            return
        await self._give(update, context, None)

    async def give_account(self, update, context) -> None:
        """Account screen button give|acc|<id>."""
        if not await self.entry(update):
            return
        _, _, account_id = self.arg(update).partition("|")
        account = await self.own_account(update, account_id)
        if account is not None:
            await self._give(update, context, {"accounts": [str(account["id"])]})

    # --- start screen settings: sc|... ------------------------------------------------------------
    async def startcfg_screen(self, update, context) -> None:
        text, markup = await startcfg_view(self.store)
        await ui.show(update, text, markup)

    async def startcfg_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        action = self.arg(update)
        settings = self.store.settings
        if action == "text":
            await self.engine.start(update, context, "sc_text")
            return
        if action == "photo":
            await self.engine.start(update, context, "sc_photo")
            return
        if action == "rmphoto":
            await settings.set_start_screen(photo="", media="", media_type="")
        elif action == "reset":
            await settings.clear_start_screen()
        elif action == "pub":
            current = await settings.get_start_screen()
            await settings.set_start_screen(public_export=not bool(current.get("public_export", True)))
        await self.startcfg_screen(update, context)

    async def startcfg_command(self, update, context) -> None:
        if await self.guard.allow(update):
            await self.startcfg_screen(update, context)

    # --- /export: admins pick one of their accounts, everybody else logs in --------------------------
    async def export_command(self, update, context) -> None:
        await self.deps.access.ensure_loaded()
        if self.deps.access.is_admin(self.uid(update)):
            accounts = await self.accounts_or_notice(update)
            if accounts is not None:
                await ui.show(
                    update,
                    "📤 Which account's session string do you want?",
                    keyboards.accounts_picker(accounts, "exp|ask", self.manager.is_online, "home", "📤"),
                )
            return
        await self.public_export(update, context)

    async def help_command(self, update, context) -> None:
        if await self.guard.allow(update):
            text = "📖 All commands — tap a button, or type the command."
            await ui.show(update, text, keyboards.help_keyboard(self.is_owner(update)))

    # --- public export: ex|start ---------------------------------------------------------------------
    async def public_export(self, update, context) -> None:
        if not await self.guard.allow_public(update):
            return
        if update.callback_query is not None:
            await update.callback_query.answer()
        cfg = await self.store.settings.get_start_screen()
        if not cfg.get("public_export", True):
            await ui.show(update, "🚫 Session export is switched off.", None)
            return
        await self.engine.start(update, context, "ex_otp")
