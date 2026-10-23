"""Incoming messages: decide whether to forward, then deliver to the admin who owns the account."""
from __future__ import annotations

import asyncio
import logging
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from telegram.error import BadRequest
from telethon import events

from telegram_manager.constants import (
    CAPTION_LIMIT,
    DOWNLOAD_TIMEOUT,
    MAX_BOT_UPLOAD_BYTES,
    UPLOAD_READ_TIMEOUT,
    UPLOAD_WRITE_TIMEOUT,
)
from telegram_manager.core.base import chunks
from telegram_manager.errors import describe_error
from telegram_manager.log_setup import mask_id

logger = logging.getLogger(__name__)

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac"}
VOICE_SUFFIXES = {".ogg", ".oga", ".opus"}  # Telethon saves voice notes as .oga


class ForwardingMixin:
    def _register_handlers(self, account_id: str, client: Any) -> None:
        @client.on(events.NewMessage(incoming=True))
        async def incoming(event: events.NewMessage.Event) -> None:
            try:
                await self._handle_incoming(account_id, event)
            except Exception:
                logger.exception("Forwarding failed for account %s", account_id)

    def _is_from_this_bot(self, sender_id: int | None, chat_id: int | None) -> bool:
        """True for messages our own bot sent. If an admin's own account is connected, every
        forwarded message arrives there as an incoming "bot message"; forwarding it again
        would loop forever."""
        bot_id = self.bot_user_id
        return bool(bot_id and (sender_id == bot_id or chat_id == bot_id))

    async def _handle_incoming(self, account_id: str, event: events.NewMessage.Event) -> None:
        sender_id = getattr(event, "sender_id", None)
        chat_id = getattr(event, "chat_id", None)
        message_id = getattr(getattr(event, "message", None), "id", None)
        if self._is_from_this_bot(sender_id, chat_id):
            logger.debug("Skipped message %s: it was sent by this bot itself", message_id)
            return
        account = self.accounts.get(account_id) or await self.store.accounts.get(account_id)
        if not account:
            logger.warning("Incoming message for unknown account %s ignored", account_id)
            return
        forward, reason = await self._should_forward(account, event)
        logger.info(
            "Incoming message | account=%s chat=%s sender=%s msg=%s -> %s (%s)",
            self.label_for_account(account),
            mask_id(chat_id),
            mask_id(sender_id),
            message_id,
            "forward" if forward else "skip",
            reason,
        )
        if forward:
            await self.forward_event(account, event)
        hook = getattr(self, "incoming_hook", None)  # the AI's private chats
        if hook is not None and getattr(event, "is_private", False):
            try:
                await hook(account, event)
            except Exception:
                logger.exception("The AI chat hook failed for account %s", account_id)

    async def _should_forward(self, account: dict[str, Any], event: events.NewMessage.Event) -> tuple[bool, str]:
        global_settings = await self.store.settings.get_forwarding(self._owner_of(account))
        kind = await self._classify_sender(event)
        name = {
            "channel": "forward_channel_messages",
            "bot": "forward_bot_messages",
            "user": "forward_user_messages",
        }[kind]
        override = account.get(name)
        enabled = bool(global_settings[name] if override is None else override)
        source = "global setting" if override is None else "account override"
        return enabled, f"{kind} message, {source} {'ON' if enabled else 'OFF'}"

    async def _classify_sender(self, event: events.NewMessage.Event) -> str:
        """\"bot\", \"user\" or \"channel\" - three simple rules:

        1. A bot is a bot anywhere: Telegram's `bot` flag is set, or the username ends in "bot".
        2. A real person writing in a private chat (DM) is a user.
        3. Everything else is a channel: channel posts, group and supergroup messages, messages
           posted in a group as a channel, anonymous admins, or a sender that cannot be identified
           outside a DM."""
        if event.is_channel and not event.is_group:
            return "channel"  # broadcast channel posts are never bots or users
        try:
            sender = await event.get_sender()  # event.sender is often not loaded yet
        except Exception:
            sender = None
        is_dm = bool(getattr(event, "is_private", False))
        if sender is None:
            return "user" if is_dm else "channel"
        if not hasattr(sender, "first_name") and hasattr(sender, "title"):
            return "channel"  # Channel / Chat entity: real users always have first_name
        if self._looks_like_bot(sender):
            return "bot"
        return "user" if is_dm else "channel"

    @staticmethod
    def _looks_like_bot(sender: Any) -> bool:
        if getattr(sender, "bot", False):
            return True
        names = [getattr(sender, "username", None)]
        names += [getattr(item, "username", None) for item in (getattr(sender, "usernames", None) or [])]
        return any(isinstance(name, str) and name.lower().endswith("bot") for name in names)

    async def forward_event(self, account: dict[str, Any], event: events.NewMessage.Event) -> None:
        """Deliver to the admin who owns this account, and only to them."""
        chat = self._owner_of(account)
        lines = [f"📱 Account {account.get('number') or '?'} — {self.label_for_account(account)}"]
        chat_line = await self._describe_chat(event)
        if chat_line:
            lines.append(f"💭 Chat: {chat_line}")
        sender_line = await self._describe_sender(event)
        if sender_line:
            lines.append(f"👤 From: {sender_line}")
        message = event.message
        origin_line = await self._describe_forward_origin(message)
        if origin_line:
            lines.append(f"↪️ Originally forwarded from: {origin_line}")
        quoted_line = await self._describe_reply_quote(message)
        if quoted_line:
            lines.append(f'↩️ Replying to: "{quoted_line}"')

        header = "\n".join(lines)
        body = message.message or ""
        peer_id = event.chat_id
        original_message_id = message.id

        def remember(sent: Any) -> None:
            self._remember_reply_target(chat, sent.message_id, account["id"], peer_id, original_message_id)

        media_note = ""
        if message.media:
            try:
                size = getattr(message.file, "size", None) or 0
                if size > MAX_BOT_UPLOAD_BYTES:
                    media_note = "\n\n📎 [media is too large to forward]"
                else:
                    with tempfile.TemporaryDirectory(prefix="telegram-forward-") as temp_dir:
                        path = await asyncio.wait_for(message.download_media(file=temp_dir), DOWNLOAD_TIMEOUT)
                        if path:
                            await self._forward_with_media(
                                chat, path, header, body, remember, sticker=bool(getattr(message, "sticker", None))
                            )
                            logger.info("Forwarded media from account %s", account["id"])
                            return
            except Exception:
                logger.exception("Could not forward media for account %s", account["id"])
                media_note = "\n\n📎 [media could not be forwarded]"

        text = (f"{header}\n\n💬 {body}" if body else header) + media_note
        for part in chunks(text):
            remember(await self.bot.send_message(chat_id=chat, text=part))
        logger.info("Forwarded text from account %s (%d chars)", account["id"], len(body))

    async def _forward_with_media(
        self,
        chat: int,
        path: str,
        header: str,
        body: str,
        remember: Callable[[Any], None],
        sticker: bool = False,
    ) -> None:
        """Send the media with as much of the text as fits in a caption.

        Once the media is out this never raises: the caller answers an exception by sending the
        whole message again as text, which would deliver it twice."""
        if sticker and await self._forward_sticker(chat, path, header, remember):
            return
        caption = f"{header}\n\n💬 {body}" if body else header
        if len(caption) <= CAPTION_LIMIT:
            remember(await self._send_media(chat, path, caption))
            return
        remember(await self._send_media(chat, path, header[:CAPTION_LIMIT]))
        try:
            for part in chunks(f"💬 {body}"):
                remember(await self.bot.send_message(chat_id=chat, text=part))
        except Exception:
            logger.exception("The media was forwarded, but its long text could not be sent")
            with suppress(Exception):
                await self.bot.send_message(
                    chat_id=chat, text="📎 [the media was forwarded, but the rest of the text could not be sent]"
                )

    async def _forward_sticker(self, chat: int, path: str, header: str, remember: Callable[[Any], None]) -> bool:
        """Send a sticker as a real sticker (stickers cannot carry a caption, so the header follows
        as a reply to it). False means Telegram refused it: the caller sends it as a file instead."""
        timeouts = {"read_timeout": UPLOAD_READ_TIMEOUT, "write_timeout": UPLOAD_WRITE_TIMEOUT}
        try:
            with open(path, "rb") as media:
                sent = await self.bot.send_sticker(chat_id=chat, sticker=media, **timeouts)
        except BadRequest as exc:
            logger.info("Bot API refused the sticker (%s); sending it as a document instead", describe_error(exc))
            return False
        remember(sent)
        try:
            remember(await self.bot.send_message(chat_id=chat, text=header, reply_to_message_id=sent.message_id))
        except Exception:
            logger.exception("The sticker was forwarded, but its header could not be sent")
        return True

    async def _describe_chat(self, event: events.NewMessage.Event) -> str:
        """Name of the group/channel the message came from. Empty for private chats, where the
        sender line already says everything."""
        if getattr(event, "is_private", True):
            return ""
        try:
            chat = await event.get_chat()
        except Exception:
            return ""
        return self.label_for_entity(chat)

    async def _describe_sender(self, event: events.NewMessage.Event) -> str:
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        return self.label_for_entity(sender)

    async def _describe_forward_origin(self, message: Any) -> str:
        """If the incoming message is itself a forward, name the original author."""
        forward = getattr(message, "forward", None)
        if not forward:
            return ""
        try:
            origin_entity = await forward.get_sender()
        except Exception:
            origin_entity = None
        return self.label_for_entity(origin_entity) or getattr(forward, "from_name", None) or ""

    async def _describe_reply_quote(self, message: Any) -> str:
        """Short preview of the message the incoming message replies to."""
        if not getattr(message, "is_reply", False):
            return ""
        try:
            quoted = await message.get_reply_message()
        except Exception:
            quoted = None
        if quoted is None:
            return ""
        text = (getattr(quoted, "message", "") or "").strip()
        if not text:
            return "[media]" if getattr(quoted, "media", None) else ""
        return text[:120] + ("…" if len(text) > 120 else "")

    async def _send_media(self, chat: int, path: str, caption: str):
        suffix = Path(path).suffix.lower()
        caption = caption[:CAPTION_LIMIT]
        if suffix in PHOTO_SUFFIXES:
            method, field = self.bot.send_photo, "photo"
        elif suffix in VIDEO_SUFFIXES:
            method, field = self.bot.send_video, "video"
        elif suffix in AUDIO_SUFFIXES:
            method, field = self.bot.send_audio, "audio"
        elif suffix in VOICE_SUFFIXES:
            method, field = self.bot.send_voice, "voice"
        else:
            method, field = self.bot.send_document, "document"
        timeouts = {"read_timeout": UPLOAD_READ_TIMEOUT, "write_timeout": UPLOAD_WRITE_TIMEOUT}
        try:
            with open(path, "rb") as media:
                return await method(chat_id=chat, caption=caption, **timeouts, **{field: media})
        except BadRequest as exc:
            if field == "document":
                raise
            # e.g. a sticker (.webp) or a huge photo Telegram refuses as a photo: send it as a file.
            logger.info("Bot API refused the %s (%s); sending it as a document instead", field, describe_error(exc))
            with open(path, "rb") as media:
                return await self.bot.send_document(chat_id=chat, document=media, caption=caption, **timeouts)
