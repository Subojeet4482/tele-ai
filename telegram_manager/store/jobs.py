"""Daily broadcast jobs. One owner/admin can have several (slot 1, 2, 3 ...)."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _doc_id(owner_id: int, slot: int) -> str:
    return f"{owner_id}_{slot}"


def _parse_id(doc_id: str) -> tuple[int, int] | None:
    """'100_2' -> (100, 2). A bare '100' is the single-job era document: slot 1."""
    owner, _, slot = doc_id.partition("_")
    try:
        return int(owner), (int(slot) if slot else 1)
    except ValueError:
        return None


class JobStore:
    def __init__(self, db, default_owner_id: int) -> None:
        self._col = db.collection("telegram_daily_jobs")
        self._legacy = db.collection("telegram_settings").document("daily_job")
        self._default_owner_id = default_owner_id

    def _list_sync(self) -> list[tuple[int, int, dict]]:
        found: dict[tuple[int, int], dict] = {}
        old_style: list[tuple[str, tuple[int, int], dict]] = []
        for snapshot in self._col.stream():
            key = _parse_id(snapshot.id)
            if key is None:
                continue
            data = snapshot.to_dict() or {}
            if "_" in snapshot.id:
                found[key] = data
            else:
                old_style.append((snapshot.id, key, data))
        for old_id, key, data in old_style:  # one job per admin used to live under the bare admin id
            if key not in found:
                found[key] = data
                self._col.document(_doc_id(*key)).set(data)
            self._col.document(old_id).delete()
        if not any(owner == self._default_owner_id for owner, _ in found):
            legacy = self._legacy.get()
            if legacy.exists and legacy.to_dict():
                # Single-admin era job: it becomes the owner's first job.
                key = (self._default_owner_id, 1)
                found[key] = legacy.to_dict()
                self._col.document(_doc_id(*key)).set(legacy.to_dict())
                self._legacy.delete()
        return [(owner, slot, data) for (owner, slot), data in sorted(found.items())]

    async def list_all(self) -> list[tuple[int, int, dict]]:
        """(owner id, slot, job data) for every saved job."""
        return await asyncio.to_thread(self._list_sync)

    async def save(self, owner_id: int, data: dict, slot: int = 1) -> None:
        await asyncio.to_thread(self._col.document(_doc_id(owner_id, slot)).set, data)

    async def delete(self, owner_id: int, slot: int | None = None) -> None:
        """Delete one job, or (slot=None) every job of this owner."""
        if slot is not None:
            await asyncio.to_thread(self._col.document(_doc_id(owner_id, slot)).delete)
            return

        def delete_all() -> None:
            for snapshot in self._col.stream():
                key = _parse_id(snapshot.id)
                if key is not None and key[0] == owner_id:
                    self._col.document(snapshot.id).delete()

        await asyncio.to_thread(delete_all)
