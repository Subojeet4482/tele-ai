"""Finding chats inside an account."""
from __future__ import annotations

import re
import time
from typing import Any

from telegram_manager.constants import DIALOG_CACHE_MIN_REFRESH, DIALOG_CACHE_TTL
from telegram_manager.errors import describe_error


class DialogsMixin:
    async def list_dialogs(self, account_id: str, limit: int = 40, owner_id: int | None = None) -> list[dict[str, Any]]:
        client = self._client_for(account_id, owner_id)
        dialogs = []
        async for dialog in client.iter_dialogs(limit=limit):
            if dialog.entity is None:
                continue
            dialogs.append({
                "peer_id": int(dialog.id),
                "name": dialog.name or str(dialog.id),
                "username": getattr(dialog.entity, "username", None) or "",
            })
        return dialogs

    async def _dialog_index(
        self, account_id: str, client: Any, force: bool = False
    ) -> tuple[tuple[float, dict[str, Any], dict[str, Any]], bool]:
        """The account's chat list indexed by id and username. Reading it is slow for accounts
        with many chats, so it is reused for a couple of minutes instead of being re-read for
        every message of a bulk send. Returns the index and whether it was read just now."""
        cached = self._dialog_cache.get(account_id)
        if cached is not None and not force and time.monotonic() - cached[0] < DIALOG_CACHE_TTL:
            return cached, False
        by_id: dict[str, Any] = {}
        by_username: dict[str, Any] = {}
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if entity is None:
                continue
            by_id.setdefault(str(dialog.id), entity)
            username = (getattr(entity, "username", None) or "").lower()
            if username:
                by_username.setdefault(username, entity)
        entry = (time.monotonic(), by_id, by_username)
        self._dialog_cache[account_id] = entry
        return entry, True

    async def _existing_entity(self, account_id: str, client: Any, target: str) -> Any:
        """Find a chat that is already open (in the dialog list) in this account.

        This never falls back to client.get_entity(): that can resolve users that have no open
        dialog, which would let "existing chats only" commands start brand-new conversations."""
        normalized = target.strip().lstrip("@").lower()
        if not normalized:
            raise ValueError("Send a @username or chat id")
        numeric = re.fullmatch(r"-?\d+", normalized) is not None

        def lookup(index: tuple[float, dict[str, Any], dict[str, Any]]) -> Any:
            return index[1].get(normalized) if numeric else index[2].get(normalized)

        index, fresh = await self._dialog_index(account_id, client)
        entity = lookup(index)
        if entity is None and not fresh and time.monotonic() - index[0] >= DIALOG_CACHE_MIN_REFRESH:
            index, _ = await self._dialog_index(account_id, client, force=True)  # opened since it was read?
            entity = lookup(index)
        if entity is None:
            raise ValueError("That chat is not already open in this account")
        return entity

    async def _known_entity(self, account_id: str, client: Any, peer_id: int) -> Any:
        """Entity for a chat that just messaged this account (replies and reactions). After a
        restart the entity cache is empty, so fall back to the dialog list."""
        try:
            return await client.get_entity(int(peer_id))
        except Exception:
            return await self._existing_entity(account_id, client, str(peer_id))

    async def _resolve_any_entity(self, client: Any, target: str) -> Any:
        """Resolve a username/phone/id even if no chat with it is open yet."""
        cleaned = target.strip()
        # Telethon reads a bare digit string as a phone number; a numeric target is an id.
        lookup: Any = int(cleaned) if re.fullmatch(r"-?\d+", cleaned) else cleaned
        try:
            return await client.get_entity(lookup)
        except Exception as exc:
            raise ValueError(f"Could not find that user from this account: {describe_error(exc)}") from exc
