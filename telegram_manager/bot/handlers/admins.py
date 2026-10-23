"""Owner only: see, add and remove admins."""
from __future__ import annotations

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase


class AdminHandlers(HandlerBase):
    async def home_screen(self, update, context) -> None:
        access = self.deps.access
        lines = ["👑 Admins", "", f"👑 Owner — {access.owner_id}"]
        for row in access.admins():
            count = len(await self.store.accounts.list(int(row["id"])))
            lines.append(f"🛡 {row.get('name') or 'Admin'} — {row['id']} · {count} account(s)")
        if not access.admins():
            lines.append("\nNo other admins yet.")
        lines += ["", "Each admin connects and sees only their own accounts."]
        await ui.show(update, "\n".join(lines), keyboards.admins_home(bool(access.admins())))

    async def home(self, update, context) -> None:
        if await self.entry_owner(update):
            await self.home_screen(update, context)

    async def add(self, update, context) -> None:
        if await self.entry_owner(update):
            await self.engine.start(update, context, "adm_add")

    async def remove(self, update, context) -> None:
        if not await self.entry_owner(update):
            return
        if not self.deps.access.admins():
            await ui.show(update, "There are no admins to remove.", keyboards.back("adm|home"))
            return
        await self.engine.start(update, context, "adm_rm")
