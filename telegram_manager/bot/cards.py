"""Small helpers for the HTML-styled screens (bold titles, quote cards). Everything dynamic is escaped."""
from __future__ import annotations

import html
import re

from telegram_manager.constants import CAPTION_LIMIT, MESSAGE_LIMIT

_TAG = re.compile(r"<[^>]+>")


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"


def quote(lines: list[str], expandable: bool = False) -> str:
    """A quote card. `lines` must already be escaped. An expandable one shows 3 lines until tapped."""
    tag = "blockquote expandable" if expandable else "blockquote"
    return f"<{tag}>" + "\n".join(lines) + "</blockquote>"


def visible_length(markup: str) -> int:
    """How many characters Telegram counts once the tags are parsed (caption limit is 1024)."""
    return len(html.unescape(_TAG.sub("", markup)))


def fit(candidates: list[list[str]], has_media: bool) -> str:
    """The first block list whose text fits the message / caption limit; the last one when none does."""
    limit = CAPTION_LIMIT if has_media else MESSAGE_LIMIT
    text = ""
    for blocks in candidates:
        text = "\n\n".join(block for block in blocks if block)
        if visible_length(text) <= limit:
            return text
    return text
