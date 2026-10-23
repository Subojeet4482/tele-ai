"""Wider control of an account: start a bot (also from a t.me link, also when it wants channels joined
first), send a picture, mark a chat read, mute / archive it, search public chats and read an account's details.

Everything here acts through the admin's OWN accounts (``_client_for`` refuses another admin's account).
Telethon request classes are imported inside the methods, like in joiner.py, so the module loads anywhere."""
from __future__ import annotations

import asyncio
import io
import logging
import random
from typing import Any, Iterable

from telegram_manager.constants import (
    BOT_START_AFTER_CLICK_WAIT,
    BOT_START_ROUNDS,
    BOT_START_SETTLE,
    BOT_START_WAIT,
    AUTOCLICK_POLL,
)
from telegram_manager.core import humanize
from telegram_manager.core.joinparse import (
    clean_payload,
    extract_join_targets,
    looks_like_force_join,
    message_links,
    parse_bot_ref,
    pick_verify_button,
)
from telegram_manager.core.models import BulkResult
from telegram_manager.core.security import chat_row
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)

CHAT_ACTIONS = ("read", "unread", "mute", "unmute", "archive", "unarchive")
MUTE_FOREVER = 2**31 - 1  # Telegram: a mute-until time in the far future


def _button_texts(message: Any) -> list[str]:
    return [str(getattr(b, "text", "") or "") for row in getattr(message, "buttons", None) or [] for b in row]


def _item(message: Any) -> tuple:
    return (int(getattr(message, "id", 0) or 0), getattr(message, "message", "") or "", tuple(_button_texts(message)))


def _signature(messages: list[Any]) -> tuple:
    """What the bot's latest messages look like: changes when it sends a new one OR edits an old one."""
    return tuple(_item(m) for m in messages)


