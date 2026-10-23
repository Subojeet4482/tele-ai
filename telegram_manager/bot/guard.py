"""Who may use the bot. Everyone else is told it is for admins only."""
from __future__ import annotations

import time

from telegram_manager.constants import DENY_REPLY_INTERVAL

DENY_TEXT = "🚫 This bot is only for its admin."
OWNER_TEXT = "🔒 Only the owner can do this."


class Guard:
    def __init__(self, access) -> None:
        self.access = access
        self._told: dict[int, float] = {}

    @staticmethod
    def user_id(update) -> int | None:
        user = update.effective_user
        return user.id if user else None

    async def allow(self, update) -> bool:
        """True for the owner and admins. Anyone else gets a short refusal (rate limited)."""
        await self.access.ensure_loaded()
        if self.access.is_admin(self.user_id(update)):
            return True
        await self._deny(update, DENY_TEXT)
        return False

    async def allow_public(self, update) -> bool:
        """For the few things anybody may do (session export of their own account): private chats only."""
        chat = update.effective_chat
        return chat is None or chat.type == "private"

    async def allow_owner(self, update) -> bool:
        if not await self.allow(update):
            return False
        if self.access.is_owner(self.user_id(update)):
            return True
        await self._deny(update, OWNER_TEXT, throttle=False)
        return False

    async def _deny(self, update, text: str, throttle: bool = True) -> None:
        chat = update.effective_chat
        if chat is not None and chat.type != "private":
            return  # never chat with strangers in groups
        query = update.callback_query
        if query is not None:
            await query.answer(text, show_alert=True)
            return
        message = update.effective_message
        uid = self.user_id(update)
        if message is None:
            return
        if throttle and uid is not None:
            now = time.monotonic()
            if now - self._told.get(uid, -DENY_REPLY_INTERVAL) < DENY_REPLY_INTERVAL:
                return
            self._told[uid] = now
            if len(self._told) > 1000:
                self._told = {k: v for k, v in self._told.items() if now - v < DENY_REPLY_INTERVAL}
        await message.reply_text(text)
