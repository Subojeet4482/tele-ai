"""One shared record that says which running copy of the app owns the sessions."""
from __future__ import annotations

import asyncio


class LeaseStore:
    def __init__(self, db) -> None:
        self._doc = db.collection("telegram_settings").document("instance_lease")

    async def get(self) -> dict | None:
        snapshot = await asyncio.to_thread(self._doc.get)
        return snapshot.to_dict() if snapshot.exists else None

    async def set(self, owner: str, timestamp: float) -> None:
        await asyncio.to_thread(self._doc.set, {"owner": owner, "ts": float(timestamp)})

    async def release(self, owner: str) -> None:
        lease = await self.get()
        if lease and lease.get("owner") == owner:
            await asyncio.to_thread(self._doc.delete)
