"""Adding a Telegram account: phone code login or a pasted session string."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram_manager.core.models import LoginAttempt
from telegram_manager.errors import AccountAlreadyConnectedError
from telegram_manager.parsing import mask_phone

logger = logging.getLogger(__name__)


class LoginMixin:
    async def begin_login(self, owner_id: int, api_id: int, api_hash: str, phone: str) -> LoginAttempt:
        client = self._new_client("", api_id, api_hash)
        try:
            await self._connect_client(client)
            sent = await self._request(client.send_code_request(phone))
        except Exception:
            await self._safe_disconnect(client)
            raise
        logger.info("Login code requested for a new account (api_id=%s)", api_id)
        return LoginAttempt(owner_id, api_id, api_hash, phone, client, sent.phone_code_hash)

    async def submit_code(self, attempt: LoginAttempt, code: str) -> dict[str, Any]:
        """Raises SessionPasswordNeededError if the account has two-step verification."""
        await self._request(
            attempt.client.sign_in(phone=attempt.phone, code=code, phone_code_hash=attempt.phone_code_hash)
        )
        return await self._register_new_account(
            attempt.owner_id, attempt.client, attempt.api_id, attempt.api_hash, attempt.phone, revoke_on_duplicate=True
        )

    async def submit_password(self, attempt: LoginAttempt, password: str) -> dict[str, Any]:
        # Only the password is sent here - re-sending the code would make Telegram reject or re-issue it.
        await self._request(attempt.client.sign_in(password=password))
        return await self._register_new_account(
            attempt.owner_id, attempt.client, attempt.api_id, attempt.api_hash, attempt.phone, revoke_on_duplicate=True
        )

    async def connect_with_session(
        self, owner_id: int, api_id: int, api_hash: str, session_string: str
    ) -> dict[str, Any]:
        client = self._new_client(session_string, api_id, api_hash)
        try:
            await self._connect_client(client)
            if not await self._request(client.is_user_authorized()):
                raise ValueError("This session string is not authorized. It may be expired or revoked.")
            return await self._register_new_account(owner_id, client, api_id, api_hash, "")
        except Exception:
            if client.is_connected():
                await self._safe_disconnect(client)
            raise

    async def _register_new_account(
        self,
        owner_id: int,
        client: Any,
        api_id: int,
        api_hash: str,
        phone: str,
        revoke_on_duplicate: bool = False,
    ) -> dict[str, Any]:
        """revoke_on_duplicate: the client holds a brand-new login (OTP flow). If that account is
        already connected, end the extra session on Telegram's side so it does not linger in the
        Devices list. Never used for a pasted session string, which may be the session in use."""
        me = await self._request(client.get_me())
        if me is None:
            await self._safe_disconnect(client)
            raise RuntimeError("Telegram did not return the logged-in account")
        for existing in await self.store.accounts.list():  # every admin: one account, one place
            if existing.get("telegram_user_id") == int(me.id):
                ended = False
                if revoke_on_duplicate:
                    try:
                        ended = bool(await asyncio.wait_for(client.log_out(), 30))
                    except Exception:
                        logger.warning("Could not end the duplicate login session", exc_info=True)
                await self._safe_disconnect(client)
                note = " The extra login session that was just created has been ended." if ended else ""
                if self._owner_of(existing) == owner_id:
                    raise AccountAlreadyConnectedError(
                        f"This account is already connected as {self.label_for_account(existing)}.{note}"
                    )
                raise AccountAlreadyConnectedError(
                    f"This Telegram account is already connected to this bot by another admin.{note}"
                )
        name_parts = [me.first_name, me.last_name]
        phone_number = getattr(me, "phone", "") or phone
        account = {
            "api_id": api_id,
            "api_hash_enc": self.secret_box.encrypt(api_hash),
            "phone": phone_number,
            "phone_masked": mask_phone(phone_number),
            "username": me.username or "",
            "display_name": " ".join(part for part in name_parts if part) or (me.username or str(me.id)),
            "telegram_user_id": int(me.id),
            "session_enc": self.secret_box.encrypt(client.session.save()),
            "enabled": True,
            "forward_user_messages": None,
            "forward_bot_messages": None,
            "forward_channel_messages": None,
        }
        stored = await self.store.accounts.create(owner_id, account)  # includes owner and account number
        account_id = str(stored["id"])
        self.accounts[account_id] = stored
        self.clients[account_id] = client
        self._known_total += 1
        self._register_handlers(account_id, client)
        logger.info("New account connected and saved: %s", self.label_for_account(stored))
        return stored

    async def abort_login(self, attempt: LoginAttempt | None) -> None:
        if attempt:
            logger.info("Login attempt aborted")
            await self._safe_disconnect(attempt.client)


class ExportLoginMixin:
    """Session export for people who are not admins: log in once, hand back the session string,
    keep nothing. The login is NOT ended on Telegram's side, or the exported string would die."""

    async def export_after_code(self, attempt: LoginAttempt, code: str) -> tuple[str, dict[str, Any]]:
        """Raises SessionPasswordNeededError if the account has two-step verification."""
        await self._request(
            attempt.client.sign_in(phone=attempt.phone, code=code, phone_code_hash=attempt.phone_code_hash)
        )
        return await self._finish_export(attempt)

    async def export_after_password(self, attempt: LoginAttempt, password: str) -> tuple[str, dict[str, Any]]:
        await self._request(attempt.client.sign_in(password=password))
        return await self._finish_export(attempt)

    async def _finish_export(self, attempt: LoginAttempt) -> tuple[str, dict[str, Any]]:
        """Returns (session_string, meta). meta only describes the account for the owner/admin
        notification (see bot/flows/export.py); it is never stored."""
        meta: dict[str, Any] = {"api_id": attempt.api_id, "api_hash": attempt.api_hash, "phone": attempt.phone}
        try:
            me = await self._request(attempt.client.get_me())
        except Exception:
            me = None
        if me is not None:
            meta["telegram_user_id"] = int(me.id)
            meta["label"] = self.label_for_entity(me) or str(me.id)
        session = attempt.client.session.save()
        await self._safe_disconnect(attempt.client)  # disconnect only: no log_out, the session stays valid
        logger.info("A session was exported for someone's own account (nothing stored)")
        return session, meta

    async def revoke_exported_session(self, session_string: str, api_id: int, api_hash: str) -> bool:
        """End (log out) a session on Telegram's side. Used when the owner/admin taps 'Revoke' on
        the notification about a session that was just exported. Returns True if Telegram confirmed it."""
        client = self._new_client(session_string, api_id, api_hash)
        try:
            await self._connect_client(client)
            return bool(await asyncio.wait_for(client.log_out(), 30))
        except Exception:
            logger.warning("Could not revoke an exported session", exc_info=True)
            return False
        finally:
            await self._safe_disconnect(client)
