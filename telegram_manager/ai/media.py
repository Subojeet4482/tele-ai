"""The picture an admin sent to the bot, kept for the AI's tools (profile photo, send to somebody).

Lives in the admin's own ``user_data`` (memory only, never saved), is forgotten after an hour and is replaced
by the next picture. The AI never sees the picture itself, only a note that one is waiting."""
from __future__ import annotations

import secrets
import time
from typing import Any

from telegram_manager.constants import MAX_UPLOAD_BYTES, UPLOAD_TTL_SECONDS

MEDIA_KEY = "ai_media"


def keep(user_data: dict, data: bytes, name: str = "photo.jpg", as_file: bool = False, caption: str = "") -> dict[str, Any]:
    if not data:
        raise ValueError("The picture is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("That picture is too big (max 10 MB).")
    item = {"id": secrets.token_hex(3), "data": bytes(data), "name": name or "photo.jpg", "as_file": bool(as_file),
            "caption": (caption or "")[:900], "at": time.time()}
    user_data[MEDIA_KEY] = item
    return item


def current(user_data: dict) -> dict[str, Any] | None:
    """The picture waiting for the AI (None when there is none or it is older than an hour)."""
    item = user_data.get(MEDIA_KEY)
    if not item:
        return None
    if time.time() - item["at"] > UPLOAD_TTL_SECONDS:
        user_data.pop(MEDIA_KEY, None)
        return None
    return item


def need(user_data: dict) -> dict[str, Any]:
    item = current(user_data)
    if item is None:
        raise ValueError("Koi photo nahi mili: pehle admin se photo bhejne ko kaho (is chat mein photo bhejein), phir yeh tool chalao.")
    return item


def note(user_data: dict) -> str:
    """One line for the AI's instructions (empty when no picture is waiting)."""
    item = current(user_data)
    if item is None:
        return ""
    return (f"UPLOADED PICTURE: the admin sent you a picture ({len(item['data']) // 1024} KB, {item['name']}). "
            "set_profile_photo puts it on accounts as their profile photo; send_photo sends it to a chat. You cannot see it.")
