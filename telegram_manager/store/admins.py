"""Admins added by the owner. The owner himself comes from OWNER_ID, not from here."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone


class AdminStore:
    def __init__(self, db) -> None:
        self._col = db.collection("telegram_admins")

    def _list_sync(self) -> list[dict]:
        rows = []
        for snapshot in self._col.stream():
            data = snapshot.to_dict() or {}
            try:
                data["id"] = int(snapshot.id)
            except ValueError:
                continue
            added = data.get("added_at")
            data["added_at"] = added.timestamp() if hasattr(added, "timestamp") else 0
            rows.append(data)
        return rows

    async def list(self) -> list[dict]:
        return await asyncio.to_thread(self._list_sync)

    async def add(self, user_id: int, added_by: int, name: str = "") -> dict:
        now = datetime.now(timezone.utc)
        await asyncio.to_thread(
            self._col.document(str(user_id)).set, {"added_by": added_by, "added_at": now, "name": name}
        )
        return {"id": user_id, "added_by": added_by, "added_at": now.timestamp(), "name": name}

    async def remove(self, user_id: int) -> None:
        await asyncio.to_thread(self._col.document(str(user_id)).delete)

    async def set_name(self, user_id: int, name: str) -> None:
        await asyncio.to_thread(self._col.document(str(user_id)).update, {"name": name})
