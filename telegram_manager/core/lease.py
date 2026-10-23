"""Two copies of the app must never use the same Telegram sessions at once (Telegram would
revoke them). The running copy refreshes a lease record; a newer copy takes it over."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telegram_manager.constants import LEASE_INTERVAL, LEASE_STALE_SECONDS, TAKEOVER_WAIT
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)


class LeaseMixin:
    @staticmethod
    def _lease_is_fresh(lease: dict[str, Any]) -> bool:
        try:
            return time.time() - float(lease.get("ts", 0)) < LEASE_STALE_SECONDS
        except (TypeError, ValueError):
            return False

    async def acquire_lease(self) -> None:
        """Claim the accounts for this copy. If another copy is running, give it time to
        notice and disconnect before we connect the same sessions."""
        if not self.use_lease:
            return
        previous = None
        try:
            previous = await self.store.lease.get()
            await self.store.lease.set(self.instance_id, time.time())
        except Exception as exc:
            logger.warning("Could not use the instance lease (%s); continuing without waiting", describe_error(exc))
            previous = None
        if previous and previous.get("owner") not in (None, self.instance_id) and self._lease_is_fresh(previous):
            logger.warning(
                "Another copy of the app (%s) is still running. Waiting %ss so it lets go of the accounts first",
                previous.get("owner"),
                TAKEOVER_WAIT,
            )
            await asyncio.sleep(TAKEOVER_WAIT)
        if self._lease_task is None or self._lease_task.done():
            self._lease_task = asyncio.create_task(self._lease_loop())

    async def _lease_loop(self) -> None:
        while True:
            await asyncio.sleep(LEASE_INTERVAL)
            try:
                await self._lease_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a Firestore hiccup must not look like a takeover
                logger.warning("Instance lease check failed: %s", describe_error(exc))

    async def _lease_tick(self) -> None:
        lease = await self.store.lease.get()
        owner = lease.get("owner") if lease else None
        if owner in (None, self.instance_id):
            if self._superseded:
                await self._resume_after_supersede("the other copy shut down")
            else:
                await self.store.lease.set(self.instance_id, time.time())
            return
        if not self._superseded:
            self._superseded = True
            logger.error(
                "Another copy of the app (%s) took over the accounts. This copy disconnects them "
                "so the sessions are not used twice",
                owner,
            )
            await self._stop_accounts()
            return
        if not self._lease_is_fresh(lease):
            await self._resume_after_supersede("the other copy stopped responding")

    async def _resume_after_supersede(self, why: str) -> None:
        await self.store.lease.set(self.instance_id, time.time())
        self._superseded = False
        logger.warning("Taking the accounts back (%s)", why)
        await self.start_saved_accounts()
        await self.notify(self.owner_id, f"🔁 This copy of the bot took the accounts back ({why}).")
