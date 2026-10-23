"""Runs before every handler (group -1) and on errors."""
from __future__ import annotations

import logging

from telegram.error import Conflict, NetworkError

from telegram_manager.bot import reply_menu
from telegram_manager.errors import describe_error
from telegram_manager.log_setup import mask_id

logger = logging.getLogger(__name__)


class Middleware:
    def __init__(self, deps, engine) -> None:
        self.deps = deps
        self.engine = engine

    async def track_update(self, update, context) -> None:
        """Logs what happened (never text, secrets or raw ids) and abandons a half-finished
        flow when an admin taps a menu button or runs a command."""
        access = self.deps.access
        await access.ensure_loaded()
        user = update.effective_user
        uid = user.id if user else None
        admin = access.is_admin(uid)
        query = update.callback_query
        message = update.effective_message
        if query is not None:
            data = query.data or ""
            logger.info("Button tap | user=%s admin=%s action=%s", mask_id(uid), admin, data.split("|", 1)[0])
            if not data.startswith("wz|"):
                context.user_data.pop("ai_wait", None)  # a button cancels a pending settings answer
                await self.engine.reset(context)  # also closes a stranger's half-finished export login
        elif message is not None and (message.text or "").startswith("/"):
            command = message.text.split(maxsplit=1)[0].split("@")[0]
            logger.info("Command | user=%s admin=%s cmd=%s", mask_id(uid), admin, command)
            context.user_data.pop("ai_wait", None)
            await self.engine.reset(context)
        elif message is not None and reply_menu.lookup(message.text) is not None:
            logger.info("Menu button | user=%s admin=%s action=%s", mask_id(uid), admin, reply_menu.lookup(message.text).key)
            await self.engine.reset(context)  # like a command: leave any half-finished flow
        elif message is not None and message.text:
            state = self.engine.active(context)
            logger.info(
                "Text message | user=%s admin=%s chars=%d step=%s reply=%s",
                mask_id(uid),
                admin,
                len(message.text),
                state.step if state else "-",
                message.reply_to_message is not None,
            )
        elif getattr(update, "message_reaction", None) is not None:
            logger.info("Reaction update | user=%s", mask_id(uid))
        else:
            logger.debug("Other update | user=%s", mask_id(uid))

    async def on_error(self, update, context) -> None:
        error = context.error
        if isinstance(error, Conflict):
            logger.warning(
                "Telegram reports another instance of this bot is polling (Conflict). This is normal for a few "
                "seconds during a redeploy; if it persists, stop the other copy."
            )
            return
        if isinstance(error, NetworkError):
            logger.warning("Network problem talking to Telegram: %s", describe_error(error))
            return
        logger.error("Unhandled error while processing an update", exc_info=error)
        user = getattr(update, "effective_user", None)
        target = user.id if user is not None and self.deps.access.is_admin(user.id) else self.deps.settings.owner_id
        try:
            await context.bot.send_message(target, f"⚠️ Something went wrong: {describe_error(error)}")
        except Exception:
            logger.debug("Could not tell the admin about the error", exc_info=True)
