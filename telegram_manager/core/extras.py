"""Extra account tools: saved API id/hash, session-file import, /unsend, /delchat and /whois."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Iterable

from telegram_manager.core.models import BulkResult
from telegram_manager.errors import AccountAlreadyConnectedError, describe_error
from telegram_manager.parsing import SessionLine

logger = logging.getLogger(__name__)

SENT_LOG_KEEP_SECONDS = 24 * 3600  # /unsend can only take back what was sent in the last day
SENT_LOG_MAX = 5000
UNSEND_MAX_MINUTES = 24 * 60
IMPORT_GAP_SECONDS = 1.0  # pause between two imported accounts


class ExtrasMixin:
    # --- saved API id / hash (/setapi) -----------------------------------------------------
    async def save_api(self, owner_id: int, api_id: int, api_hash: str) -> None:
        await self.store.settings.set_api_creds(owner_id, int(api_id), self.secret_box.encrypt(api_hash))

    async def clear_api(self, owner_id: int) -> None:
        await self.store.settings.set_api_creds(owner_id, None, None)

    async def saved_api(self, owner_id: int) -> tuple[int, str] | None:
        creds = await self.store.settings.get_api_creds(owner_id)
        if not creds:
            return None
        try:
            return creds["api_id"], self.secret_box.decrypt(creds["api_hash_enc"])
        except Exception:
            logger.warning("The saved API hash could not be decrypted", exc_info=True)
            return None

    # --- import many sessions from a .txt ---------------------------------------------------
    async def import_sessions(
        self, owner_id: int, lines: Iterable[SessionLine], default_api: tuple[int, str] | None
    ) -> dict[str, Any]:
        """Add every session of the list. Returns {"added": [labels], "failed": [reasons], "skipped": n}."""
        report: dict[str, Any] = {"added": [], "failed": [], "duplicate": 0}
        for index, item in enumerate(lines, start=1):
            api = (item.api_id, item.api_hash) if item.api_id and item.api_hash else default_api
            if api is None:
                report["failed"].append(f"#{index}: no API ID/hash (use /setapi first, or write api_id:api_hash:session)")
                continue
            try:
                account = await self.connect_with_session(owner_id, api[0], api[1], item.session)
            except AccountAlreadyConnectedError:
                report["duplicate"] += 1
            except Exception as exc:
                report["failed"].append(f"#{index}: {describe_error(exc)}")
            else:
                report["added"].append(self.label_for_account(account))
            await asyncio.sleep(IMPORT_GAP_SECONDS)
        return report

    # --- remembering what was sent ----------------------------------------------------------
    def _remember_sent(self, account_id: str, target: str, msg_id: int, owner_id: int | None = None) -> None:
        if not msg_id:
            return
        if owner_id is None:
            owner_id = self._owner_of(self.accounts.get(account_id) or {})
        now = time.time()
        log = [e for e in self._sent_log.get(owner_id, []) if now - e["at"] < SENT_LOG_KEEP_SECONDS]
        log.append({"account_id": str(account_id), "target": target, "msg_id": int(msg_id), "at": now})
        self._sent_log[owner_id] = log[-SENT_LOG_MAX:]

    def sent_count(self, owner_id: int, minutes: int, account_ids: Iterable[str] | None = None) -> int:
        return len(self._recent_sent(owner_id, minutes, account_ids))

    def _recent_sent(self, owner_id: int, minutes: int, account_ids: Iterable[str] | None = None) -> list[dict]:
        cutoff = time.time() - minutes * 60
        wanted = None if account_ids is None else {str(a) for a in account_ids}
        return [
            e for e in self._sent_log.get(owner_id, [])
            if e["at"] >= cutoff and (wanted is None or e["account_id"] in wanted)
        ]

    async def unsend_recent(
        self, owner_id: int, minutes: int, account_ids: Iterable[str] | None = None, delay_seconds: int = 1
    ) -> tuple[BulkResult, int]:
        """Delete (for everybody) the messages this bot sent in the last `minutes` minutes.
        Returns (per-account result, number of messages deleted)."""
        if not 1 <= minutes <= UNSEND_MAX_MINUTES:
            raise ValueError(f"Choose between 1 and {UNSEND_MAX_MINUTES} minutes.")
        entries = self._recent_sent(owner_id, minutes, account_ids)
        by_account: dict[str, dict[str, list[int]]] = {}
        for entry in entries:
            by_account.setdefault(entry["account_id"], {}).setdefault(entry["target"], []).append(entry["msg_id"])
        deleted = 0

        async def action(account_id: str) -> None:
            nonlocal deleted
            client = self._client_for(account_id, owner_id)
            for target, ids in by_account.get(account_id, {}).items():
                entity = await self._resolve_any_entity(client, target)
                await client.delete_messages(entity, ids, revoke=True)
                deleted += len(ids)
                gone = {(account_id, target, i) for i in ids}
                self._sent_log[owner_id] = [
                    e for e in self._sent_log.get(owner_id, [])
                    if (e["account_id"], e["target"], e["msg_id"]) not in gone
                ]

        result = await self._for_each_account(
            owner_id, action, delay_seconds, "/unsend", only_ids=list(by_account) if by_account else []
        )
        return result, deleted

    # --- delete a chat (only on the accounts' side) ----------------------------------------
    async def delete_chat_on_account(self, account_id: str, target: str, owner_id: int | None = None) -> None:
        client = self._client_for(account_id, owner_id)
        entity = await self._resolve_any_entity(client, target)
        await client.delete_dialog(entity)  # revoke=False: the other person keeps their copy
        self._dialog_cache.pop(account_id, None)
        logger.info("Deleted a chat on account %s", account_id)

    async def delete_chat_selected(
        self, owner_id: int, account_ids: Iterable[str], target: str, delay_seconds: int = 1
    ) -> BulkResult:
        return await self._for_each_account(
            owner_id,
            lambda account_id: self.delete_chat_on_account(account_id, target, owner_id),
            delay_seconds,
            "/delchat",
            only_ids=account_ids,
        )

    # --- who is this? -------------------------------------------------------------------------
    async def lookup_user(self, owner_id: int, target: str) -> dict[str, Any]:
        """Id, @username and name of a user/chat, asked through the admin's first online account."""
        for account in await self.store.accounts.list(owner_id):
            account_id = str(account["id"])
            if not self.is_online(account_id):
                continue
            entity = await self._resolve_any_entity(self.clients[account_id], target)
            return {
                "id": getattr(entity, "id", None),
                "username": getattr(entity, "username", None) or "",
                "name": self.label_for_entity(entity) or "",
                "is_bot": bool(getattr(entity, "bot", False)),
                "kind": type(entity).__name__,
                "via": account.get("number"),
            }
        raise ValueError("None of your accounts is connected right now.")

    # --- reading (for the AI) -----------------------------------------------------------------
    async def read_chat(self, owner_id: int, account_id: str, target: str, limit: int = 10) -> list[dict[str, Any]]:
        """The latest messages of one chat of one account, oldest first. Texts are shortened."""
        client = self._client_for(account_id, owner_id)
        entity = await self._resolve_any_entity(client, target)
        messages = await client.get_messages(entity, limit=max(1, min(int(limit), 30)))
        rows = []
        for message in reversed(list(messages)):
            when = getattr(message, "date", None)
            rows.append({
                "from": "me" if getattr(message, "out", False) else "them",
                "text": (getattr(message, "text", "") or "[media]")[:400],
                "time": when.astimezone(self.tz).strftime("%d %b %H:%M") if when else "",
            })
        return rows

    async def unread_chats(self, owner_id: int, account_id: str, limit: int = 15) -> list[dict[str, Any]]:
        client = self._client_for(account_id, owner_id)
        rows: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=150):
            if not getattr(dialog, "unread_count", 0):
                continue
            username = getattr(dialog.entity, "username", None)
            last = getattr(getattr(dialog, "message", None), "text", "") or "[media]"
            rows.append({
                "name": dialog.name or str(dialog.id),
                "chat": f"@{username}" if username else str(dialog.id),
                "unread": int(dialog.unread_count),
                "last": last[:200],
            })
            if len(rows) >= limit:
                break
        return rows

    async def find_chats(self, owner_id: int, account_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        client = self._client_for(account_id, owner_id)
        wanted = (query or "").strip().lower().lstrip("@")
        rows: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=300):
            username = (getattr(dialog.entity, "username", None) or "")
            if wanted and wanted not in (dialog.name or "").lower() and wanted not in username.lower():
                continue
            rows.append({"name": dialog.name or str(dialog.id), "chat": f"@{username}" if username else str(dialog.id)})
            if len(rows) >= limit:
                break
        return rows

    async def block_selected(self, owner_id: int, account_ids: Iterable[str], target: str, delay_seconds: int = 1) -> BulkResult:
        return await self._for_each_account(
            owner_id,
            lambda account_id: self.block_on_account(account_id, target, owner_id),
            delay_seconds,
            "block",
            only_ids=account_ids,
        )

    # --- joining a group / channel -------------------------------------------------------------
    async def join_chat_on_account(self, account_id: str, link: str, owner_id: int | None = None) -> None:
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import ImportChatInviteRequest

        client = self._client_for(account_id, owner_id)
        invite = re.search(r"(?:(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)|tg://join\?invite=)([\w-]+)", link or "", re.I)
        if invite:
            await client(ImportChatInviteRequest(invite.group(1)))
            return
        name = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", (link or "").strip()).lstrip("@").split("/")[0]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name):
            raise ValueError("Send an invite link (t.me/+...) or a public @username.")
        await client(JoinChannelRequest(await client.get_entity(name)))

    async def join_selected(self, owner_id: int, account_ids: Iterable[str], link: str, delay_seconds: int = 3) -> BulkResult:
        return await self._for_each_account(
            owner_id, lambda account_id: self.join_chat_on_account(account_id, link, owner_id), delay_seconds, "join", only_ids=account_ids
        )
