"""One way to put a screen in front of the user: edit the tapped message, or reply to a typed one."""
from __future__ import annotations

import logging
from contextlib import suppress

from telegram.error import BadRequest

logger = logging.getLogger(__name__)


async def show(update, text: str, reply_markup=None, parse_mode: str | None = None):
    """Returns the message that now carries the screen (so a caller can keep editing it), or None.
    parse_mode="HTML" is for screens that escape their own dynamic text."""
    extra = {"parse_mode": parse_mode} if parse_mode else {}
    query = update.callback_query
    if query is not None and query.message is not None:
        try:
            edited = await query.edit_message_text(text, reply_markup=reply_markup, **extra)
        except BadRequest as exc:
            reason = str(exc).lower()
            if "not modified" in reason:
                return query.message
            if "no text in the message" in reason or "there is no text" in reason:
                # The tapped message is a photo (the start screen with an image): swap it for a text screen.
                with suppress(Exception):
                    await query.message.delete()
                return await query.get_bot().send_message(
                    chat_id=query.message.chat_id, text=text, reply_markup=reply_markup, **extra
                )
            raise
        return edited if hasattr(edited, "message_id") else query.message
    message = update.effective_message
    if message is not None:
        return await message.reply_text(text, reply_markup=reply_markup, **extra)
    return None


_SENDERS = {"photo": "send_photo", "animation": "send_animation", "video": "send_video"}


async def show_media(update, context, media: str, kind: str, caption: str, reply_markup=None, parse_mode: str | None = None):
    """A screen with a photo, GIF or video on top. A tapped text message is replaced (Telegram cannot turn a
    text message into a media one). Falls back to plain text if the media cannot be sent."""
    query = update.callback_query
    chat = update.effective_chat
    if query is not None and query.message is not None:
        with suppress(Exception):
            await query.message.delete()
    extra = {"parse_mode": parse_mode} if parse_mode else {}
    if kind == "video":
        extra["supports_streaming"] = True
    sender = getattr(context.bot, _SENDERS.get(kind, "send_photo"))
    try:
        return await sender(chat.id, media, caption=caption, reply_markup=reply_markup, **extra)
    except Exception as exc:
        logger.warning("Could not send the start %s: %s", kind, exc)
        extra.pop("supports_streaming", None)
        return await context.bot.send_message(chat.id, caption, reply_markup=reply_markup, **extra)


async def show_photo(update, context, photo: str, caption: str, reply_markup=None, parse_mode: str | None = None):
    return await show_media(update, context, photo, "photo", caption, reply_markup, parse_mode)
