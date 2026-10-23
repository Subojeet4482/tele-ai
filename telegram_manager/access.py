"""Who may use the bot: the owner (from OWNER_ID) plus admins the owner adds."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class AccessControl:
    def __init__(self, store, owner_id: int) -> None:
        self._store = store
        self.owner_id = owner_id
        self._admins: dict[int, dict] | None = None

    async def ensure_loaded(self) -> None:
        if self._admins is not None:
            return
        try:
            rows = await self._store.admins.list()
        except Exception as exc:
            logger.warning("Could not load the admin list (%s); only the owner can use the bot until it loads", exc)
            return
        self._admins = {row["id"]: row for row in rows}

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id == self.owner_id

    def is_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return user_id == self.owner_id or (self._admins is not None and user_id in self._admins)

    def admins(self) -> list[dict]:
        """Admins added by the owner (the owner is not in this list)."""
        return sorted((self._admins or {}).values(), key=lambda row: row.get("added_at") or 0)

    def admin(self, user_id: int) -> dict | None:
        return (self._admins or {}).get(user_id)

    async def add(self, user_id: int, added_by: int, name: str = "") -> None:
        await self.ensure_loaded()
        if self._admins is None:
            raise RuntimeError("Could not read the admin list from Firestore. Try again in a moment.")
        row = await self._store.admins.add(user_id, added_by, name)
        self._admins[user_id] = row

    async def remove(self, user_id: int) -> None:
        await self.ensure_loaded()
        await self._store.admins.remove(user_id)
        if self._admins is not None:
            self._admins.pop(user_id, None)

    async def remember_name(self, user_id: int, name: str) -> None:
        row = (self._admins or {}).get(user_id)
        if row is None or not name or row.get("name") == name:
            return
        row["name"] = name
        try:
            await self._store.admins.set_name(user_id, name)
        except Exception as exc:
            logger.debug("Could not save admin name: %s", exc)
