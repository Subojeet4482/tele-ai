"""Click inline buttons of a bot's reply by name, for /all @bot hello [follow] (join).

A name matches any button whose text contains it (ignoring case), so [join] also presses
"✅ Joined" or "Join channel". Names are pressed in the order given, each one after waiting
for a matching button to show up in the chat. A button that opens a link to a channel or an
invite link makes the account join that channel; every other button is simply pressed."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel

from telegram_manager.constants import AUTOCLICK_PAUSE, AUTOCLICK_POLL, AUTOCLICK_WAIT
from telegram_manager.log_setup import mask_id

logger = logging.getLogger(__name__)

_HOSTS = r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/"
_INVITE = re.compile(_HOSTS + r"(?:joinchat/|\+)([A-Za-z0-9_-]{10,})", re.I)
_PUBLIC = re.compile(_HOSTS + r"([A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#]|$)", re.I)
_NOT_CHANNELS = {"addlist", "addstickers", "addemoji", "joinchat", "share", "proxy", "socks", "setlanguage", "boost"}


def button_matches(name: str, text: str | None) -> bool:
    return name.strip().casefold() in (text or "").casefold()


class AutoClickMixin:
    async def click_buttons(self, client: Any, entity: Any, names: list[str], after_id: int = 0) -> list[str]:
        """Press the buttons called `names`, in order. Only messages newer than `after_id` are looked at
        (0 = also what is already in the chat). Returns what was done; raises ValueError naming the
        first button that never showed up."""
        loop = asyncio.get_running_loop()
        done: list[str] = []
        used: set[tuple] = set()
        for name in names:
            deadline = loop.time() + AUTOCLICK_WAIT
            while True:
                found = await self._find_button(client, entity, name, after_id, used)
                if found is not None:
                    message, row, col, button = found
                    used.add((message.id, row, col, button.text, message.message))
                    note = await self._press(client, message, row, col, button)
                    done.append(f"{name}: {note}")
                    logger.info("Auto-click on %s: %s", mask_id(getattr(entity, "id", None)), note)
                    await asyncio.sleep(AUTOCLICK_PAUSE)  # let the bot answer before the next look
                    break
                if loop.time() >= deadline:
                    before = f" (already done: {'; '.join(done)})" if done else ""
                    raise ValueError(f"no button with '{name}' in its text showed up{before}")
                await asyncio.sleep(AUTOCLICK_POLL)
        return done

    async def _find_button(self, client: Any, entity: Any, name: str, after_id: int, used: set[tuple]):
        messages = await client.get_messages(entity, limit=8)  # newest first
        for message in messages:
            if getattr(message, "out", False) or message.id <= after_id:
                continue
            for row, buttons in enumerate(message.buttons or []):
                for col, button in enumerate(buttons):
                    key = (message.id, row, col, button.text, message.message)
                    if button_matches(name, button.text) and key not in used:
                        return message, row, col, button
        return None

    async def _press(self, client: Any, message: Any, row: int, col: int, button: Any) -> str:
        url = getattr(button, "url", None)
        if url:
            return await self._open_link(client, url)
        try:
            await message.click(row, col)
        except asyncio.TimeoutError:
            pass  # the press went out; the bot just did not answer the callback in time
        except Exception as exc:
            if exc.__class__.__name__ != "BotResponseTimeoutError":
                raise
        return "pressed"

    async def _open_link(self, client: Any, url: str) -> str:
        """A link button: join the channel it points to (a plain link needs no click)."""
        try:
            invite = _INVITE.search(url)
            if invite:
                await client(ImportChatInviteRequest(invite.group(1)))
                return "joined the channel"
            public = _PUBLIC.search(url)
            if public is None or public.group(1).lower() in _NOT_CHANNELS:
                return "link skipped (not a channel link)"
            entity = await client.get_entity(public.group(1))
            if not isinstance(entity, Channel):
                return "link skipped (it is not a channel)"
            await client(JoinChannelRequest(entity))
            return "joined the channel"
        except Exception as exc:
            if exc.__class__.__name__ == "UserAlreadyParticipantError":
                return "already in the channel"
            raise
