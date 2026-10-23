from __future__ import annotations

import re

from telegram_manager.bot import keyboards, ui


class HandlerBase:
    def __init__(self, deps, engine) -> None:
        self.deps = deps
        self.engine = engine
        self.store = deps.store
        self.manager = deps.manager
        self.guard = deps.guard

    @staticmethod
    def uid(update) -> int:
        return update.effective_user.id

    def is_owner(self, update) -> bool:
        return self.deps.access.is_owner(self.uid(update))

    async def entry(self, update) -> bool:
        """Guard + acknowledge a button tap. False means the user is not allowed."""
        if not await self.guard.allow(update):
            return False
        if update.callback_query is not None:
            await update.callback_query.answer()
        return True

    async def entry_owner(self, update) -> bool:
        if not await self.guard.allow_owner(update):
            return False
        if update.callback_query is not None:
            await update.callback_query.answer()
        return True

    async def own_account(self, update, account_id: str, back: str = "acc_list") -> dict | None:
        """One of this admin's accounts, or a 'not found' screen. Never another admin's."""
        account = await self.store.accounts.get(account_id, owner_id=self.uid(update))
        if account is None:
            await ui.show(update, "Account not found.", keyboards.back(back))
        return account

    async def accounts_or_notice(self, update) -> list[dict] | None:
        accounts = await self.store.accounts.list(self.uid(update))
        if not accounts:
            await ui.show(update, "You have no connected accounts yet.", keyboards.no_accounts())
            return None
        return accounts

    @staticmethod
    def command_args(update) -> str:
        match = re.match(r"^/\S+\s*(.*)$", update.effective_message.text or "", re.S)
        return match.group(1) if match else ""

    @staticmethod
    def arg(update) -> str:
        """The part after the first | in the tapped button's data."""
        data = update.callback_query.data or ""
        return data.split("|", 1)[1] if "|" in data else ""
