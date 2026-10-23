"""Security and housekeeping of one account: 2-step password, other devices (sessions), channels and groups
it owns, blocked users, privacy switches and profile photos. Telethon is imported inside each method so the
module loads (and the tests run) even where only stand-ins are installed."""
from __future__ import annotations

import logging
import re
from typing import Any

from telegram_manager.log_setup import mask_id

logger = logging.getLogger(__name__)

PRIVACY_KEYS = {
    "phone": ("InputPrivacyKeyPhoneNumber", "who sees the phone number"),
    "last_seen": ("InputPrivacyKeyStatusTimestamp", "who sees last seen / online"),
    "photo": ("InputPrivacyKeyProfilePhoto", "who sees the profile photo"),
    "about": ("InputPrivacyKeyAbout", "who sees the bio"),
    "groups": ("InputPrivacyKeyChatInvite", "who can add the account to groups and channels"),
    "calls": ("InputPrivacyKeyPhoneCall", "who can call the account"),
    "forwards": ("InputPrivacyKeyForwards", "who can link to the account when forwarding"),
}
PRIVACY_WHO = {
    "everyone": "InputPrivacyValueAllowAll",
    "contacts": "InputPrivacyValueAllowContacts",
    "nobody": "InputPrivacyValueDisallowAll",
}
_WHO_WORDS = {
    "everyone": "everyone", "all": "everyone", "everybody": "everyone", "sab": "everyone", "public": "everyone",
    "contacts": "contacts", "contact": "contacts", "my_contacts": "contacts",
    "nobody": "nobody", "none": "nobody", "no_one": "nobody", "koi_nahi": "nobody", "private": "nobody",
}
_KEY_WORDS = {
    "phone": "phone", "number": "phone", "phone_number": "phone", "mobile": "phone",
    "last_seen": "last_seen", "lastseen": "last_seen", "online": "last_seen", "seen": "last_seen", "status": "last_seen",
    "photo": "photo", "profile_photo": "photo", "dp": "photo", "picture": "photo",
    "about": "about", "bio": "about",
    "groups": "groups", "group": "groups", "invite": "groups", "add_to_groups": "groups", "channels": "groups",
    "calls": "calls", "call": "calls", "phone_call": "calls",
    "forwards": "forwards", "forward": "forwards",
}

_ERRORS = {
    "PasswordHashInvalidError": "The current 2-step password is wrong.",
    "FreshResetAuthorisationForbiddenError": "Telegram blocks this for a few hours after a new login (the bot's session is too new). Try again later.",
    "ChannelPrivateError": "That chat is private or the account is not in it.",
    "ChatAdminRequiredError": "The account is not the owner/admin of that chat.",
    "ChatNotModifiedError": "Nothing to change: it is already like that.",
    "ChannelsAdminPublicTooMuchError": "This account already owns too many public channels/groups.",
    "ChannelsTooMuchError": "This account has joined or created too many channels/groups.",
    "UsernameOccupiedError": "That @username is already taken.",
    "UsernameInvalidError": "Telegram says that @username is not valid.",
    "PasswordEmptyError": "There is no 2-step password to change.",
    "NewSaltInvalidError": "Telegram refused the new password, try again.",
    "EmailUnconfirmedError": "Telegram wants a recovery e-mail confirmed first.",
}


def friendly(exc: Exception) -> Exception:
    """A ValueError with plain words for the Telegram errors people actually hit; anything else unchanged."""
    text = _ERRORS.get(exc.__class__.__name__)
    return ValueError(text) if text else exc


def resolve_privacy(setting: str, who: str) -> tuple[str, str]:
    key = _KEY_WORDS.get(re.sub(r"[\s\-]+", "_", str(setting or "").strip().lower()))
    if key is None:
        raise ValueError("setting must be one of: " + ", ".join(PRIVACY_KEYS))
    audience = _WHO_WORDS.get(re.sub(r"[\s\-]+", "_", str(who or "").strip().lower()))
    if audience is None:
        raise ValueError("who must be one of: everyone, contacts, nobody")
    return key, audience


