"""Connected Telegram accounts. Every account belongs to exactly one admin (owner_id)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from telegram_manager.parsing import mask_phone

logger = logging.getLogger(__name__)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class AccountStore:
    def __init__(self, db, default_owner_id: int, settings) -> None:
        self._col = db.collection("telegram_accounts")
        self._default_owner_id = default_owner_id
        self._settings = settings
        self._cache: list[dict] | None = None
        self._version = 0
        self._create_lock = asyncio.Lock()

    # --- reading -----------------------------------------------------------------
    def _persist(self, doc_id: str, values: dict) -> None:
        try:
            self._col.document(doc_id).update(values)
        except Exception as exc:
            logger.warning("Could not save migrated account fields: %s", exc)

    def _list_sync(self) -> list[dict]:
        rows = []
        for snapshot in self._col.order_by("created_at").stream():
            row = snapshot.to_dict() or {}
            row["id"] = snapshot.id
            rows.append(row)
        self._normalise(rows)
        return rows

    def _normalise(self, rows: list[dict]) -> None:
        """Older accounts had no owner (single-admin bot): they belong to the owner.
        Numbers are per admin, so every admin sees 1, 2, 3 ... for their own accounts."""
        for row in rows:
            if not _is_int(row.get("owner_id")):
                row["owner_id"] = self._default_owner_id
                self._persist(row["id"], {"owner_id": self._default_owner_id})
        for row in rows:  # accounts saved before phone_masked existed
            if row.get("phone") and not row.get("phone_masked"):
                row["phone_masked"] = mask_phone(row["phone"])
                self._persist(row["id"], {"phone_masked": row["phone_masked"]})
        groups: dict[int, list[dict]] = {}
        for row in rows:
            groups.setdefault(row["owner_id"], []).append(row)
        for group in groups.values():
            seen: set[int] = set()
            pending = []
            for row in group:
                number = row.get("number")
                if _is_int(number) and number > 0 and number not in seen:
                    seen.add(number)
                else:
                    pending.append(row)
            following = max(seen, default=0) + 1
            for row in pending:
                row["number"] = following
                self._persist(row["id"], {"number": following})
                following += 1

    async def _all(self) -> list[dict]:
        if self._cache is not None:
            return [dict(row) for row in self._cache]
        version = self._version
        rows = await asyncio.to_thread(self._list_sync)
        if version == self._version:
            self._cache = rows
        return [dict(row) for row in rows]

    async def list(self, owner_id: int | None = None) -> list[dict]:
        rows = await self._all()
        if owner_id is None:
            return rows
        return sorted((row for row in rows if row["owner_id"] == owner_id), key=lambda row: row.get("number") or 0)

    async def get(self, account_id: str, owner_id: int | None = None) -> dict | None:
        for row in await self._all():
            if row["id"] == account_id:
                if owner_id is not None and row["owner_id"] != owner_id:
                    return None
                return row
        return None

    async def find_by_alias(self, owner_id: int, alias: str) -> dict | None:
        """Find one of this admin's accounts by number, @username, name or phone tail."""
        wanted = (alias or "").strip().lower().lstrip("@")
        if not wanted:
            return None
        rows = await self.list(owner_id)
        if wanted.isdigit():
            for row in rows:
                if row.get("number") == int(wanted):
                    return row
        for row in rows:
            candidates = {
                str(row.get("display_name", "")).lower(),
                str(row.get("username", "")).lower().lstrip("@"),
                str(row.get("phone_masked", "")).lower(),
            }
            if wanted in candidates:
                return row
        # Phone: the full number or just its last digits (at least 4, to avoid accidental hits).
        # Only an unambiguous match counts; two accounts ending the same way return None.
        digits = re.sub(r"\D", "", alias or "")
        if len(digits) >= 4:
            matches = [row for row in rows if re.sub(r"\D", "", str(row.get("phone") or "")).endswith(digits)]
            if len(matches) == 1:
                return matches[0]
        return None

    # --- writing -----------------------------------------------------------------
    def _touch(self) -> None:
        self._version += 1
        self._cache = None

    async def create(self, owner_id: int, values: dict) -> dict:
        async with self._create_lock:
            existing = await self.list(owner_id)
            counter = await self._settings.get_counter(owner_id)
            number = max(counter, max((row.get("number", 0) for row in existing), default=0) + 1)
            data = {
                **values,
                "owner_id": owner_id,
                "number": number,
                "created_at": datetime.now(timezone.utc),
            }
            self._touch()
            try:
                reference = self._col.document()
                await asyncio.to_thread(reference.set, data)
            finally:
                self._touch()
            try:
                await self._settings.set_counter(owner_id, number + 1)
            except Exception as exc:  # the account exists; the counter is only a high-water mark
                logger.warning("Could not save the account counter: %s", exc)
            return {**data, "id": reference.id}

    async def update(self, account_id: str, values: dict) -> None:
        self._touch()
        try:
            await asyncio.to_thread(self._col.document(account_id).update, values)
        finally:
            self._touch()

    async def transfer(self, account_id: str, new_owner_id: int) -> dict:
        """Give an account to another admin. It gets the next free number in its new place."""
        async with self._create_lock:
            row = await self.get(account_id)
            if row is None:
                raise ValueError("Account not found")
            existing = await self.list(new_owner_id)
            counter = await self._settings.get_counter(new_owner_id)
            number = max(counter, max((r.get("number", 0) for r in existing), default=0) + 1)
            await self.update(account_id, {"owner_id": new_owner_id, "number": number})
            try:
                await self._settings.set_counter(new_owner_id, number + 1)
            except Exception as exc:  # only a high-water mark
                logger.warning("Could not save the account counter: %s", exc)
            return {**row, "owner_id": new_owner_id, "number": number}

    async def delete(self, account_id: str) -> None:
        self._touch()
        try:
            await asyncio.to_thread(self._col.document(account_id).delete)
        finally:
            self._touch()
