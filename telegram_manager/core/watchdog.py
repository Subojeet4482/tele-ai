"""Keeps accounts connected: reconnects dropped ones and reports dead sessions to their owner."""
from __future__ import annotations

import asyncio
import logging
import time

from telethon import errors

from telegram_manager.account_status import classify_dead
from telegram_manager.constants import LIVENESS_EVERY_TICKS, WATCHDOG_INTERVAL
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)

# Errors that mean the session itself is dead (logged out elsewhere, banned, ...).
# Retrying can never fix these, so the watchdog stops trying and tells the account's owner.
DEAD_SESSION_ERRORS = tuple(
    getattr(errors, name)
    for name in (
        "AuthKeyUnregisteredError",
        "AuthKeyDuplicatedError",
        "SessionRevokedError",
        "SessionExpiredError",
        "UserDeactivatedError",
        "UserDeactivatedBanError",
    )
    if hasattr(errors, name)
)


class WatchdogMixin:
    def start_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_last = time.time()
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        logger.info("Connection watchdog started (checks every %ss)", WATCHDOG_INTERVAL)
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            try:
                await self.watchdog_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Watchdog tick failed")
            self._watchdog_last = time.time()

    async def watchdog_tick(self) -> None:
        """Bring back accounts that dropped offline, and pick up accounts that never
        managed to connect at startup."""
        if self._superseded:
            return
        self._watchdog_ticks += 1
        deep_check = self._watchdog_ticks % LIVENESS_EVERY_TICKS == 0
        for account_id, client in list(self.clients.items()):
            if account_id in self._removing:
                continue
            account = self.accounts.get(account_id, {"display_name": account_id})
            label = self.label_for_account(account)
            owner = self._owner_of(account)
            if not client.is_connected():
                logger.warning("Account %s is disconnected - reconnecting", label)
                try:
                    await self._connect_client(client)
                except DEAD_SESSION_ERRORS as exc:
                    await self._drop_dead_account(account_id, label, describe_error(exc), exc.__class__.__name__)
                except Exception as exc:
                    logger.warning("Reconnect failed for %s: %s", label, describe_error(exc))
                    if account_id not in self._offline_notified:
                        self._offline_notified.add(account_id)
                        await self.notify(
                            owner,
                            f"🔴 {label} lost its connection and could not reconnect yet ({describe_error(exc)}). "
                            "I will keep trying.",
                        )
                else:
                    logger.info("Account %s reconnected", label)
                    if account_id in self._offline_notified:
                        self._offline_notified.discard(account_id)
                        await self.notify(owner, f"🟢 {label} is back online.")
                continue
            if deep_check:
                try:
                    await self._request(client.get_me(), 30)
                    logger.debug("Liveness check ok for %s", label)
                except DEAD_SESSION_ERRORS as exc:
                    await self._drop_dead_account(account_id, label, describe_error(exc), exc.__class__.__name__)
                except Exception as exc:
                    logger.warning("Liveness check failed for %s: %s", label, describe_error(exc))

        try:
            saved = await self.store.accounts.list()
        except Exception as exc:
            logger.warning("Watchdog could not read accounts from Firestore: %s", describe_error(exc))
            return
        self._known_total = len(saved)
        for account in saved:
            account_id = str(account["id"])
            if (
                account_id in self.clients
                or account_id in self._dead_accounts
                or account_id in self._removing
                or not account.get("enabled", True)
            ):
                continue
            label = self.label_for_account(account)
            owner = self._owner_of(account)
            logger.info("Retrying connection for %s", label)
            try:
                await self._connect_saved_account(account)
            except DEAD_SESSION_ERRORS as exc:
                await self._drop_dead_account(account_id, label, describe_error(exc), exc.__class__.__name__)
            except Exception as exc:
                reason = describe_error(exc)
                logger.warning("Retry failed for %s: %s", label, reason)
                if account_id not in self._offline_notified:
                    self._offline_notified.add(account_id)
                    await self.notify(owner, f"⚠️ Could not connect {label}: {reason}")
            else:
                if account_id in self.clients and account_id in self._offline_notified:
                    self._offline_notified.discard(account_id)
                    await self.notify(owner, f"🟢 {label} is connected.")

    async def _drop_dead_account(self, account_id: str, label: str, reason: str, kind: str = "") -> None:
        logger.error("Session for %s is dead: %s", label, reason)
        account = self.accounts.get(account_id, {})
        client = self.clients.pop(account_id, None)
        if client is not None:
            await self._safe_disconnect(client)
        self._dead_accounts[account_id] = reason
        self._dead_kinds[account_id] = kind
        self._offline_notified.add(account_id)
        status = classify_dead(kind, reason)
        what = {"banned": "banned by Telegram", "deleted": "deactivated or deleted"}.get(status, "logged out by Telegram")
        await self.notify(
            self._owner_of(account),
            f"⚠️ {label} was {what} ({reason}). Forwarding for it has stopped. "
            "Log it out here and connect it again.",
        )
