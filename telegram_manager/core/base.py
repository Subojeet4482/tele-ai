"""Shared state and small helpers. The feature mixins in this package build on it."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from contextlib import suppress
from typing import Any, Awaitable
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.sessions import StringSession

from telegram_manager.constants import CONNECT_TIMEOUT, MESSAGE_LIMIT, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


def chunks(text: str, size: int = MESSAGE_LIMIT) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class ManagerBase:
    def __init__(
        self,
        store,
        secret_box,
        bot,
        owner_id: int,
        timezone: str = "UTC",
        bot_user_id: int | None = None,
        use_lease: bool = True,
    ) -> None:
        self.store = store
        self.secret_box = secret_box
        self.bot = bot
        self.owner_id = owner_id  # the bot owner; also the fallback for notifications
        self.bot_user_id = bot_user_id
        self.tz = ZoneInfo(timezone)
        self.use_lease = use_lease
        self.instance_id = uuid.uuid4().hex[:12]
        self._lease_task: asyncio.Task | None = None
        self._superseded = False
        self._removing: set[str] = set()  # accounts being logged out; the watchdog must leave them alone
        # account_id -> (built_at, {dialog id: entity}, {username: entity})
        self._dialog_cache: dict[str, tuple[float, dict[str, Any], dict[str, Any]]] = {}
        self.clients: dict[str, TelegramClient] = {}
        self.accounts: dict[str, dict[str, Any]] = {}
        # (admin chat id, message id) -> (account_id, peer_id, original_message_id). Also in Firestore.
        self._reply_map: OrderedDict[tuple[int, int], tuple[str, int, int]] = OrderedDict()
        self._reply_map_limit = 1000
        self._background: set[asyncio.Task] = set()
        self._daily: dict[tuple[int, int], tuple[Any, asyncio.Task]] = {}  # (admin id, slot) -> (job, task)
        self._watchdog_task: asyncio.Task | None = None
        self._watchdog_ticks = 0
        self._watchdog_last: float | None = None
        self._known_total = 0
        # account_id -> reason: sessions that can never work again; the watchdog stops retrying them.
        self._dead_accounts: dict[str, str] = {}
        self._dead_kinds: dict[str, str] = {}  # account_id -> the Telegram error class (tells a ban from a logout)
        self._offline_notified: set[str] = set()
        # owner id -> [{"account_id", "target", "msg_id", "at"}]: what the bot sent, so /unsend can take it back.
        self._sent_log: dict[int, list[dict[str, Any]]] = {}

    # --- telethon client helpers ---------------------------------------------------
    def _new_client(self, session_string: str, api_id: int, api_hash: str) -> TelegramClient:
        client = TelegramClient(
            StringSession(session_string),
            api_id,
            api_hash,
            # Telethon gives up after 5 failed reconnects by default and then stays offline
            # forever; -1 means "keep retrying".
            connection_retries=-1,
            retry_delay=3,
            auto_reconnect=True,
        )
        # Telethon parses Markdown by default; everything we send must go out exactly as typed.
        client.parse_mode = None
        return client

    async def _connect_client(self, client: TelegramClient, timeout: float = CONNECT_TIMEOUT) -> None:
        """client.connect() with infinite retries would wait forever while offline."""
        try:
            await asyncio.wait_for(client.connect(), timeout)
        except asyncio.TimeoutError as exc:
            await self._safe_disconnect(client)
            raise ConnectionError(f"Timed out after {int(timeout)}s while connecting to Telegram") from exc

    async def _safe_disconnect(self, client: TelegramClient) -> None:
        with suppress(Exception):
            await asyncio.wait_for(client.disconnect(), 15)

    @staticmethod
    async def _request(awaitable: Awaitable[Any], timeout: float = REQUEST_TIMEOUT) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except asyncio.TimeoutError as exc:
            raise ConnectionError(f"Telegram did not answer within {int(timeout)}s") from exc

    def is_online(self, account_id: str) -> bool:
        client = self.clients.get(account_id)
        return bool(client and client.is_connected())

    def _client_for(self, account_id: str, owner_id: int | None = None) -> TelegramClient:
        """The live client of an account, refusing accounts that belong to another admin."""
        account = self.accounts.get(account_id)
        if account is not None and owner_id is not None and self._owner_of(account) != owner_id:
            raise ValueError("Account not found")
        client = self.clients.get(account_id)
        if client is None:
            raise ValueError("This account is not connected")
        return client

    def _owner_of(self, account: dict[str, Any]) -> int:
        try:
            return int(account.get("owner_id") or self.owner_id)
        except (TypeError, ValueError):
            return self.owner_id

    # --- notifications / tasks ---------------------------------------------------------
    async def notify(self, owner_id: int | None, text: str) -> None:
        if self.bot is None:
            return
        chat_id = owner_id or self.owner_id
        try:
            for part in chunks(text):
                await self.bot.send_message(chat_id=chat_id, text=part)
        except Exception:
            logger.exception("Could not notify an admin")

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- labels ----------------------------------------------------------------------
    @staticmethod
    def label_for_account(account: dict[str, Any]) -> str:
        name = account.get("display_name") or "Unnamed account"
        username = account.get("username") or ""
        if username and name != f"@{username}":
            return f"{name} (@{username})"
        return name

    @staticmethod
    def label_for_entity(entity: Any) -> str:
        if entity is None:
            return ""
        username = getattr(entity, "username", None)
        title = getattr(entity, "title", None)  # channels/groups
        parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
        full_name = " ".join(part for part in parts if part)
        label = title or full_name or (f"@{username}" if username else "")
        if not label:
            return ""
        if username and label != f"@{username}":
            label = f"{label} (@{username})"
        return label
