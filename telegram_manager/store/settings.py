"""Per-admin settings: global forwarding switches and the account-number counter."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

FORWARD_KEYS = ("forward_user_messages", "forward_bot_messages", "forward_channel_messages")
_MIGRATED_KEYS = (*FORWARD_KEYS, "next_account_number")


class SettingsStore:
    def __init__(self, db, default_owner_id: int) -> None:
        self._docs = db.collection("telegram_admin_settings")
        self._legacy = db.collection("telegram_settings").document("global")
        self._start = db.collection("telegram_settings").document("start_screen")
        self._start_cache: dict | None = None
        self._default_owner_id = default_owner_id
        self._cache: dict[int, dict] = {}
        self._versions: dict[int, int] = {}

    def _load_sync(self, owner_id: int) -> dict:
        snapshot = self._docs.document(str(owner_id)).get()
        if snapshot.exists:
            return snapshot.to_dict() or {}
        if owner_id == self._default_owner_id:
            # First start after the multi-admin upgrade: bring the owner's old global settings along.
            legacy = self._legacy.get()
            if legacy.exists:
                data = {k: v for k, v in (legacy.to_dict() or {}).items() if k in _MIGRATED_KEYS}
                if data:
                    self._docs.document(str(owner_id)).set(data)
                return data
        return {}

    async def _data(self, owner_id: int) -> dict:
        cached = self._cache.get(owner_id)
        if cached is not None:
            return dict(cached)
        version = self._versions.get(owner_id, 0)
        data = await asyncio.to_thread(self._load_sync, owner_id)
        if version == self._versions.get(owner_id, 0):
            self._cache[owner_id] = data
        return dict(data)

    async def _write(self, owner_id: int, values: dict) -> None:
        self._versions[owner_id] = self._versions.get(owner_id, 0) + 1
        try:
            await asyncio.to_thread(self._docs.document(str(owner_id)).set, values, merge=True)
        finally:
            self._versions[owner_id] = self._versions.get(owner_id, 0) + 1
            self._cache.pop(owner_id, None)

    async def get_forwarding(self, owner_id: int) -> dict[str, bool]:
        data = await self._data(owner_id)
        return {key: bool(data.get(key, False)) for key in FORWARD_KEYS}

    async def set_forwarding(self, owner_id: int, name: str, value: bool) -> None:
        if name not in FORWARD_KEYS:
            raise ValueError(f"Unknown forwarding setting: {name}")
        await self._write(owner_id, {name: bool(value)})

    # --- proxies: the admin's pool (passwords / secrets are stored encrypted) -------------------------------------
    async def get_proxies(self, owner_id: int) -> list[dict]:
        value = (await self._data(owner_id)).get("proxies")
        return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    async def set_proxies(self, owner_id: int, proxies: list[dict]) -> None:
        await self._write(owner_id, {"proxies": proxies})

    async def get_proxy_source(self, owner_id: int) -> dict:
        """{"url", "use", "fetched_at", "last_count"}: where the URL proxies come from (missing = defaults)."""
        value = (await self._data(owner_id)).get("proxy_source")
        return dict(value) if isinstance(value, dict) else {}

    async def set_proxy_source(self, owner_id: int, **values) -> None:
        current = await self.get_proxy_source(owner_id)
        current.update(values)
        await self._write(owner_id, {"proxy_source": current})

    # --- temp mail (1secmail): the admin's current mailbox ------------------------------------------------------
    async def get_tempmail(self, owner_id: int) -> dict:
        """{"address", "created", "seen": [message ids already shown]} or {}."""
        value = (await self._data(owner_id)).get("tempmail")
        return dict(value) if isinstance(value, dict) else {}

    async def set_tempmail(self, owner_id: int, data: dict) -> None:
        await self._write(owner_id, {"tempmail": data})

    async def get_api_creds(self, owner_id: int) -> dict | None:
        """{"api_id": int, "api_hash_enc": str} saved with /setapi, or None."""
        data = await self._data(owner_id)
        if data.get("api_id") and data.get("api_hash_enc"):
            return {"api_id": int(data["api_id"]), "api_hash_enc": str(data["api_hash_enc"])}
        return None

    async def set_api_creds(self, owner_id: int, api_id: int | None, api_hash_enc: str | None) -> None:
        await self._write(owner_id, {"api_id": api_id, "api_hash_enc": api_hash_enc})

    # --- AI assistant (/ai): name, prompt, model, mode, keys (encrypted), panel mode ----------------
    async def get_ai(self, owner_id: int) -> dict:
        """The admin's AI settings (name, prompt, model, mode, encrypted keys). Missing fields = defaults."""
        value = (await self._data(owner_id)).get("ai")
        return dict(value) if isinstance(value, dict) else {}

    async def set_ai(self, owner_id: int, **values) -> None:
        current = await self.get_ai(owner_id)
        current.update(values)
        await self._write(owner_id, {"ai": current})

    async def get_ai_chats(self, owner_id: int) -> list[dict]:
        """The AI's running private chats (see ai/conversation.py)."""
        value = (await self._data(owner_id)).get("ai_chats")
        return [dict(item) for item in value] if isinstance(value, list) else []

    async def set_ai_chats(self, owner_id: int, chats: list[dict]) -> None:
        await self._write(owner_id, {"ai_chats": chats})

    async def get_counter(self, owner_id: int) -> int:
        try:
            return max(1, int((await self._data(owner_id)).get("next_account_number", 1)))
        except (TypeError, ValueError):
            return 1

    async def set_counter(self, owner_id: int, value: int) -> None:
        await self._write(owner_id, {"next_account_number": int(value)})

    # --- start screen: one for the whole bot, shown to everybody who sends /start ----------
    async def get_start_screen(self) -> dict:
        """{"text", "media" (file id), "media_type" (photo|animation|video), "photo" (old), "public_export"}; missing = default."""
        if self._start_cache is None:
            try:
                snapshot = await asyncio.to_thread(self._start.get)
                self._start_cache = (snapshot.to_dict() or {}) if snapshot.exists else {}
            except Exception as exc:
                logger.warning("Could not load the start screen settings: %s", exc)
                return {}
        return dict(self._start_cache)

    @staticmethod
    def start_media(cfg: dict) -> tuple[str | None, str]:
        """(file id, kind) of the start screen's picture / GIF / video. Old settings only have `photo`."""
        if cfg.get("media"):
            return cfg["media"], cfg.get("media_type") or "photo"
        if cfg.get("photo"):
            return cfg["photo"], "photo"
        return None, "photo"

    async def set_start_screen(self, **values) -> None:
        await asyncio.to_thread(self._start.set, values, merge=True)
        self._start_cache = None

    async def clear_start_screen(self) -> None:
        """Back to the built-in welcome text and no image (keeps the public-export switch)."""
        current = await self.get_start_screen()
        await asyncio.to_thread(self._start.set, {"public_export": bool(current.get("public_export", True))})
        self._start_cache = None
