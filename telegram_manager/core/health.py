"""Is each account healthy? Telegram bans, ended sessions and spam limits, found out on request."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from telegram_manager.account_status import UNUSABLE, classify_dead, parse_spambot
from telegram_manager.core.watchdog import DEAD_SESSION_ERRORS
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)


def _is_frozen(exc: BaseException) -> bool:
    """Telegram 'froze' the account: most requests are refused with a FROZEN_* error."""
    return "FROZEN" in f"{exc.__class__.__name__} {exc}".upper()


class HealthMixin:
    def account_health(self, row: dict[str, Any]) -> dict[str, Any]:
        """{"status", "online", "note", "checked_at"} from what the manager knows right now (no network).
        status: online / offline / banned / deleted / logged_out / limited / disabled."""
        account_id = str(row["id"])
        saved = row.get("health") or {}
        checked_at = str(saved.get("checked_at") or "")
        online = self.is_online(account_id)
        if account_id in self._dead_accounts:
            reason = self._dead_accounts[account_id]
            status = classify_dead(self._dead_kinds.get(account_id, ""), reason)
            return {"status": status, "online": False, "note": reason, "checked_at": checked_at}
        if not row.get("enabled", True):
            return {"status": "disabled", "online": online, "note": "switched off", "checked_at": checked_at}
        if not online:
            return {"status": "offline", "online": False, "note": "not connected right now (reconnecting)", "checked_at": checked_at}
        if saved.get("state") in ("banned", "limited"):
            return {"status": saved["state"], "online": True, "note": str(saved.get("note") or ""), "checked_at": checked_at}
        return {"status": "online", "online": True, "note": "", "checked_at": checked_at}

    async def _save_health(self, account_id: str, state: str, note: str) -> None:
        saved = {"state": state, "note": note, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        await self.store.accounts.update(account_id, {"health": saved})
        if account_id in self.accounts:
            self.accounts[account_id]["health"] = saved

    async def _ask_spambot(self, client) -> str:
        """Send /start to @SpamBot from this account and return its answer."""
        async with client.conversation("SpamBot", timeout=25, max_messages=10) as conversation:
            await conversation.send_message("/start")
            reply = await conversation.get_response()
        return getattr(reply, "raw_text", None) or getattr(reply, "text", "") or ""

    async def check_account(self, owner_id: int, account_id: str, spam: bool = True) -> dict[str, Any]:
        """Ask Telegram how this account is doing and remember the answer.
        An account that cannot connect is probed once; a connected one answers get_me (a banned or
        logged-out session fails right there) and, with spam=True, @SpamBot says whether it is limited.
        Returns the health dict plus "checked" (False when Telegram could not be asked)."""
        row = await self.store.accounts.get(account_id, owner_id=owner_id)
        if row is None:
            raise ValueError("Account not found")
        label = self.label_for_account(row)
        health = self.account_health(row)
        if health["status"] in UNUSABLE or health["status"] == "disabled":
            return {**health, "checked": False}
        if account_id not in self.clients:  # offline: try to bring it back, which also tells us why it will not come
            try:
                await self._connect_saved_account(row)
            except DEAD_SESSION_ERRORS as exc:
                await self._drop_dead_account(account_id, label, describe_error(exc), exc.__class__.__name__)
            except Exception as exc:
                return {**health, "note": f"could not connect: {describe_error(exc)}", "checked": False}
            health = self.account_health(row)
            if account_id not in self.clients:
                return {**health, "checked": health["status"] in UNUSABLE}
        client = self.clients[account_id]
        try:
            await self._request(client.get_me(), 30)
        except DEAD_SESSION_ERRORS as exc:
            await self._drop_dead_account(account_id, label, describe_error(exc), exc.__class__.__name__)
            return {**self.account_health(row), "checked": True}
        except Exception as exc:
            if _is_frozen(exc):
                await self._save_health(account_id, "banned", f"frozen by Telegram ({describe_error(exc)})"[:160])
                return {**self.account_health(await self.store.accounts.get(account_id)), "checked": True}
            return {**health, "note": f"could not check: {describe_error(exc)}", "checked": False}
        if not spam:
            return {**health, "checked": True}
        try:
            state, note = parse_spambot(await self._request(self._ask_spambot(client), 45))
        except Exception as exc:
            if _is_frozen(exc):
                await self._save_health(account_id, "banned", f"frozen by Telegram ({describe_error(exc)})"[:160])
                return {**self.account_health(await self.store.accounts.get(account_id)), "checked": True}
            return {**health, "note": f"@SpamBot did not answer: {describe_error(exc)}", "checked": False}
        if state == "unknown":
            return {**health, "note": f"@SpamBot said something unexpected: {note}", "checked": False}
        await self._save_health(account_id, "limited" if state == "limited" else "ok", note)
        return {**self.account_health(await self.store.accounts.get(account_id)), "checked": True}
