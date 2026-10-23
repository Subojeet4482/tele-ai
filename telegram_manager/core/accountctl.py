"""Full control of one account from its settings screen: transfer, username, session export."""
from __future__ import annotations

import logging
import re

from telethon.tl.functions.account import UpdateUsernameRequest

from telegram_manager.constants import USERNAME_MAX, USERNAME_MIN
from telegram_manager.log_setup import mask_id

logger = logging.getLogger(__name__)

_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def parse_username(raw: str) -> str:
    """'@name' / 'name' -> 'name'. '-' (or empty) -> '' which removes the username."""
    value = (raw or "").strip().lstrip("@")
    if value in ("", "-"):
        return ""
    if not USERNAME_MIN <= len(value) <= USERNAME_MAX:
        raise ValueError(f"A username has {USERNAME_MIN}–{USERNAME_MAX} characters.")
    if not _USERNAME.fullmatch(value) or value.endswith("_") or "__" in value:
        raise ValueError("Use letters, digits and _ only; start with a letter, no double or trailing _.")
    return value


_USERNAME_ERRORS = {
    "UsernameOccupiedError": "That username is already taken.",
    "UsernameInvalidError": "Telegram says that username is not valid.",
    "UsernameNotModifiedError": "The account already has that username.",
    "UsernamePurchaseAvailableError": "That username is reserved for sale on Fragment.",
}


class AccountControlMixin:
    async def set_username(self, account_id: str, owner_id: int | None, username: str) -> str:
        """Set the account's @username; "" removes it. Returns the new username ("" if none)."""
        client = self._client_for(account_id, owner_id)
        try:
            await client(UpdateUsernameRequest(username))
        except Exception as exc:
            friendly = _USERNAME_ERRORS.get(exc.__class__.__name__)
            if friendly:
                raise ValueError(friendly) from exc
            raise
        await self.store.accounts.update(account_id, {"username": username})
        if account_id in self.accounts:
            self.accounts[account_id]["username"] = username
        logger.info("Changed the username of account %s", mask_id(account_id))
        return username

    async def export_session(self, account_id: str, owner_id: int) -> str:
        """The account's Telethon session string (decrypted). Only for its owner. Never logged."""
        row = await self.store.accounts.get(account_id, owner_id=owner_id)
        if row is None:
            raise ValueError("Account not found")
        client = self.clients.get(account_id)
        if client is not None and client.is_connected():
            try:
                return client.session.save()
            except Exception:
                logger.debug("Live session read failed; using the stored one")
        return self.secret_box.decrypt(row["session_enc"])
        # (the stored copy is what the bot reconnects with, so both are the same login)

    async def transfer_account(self, account_id: str, from_owner: int, to_owner: int) -> dict:
        """Move one account between the owner and an admin. It keeps running; only who sees and
        controls it changes. Replies to old forwards stop working for the previous holder."""
        row = await self.store.accounts.get(account_id, owner_id=from_owner)
        if row is None:
            raise ValueError("Account not found")
        if from_owner == to_owner:
            raise ValueError("That account is already there.")
        moved = await self.store.accounts.transfer(account_id, to_owner)
        if account_id in self.accounts:
            self.accounts[account_id].update({"owner_id": to_owner, "number": moved["number"]})
        await self.detach_account_from_jobs(from_owner, account_id)
        logger.info("Account %s moved to another admin", mask_id(account_id))
        return moved
