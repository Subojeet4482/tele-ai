"""Which incoming messages are forwarded: global switches and per-account overrides."""
from __future__ import annotations

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase

SWITCHES = {
    "u": ("👤 User messages", "forward_user_messages"),
    "b": ("🤖 Bot messages", "forward_bot_messages"),
    "c": ("📢 Channel & group messages", "forward_channel_messages"),
}
_VALUES = {"on": True, "off": False, "global": None, "default": None, "reset": None}


class ForwardingHandlers(HandlerBase):
    # --- global switches ----------------------------------------------------------------
    async def home(self, update, context) -> None:
        if not await self.entry(update):
            return
        await self._home_screen(update)

    async def _home_screen(self, update) -> None:
        settings = await self.store.settings.get_forwarding(self.uid(update))
        await ui.show(
            update,
            "📥 Forwarding\n\nChoose which incoming messages are forwarded to you. "
            "These are your defaults; each account can override them.",
            keyboards.forwarding_home(settings),
        )

    async def toggle_global(self, update, context) -> None:
        if not await self.entry(update):
            return
        key = update.callback_query.data.split("|")[2]
        if key not in SWITCHES:
            return
        name = SWITCHES[key][1]
        settings = await self.store.settings.get_forwarding(self.uid(update))
        await self.store.settings.set_forwarding(self.uid(update), name, not settings[name])
        await self._home_screen(update)

    # --- per account ---------------------------------------------------------------------
    async def account_picker(self, update, context) -> None:
        if not await self.entry(update):
            return
        await self._picker(update)

    async def _picker(self, update) -> None:
        accounts = await self.accounts_or_notice(update)
        if accounts is None:
            return
        await ui.show(
            update,
            "📱 Which account do you want to change?",
            keyboards.accounts_picker(accounts, "acc", self.manager.is_online, "fw|home"),
        )

    async def account_screen(self, update, context) -> None:
        """Button acc|<id>."""
        if not await self.entry(update):
            return
        account = await self.own_account(update, self.arg(update))
        if account is not None:
            await self._screen(update, account)

    async def _screen(self, update, account: dict) -> None:
        forwarding = await self.store.settings.get_forwarding(self.uid(update))
        labels = {}
        for key, (title, name) in SWITCHES.items():
            override = account.get(name)
            if override is None:
                labels[key] = f"{title}: 🌐 {'ON' if forwarding[name] else 'OFF'} (default)"
            else:
                labels[key] = f"{title}: {'🟢 ON' if override else '🔴 OFF'} (this account)"
        dot = "🟢 online" if self.manager.is_online(str(account["id"])) else "🔴 offline"
        await ui.show(
            update,
            f"📱 {account.get('number')}. {self.manager.label_for_account(account)} — {dot}\n"
            + (f"📞 {account.get('phone') or account['phone_masked']}\n" if account.get("phone") or account.get("phone_masked") else "")
            + (f"🔤 @{account['username']}\n" if account.get("username") else "🔤 No username\n")
            + "\n"
            "Tap a switch to change it. It cycles: default → ON → OFF → default.\n"
            "“Default” follows your setting in Menu → Forwarding.",
            keyboards.account_screen(str(account["id"]), labels, has_username=bool(account.get("username"))),
        )

    async def cycle(self, update, context) -> None:
        """Button fw|set|<account id>|<u|b|c>."""
        if not await self.entry(update):
            return
        parts = update.callback_query.data.split("|")
        if len(parts) != 4 or parts[3] not in SWITCHES:
            return
        account = await self.own_account(update, parts[2])
        if account is None:
            return
        name = SWITCHES[parts[3]][1]
        current = account.get(name)
        following = True if current is None else (False if current else None)
        await self.manager.set_forward_override(str(account["id"]), name, following)
        refreshed = await self.store.accounts.get(str(account["id"]), owner_id=self.uid(update))
        if refreshed is not None:
            await self._screen(update, refreshed)

    # --- commands: /usermessage /botmessage /channelmessage -----------------------------
    async def command(self, update, context, key: str) -> None:
        if not await self.guard.allow(update):
            return
        words = self.command_args(update).split()
        if not words:
            await self._picker(update)
            return
        value_word = words[-1].lower() if len(words) >= 2 else ""
        alias = " ".join(words[:-1]) if value_word in _VALUES else " ".join(words)
        account = await self.store.accounts.find_by_alias(self.uid(update), alias)
        if account is None:
            await ui.show(update, "Account not found. Use /list to see your accounts.", keyboards.back("home"))
            return
        if value_word in _VALUES:
            await self.manager.set_forward_override(str(account["id"]), SWITCHES[key][1], _VALUES[value_word])
            account = await self.store.accounts.get(str(account["id"]), owner_id=self.uid(update)) or account
        await self._screen(update, account)
