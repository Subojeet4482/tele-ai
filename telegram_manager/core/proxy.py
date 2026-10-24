"""Proxies for the accounts: a pool per admin, tests, and connecting with retry.

How an account uses proxies (saved on the account: `proxy_id`, `proxy_auto`):
  direct   - neither set: no proxy (the default, nothing changes for existing accounts)
  fixed    - `proxy_id`: always that proxy (a failed connect is retried a few times, then it stays offline)
  auto     - `proxy_auto`: its proxy first, and when that fails the next working one of the pool, up to PROXY_MAX_TRIES;
             a proxy that failed rests for PROXY_COOLDOWN seconds. The watchdog does the same when a connection drops.
Passwords and MTProxy secrets are stored encrypted (like sessions) and never shown or sent to the AI."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any, Iterable

from telegram_manager.core.models import BulkResult
from telegram_manager.core.proxyparse import describe
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)

PROXY_COOLDOWN = 300  # seconds a failed proxy is left alone
PROXY_MAX_TRIES = 4  # proxies tried for one connect
PROXY_FIXED_RETRIES = 2  # tries of a fixed proxy
PROXY_CONNECT_TIMEOUT = 20  # seconds per try
PROXY_TEST_TIMEOUT = 15
PROXY_TEST_PARALLEL = 5
PROXY_TEST_MAX = 40


def uses_proxy(account: dict[str, Any]) -> bool:
    return bool(account.get("proxy_id") or account.get("proxy_auto"))


class ProxyMixin:
    # --- state kept in memory (mixins have no __init__) --------------------------------------------------
    def _proxy_state(self) -> dict[str, Any]:
        state = self.__dict__.get("_proxy_runtime")
        if state is None:
            state = self.__dict__["_proxy_runtime"] = {"cool": {}, "health": {}}
        return state

    def _cool(self, owner_id: int, proxy_id: str) -> None:
        self._proxy_state()["cool"][(owner_id, proxy_id)] = time.time() + PROXY_COOLDOWN

    def _uncool(self, owner_id: int, proxy_id: str) -> None:
        self._proxy_state()["cool"].pop((owner_id, proxy_id), None)

    def proxy_cooling(self, owner_id: int, proxy_id: str) -> bool:
        return self._proxy_state()["cool"].get((owner_id, proxy_id), 0) > time.time()

    def proxy_health(self, owner_id: int, proxy_id: str) -> dict[str, Any] | None:
        return self._proxy_state()["health"].get((owner_id, proxy_id))

    # --- the pool ---------------------------------------------------------------------------------------------
    async def proxy_pool(self, owner_id: int) -> list[dict[str, Any]]:
        """The admin's saved proxies (passwords still encrypted), in the order they were added."""
        return await self.store.settings.get_proxies(owner_id)

    def _plain(self, row: dict[str, Any]) -> dict[str, Any]:
        """A saved proxy with its password / secret decrypted (only ever handed to Telethon)."""
        out = {k: row.get(k, "") for k in ("scheme", "host", "port", "user")}
        out["password"] = self.secret_box.decrypt(row["password_enc"]) if row.get("password_enc") else ""
        out["secret"] = self.secret_box.decrypt(row["secret_enc"]) if row.get("secret_enc") else ""
        return out

    async def proxy_add(self, owner_id: int, proxies: Iterable[dict[str, Any]], src: str = "manual") -> dict[str, int]:
        """Save parsed proxies (see proxyparse). src: "manual" (typed / pasted / a .txt) or "url" (see proxysource).
        Returns {'added': n, 'duplicates': n, 'total': n}."""
        pool = await self.proxy_pool(owner_id)
        known = {(r["scheme"], r["host"], int(r["port"]), r.get("user", "")) for r in pool}
        added = duplicates = 0
        for proxy in proxies:
            key = (proxy["scheme"], proxy["host"], int(proxy["port"]), proxy.get("user", ""))
            if key in known:
                duplicates += 1
                continue
            known.add(key)
            added += 1
            pool.append({
                "id": secrets.token_hex(3), "src": "url" if src == "url" else "manual", "scheme": proxy["scheme"], "host": proxy["host"], "port": int(proxy["port"]),
                "user": proxy.get("user", ""),
                "password_enc": self.secret_box.encrypt(proxy["password"]) if proxy.get("password") else "",
                "secret_enc": self.secret_box.encrypt(proxy["secret"]) if proxy.get("secret") else "",
            })
        if added:
            await self.store.settings.set_proxies(owner_id, pool)
        return {"added": added, "duplicates": duplicates, "total": len(pool)}

    async def proxy_remove(self, owner_id: int, proxy_ids: Iterable[str]) -> int:
        """Delete proxies. Accounts that used one fall back to direct (or stay auto when they were auto)."""
        gone = set(proxy_ids)
        pool = await self.proxy_pool(owner_id)
        kept = [r for r in pool if r["id"] not in gone]
        if len(kept) == len(pool):
            return 0
        await self.store.settings.set_proxies(owner_id, kept)
        for account_id, account in list(self.accounts.items()):
            if self._owner_of(account) == owner_id and account.get("proxy_id") in gone:
                values = {"proxy_id": ""}
                account.update(values)
                await self.store.accounts.update(account_id, values)
        return len(pool) - len(kept)

    # --- testing -------------------------------------------------------------------------------------------------
    async def _api_for_tests(self, owner_id: int) -> tuple[int, str]:
        saved = await self.saved_api(owner_id)
        if saved:
            return saved
        for account in self.accounts.values():
            if self._owner_of(account) == owner_id and account.get("api_hash_enc"):
                return int(account["api_id"]), self.secret_box.decrypt(str(account["api_hash_enc"]))
        raise ValueError("To test a proxy an API id is needed: connect one account first (or save one with /setapi).")

    async def proxy_test(self, owner_id: int, proxy_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Open a real Telegram connection through each proxy (no login). Returns one row per proxy:
        {'id', 'label', 'ok', 'ms', 'error'}."""
        pool = await self.proxy_pool(owner_id)
        wanted = set(proxy_ids) if proxy_ids is not None else None
        rows = [r for r in pool if wanted is None or r["id"] in wanted][:PROXY_TEST_MAX]
        if not rows:
            raise ValueError("There is no proxy to test.")
        api_id, api_hash = await self._api_for_tests(owner_id)
        gate = asyncio.Semaphore(PROXY_TEST_PARALLEL)

        async def one(row: dict[str, Any]) -> dict[str, Any]:
            plain = self._plain(row)
            result = {"id": row["id"], "label": describe(plain), "ok": False, "ms": 0, "error": ""}
            async with gate:
                started = time.monotonic()
                client = None
                try:
                    client = self._new_client("", api_id, api_hash, proxy=plain)
                    await self._connect_client(client, PROXY_TEST_TIMEOUT)
                    result.update(ok=True, ms=int((time.monotonic() - started) * 1000))
                except Exception as exc:
                    result["error"] = describe_error(exc)[:120]
                finally:
                    if client is not None:
                        await self._safe_disconnect(client)
            if result["ok"]:
                self._uncool(owner_id, row["id"])
            else:
                self._cool(owner_id, row["id"])
            self._proxy_state()["health"][(owner_id, row["id"])] = {**result, "at": time.time()}
            return result

        return list(await asyncio.gather(*(one(r) for r in rows)))

    # --- connecting with retry ---------------------------------------------------------------------------------------
    async def _proxy_candidates(self, account: dict[str, Any]) -> list[dict[str, Any]]:
        """The proxies to try for this account, best first."""
        owner = self._owner_of(account)
        pool = await self.proxy_pool(owner)
        by_id = {r["id"]: r for r in pool}
        current = by_id.get(account.get("proxy_id") or "")
        if not account.get("proxy_auto"):
            return [current] * PROXY_FIXED_RETRIES if current else []
        eligible = await self.proxy_pool_for_accounts(owner)  # manual / url / both, as the admin chose
        others = [r for r in eligible if not current or r["id"] != current["id"]]
        fresh = [r for r in others if not self.proxy_cooling(owner, r["id"])]
        rested = [r for r in others if self.proxy_cooling(owner, r["id"])]
        order = ([current] if current else []) + fresh + rested  # a resting proxy is the last resort, never skipped for good
        return order[:PROXY_MAX_TRIES]

    async def _connect_via_proxies(self, account: dict[str, Any], session_string: str, api_id: int, api_hash: str):
        """A connected Telethon client for the account through the first proxy that works.
        Raises ConnectionError (all failed). A dead session is not a proxy problem and is raised as it is."""
        from telegram_manager.core.watchdog import DEAD_SESSION_ERRORS

        owner = self._owner_of(account)
        candidates = await self._proxy_candidates(account)
        if not candidates:
            raise ConnectionError("This account should use a proxy but the pool is empty: add proxies or switch it to direct.")
        errors: list[str] = []
        for row in candidates:
            plain = self._plain(row)
            client = self._new_client(session_string, api_id, api_hash, proxy=plain)
            try:
                await self._connect_client(client, PROXY_CONNECT_TIMEOUT)
            except DEAD_SESSION_ERRORS:
                await self._safe_disconnect(client)
                raise
            except Exception as exc:
                await self._safe_disconnect(client)
                self._cool(owner, row["id"])
                errors.append(f"{describe(plain)}: {describe_error(exc)}"[:140])
                logger.warning("Proxy %s failed for %s: %s", describe(plain), self.label_for_account(account), describe_error(exc))
                continue
            self._uncool(owner, row["id"])
            if account.get("proxy_id") != row["id"]:  # remember the proxy that works
                account["proxy_id"] = row["id"]
                try:
                    await self.store.accounts.update(str(account["id"]), {"proxy_id": row["id"]})
                except Exception:
                    logger.debug("Could not save the working proxy", exc_info=True)
            return client
        raise ConnectionError("Every proxy failed (" + "; ".join(errors[-3:]) + ")")

    async def _swap_client(self, account_id: str, make_client) -> None:
        """Connect a NEW client first and only then retire the old one, so a failure never takes a working account offline."""
        account = self.accounts[account_id]
        api_hash = self.secret_box.decrypt(str(account["api_hash_enc"]))
        session_string = self.secret_box.decrypt(str(account["session_enc"]))
        client = await make_client(account, session_string, int(account["api_id"]), api_hash)
        try:
            if not await self._request(client.is_user_authorized()):
                raise ConnectionError("the session is no longer authorized")
        except BaseException:
            await self._safe_disconnect(client)
            raise
        old = self.clients.get(account_id)
        self.clients[account_id] = client
        self._register_handlers(account_id, client)
        if old is not None:
            await self._safe_disconnect(old)

    async def _direct_client(self, account, session_string, api_id, api_hash):
        client = self._new_client(session_string, api_id, api_hash)
        try:
            await self._connect_client(client)
        except BaseException:
            await self._safe_disconnect(client)
            raise
        return client

    async def reconnect_account(self, account_id: str) -> None:
        """(Re)connect one account the way it is configured now: through its proxies, or direct."""
        account = self.accounts[account_id]
        await self._swap_client(account_id, self._connect_via_proxies if uses_proxy(account) else self._direct_client)

    async def proxy_failover(self, account_id: str) -> bool:
        """The watchdog calls this when an account that uses proxies lost its connection: move it to the next working
        proxy. Returns True when the account is online again; False when it does not use proxies or nothing worked."""
        account = self.accounts.get(account_id)
        if account is None or not uses_proxy(account):
            return False
        owner = self._owner_of(account)
        if account.get("proxy_id"):
            self._cool(owner, account["proxy_id"])  # the one it was on has just failed
        try:
            await self.reconnect_account(account_id)
        except Exception as exc:
            logger.warning("Proxy failover failed for %s: %s", self.label_for_account(account), describe_error(exc))
            return False
        return True

    # --- giving proxies to accounts ----------------------------------------------------------------------------------
    async def proxy_assign(self, owner_id: int, account_ids: Iterable[str], choice: str, delay_seconds: int = 2) -> BulkResult:
        """choice: 'auto' (rotate through the pool with retry) · 'off' (direct) · a proxy id. Applies at once."""
        pool = {r["id"]: r for r in await self.proxy_pool(owner_id)}
        if choice == "auto" and not await self.proxy_pool_for_accounts(owner_id) and (await self.proxy_source_get(owner_id))["use"] != "manual":
            from telegram_manager.core.proxysource import ProxySourceError  # nothing saved yet: the default URL works without any setup

            try:
                await self.proxy_url_refresh(owner_id)
            except ProxySourceError as exc:
                raise ValueError(f"The proxy pool is empty and the proxy link failed: {exc}") from exc
            pool = {r["id"]: r for r in await self.proxy_pool(owner_id)}
        if choice not in ("auto", "off") and choice not in pool:
            raise ValueError("Unknown proxy: use auto, off or a proxy from the list.")
        if choice != "off" and not pool:
            raise ValueError("The proxy pool is empty: add proxies first.")
        values = {"proxy_auto": True, "proxy_id": ""} if choice == "auto" else (
            {"proxy_auto": False, "proxy_id": ""} if choice == "off" else {"proxy_auto": False, "proxy_id": choice})

        async def one(account_id: str) -> None:
            account = self.accounts[account_id]
            before = {k: account.get(k) for k in values}
            account.update(values)
            try:
                if self.is_online(account_id):
                    await self.reconnect_account(account_id)
            except BaseException:
                account.update(before)  # still on the old setting and still online
                raise
            await self.store.accounts.update(account_id, values)

        return await self._for_each_account(owner_id, one, delay_seconds, "proxy", only_ids=account_ids)
