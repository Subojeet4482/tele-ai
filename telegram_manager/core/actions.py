"""More Telegram actions for one of the admin's own accounts: edit / reply / forward / pin / delete a message, search a chat,
manage group members and settings, contacts, auto-delete timers, and send a video / voice / file (not only a picture).

Guard rails (on purpose): at most MAX_MEMBERS_PER_CALL people are added per call and never in bulk across chats, no admin
gets the right to add other admins, and the self-destruct time of an account cannot be set shorter than 3 months."""
from __future__ import annotations

import asyncio
import io
import logging
import random
from typing import Any, Iterable

from telegram_manager.core import humanize
from telegram_manager.core.models import BulkResult
from telegram_manager.core.security import kind_of

logger = logging.getLogger(__name__)

MAX_MEMBERS_PER_CALL = 10
MAX_LIST = 30
SLOW_MODE_SECONDS = (0, 10, 30, 60, 300, 900, 3600)
AUTO_DELETE_SECONDS = {"off": 0, "1d": 86400, "1w": 604800, "1m": 2678400}
ACCOUNT_TTL_DAYS = (90, 180, 365)
MEDIA_KINDS = ("photo", "video", "voice", "audio", "document")
LOCKED = {"send_messages": False, "send_media": False, "send_stickers": False, "send_gifs": False, "send_polls": False, "embed_link_previews": False}
UNLOCKED = {key: True for key in LOCKED}


