"""Routing an admin's reply or reaction on a forwarded message back to the original chat."""
from __future__ import annotations

import logging
from typing import Any

from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

from telegram_manager.log_setup import mask_id

logger = logging.getLogger(__name__)


class RepliesMixin:
    def _remember_reply_target(
        self, chat_id: int, admin_message_id: int, account_id: str, peer_id: int, original_message_id: int
    ) -> None:
        key = (chat_id, admin_message_id)
        self._reply_map[key] = (account_id, peer_id, original_message_id)
        while len(self._reply_map) > self._reply_map_limit:
            self._reply_map.popitem(last=False)
        self._spawn(self._persist_reply_target(chat_id, admin_message_id, account_id, peer_id, original_message_id))

    async def _persist_reply_target(
        self, chat_id: int, admin_message_id: int, account_id: str, peer_id: int, original_message_id: int
    ) -> None:
        try:
            await self.store.replies.save(chat_id, admin_message_id, account_id, peer_id, original_message_id)
        except Exception:
            logger.warning("Could not persist reply target %s", admin_message_id, exc_info=True)

    async def _lookup_reply_target(
        self, owner_id: int, chat_id: int, admin_message_id: int
    ) -> tuple[Any, Any, Any] | None:
        """The forward's source, but only if the account belongs to the admin who is replying."""
        key = (chat_id, admin_message_id)
        target = self._reply_map.get(key)
        if not target:
            try:
                target = await self.store.replies.get(chat_id, admin_message_id)
            except Exception:
                logger.warning("Could not load reply target %s", admin_message_id, exc_info=True)
                return None
            if target:
                self._reply_map[key] = target
        if not target:
            return None
        account = self.accounts.get(target[0])
        if account is not None and self._owner_of(account) != owner_id:
            return None
        return target

    async def reply_via_forward(self, owner_id: int, chat_id: int, admin_message_id: int, text: str) -> bool:
        """If admin_message_id is a message we forwarded, send text back to that chat through
        the account it came from, as a quoted reply. False if it isn't a known forward."""
        target = await self._lookup_reply_target(owner_id, chat_id, admin_message_id)
        if not target:
            return False
        account_id, peer_id, original_message_id = target
        client = self._client_for(account_id, owner_id)
        entity = await self._known_entity(account_id, client, peer_id)
        await client.send_message(entity, text, reply_to=original_message_id, parse_mode=None)
        logger.info("Reply sent through account %s to chat %s", account_id, mask_id(peer_id))
        return True

    async def react_via_forward(self, owner_id: int, chat_id: int, admin_message_id: int, emoji: str) -> bool:
        """Place the same emoji reaction on the original message a forward came from."""
        target = await self._lookup_reply_target(owner_id, chat_id, admin_message_id)
        if not target:
            return False
        account_id, peer_id, original_message_id = target
        client = self._client_for(account_id, owner_id)
        entity = await self._known_entity(account_id, client, peer_id)
        await client(
            SendReactionRequest(peer=entity, msg_id=original_message_id, reaction=[ReactionEmoji(emoticon=emoji)])
        )
        logger.info("Reaction %s mirrored through account %s to chat %s", emoji, account_id, mask_id(peer_id))
        return True
