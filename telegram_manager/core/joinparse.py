"""Pure helpers for 'join first' (force-join) flows: find the channels a bot asks for and the
button that confirms it. No Telegram calls here, so it is easy to test."""
from __future__ import annotations

import re
from urllib.parse import parse_qs

_HOSTS = r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/"
_INVITE = re.compile(_HOSTS + r"((?:joinchat/|\+)[A-Za-z0-9_-]{10,})", re.I)
_PUBLIC = re.compile(_HOSTS + r"([A-Za-z][A-Za-z0-9_]{3,31})(?![A-Za-z0-9_/+])", re.I)
_AT_NAME = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{4,31})")
_NOT_CHANNELS = {"addlist", "addstickers", "addemoji", "joinchat", "share", "proxy", "socks", "setlanguage", "boost"}

VERIFY_WORDS = (
    "joined", "i joined", "verify", "check", "done", "confirm", "continue", "i have joined",
    "join kar liya", "ho gaya", "maine join", "सत्यापित", "✅",
)


def extract_join_targets(texts: list[str]) -> list[str]:
    """Links and @names a message asks you to join, in order, without duplicates. A bot's own
    @name is skipped (names ending in 'bot')."""
    found: list[str] = []
    for text in texts:
        for match in _INVITE.finditer(text or ""):
            found.append("https://t.me/" + match.group(1))
        for match in _PUBLIC.finditer(text or ""):
            name = match.group(1)
            if name.lower() not in _NOT_CHANNELS and not name.lower().endswith("bot") and not name.lower().startswith("+"):
                found.append("@" + name)
        for match in _AT_NAME.finditer(text or ""):
            if not match.group(1).lower().endswith("bot"):
                found.append("@" + match.group(1))
    seen: set[str] = set()
    unique = []
    for item in found:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def pick_verify_button(button_texts: list[str]) -> str | None:
    """The button that most likely says 'I joined, check me' (None when nothing looks like it)."""
    for word in VERIFY_WORDS:
        for text in button_texts:
            if word.casefold() in (text or "").casefold():
                return text
    return None


# --- starting a bot --------------------------------------------------------------------------------
_BOT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_PAYLOAD = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TME = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/([^/?#\s]+)(?:/[^?#\s]*)?(?:\?([^#\s]*))?", re.I)
_TG_RESOLVE = re.compile(r"^tg://resolve\?(\S*)$", re.I)

# Words a bot uses when it will not work before you joined something (English, Hinglish, Hindi, Russian, Arabic).
JOIN_HINTS = re.compile(
    r"join|subscribe|channel|group|must|first|mandatory|required|pehle|zaroori|जॉइन|ज्वाइन|सब्सक्राइब|चैनल|"
    r"подпис|присоед|канал|اشترك|انضم|قناة",
    re.I,
)


def clean_payload(payload: str) -> str:
    payload = (payload or "").strip()
    if payload and not _PAYLOAD.fullmatch(payload):
        raise ValueError("The start payload may only have letters, digits, _ and - (up to 64 characters).")
    return payload


def parse_bot_ref(ref: str) -> tuple[str, str]:
    """(bot username, start payload) from '@bot', 'bot', 't.me/bot?start=abc', 'https://t.me/bot?start=abc',
    'tg://resolve?domain=bot&start=abc' or '@bot /start abc'. The payload is '' when there is none."""
    text = (ref or "").strip()
    if not text:
        raise ValueError("Which bot? Give its @username or its link.")
    payload = ""
    resolved = _TG_RESOLVE.match(text)
    if resolved:
        params = parse_qs(resolved.group(1))
        name, payload = (params.get("domain") or [""])[0], (params.get("start") or [""])[0]
    else:
        link = _TME.match(text)
        if link:
            params = parse_qs(link.group(2) or "")
            name, payload = link.group(1), (params.get("start") or [""])[0]
        else:
            head, _, rest = text.partition(" ")
            name, payload = head, re.sub(r"^/start\s*", "", rest.strip())
    name = name.lstrip("@")
    if not _BOT_NAME.fullmatch(name):
        raise ValueError(f"'{ref}' does not look like a bot @username or t.me link.")
    return name, clean_payload(payload)


def looks_like_force_join(texts: list[str], button_texts: list[str]) -> bool:
    """Does the bot's answer ask to join something first (so joining its links is what the admin wants)?"""
    if pick_verify_button(button_texts):
        return True
    return any(JOIN_HINTS.search(t or "") for t in texts)


def message_links(message) -> list[str]:
    """Every link of one Telegram message: URL buttons and hidden links behind words (Telethon objects)."""
    found: list[str] = []
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            url = getattr(button, "url", None)
            if url:
                found.append(str(url))
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            found.append(str(url))
    return list(dict.fromkeys(found))