def kind_of(entity: Any) -> str:
    """channel · supergroup · group (a basic group)."""
    if entity.__class__.__name__ == "Channel":
        return "channel" if getattr(entity, "broadcast", False) else "supergroup"
    return "group"


def chat_row(entity: Any) -> dict[str, Any]:
    return {
        "id": int(getattr(entity, "id", 0)),
        "title": str(getattr(entity, "title", "") or "Unnamed"),
        "username": str(getattr(entity, "username", "") or ""),
        "kind": kind_of(entity),
        "owner": bool(getattr(entity, "creator", False)),
        "members": getattr(entity, "participants_count", None),
    }


def session_row(auth: Any) -> dict[str, Any]:
    active = getattr(auth, "date_active", None)
    return {
        "hash": int(getattr(auth, "hash", 0)),
        "current": bool(getattr(auth, "current", False)),
        "device": str(getattr(auth, "device_model", "") or ""),
        "platform": str(getattr(auth, "platform", "") or ""),
        "app": str(getattr(auth, "app_name", "") or ""),
        "country": str(getattr(auth, "country", "") or ""),
        "ip": str(getattr(auth, "ip", "") or ""),
        "active": active.strftime("%Y-%m-%d %H:%M") if hasattr(active, "strftime") else str(active or "")[:16],
    }


def session_line(row: dict[str, Any]) -> str:
    where = f" · {row['country']}" if row["country"] else ""
    mark = "📍 this bot" if row["current"] else "🖥"
    return f"{mark} {row['app'] or 'app'} on {row['device'] or row['platform'] or 'device'}{where} · last active {row['active']}"


