"""Owner only: hand out a one-time link to the live logs page."""
from __future__ import annotations

from telegram import LinkPreviewOptions

from telegram_manager.bot import keyboards
from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.constants import LOGIN_KEY_TTL


class LogsHandlers(HandlerBase):
    async def logs(self, update, context) -> None:
        if not await self.entry_owner(update):
            return
        message = update.effective_message
        access = self.deps.log_access
        base = self.deps.settings.public_url
        if access is None or self.deps.log_buffer is None:
            await message.reply_text("📜 The logs page is turned off.")
            return
        if not base:
            await message.reply_text("📜 Set PUBLIC_URL (your app's https address) so I can build the link.")
            return
        key = access.new_login_key()
        url = f"{base.rstrip('/')}/logs/login?key={key}"
        text = (
            f"📜 Live logs\n{url}\n\n"
            f"• The link works once and expires in {LOGIN_KEY_TTL // 60} minutes.\n"
            "• Opening it starts a 12-hour browser session; no secret stays in the address bar.\n"
            "• Ids in the logs are masked."
        )
        await message.reply_text(
            text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),  # a preview fetch must not use the link up
            reply_markup=keyboards.back("menu", "⚙️ Menu"),
        )
