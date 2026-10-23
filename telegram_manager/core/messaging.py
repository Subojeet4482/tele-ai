"""Sending and blocking, on one account or on all of an admin's accounts."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Iterable

from telethon.tl.functions.contacts import BlockRequest

from telegram_manager.constants import BULK_DELAY_MAX, BULK_DELAY_MIN, OPEN_CHAT_MIN_DELAY
from telegram_manager.core import humanize
from telegram_manager.core.models import BulkResult
from telegram_manager.errors import describe_error
from telegram_manager.log_setup import mask_id

logger = logging.getLogger(__name__)


ProgressCallback = Callable[[BulkResult, str], Awaitable[None]]  # (result so far, account being worked on)


def _with_buttons(buttons: list[str] | None) -> dict:
    """Only pass `buttons` on when there are some, so plain sends keep their simple call."""
    return {"buttons": buttons} if buttons else {}


class MessagingMixin:
    # --- one account -----------------------------------------------------------------
    async def _deliver(self, client, entity, text: str, buttons: list[str] | None, account_id: str = "") -> int:
        """Send the message (if any), then press the named buttons of the bot's reply.
        Returns the id of the message that was sent (0 when only buttons were pressed).
        Like a person: the chat is opened and read first, then 'typing…' is shown for a while."""
        after_id = 0
        if text:
            await humanize.before_send(client, entity, text, account_id)
            sent = await client.send_message(entity, text, parse_mode=None)
            after_id = int(getattr(sent, "id", 0) or 0)
        if buttons:
            await self.click_buttons(client, entity, buttons, after_id)
        return after_id

    async def send_to_existing_dialog(
        self, account_id: str, target: str, text: str, owner_id: int | None = None, buttons: list[str] | None = None
    ) -> None:
        client = self._client_for(account_id, owner_id)
        entity = await self._existing_entity(account_id, client, target)
        sent_id = await self._deliver(client, entity, text, buttons, account_id)
        self._remember_sent(account_id, target, sent_id, owner_id)
        logger.info("Sent message via account %s to %s (existing chat)", account_id, mask_id(target))

    async def send_to_any(
        self, account_id: str, target: str, text: str, owner_id: int | None = None, buttons: list[str] | None = None
    ) -> None:
        client = self._client_for(account_id, owner_id)
        entity = await self._resolve_any_entity(client, target)
        sent_id = await self._deliver(client, entity, text, buttons, account_id)
        self._remember_sent(account_id, target, sent_id, owner_id)
        logger.info("Sent message via account %s to %s (chat opened if needed)", account_id, mask_id(target))

    async def block_on_account(self, account_id: str, target: str, owner_id: int | None = None) -> None:
        client = self._client_for(account_id, owner_id)
        entity = await self._resolve_any_entity(client, target)
        await client(BlockRequest(id=entity))
        logger.info("Blocked %s on account %s", mask_id(target), account_id)

    async def send_one_by_number(self, owner_id: int, number: int, target: str, text: str) -> None:
        account = await self.account_by_number(owner_id, number)
        if not account:
            raise ValueError("Account number was not found")
        await self.send_to_existing_dialog(str(account["id"]), target, text, owner_id)

    # --- all of one admin's accounts ---------------------------------------------------
    async def _for_each_account(
        self,
        owner_id: int,
        action: Callable[[str], Awaitable[None]],
        delay_seconds: int,
        what: str,
        on_progress: ProgressCallback | None = None,
        only_ids: Iterable[str] | None = None,
    ) -> BulkResult:
        """Run action(account_id) on every connected account of this admin.

        delay 0  = instant: every account acts at the same time.
        delay N  = one account acts, the bot waits N seconds, then the next account.

        Offline accounts are reported and skipped without waiting: the delay exists to space
        out real requests, not to pause on accounts that do nothing."""
        delay = max(BULK_DELAY_MIN, min(int(delay_seconds), BULK_DELAY_MAX))
        accounts = [a for a in await self.store.accounts.list(owner_id) if a.get("enabled", True)]
        if only_ids is not None:
            wanted = {str(item) for item in only_ids}
            accounts = [a for a in accounts if str(a["id"]) in wanted]
        result = BulkResult(total=len(accounts))

        async def report(current: str) -> None:
            if on_progress is not None:
                try:
                    await on_progress(result, current)
                except Exception:
                    logger.debug("Progress callback failed", exc_info=True)

        pace = "all at once" if not delay else f"{delay}s apart"
        logger.info("Starting %s on %d account(s), %s", what, len(accounts), pace)
        runnable: list[tuple[int, str, str]] = []
        for index, account in enumerate(accounts, start=1):
            account_id = str(account["id"])
            label = account.get("display_name", account_id)
            if self.is_online(account_id):
                runnable.append((index, account_id, label))
            else:
                logger.warning("%s: account %s/%s is not connected - skipped", what, index, len(accounts))
                result.errors.append(f"{label}: skipped, this account is not connected right now")
        await report("")

        async def run_one(index: int, account_id: str, label: str) -> None:
            await report(label)
            try:
                await action(account_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = describe_error(exc)
                logger.warning("%s: account %s/%s failed: %s", what, index, len(accounts), reason)
                result.errors.append(f"{label}: {reason}")
            else:
                result.ok += 1
                logger.info("%s: account %s/%s ok", what, index, len(accounts))
            await report("")

        if delay == 0:
            await asyncio.gather(*(run_one(*item) for item in runnable))
        else:
            for position, item in enumerate(runnable):
                if position:
                    await asyncio.sleep(humanize.jitter_delay(delay, item[1]) if humanize.enabled() else delay)
                await run_one(*item)
        logger.info("Finished %s: %d ok, %d problem(s)", what, result.ok, len(result.errors))
        return result

    async def send_all(
        self,
        owner_id: int,
        target: str,
        text: str,
        delay_seconds: int,
        on_progress: ProgressCallback | None = None,
        buttons: list[str] | None = None,
    ) -> BulkResult:
        """/all and /click: only chats that are already open in each account. `buttons` are pressed
        after the message (an empty text means: only press them)."""
        return await self._for_each_account(
            owner_id,
            lambda account_id: self.send_to_existing_dialog(account_id, target, text, owner_id, **_with_buttons(buttons)),
            delay_seconds,
            "/all",
            on_progress,
        )

    async def send_all_any(
        self,
        owner_id: int,
        target: str,
        text: str,
        delay_seconds: int,
        on_progress: ProgressCallback | None = None,
        buttons: list[str] | None = None,
    ) -> BulkResult:
        """/alll: opens the chat if needed. Accounts are always at least OPEN_CHAT_MIN_DELAY apart."""
        return await self._for_each_account(
            owner_id,
            lambda account_id: self.send_to_any(account_id, target, text, owner_id, **_with_buttons(buttons)),
            max(int(delay_seconds), OPEN_CHAT_MIN_DELAY),
            "/alll",
            on_progress,
        )

    async def block_all(
        self, owner_id: int, target: str, delay_seconds: int, on_progress: ProgressCallback | None = None
    ) -> BulkResult:
        return await self._for_each_account(
            owner_id,
            lambda account_id: self.block_on_account(account_id, target, owner_id),
            delay_seconds,
            "/allblock",
            on_progress,
        )

    async def send_selected(
        self,
        owner_id: int,
        account_ids: Iterable[str],
        target: str,
        text: str,
        delay_seconds: int,
        open_chat: bool = True,
        on_progress: ProgressCallback | None = None,
        buttons: list[str] | None = None,
    ) -> BulkResult:
        """/multi, the multi daily broadcast (and a timed /message): only the accounts the admin picked.
        open_chat=True opens the chat if needed, so accounts stay at least OPEN_CHAT_MIN_DELAY apart."""
        send = self.send_to_any if open_chat else self.send_to_existing_dialog
        delay = max(int(delay_seconds), OPEN_CHAT_MIN_DELAY) if open_chat else int(delay_seconds)
        return await self._for_each_account(
            owner_id,
            lambda account_id: send(account_id, target, text, owner_id, **_with_buttons(buttons)),
            delay,
            "/multi" if open_chat else "/message",
            on_progress,
            only_ids=account_ids,
        )
