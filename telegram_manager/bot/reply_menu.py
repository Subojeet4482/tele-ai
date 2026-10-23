"""The bottom (reply) keyboard: the home menu that stays under the chat like a normal app's tab bar.

Tapping a button sends its text as a message; MenuButtons.press() turns that text back into the
same action as the matching inline button. With custom emoji icons switched on, Telegram sends the
text WITHOUT the leading emoji, so lookup() understands both spellings."""
from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass

from telegram_manager.bot import keyboards
from telegram_manager.bot.handlers.base import HandlerBase

logger = logging.getLogger(__name__)

PLACEHOLDER = "Pick an option 👇"
AI_PLACEHOLDER = "Kuch bhi likho — main kar dunga…"


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    style: str | None = None
    owner_only: bool = False


# One row per line; same idea as a shop bot's home keyboard: one wide green action, then a 2-column grid.
LAYOUT: list[list[Item]] = [
    [Item("connect", "🔗 Connect Account", "success")],
    [Item("accounts", "📋 My Accounts", "primary"), Item("sendall", "📨 Message All", "primary")],
    [Item("sendone", "💬 Message One", "primary"), Item("click", "🖱 Click Buttons", "primary")],
    [Item("daily", "🕒 Daily Broadcast", "primary"), Item("forwarding", "📥 Forwarding", "primary")],
    [Item("give", "🎁 Give Accounts", "primary"), Item("export", "📤 Export Session", "primary")],
    [Item("startscreen", "🎨 Start Screen", "primary"), Item("menu", "⚙️ Menu", "primary")],
    [Item("settings", "⚙️ Settings", "primary"), Item("security", "🔐 Security", "primary")],
    [Item("logout", "🚪 Logout", "danger"), Item("help", "📖 Help", "primary")],
    [Item("admins", "👑 Admins", "primary", True), Item("logs", "📜 Live Logs", "primary", True)],
]
# AI mode keeps the chat clean: you just write, and only the Menu button sits below.
AI_LAYOUT: list[list[Item]] = [
    [Item("menu", "⚙️ Menu", "primary")],
]
# Buttons of the older AI keyboard. Phones keep an old keyboard until a new one is sent, so taps on them still work.
LEGACY_AI_ITEMS: list[Item] = [
    Item("ai_settings", "⚙️ AI Settings", "primary"),
    Item("ai_keys", "🔑 AI Keys", "primary"),
    Item("ai_new", "🧹 Nayi Baat", "primary"),
    Item("to_manual", "🖐 Manual Mode", "primary"),
]
ITEMS: dict[str, Item] = {item.key: item for item in (*(i for row in (*LAYOUT, *AI_LAYOUT) for i in row), *LEGACY_AI_ITEMS)}


def keyboard(is_owner: bool):
    rows = []
    for layout_row in LAYOUT:
        row = [keyboards.KBtn(item.label, style=item.style) for item in layout_row if is_owner or not item.owner_only]
        if row:
            rows.append(row)
    return keyboards.reply_keyboard(rows, PLACEHOLDER)


def ai_keyboard():
    """The small bottom keyboard of AI mode."""
    rows = [[keyboards.KBtn(item.label, style=item.style) for item in row] for row in AI_LAYOUT]
    return keyboards.reply_keyboard(rows, AI_PLACEHOLDER)


def _spellings() -> dict[str, Item]:
    """Every text a tap can arrive as: the label, and the label without its emoji when an icon replaces it."""
    found: dict[str, Item] = {}
    for item in ITEMS.values():
        for text in (item.label, keyboards.strip_icon(item.label)):
            found[text.strip()] = item
    return found


def lookup(text: str | None) -> Item | None:
    if not text:
        return None
    return _spellings().get(text.strip())


def pattern() -> str:
    """Regex for the message filter (recomputed at wiring time, after the icons were configured)."""
    import re

    return "^(" + "|".join(re.escape(text) for text in sorted(_spellings(), key=len, reverse=True)) + ")$"


class MenuButtons(HandlerBase):
    def __init__(self, deps, engine, parts) -> None:
        super().__init__(deps, engine)
        self.parts = parts  # the other handler objects, filled in by wire()

    async def _dispatch(self, key: str, update, context) -> None:
        p = self.parts
        if key == "sendall":
            await p.commands.open_bulk(update, context, "all")
        elif key == "click":
            await p.commands.open_bulk(update, context, "click")
        elif key == "to_manual":
            await p.start.switch_mode(update, context, "manual")
        else:
            handler = {
                "connect": p.accounts.connect_menu,
                "accounts": p.accounts.list_accounts,
                "sendone": p.commands.message_button,
                "daily": p.schedule.home,
                "forwarding": p.forwarding.home,
                "give": p.control.give_start,
                "export": p.control.export_command,
                "startscreen": p.control.startcfg_command,
                "menu": p.start.menu,
                "logout": p.accounts.logout_menu,
                "help": p.control.help_command,
                "admins": p.admins.home,
                "logs": p.logs.logs,
                "ai_settings": p.ai.settings_command,
                "ai_keys": p.ai.keys_command,
                "ai_new": p.ai.new_chat,
                "settings": p.security.settings_command,
                "security": p.security.security_command,
            }.get(key)
            if handler is not None:
                await handler(update, context)

    async def press(self, update, context) -> None:
        item = lookup(update.effective_message.text)
        if item is None or not await self.guard.allow(update):
            return
        if item.owner_only and not await self.guard.allow_owner(update):
            return
        chat_id = update.effective_chat.id
        loading = None
        with suppress(Exception):
            await context.bot.send_chat_action(chat_id, "typing")
            name = item.label.split(" ", 1)[-1]  # the words, without the emoji
            loading = await update.effective_message.reply_text(f"⏳ Loading {name}…")
        try:
            await self._dispatch(item.key, update, context)
        finally:
            if loading is not None:
                with suppress(Exception):
                    await context.bot.delete_message(chat_id, loading.message_id)
