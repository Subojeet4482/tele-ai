"""Join groups / channels the way a person would, check that it really worked, and handle bots
that say 'join our channel first, then press Verify'."""
from __future__ import annotations

import asyncio
import logging
import random
import re

from telegram_manager.core import humanize
from telegram_manager.core.joinparse import extract_join_targets, pick_verify_button

logger = logging.getLogger(__name__)


class JoinerMixin:
    async def join_with_status(self, account_id: str, link: str, owner_id: int | None = None) -> str:
        """Join and confirm. Returns: 'joined', 'already in', 'request sent (admin must approve)',
        or raises with the reason (FloodWait says how long to wait)."""
        from telethon.tl.functions.channels import GetParticipantRequest

        client = self._client_for(account_id, owner_id)
        if humanize.enabled():
            await asyncio.sleep(random.uniform(1.0, 3.5) * humanize.account_speed(account_id))
        try:
            await self.join_chat_on_account(account_id, link, owner_id)
            status = "joined"
        except Exception as exc:
            kind = exc.__class__.__name__
            if kind == "UserAlreadyParticipantError":
                return "already in"
            if kind == "InviteRequestSentError":
                return "request sent (admin must approve)"
            if kind == "FloodWaitError":
                raise ValueError(f"Telegram says wait {getattr(exc, 'seconds', '?')}s before joining again") from exc
            raise
        try:  # prove it: ask Telegram whether this account is now a member
            name = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", (link or "").strip()).lstrip("@").split("/")[0]
            if not name.startswith(("+", "joinchat", "tg:")):
                entity = await client.get_entity(name)
                await client(GetParticipantRequest(entity, await client.get_me()))
        except Exception as exc:
            if exc.__class__.__name__ in {"UserNotParticipantError", "ChannelPrivateError"}:
                return "not a member yet (join did not stick)"
            logger.debug("membership check skipped", exc_info=True)
        return status

    async def force_join_flow(self, account_id: str, bot: str, owner_id: int | None = None) -> list[str]:
        """The bot asked you to join first: read its last message, join every channel it names (with
        pauses), then press the 'I joined / Verify' button. Returns a step-by-step report."""
        client = self._client_for(account_id, owner_id)
        entity = await self._resolve_any_entity(client, bot)
        messages = await client.get_messages(entity, limit=5)
        texts: list[str] = []
        buttons: list[str] = []
        for message in messages:
            if getattr(message, "out", False):
                continue
            texts.append(message.message or "")
            for row in message.buttons or []:
                for button in row:
                    buttons.append(button.text or "")
                    if getattr(button, "url", None):
                        texts.append(button.url)
        targets = extract_join_targets(texts)
        report: list[str] = []
        for target in targets:
            try:
                report.append(f"{target}: {await self.join_with_status(account_id, target, owner_id)}")
            except Exception as exc:
                report.append(f"{target}: failed - {exc}")
            await asyncio.sleep(random.uniform(2.0, 5.0) if humanize.enabled() else 0)
        verify = pick_verify_button(buttons)
        if verify:
            try:
                await self.click_buttons(client, entity, [verify], 0)
                report.append(f"pressed '{verify}'")
            except Exception as exc:
                report.append(f"verify button failed - {exc}")
        else:
            report.append("no verify button found - check the bot chat")
        return report

    async def force_join_selected(self, owner_id: int, account_ids, bot: str, delay_seconds: int = 5):
        reports: dict[str, list[str]] = {}

        async def one(account_id: str) -> None:
            reports[account_id] = await self.force_join_flow(account_id, bot, owner_id)

        result = await self._for_each_account(owner_id, one, delay_seconds, "force-join", only_ids=account_ids)
        return result, reports
