"""Typed messages (flow answers, replies to forwards) and reactions."""
from __future__ import annotations

import logging

from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.constants import MAX_UPLOAD_BYTES
from telegram_manager.errors import describe_error, explain_send_error

logger = logging.getLogger(__name__)


MAX_PHOTO_BYTES = 10 * 1024 * 1024  # Telegram accepts profile photos far below this


class TextHandlers(HandlerBase):
    async def _allowed(self, update, context) -> bool:
        """Admins always; somebody else only while they are inside the public session export."""
        await self.deps.access.ensure_loaded()
        if self.deps.access.is_admin(self.uid(update)):
            return True
        state = self.engine.active(context)
        spec = self.engine.specs.get(state.name) if state is not None else None
        if spec is not None and spec.public and await self.guard.allow_public(update):
            return True
        return await self.guard.allow(update)  # tells them it is for admins

    async def photo_input(self, update, context) -> None:
        """A photo (or image file) sent to the bot: only used by a step that asks for one."""
        if not await self._allowed(update, context):
            return
        message = update.effective_message
        if message is None:
            return
        state = self.engine.active(context)
        source = message.photo[-1] if getattr(message, "photo", None) else getattr(message, "document", None)
        if source is None:
            return
        if getattr(source, "file_size", 0) and source.file_size > MAX_PHOTO_BYTES:
            await message.reply_text("❌ That image is too big. Send one under 10 MB.")
            return
        try:
            data = bytes(await (await source.get_file()).download_as_bytearray())
        except Exception as exc:
            await message.reply_text(f"❌ Could not read that photo: {describe_error(exc)}")
            return
        if state is not None:  # inside a flow: only a step that asks for a picture takes it, anything else is ignored as before
            await self.engine.on_photo(update, context, data, getattr(source, "file_id", None))
            return
        is_file = not getattr(message, "photo", None)
        await self.upload_for_ai(update, context, data, "photo", (getattr(source, "file_name", None) or "photo.jpg") if is_file else "photo.jpg", as_file=is_file)

    async def media_input(self, update, context) -> None:
        """A GIF, video, voice message or audio sent to the bot: a flow step that asks for a GIF / video takes it
        (the start screen); outside a flow it is kept for the AI's tools (send it to somebody)."""
        if not await self._allowed(update, context):
            return
        message = update.effective_message
        found = [(kind, getattr(message, attr, None)) for attr, kind in
                 (("animation", "animation"), ("video", "video"), ("video_note", "video"), ("voice", "voice"), ("audio", "audio"))]
        kind, source = next(((k, v) for k, v in found if v is not None), (None, None))
        if source is None:
            return
        if self.engine.active(context) is not None:
            if kind in ("animation", "video"):
                await self.engine.on_media(update, context, kind, source.file_id)
            return
        if getattr(source, "file_size", 0) and source.file_size > MAX_UPLOAD_BYTES:
            await message.reply_text("❌ That file is too big. A bot can take up to 20 MB.")
            return
        try:
            data = bytes(await (await source.get_file()).download_as_bytearray())
        except Exception as exc:
            await message.reply_text(f"❌ Could not read that file: {describe_error(exc)}")
            return
        defaults = {"animation": "video.mp4", "video": "video.mp4", "voice": "voice.ogg", "audio": "audio.mp3"}
        await self.upload_for_ai(update, context, data, "video" if kind == "animation" else kind,
                                 getattr(source, "file_name", None) or defaults[kind])

    async def custom_emoji_input(self, update, context) -> None:
        """An admin sends a message with custom emoji: tell them the IDs (for BUTTON_ICONS)."""
        if self.engine.active(context) is not None:
            await self.text_input(update, context)  # a flow is waiting for text: this is its answer
            return
        if not await self._allowed(update, context):
            return
        message = update.effective_message
        text = message.text or ""
        raw = text.encode("utf-16-le")
        found = []
        for entity in message.entities or []:
            if getattr(entity, "type", "") != "custom_emoji":
                continue
            char = raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le", "ignore")
            found.append((char, entity.custom_emoji_id))
        if not found:
            return
        lines = ["🔎 Custom emoji IDs:"] + [f"{char} → {emoji_id}" for char, emoji_id in found]
        sample = ", ".join(f'"{char}": "{emoji_id}"' for char, emoji_id in found)
        lines += ["", "To use them as button icons, put this in BUTTON_ICONS (the emoji on the left is the one a button starts with):", "{" + sample + "}"]
        await message.reply_text("\n".join(lines))

    async def text_input(self, update, context) -> None:
        if not await self._allowed(update, context):
            return
        message = update.effective_message
        text = (message.text or "").strip()
        if not text:
            return
        if await self.engine.on_text(update, context, text):
            return
        replied = message.reply_to_message
        ai = getattr(self, "ai", None)  # set by the registry
        if ai is not None and await ai.pre_route(update, context, text):
            return  # a settings answer, or a key/API hash typed into the chat (kept away from the AI)
        force_ai = ai is not None and ai.has_prefix(text)  # "ai ..." on a forwarded message asks the AI instead of replying
        if replied is not None and not force_ai:
            try:
                handled = await self.manager.reply_via_forward(
                    self.uid(update), update.effective_chat.id, replied.message_id, text
                )
            except Exception as exc:
                logger.warning("Reply was not sent: %s", describe_error(exc))
                await message.reply_text(f"❌ Reply was not sent: {explain_send_error(exc)}")
                return
            if handled:
                await message.reply_text("✅ Reply sent to that chat.")
                return
        if ai is None:
            await message.reply_text("Use /start or /menu to open the account manager.")
            return
        mode = await ai.ui_mode(self.uid(update))
        if mode != "manual" or force_ai:  # Manual mode keeps plain text away from the AI; "ai ..." and /ai still reach it
            if await ai.ask(update, context, text, replied):
                return
        await ai.not_answered(update, "ai" if force_ai and mode == "manual" else mode)

    async def on_reaction(self, update, context) -> None:
        reaction_update = update.message_reaction
        if reaction_update is None:
            return
        user = reaction_update.user
        await self.deps.access.ensure_loaded()
        if not user or not self.deps.access.is_admin(user.id):
            return  # only admins' reactions are mirrored
        emoji = None
        for reaction in reaction_update.new_reaction or []:
            candidate = getattr(reaction, "emoji", None)
            if candidate:
                emoji = candidate
                break
        if not emoji:
            return  # a reaction was removed, or it is not a plain emoji
        try:
            await self.manager.react_via_forward(user.id, reaction_update.chat.id, reaction_update.message_id, emoji)
        except Exception as exc:
            logger.exception("Could not mirror reaction to the original chat")
            await self.manager.notify(
                user.id, f"⚠️ Could not place your {emoji} reaction on the original message: {describe_error(exc)}"
            )
