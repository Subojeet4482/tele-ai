from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class AccountsMixin:
    async def account_by_number(self, owner_id: int, number: int) -> dict[str, Any] | None:
        for account in await self.store.accounts.list(owner_id):
            if account.get("number") == number:
                return account
        return None

    async def refresh_accounts(self, owner_id: int) -> tuple[int, int, int]:
        """Read every online account's current name and @username from Telegram and save what changed.
        Returns (changed, failed, offline)."""
        rows = await self.store.accounts.list(owner_id)
        online = [row for row in rows if self.is_online(str(row["id"]))]

        async def one(row: dict[str, Any]) -> bool:
            account_id = str(row["id"])
            me = await self._request(self.clients[account_id].get_me(), 20)
            name = " ".join(part for part in (me.first_name, me.last_name) if part) or me.username or str(me.id)
            changes = {}
            if name != row.get("display_name"):
                changes["display_name"] = name
            if (me.username or "") != (row.get("username") or ""):
                changes["username"] = me.username or ""
            if changes:
                await self.store.accounts.update(account_id, changes)
                if account_id in self.accounts:
                    self.accounts[account_id].update(changes)
            return bool(changes)

        results = await asyncio.gather(*(one(row) for row in online), return_exceptions=True)
        failed = sum(1 for result in results if isinstance(result, BaseException))
        for row, result in zip(online, results):
            if isinstance(result, BaseException):
                logger.warning("Could not refresh account %s: %s", row.get("number"), result)
        changed = sum(1 for result in results if result is True)
        return changed, failed, len(rows) - len(online)

    async def set_forward_override(self, account_id: str, name: str, value: bool | None) -> None:
        """Per-account forwarding switch. None means "follow the admin's global setting"."""
        await self.store.accounts.update(account_id, {name: value})
        if account_id in self.accounts:
            self.accounts[account_id][name] = value

    async def remove_account(self, account_id: str, owner_id: int | None = None) -> bool | None:
        """Log the account out on Telegram's side, disconnect it and delete its stored session.
        Returns True if Telegram confirmed the logout, False if not, None if it is already
        being removed (a double tap). owner_id, when given, must match the account's owner."""
        if account_id in self._removing:
            return None
        row = await self.store.accounts.get(account_id)
        if owner_id is not None and row is not None and self._owner_of(row) != owner_id:
            raise ValueError("Account not found")
        # Marked first: while Telegram's logout is in flight the watchdog must not see
        # "saved but not connected" and reconnect the account we are removing.
        self._removing.add(account_id)
        try:
            client = self.clients.pop(account_id, None)
            existed = client is not None or row is not None
            self.accounts.pop(account_id, None)
            self._dead_accounts.pop(account_id, None)
            self._dead_kinds.pop(account_id, None)
            self._offline_notified.discard(account_id)
            self._dialog_cache.pop(account_id, None)
            revoked = False
            if client:
                try:
                    revoked = bool(await asyncio.wait_for(client.log_out(), 30))
                except Exception:
                    logger.warning("Telegram log_out failed for %s", account_id, exc_info=True)
                    await self._safe_disconnect(client)
            await self.store.accounts.delete(account_id)
            if existed:
                self._known_total = max(0, self._known_total - 1)
            logger.info("Account %s removed (telegram logout confirmed: %s)", account_id, revoked)
            return revoked
        finally:
            self._removing.discard(account_id)