def _short(text: str, n: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class ActionsMixin:
    async def _entity(self, account_id: str, target: str, owner_id: int | None):
        client = self._client_for(account_id, owner_id)
        return client, await self._resolve_any_entity(client, target)

    @staticmethod
    async def _message_id(client: Any, entity: Any, selector: Any) -> int:
        """A message id, or 'last' (the account's own newest message here) / 'last_incoming' (the other side's newest)."""
        text = str(selector if selector is not None else "last").strip().lower()
        if text.lstrip("-").isdigit():
            return int(text)
        if text not in ("last", "last_incoming"):
            raise ValueError("message must be an id number, 'last' (my newest message) or 'last_incoming'.")
        for message in await client.get_messages(entity, limit=40):
            if bool(getattr(message, "out", False)) == (text == "last"):
                return int(message.id)
        raise ValueError("No such message found in the last 40 messages of that chat.")

    # --- messages -------------------------------------------------------------------------------------------------------
    async def edit_message(self, account_id: str, target: str, selector: Any, text: str, owner_id: int | None = None) -> str:
        if not (text or "").strip():
            raise ValueError("The new text is empty.")
        client, entity = await self._entity(account_id, target, owner_id)
        message_id = await self._message_id(client, entity, selector if selector is not None else "last")
        await client.edit_message(entity, message_id, text, parse_mode=None)
        return f"message {message_id} edit ho gaya"

    async def reply_message(self, account_id: str, target: str, selector: Any, text: str, owner_id: int | None = None) -> str:
        if not (text or "").strip():
            raise ValueError("The reply is empty.")
        client, entity = await self._entity(account_id, target, owner_id)
        message_id = await self._message_id(client, entity, selector if selector is not None else "last_incoming")
        await humanize.before_send(client, entity, text, account_id)
        sent = await client.send_message(entity, text, reply_to=message_id, parse_mode=None)
        self._remember_sent(account_id, target, int(getattr(sent, "id", 0) or 0), owner_id)
        return f"message {message_id} ko reply bhej diya"

    async def forward_message(self, account_id: str, source: str, selector: Any, destination: str, owner_id: int | None = None) -> str:
        client, origin = await self._entity(account_id, source, owner_id)
        target = await self._resolve_any_entity(client, destination)
        message_id = await self._message_id(client, origin, selector if selector is not None else "last_incoming")
        await client.forward_messages(target, message_id, origin)
        return f"message {message_id} {destination} ko forward ho gaya"

    async def pin_message(self, account_id: str, target: str, selector: Any, owner_id: int | None = None, unpin: bool = False) -> str:
        client, entity = await self._entity(account_id, target, owner_id)
        message_id = await self._message_id(client, entity, selector if selector is not None else "last")
        if unpin:
            await client.unpin_message(entity, message_id)
            return f"message {message_id} unpin ho gaya"
        await client.pin_message(entity, message_id, notify=False)
        return f"message {message_id} pin ho gaya"

    async def delete_message(self, account_id: str, target: str, selector: Any, owner_id: int | None = None) -> str:
        client, entity = await self._entity(account_id, target, owner_id)
        message_id = await self._message_id(client, entity, selector if selector is not None else "last")
        await client.delete_messages(entity, [message_id])
        return f"message {message_id} delete ho gaya"

    async def search_messages(self, account_id: str, target: str, query: str, owner_id: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            raise ValueError("What should be searched for?")
        client, entity = await self._entity(account_id, target, owner_id)
        found = await client.get_messages(entity, limit=max(1, min(int(limit), 20)), search=query)
        return [{"id": int(m.id), "mine": bool(getattr(m, "out", False)), "date": str(getattr(m, "date", "") or "")[:16],
                 "text": _short(getattr(m, "message", "") or "")} for m in found]

    # --- pictures, videos, voice messages, files -----------------------------------------------------------------------------
    async def send_media_on_account(
        self, account_id: str, target: str, data: bytes, caption: str = "", owner_id: int | None = None,
        filename: str = "file", kind: str = "document",
    ) -> None:
        if kind not in MEDIA_KINDS:
            raise ValueError("kind must be one of: " + ", ".join(MEDIA_KINDS))
        if not data:
            raise ValueError("There is nothing to send.")
        client, entity = await self._entity(account_id, target, owner_id)
        await humanize.before_send(client, entity, caption, account_id)
        buffer = io.BytesIO(data)
        buffer.name = filename
        sent = await client.send_file(
            entity, buffer, caption=caption or None, force_document=(kind == "document"),
            voice_note=(kind == "voice"), supports_streaming=(kind == "video"),
        )
        self._remember_sent(account_id, target, int(getattr(sent, "id", 0) or 0), owner_id)

    async def send_media_selected(
        self, owner_id: int, account_ids: Iterable[str], target: str, data: bytes, caption: str, delay_seconds: int, filename: str, kind: str,
    ) -> BulkResult:
        return await self._for_each_account(
            owner_id, lambda account_id: self.send_media_on_account(account_id, target, data, caption, owner_id, filename, kind),
            delay_seconds, "send file", only_ids=account_ids,
        )

    # --- group / channel: people -----------------------------------------------------------------------------------------------------
    async def member_action(self, account_id: str, chat: str, action: str, users: list[str], owner_id: int | None = None) -> list[str]:
        """add · kick · ban · unban · promote · demote. Returns one line per person."""
        if action not in ("add", "kick", "ban", "unban", "promote", "demote"):
            raise ValueError("action must be add, kick, ban, unban, promote or demote.")
        users = [u for u in (users or []) if str(u).strip()]
        if not users:
            raise ValueError("Which people? Give @usernames or ids.")
        if len(users) > MAX_MEMBERS_PER_CALL:
            raise ValueError(f"At most {MAX_MEMBERS_PER_CALL} people at a time.")
        client, entity = await self._entity(account_id, chat, owner_id)
        lines: list[str] = []
        for number, user in enumerate(users):
            if number:
                await asyncio.sleep(random.uniform(3.0, 6.0) if humanize.enabled() else 0)
            try:
                person = await self._resolve_any_entity(client, str(user))
                if action == "add":
                    if kind_of(entity) == "group":
                        from telethon.tl.functions.messages import AddChatUserRequest

                        await client(AddChatUserRequest(chat_id=entity.id, user_id=person, fwd_limit=0))
                    else:
                        from telethon.tl.functions.channels import InviteToChannelRequest

                        await client(InviteToChannelRequest(channel=entity, users=[person]))
                elif action == "kick":
                    await client.kick_participant(entity, person)
                elif action == "ban":
                    await client.edit_permissions(entity, person, view_messages=False)
                elif action == "unban":
                    await client.edit_permissions(entity, person, view_messages=True)
                elif action == "promote":  # never the right to appoint further admins
                    await client.edit_admin(entity, person, is_admin=True, change_info=False, post_messages=True, edit_messages=True,
                                            delete_messages=True, ban_users=True, invite_users=True, pin_messages=True, add_admins=False)
                else:
                    await client.edit_admin(entity, person, is_admin=False)
                lines.append(f"{user}: {action} ✅")
            except Exception as exc:
                from telegram_manager.errors import describe_error

                lines.append(f"{user}: {action} ❌ {describe_error(exc)}")
        return lines

    async def list_members(self, account_id: str, chat: str, owner_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        client, entity = await self._entity(account_id, chat, owner_id)
        people = await client.get_participants(entity, limit=max(1, min(int(limit), MAX_LIST)))
        return [{"id": int(p.id), "name": " ".join(x for x in (getattr(p, "first_name", None), getattr(p, "last_name", None)) if x) or "?",
                 "username": getattr(p, "username", "") or "", "bot": bool(getattr(p, "bot", False))} for p in people]

    # --- group / channel: settings ---------------------------------------------------------------------------------------------------------
    async def chat_setting(
        self, account_id: str, chat: str, action: str, owner_id: int | None = None, seconds: int = 0, link: str = "", data: bytes = b"",
    ) -> str:
        """invite_link · revoke_link · slowmode · lock · unlock · photo · approve_requests · auto_delete"""
        client, entity = await self._entity(account_id, chat, owner_id)
        if action == "invite_link":
            from telethon.tl.functions.messages import ExportChatInviteRequest

            invite = await client(ExportChatInviteRequest(peer=entity))
            return f"naya invite link: {invite.link}"
        if action == "revoke_link":
            from telethon.tl.functions.messages import EditExportedChatInviteRequest

            if not link:
                raise ValueError("Which invite link should be revoked?")
            await client(EditExportedChatInviteRequest(peer=entity, link=link, revoked=True))
            return "invite link band ho gaya"
        if action == "slowmode":
            if seconds not in SLOW_MODE_SECONDS:
                raise ValueError("Slow mode seconds must be one of: " + ", ".join(map(str, SLOW_MODE_SECONDS)) + " (0 = off).")
            from telethon.tl.functions.channels import ToggleSlowModeRequest

            await client(ToggleSlowModeRequest(channel=entity, seconds=seconds))
            return "slow mode band" if seconds == 0 else f"slow mode {seconds}s"
        if action in ("lock", "unlock"):
            await client.edit_permissions(entity, **(LOCKED if action == "lock" else UNLOCKED))
            return "ab sirf admins likh sakte hain" if action == "lock" else "sab members phir se likh sakte hain"
        if action == "photo":
            if not data:
                raise ValueError("No picture to use.")
            buffer = io.BytesIO(data)
            buffer.name = "chat.jpg"
            uploaded = await client.upload_file(buffer)
            if kind_of(entity) == "group":
                from telethon.tl.functions.messages import EditChatPhotoRequest
                from telethon.tl.types import InputChatUploadedPhoto

                await client(EditChatPhotoRequest(chat_id=entity.id, photo=InputChatUploadedPhoto(file=uploaded)))
            else:
                from telethon.tl.functions.channels import EditPhotoRequest
                from telethon.tl.types import InputChatUploadedPhoto

                await client(EditPhotoRequest(channel=entity, photo=InputChatUploadedPhoto(file=uploaded)))
            return "chat ki photo badal gayi"
        if action == "approve_requests":
            from telethon.tl.functions.messages import HideAllChatJoinRequestsRequest

            await client(HideAllChatJoinRequestsRequest(peer=entity, approved=True))
            return "saari join requests approve ho gayi"
        if action == "auto_delete":
            if seconds not in AUTO_DELETE_SECONDS.values():
                raise ValueError("auto_delete must be off, 1d, 1w or 1m.")
            from telethon.tl.functions.messages import SetHistoryTTLRequest

            await client(SetHistoryTTLRequest(peer=entity, period=seconds))
            return "auto-delete band" if seconds == 0 else f"messages {seconds // 86400} din baad apne aap delete honge"
        raise ValueError("action must be invite_link, revoke_link, slowmode, lock, unlock, photo, approve_requests or auto_delete.")

    # --- contacts -----------------------------------------------------------------------------------------------------------------------------
    async def contact_action(
        self, account_id: str, action: str, owner_id: int | None = None, phone: str = "", first_name: str = "", last_name: str = "", user: str = "",
    ) -> Any:
        client = self._client_for(account_id, owner_id)
        if action == "list":
            from telethon.tl.functions.contacts import GetContactsRequest

            result = await client(GetContactsRequest(hash=0))
            users = list(getattr(result, "users", None) or [])
            return [{"id": int(u.id), "name": " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x) or "?",
                     "username": getattr(u, "username", "") or ""} for u in users[:MAX_LIST]], len(users)
        if action == "add":
            digits = "".join(ch for ch in phone if ch.isdigit())
            if len(digits) < 8 or not first_name.strip():
                raise ValueError("Phone number (with country code) and a first name are needed.")
            from telethon.tl.functions.contacts import ImportContactsRequest
            from telethon.tl.types import InputPhoneContact

            result = await client(ImportContactsRequest([InputPhoneContact(client_id=0, phone="+" + digits, first_name=first_name.strip(), last_name=last_name.strip())]))
            if not getattr(result, "imported", None):
                raise ValueError("That number is not on Telegram (or hides itself).")
            return "contact save ho gaya"
        if action == "delete":
            from telethon.tl.functions.contacts import DeleteContactsRequest

            person = await self._resolve_any_entity(client, user)
            await client(DeleteContactsRequest(id=[person]))
            return "contact delete ho gaya"
        raise ValueError("action must be list, add or delete.")

    # --- the account itself ---------------------------------------------------------------------------------------------------------------------------
    async def account_setting(self, account_id: str, action: str, owner_id: int | None = None, value: Any = None) -> str:
        client = self._client_for(account_id, owner_id)
        if action == "auto_delete_default":
            seconds = AUTO_DELETE_SECONDS.get(str(value).strip().lower())
            if seconds is None:
                raise ValueError("value must be off, 1d, 1w or 1m.")
            from telethon.tl.functions.messages import SetDefaultHistoryTTLRequest

            await client(SetDefaultHistoryTTLRequest(period=seconds))
            return "naye chats mein auto-delete " + ("band" if seconds == 0 else f"{value}")
        if action == "self_destruct":
            try:
                days = int(value)
            except (TypeError, ValueError):
                days = 0
            if days not in ACCOUNT_TTL_DAYS:
                raise ValueError("The account self-destruct time must be 90, 180 or 365 days (shorter would be dangerous).")
            from telethon.tl.functions.account import SetAccountTTLRequest
            from telethon.tl.types import AccountDaysTTL

            await client(SetAccountTTLRequest(ttl=AccountDaysTTL(days=days)))
            return f"agar account {days} din use na ho to Telegram use delete kar dega"
        raise ValueError("action must be auto_delete_default or self_destruct.")
