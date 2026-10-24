from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram_manager.core.proxy import uses_proxy
from telegram_manager.crypto import SecretDecryptError
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)


class LifecycleMixin:
    async def start_saved_accounts(self) -> None:
        try:
            accounts = [a for a in await self.store.accounts.list() if a.get("enabled", True)]
        except Exception as exc:
            logger.exception("Could not load saved accounts from Firestore")
            await self.notify(
                self.owner_id,
                f"⚠️ Could not load saved accounts from Firestore: {describe_error(exc)}. Will keep retrying.",
            )
            accounts = []
        self._known_total = len(accounts)
        logger.info("Starting %d saved account(s)", len(accounts))
        results = await asyncio.gather(*(self._connect_saved_account(a) for a in accounts), return_exceptions=True)
        for account, result in zip(accounts, results):
            if isinstance(result, Exception):
                reason = describe_error(result)
                logger.error("Could not connect account %s: %s", account.get("id"), reason, exc_info=result)
                self._offline_notified.add(str(account.get("id")))
                label = self.label_for_account(account)
                await self.notify(self._owner_of(account), f"⚠️ Could not connect {label}: {reason}")
        if self._superseded:
            logger.warning("Startup stopped: another copy of the app took over the accounts")
            return
        await self.restore_daily_jobs()
        self.start_watchdog()
        logger.info(
            "Startup finished: %d/%d account(s) online",
            sum(1 for a in accounts if self.is_online(str(a["id"]))),
            len(accounts),
        )

    async def _connect_saved_account(self, account: dict[str, Any]) -> None:
        account_id = str(account["id"])
        if account_id in self.clients or account_id in self._removing or self._superseded:
            return
        label = self.label_for_account(account)
        try:
            api_hash = self.secret_box.decrypt(str(account["api_hash_enc"]))
            session_string = self.secret_box.decrypt(str(account["session_enc"]))
        except SecretDecryptError as exc:
            self._dead_accounts[account_id] = str(exc)
            raise
        proxied = uses_proxy(account)
        try:
            if proxied:  # connected already: tries its proxies in turn (core/proxy.py)
                client = await self._connect_via_proxies(account, session_string, int(account["api_id"]), api_hash)
            else:
                client = self._new_client(session_string, int(account["api_id"]), api_hash)
        except ValueError as exc:
            # A malformed saved session can never work; do not retry it every minute.
            self._dead_accounts[account_id] = f"the saved session is not valid ({describe_error(exc)})"
            raise
        try:
            if not proxied:
                await self._connect_client(client)
            authorized = await self._request(client.is_user_authorized())
        except BaseException:
            await self._safe_disconnect(client)
            raise
        if not authorized:
            logger.warning("Account %s is no longer authorized", label)
            await self._safe_disconnect(client)
            self._dead_accounts[account_id] = "session expired or revoked"
            self._offline_notified.add(account_id)
            await self.notify(
                self._owner_of(account),
                f"⚠️ {label} is no longer authorized (session expired or revoked). Log it out and connect it again.",
            )
            return
        if account_id in self._removing or self._superseded:
            await self._safe_disconnect(client)  # logged out (or handed to another copy) while connecting
            return
        self.accounts[account_id] = account
        self.clients[account_id] = client
        self._register_handlers(account_id, client)
        self._dead_accounts.pop(account_id, None)
        self._dead_kinds.pop(account_id, None)
        logger.info("Account %s connected", label)

    async def _stop_accounts(self) -> None:
        """Disconnect every account and stop the background jobs (daily jobs stay saved in
        Firestore, so whoever runs the accounts next restores them)."""
        self._cancel_all_daily()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        clients = list(self.clients.values())
        self.clients.clear()
        self._dialog_cache.clear()
        logger.info("Disconnecting %d account(s)", len(clients))
        await asyncio.gather(*(self._safe_disconnect(client) for client in clients), return_exceptions=True)

    async def stop_all(self) -> None:
        if self._lease_task is not None:
            self._lease_task.cancel()
            self._lease_task = None
        await self._stop_accounts()
        if self.use_lease:
            # Only after the accounts are disconnected, so the next copy never overlaps with us.
            try:
                await self.store.lease.release(self.instance_id)
            except Exception as exc:
                logger.warning("Could not release the instance lease: %s", describe_error(exc))
