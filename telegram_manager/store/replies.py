"""Remembers which account/chat a forwarded message came from, so replies can be routed back.

Enable a Firestore TTL policy on the `expires_at` field of the `telegram_reply_map`
collection to have old entries removed automatically."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

REPLY_MAP_TTL_DAYS = 30


class ReplyStore:
    def __init__(self, db, default_owner_id: int) -> None:
        self._col = db.collection("telegram_reply_map")
        self._default_owner_id = default_owner_id

    async def save(self, chat_id: int, message_id: int, account_id: str, peer_id: int, original_id: int) -> None:
        # Message ids repeat between chats, so the key includes the admin's chat id.
        now = datetime.now(timezone.utc)
        data = {
            "account_id": account_id,
            "peer_id": int(peer_id),
            "original_message_id": int(original_id),
            "chat_id": int(chat_id),
            "created_at": now,
            "expires_at": now + timedelta(days=REPLY_MAP_TTL_DAYS),
        }
        await asyncio.to_thread(self._col.document(f"{chat_id}_{message_id}").set, data)

    def _get_sync(self, chat_id: int, message_id: int) -> dict | None:
        snapshot = self._col.document(f"{chat_id}_{message_id}").get()
        if not snapshot.exists and chat_id == self._default_owner_id:
            snapshot = self._col.document(str(message_id)).get()  # key format of the single-admin version
        return snapshot.to_dict() if snapshot.exists else None

    async def get(self, chat_id: int, message_id: int) -> tuple[str, int, int] | None:
        data = await asyncio.to_thread(self._get_sync, chat_id, message_id)
        if not data:
            return None
        try:
            return str(data["account_id"]), int(data["peer_id"]), int(data["original_message_id"])
        except (KeyError, TypeError, ValueError):
            return None
