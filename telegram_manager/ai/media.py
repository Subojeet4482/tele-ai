"""The picture / video / voice message / file an admin sent to the bot, kept for the AI's tools (profile photo, send to somebody).

Lives in the admin's own ``user_data`` (memory only, never saved), is forgotten after an hour and is replaced
by the next picture. The AI never sees the picture itself, only a note that one is waiting."""
from __future__ import annotations

import secrets
import time
from typing import Any

from telegram_manager.constants import MAX_UPLOAD_BYTES, UPLOAD_TTL_SECONDS

MEDIA_KEY = "ai_media"


KINDS = ("photo", "video", "voice", "audio", "document")


def keep(user_data: dict, data: bytes, name: str = "photo.jpg", as_file: bool = False, caption: str = "", kind: str = "photo") -> dict[str, Any]:
    if not data:
        raise ValueError("The picture is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("That file is too big (max 20 MB).")
    if kind not in KINDS:
        raise ValueError("Unsupported kind of file.")
    item = {"id": secrets.token_hex(3), "kind": kind, "data": bytes(data), "name": name or "photo.jpg", "as_file": bool(as_file),
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
        raise ValueError("Koi photo ya file nahi mili: pehle admin se photo / video / file is chat mein bhejne ko kaho, phir yeh tool chalao.")
    return item


def need_photo(user_data: dict) -> dict[str, Any]:
    item = need(user_data)
    if item["kind"] != "photo":
        raise ValueError(f"The uploaded file is a {item['kind']}, not a picture: a profile photo needs a photo.")
    return item


def note(user_data: dict) -> str:
    """One line for the AI's instructions (empty when no picture is waiting)."""
    item = current(user_data)
    if item is None:
        return ""
    return (f"UPLOADED FILE: the admin sent you a {item['kind']} ({len(item['data']) // 1024} KB, {item['name']}). "
            "send_photo sends it to a chat (any kind); set_profile_photo and group_settings photo (photos only) use it as a picture. You cannot see or hear it.")