class ControlMixin:
    # --- reading the bot's side of a chat -------------------------------------------------------
    @staticmethod
    async def _incoming(client: Any, entity: Any, limit: int = 4) -> list[Any]:
        """The newest messages the other side wrote, oldest first."""
        found = [m for m in await client.get_messages(entity, limit=limit + 4) if not getattr(m, "out", False)]
        return list(reversed(found[:limit]))

    async def _wait_for_change(self, client: Any, entity: Any, before: tuple, wait: float) -> tuple[list[Any], tuple]:
        """Wait until the bot's latest messages differ from `before` (a new message or an edited one).
        Returns (the NEW or EDITED messages only, signature); the signature equals `before` when nothing happened in time."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        known = set(before)
        while True:
            messages = await self._incoming(client, entity)
            sig = _signature(messages)
            if sig != before:
                await asyncio.sleep(BOT_START_SETTLE)  # a second message often follows the first
                messages = await self._incoming(client, entity)
                return [m for m in messages if _item(m) not in known], _signature(messages)
            if loop.time() >= deadline:
                return [], sig
            await asyncio.sleep(AUTOCLICK_POLL)

    # --- starting a bot ----------------------------------------------------------------------------
    async def _send_start(self, client: Any, entity: Any, payload: str, report: list[str]) -> None:
        """/start (with the payload of a t.me/bot?start=... link). A bot the account had blocked is unblocked first."""
        from telethon.tl.functions.contacts import UnblockRequest
        from telethon.tl.functions.messages import StartBotRequest

        async def go() -> None:
            if payload:
                await client(StartBotRequest(bot=entity, peer=entity, start_param=payload))
            else:
                await client.send_message(entity, "/start", parse_mode=None)

        try:
            await go()
        except Exception as exc:
            if exc.__class__.__name__ != "YouBlockedUserError":
                raise
            await client(UnblockRequest(id=entity))
            report.append("bot blocked tha: unblock kiya")
            await go()

    async def start_bot_flow(
        self, account_id: str, ref: str, owner_id: int | None = None, payload: str | None = None, join: bool = True
    ) -> dict[str, Any]:
        """Switch a bot on from one account: /start (+ the payload of its link). If the bot says 'join our channel(s)
        first', join them with human pauses, press its Verify button (or send /start again) and read its answer.
        Returns {'steps': [...], 'reply': the bot's last words, 'still_asks_to_join': bool}."""
        name, link_payload = parse_bot_ref(ref)
        payload = clean_payload(payload) if payload else link_payload
        client = self._client_for(account_id, owner_id)
        entity = await self._resolve_any_entity(client, name)
        if not getattr(entity, "bot", False):
            raise ValueError(f"@{name} ek bot nahi hai (user / channel hai): start_bot sirf bots ke liye hai.")
        steps: list[str] = []
        before = _signature(await self._incoming(client, entity))
        await self._send_start(client, entity, payload, steps)
        steps.append("/start bheja" + (f" (payload {payload})" if payload else ""))
        tried: set[str] = set()
        messages: list[Any] = []
        still_asks = False
        wait = BOT_START_WAIT
        for round_no in range(BOT_START_ROUNDS):
            fresh_messages, sig = await self._wait_for_change(client, entity, before, wait)
            if sig == before:
                steps.append("bot ne jawab nahi diya" if round_no == 0 else "bot ne naya message nahi diya")
                break
            before, messages = sig, fresh_messages
            texts = [getattr(m, "message", "") or "" for m in messages]
            buttons = [t for m in messages for t in _button_texts(m)]
            links = [u for m in messages for u in message_links(m)]
            if not join or not looks_like_force_join(texts, buttons):
                break  # the bot works (or the admin said not to join anything)
            targets = extract_join_targets(texts + links)
            fresh = [t for t in targets if t.lower() not in tried]
            if not fresh:
                still_asks = bool(targets)  # it still wants something we already joined
                break
            for target in fresh:
                tried.add(target.lower())
                try:
                    steps.append(f"{target}: {await self.join_with_status(account_id, target, owner_id)}")
                except Exception as exc:
                    steps.append(f"{target}: join nahi hua - {describe_error(exc)}")
                await asyncio.sleep(random.uniform(2.0, 5.0) if humanize.enabled() else 0)
            verify = pick_verify_button(buttons)
            if verify:
                try:
                    await self.click_buttons(client, entity, [verify], 0)
                    steps.append(f"'{verify}' dabaya")
                except Exception as exc:
                    steps.append(f"'{verify}' button nahi dab saka - {describe_error(exc)}")
                    break
            else:
                await self._send_start(client, entity, payload, steps)
                steps.append("join ke baad dobara /start bheja")
            wait = BOT_START_AFTER_CLICK_WAIT
        else:
            still_asks = True  # rounds used up and the bot kept asking
        last = next((t for t in reversed([getattr(m, "message", "") or "" for m in messages]) if t.strip()), "")
        return {"steps": steps, "reply": last.strip()[:300], "still_asks_to_join": still_asks}

    async def start_bot_selected(
        self, owner_id: int, account_ids: Iterable[str], ref: str, payload: str | None = None, join: bool = True,
        delay_seconds: int = 5,
    ) -> tuple[BulkResult, dict[str, dict[str, Any]]]:
        reports: dict[str, dict[str, Any]] = {}

        async def one(account_id: str) -> None:
            reports[account_id] = await self.start_bot_flow(account_id, ref, owner_id, payload, join)

        result = await self._for_each_account(owner_id, one, delay_seconds, "start-bot", only_ids=account_ids)
        return result, reports

    # --- pictures ------------------------------------------------------------------------------
    async def send_photo_on_account(
        self, account_id: str, target: str, data: bytes, caption: str = "", owner_id: int | None = None,
        filename: str = "photo.jpg", as_file: bool = False,
    ) -> None:
        if not data:
            raise ValueError("There is no picture to send.")
        client = self._client_for(account_id, owner_id)
        entity = await self._resolve_any_entity(client, target)
        await humanize.before_send(client, entity, caption, account_id)
        buffer = io.BytesIO(data)
        buffer.name = filename
        sent = await client.send_file(entity, buffer, caption=caption or None, force_document=as_file)
        self._remember_sent(account_id, target, int(getattr(sent, "id", 0) or 0), owner_id)
        logger.info("Sent a picture via account %s", account_id)

    async def send_photo_selected(
        self, owner_id: int, account_ids: Iterable[str], target: str, data: bytes, caption: str = "", delay_seconds: int = 5,
        filename: str = "photo.jpg", as_file: bool = False,
    ) -> BulkResult:
        return await self._for_each_account(
            owner_id,
            lambda account_id: self.send_photo_on_account(account_id, target, data, caption, owner_id, filename, as_file),
            delay_seconds, "send photo", only_ids=account_ids,
        )

    async def set_profile_photo_selected(
        self, owner_id: int, account_ids: Iterable[str], data: bytes, delay_seconds: int = 3
    ) -> BulkResult:
        return await self._for_each_account(
            owner_id, lambda account_id: self.set_profile_photo(account_id, owner_id, data), delay_seconds, "profile photo",
            only_ids=account_ids,
        )

    # --- chat housekeeping -------------------------------------------------------------------------
    async def chat_action(self, account_id: str, target: str, action: str, owner_id: int | None = None) -> str:
        """read ('seen') · unread · mute · unmute · archive · unarchive one chat. Returns what was done."""
        if action not in CHAT_ACTIONS:
            raise ValueError("action must be one of: " + ", ".join(CHAT_ACTIONS))
        client = self._client_for(account_id, owner_id)
        entity = await self._resolve_any_entity(client, target)
        if action == "read":
            await client.send_read_acknowledge(entity)
            return "messages seen (read) mark ho gaye"
        if action in ("archive", "unarchive"):
            await client.edit_folder(entity, 1 if action == "archive" else 0)
            return "chat archive ho gayi" if action == "archive" else "chat archive se bahar aa gayi"
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.functions.messages import MarkDialogUnreadRequest
        from telethon.tl.types import InputDialogPeer, InputNotifyPeer, InputPeerNotifySettings

        peer = await client.get_input_entity(entity)
        if action == "unread":
            await client(MarkDialogUnreadRequest(peer=InputDialogPeer(peer), unread=True))
            return "chat unread mark ho gayi"
        until = MUTE_FOREVER if action == "mute" else 0
        await client(UpdateNotifySettingsRequest(peer=InputNotifyPeer(peer), settings=InputPeerNotifySettings(mute_until=until)))
        return "chat mute ho gayi" if action == "mute" else "chat unmute ho gayi"

    async def chat_action_selected(
        self, owner_id: int, account_ids: Iterable[str], target: str, action: str, delay_seconds: int = 1
    ) -> BulkResult:
        return await self._for_each_account(
            owner_id, lambda account_id: self.chat_action(account_id, target, action, owner_id), delay_seconds, action,
            only_ids=account_ids,
        )

    # --- finding public chats / bots ---------------------------------------------------------------
    async def search_public(self, account_id: str, query: str, owner_id: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Public channels, groups, bots and people matching a word (Telegram's own search)."""
        from telethon.tl.functions.contacts import SearchRequest

        query = (query or "").strip()
        if len(query) < 2:
            raise ValueError("Search word is too short.")
        client = self._client_for(account_id, owner_id)
        result = await client(SearchRequest(q=query, limit=max(1, min(int(limit), 20))))
        rows: list[dict[str, Any]] = []
        for chat in getattr(result, "chats", None) or []:
            row = chat_row(chat)
            rows.append({"kind": row["kind"], "title": row["title"], "username": row["username"], "id": row["id"]})
        for user in getattr(result, "users", None) or []:
            name = " ".join(p for p in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if p) or "?"
            rows.append({"kind": "bot" if getattr(user, "bot", False) else "user", "title": name,
                         "username": getattr(user, "username", None) or "", "id": getattr(user, "id", 0)})
        return rows[:limit]

    # --- everything about one account --------------------------------------------------------------
    async def account_info(self, account_id: str, owner_id: int | None = None) -> dict[str, Any]:
        """Id, full phone number, names, @username, Premium and bio of one connected account (shown to its admin only)."""
        client = self._client_for(account_id, owner_id)
        me = await client.get_me()
        row = self.accounts.get(account_id) or {}
        phone = str(getattr(me, "phone", "") or row.get("phone") or "")
        info: dict[str, Any] = {
            "id": getattr(me, "id", None),
            "first_name": getattr(me, "first_name", "") or "",
            "last_name": getattr(me, "last_name", "") or "",
            "username": getattr(me, "username", "") or "",
            "phone": ("+" + phone.lstrip("+")) if phone else "",
            "premium": bool(getattr(me, "premium", False)),
            "about": "",
        }
        try:
            from telethon.tl.functions.users import GetFullUserRequest

            full = await client(GetFullUserRequest(me))
            info["about"] = str(getattr(getattr(full, "full_user", None), "about", "") or "")
        except Exception:
            logger.debug("bio could not be read", exc_info=True)
        return info