class SecurityMixin:
    # --- 2-step password ------------------------------------------------------------------
    async def two_fa_status(self, account_id: str, owner_id: int | None = None) -> dict[str, Any]:
        from telethon.tl.functions.account import GetPasswordRequest

        client = self._client_for(account_id, owner_id)
        pwd = await client(GetPasswordRequest())
        return {"enabled": bool(getattr(pwd, "has_password", False)), "hint": str(getattr(pwd, "hint", "") or "")}

    async def set_two_fa(
        self, account_id: str, owner_id: int | None, action: str, password: str = "", current: str = "", hint: str = ""
    ) -> str:
        """action: enable (needs password) · change (current + password) · disable (current). Returns a one-line result.
        The passwords never touch the log and are not kept."""
        if action not in ("enable", "change", "disable"):
            raise ValueError("action must be enable, change or disable")
        status = await self.two_fa_status(account_id, owner_id)
        if action == "enable" and status["enabled"]:
            raise ValueError("2-step verification is already ON for this account (use change).")
        if action in ("change", "disable") and not status["enabled"]:
            raise ValueError("2-step verification is already OFF for this account.")
        if action != "disable" and len(password) < 1:
            raise ValueError("The new password is empty.")
        if action != "enable" and not current:
            raise ValueError("The current 2-step password is needed.")
        client = self._client_for(account_id, owner_id)
        try:
            await client.edit_2fa(
                current_password=current or None,
                new_password=None if action == "disable" else password,
                hint=hint or "",
            )
        except Exception as exc:
            raise friendly(exc) from exc
        logger.info("2-step verification %s on account %s", action, mask_id(account_id))
        return {"enable": "2-step verification is ON", "change": "2-step password changed", "disable": "2-step verification is OFF"}[action]

    # --- other devices (sessions) ---------------------------------------------------------
    async def list_sessions(self, account_id: str, owner_id: int | None = None) -> list[dict[str, Any]]:
        from telethon.tl.functions.account import GetAuthorizationsRequest

        client = self._client_for(account_id, owner_id)
        result = await client(GetAuthorizationsRequest())
        return [session_row(auth) for auth in getattr(result, "authorizations", [])]

    async def terminate_sessions(
        self, account_id: str, owner_id: int | None = None, match: str = ""
    ) -> dict[str, Any]:
        """End every session except the one this bot uses. `match` limits it to sessions whose app / device /
        platform / country / ip contains that text. Returns {"ended": n, "kept": n, "errors": [...]}."""
        from telethon.tl.functions.account import ResetAuthorizationRequest

        client = self._client_for(account_id, owner_id)
        rows = await self.list_sessions(account_id, owner_id)
        needle = (match or "").strip().lower()
        ended, errors = 0, []
        for row in rows:
            if row["current"]:
                continue
            haystack = " ".join(str(row[k]) for k in ("app", "device", "platform", "country", "ip")).lower()
            if needle and needle not in haystack:
                continue
            try:
                await client(ResetAuthorizationRequest(hash=row["hash"]))
                ended += 1
            except Exception as exc:
                nicer = friendly(exc)
                errors.append(str(nicer) if nicer is not exc else exc.__class__.__name__)
                if exc.__class__.__name__ == "FreshResetAuthorisationForbiddenError":
                    break  # every other one fails the same way
        logger.info("Ended %s session(s) of account %s", ended, mask_id(account_id))
        return {"ended": ended, "kept": len(rows) - ended, "errors": list(dict.fromkeys(errors))}

    # --- channels and groups --------------------------------------------------------------
    async def list_chats_of(
        self, account_id: str, owner_id: int | None = None, kind: str = "all", owned_only: bool = False, limit: int = 200
    ) -> list[dict[str, Any]]:
        client = self._client_for(account_id, owner_id)
        found = []
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            if entity is None or entity.__class__.__name__ not in ("Channel", "Chat"):
                continue
            row = chat_row(entity)
            if kind in ("channel", "supergroup", "group") and row["kind"] != kind:
                continue
            if kind == "groups" and row["kind"] == "channel":
                continue
            if owned_only and not row["owner"]:
                continue
            found.append(row)
        return found

    async def _chat_entity(self, account_id: str, owner_id: int | None, ref: str) -> Any:
        """A channel / group of this account by id, @username, link or part of the title (exactly one must fit)."""
        client = self._client_for(account_id, owner_id)
        needle = re.sub(r"^(https?://)?t\.me/", "", str(ref or "").strip()).lstrip("@").lower()
        if not needle:
            raise ValueError("Which channel or group? Give its name, @username or id.")
        exact, partial = [], []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if entity is None or entity.__class__.__name__ not in ("Channel", "Chat"):
                continue
            title = str(getattr(entity, "title", "") or "").lower()
            username = str(getattr(entity, "username", "") or "").lower()
            ids = {str(entity.id), str(dialog.id), str(dialog.id).removeprefix("-100").removeprefix("-")}
            if needle in ids or needle == username or needle == title:
                exact.append(entity)
            elif needle in title:
                partial.append(entity)
        found = exact or partial
        if not found:
            raise ValueError(f"No channel or group like '{ref}' on this account.")
        if len(found) > 1:
            names = ", ".join(str(getattr(e, "title", "") or e.id) for e in found[:6])
            raise ValueError(f"'{ref}' fits {len(found)} chats: {names}. Use the exact name, @username or id.")
        return found[0]

    async def delete_owned_chat(self, account_id: str, owner_id: int | None, ref: str) -> str:
        """Delete a channel / group for everybody. Only its creator can; returns the title."""
        client = self._client_for(account_id, owner_id)
        entity = await self._chat_entity(account_id, owner_id, ref)
        if not getattr(entity, "creator", False):
            raise ValueError(f"This account is not the owner of '{getattr(entity, 'title', ref)}', so it can only leave it (delete_chat).")
        title = str(getattr(entity, "title", "") or ref)
        try:
            if entity.__class__.__name__ == "Channel":
                from telethon.tl.functions.channels import DeleteChannelRequest

                await client(DeleteChannelRequest(channel=entity))
            else:
                from telethon.tl.functions.messages import DeleteChatRequest

                await client(DeleteChatRequest(chat_id=entity.id))
        except Exception as exc:
            raise friendly(exc) from exc
        self._dialog_cache.pop(account_id, None)
        logger.info("Deleted an owned chat of account %s", mask_id(account_id))
        return title

    async def create_chat(
        self, account_id: str, owner_id: int | None, title: str, about: str = "", kind: str = "channel", username: str = ""
    ) -> dict[str, Any]:
        """Create a channel or a supergroup (optionally public with a @username). Returns title, kind, link."""
        from telethon.tl.functions.channels import CreateChannelRequest, UpdateUsernameRequest

        title = (title or "").strip()
        if not title or len(title) > 128:
            raise ValueError("The title needs 1 to 128 characters.")
        if kind not in ("channel", "group", "supergroup"):
            raise ValueError("kind must be channel or group")
        client = self._client_for(account_id, owner_id)
        broadcast = kind == "channel"
        try:
            updates = await client(CreateChannelRequest(title=title, about=(about or "")[:255], broadcast=broadcast, megagroup=not broadcast))
            entity = updates.chats[0]
            name = (username or "").strip().lstrip("@")
            if name:
                await client(UpdateUsernameRequest(channel=entity, username=name))
        except Exception as exc:
            raise friendly(exc) from exc
        self._dialog_cache.pop(account_id, None)
        link = f"https://t.me/{name}" if (username or "").strip() else ""
        return {"title": title, "kind": "channel" if broadcast else "group", "link": link}

    async def edit_chat_info(
        self, account_id: str, owner_id: int | None, ref: str, title: str | None = None, about: str | None = None
    ) -> str:
        from telethon.tl.functions.channels import EditTitleRequest
        from telethon.tl.functions.messages import EditChatAboutRequest, EditChatTitleRequest

        if title is None and about is None:
            raise ValueError("What to change: title or about?")
        client = self._client_for(account_id, owner_id)
        entity = await self._chat_entity(account_id, owner_id, ref)
        try:
            if title is not None:
                if entity.__class__.__name__ == "Channel":
                    await client(EditTitleRequest(channel=entity, title=title))
                else:
                    await client(EditChatTitleRequest(chat_id=entity.id, title=title))
            if about is not None:
                await client(EditChatAboutRequest(peer=entity, about=about[:255]))
        except Exception as exc:
            raise friendly(exc) from exc
        self._dialog_cache.pop(account_id, None)
        return title or str(getattr(entity, "title", "") or ref)

    # --- blocked users, privacy, photos ---------------------------------------------------
    async def blocked_users(self, account_id: str, owner_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from telethon.tl.functions.contacts import GetBlockedRequest

        client = self._client_for(account_id, owner_id)
        result = await client(GetBlockedRequest(offset=0, limit=limit))
        return [
            {"id": int(u.id), "name": " ".join(p for p in (getattr(u, "first_name", ""), getattr(u, "last_name", "")) if p) or "Unnamed",
             "username": str(getattr(u, "username", "") or "")}
            for u in getattr(result, "users", [])
        ]

    async def unblock_on_account(self, account_id: str, target: str, owner_id: int | None = None) -> None:
        from telethon.tl.functions.contacts import UnblockRequest

        client = self._client_for(account_id, owner_id)
        entity = await self._resolve_any_entity(client, target)
        await client(UnblockRequest(id=entity))

    async def set_privacy(self, account_id: str, owner_id: int | None, setting: str, who: str) -> str:
        from telethon.tl.functions.account import SetPrivacyRequest
        from telethon.tl import types

        key, audience = resolve_privacy(setting, who)
        client = self._client_for(account_id, owner_id)
        try:
            await client(SetPrivacyRequest(key=getattr(types, PRIVACY_KEYS[key][0])(), rules=[getattr(types, PRIVACY_WHO[audience])()]))
        except Exception as exc:
            raise friendly(exc) from exc
        return f"{PRIVACY_KEYS[key][1]}: {audience}"

    async def delete_profile_photos(self, account_id: str, owner_id: int | None = None) -> int:
        from telethon.tl.functions.photos import DeletePhotosRequest
        from telethon.utils import get_input_photo

        client = self._client_for(account_id, owner_id)
        photos = await client.get_profile_photos("me")
        if not photos:
            return 0
        await client(DeletePhotosRequest(id=[get_input_photo(photo) for photo in photos]))
        return len(photos)
