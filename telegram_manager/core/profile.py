"""Change an account's name, bio and profile photo."""
from __future__ import annotations

import io
import logging

from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest

from telegram_manager.log_setup import mask_id

logger = logging.getLogger(__name__)

MAX_NAME = 64  # Telegram's limit for first and last name
MAX_BIO = 70  # Telegram's limit for the bio of a normal account


class ProfileMixin:
    async def update_profile(
        self,
        account_id: str,
        owner_id: int | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        about: str | None = None,
    ) -> str:
        """Change only what is given (None = leave as it is, "" = clear). Returns the new display name."""
        client = self._client_for(account_id, owner_id)
        fields = {"first_name": first_name, "last_name": last_name, "about": about}
        await client(UpdateProfileRequest(**{key: value for key, value in fields.items() if value is not None}))
        logger.info("Updated the profile of account %s", mask_id(account_id))
        if first_name is None and last_name is None:
            return str((self.accounts.get(account_id) or {}).get("display_name") or "")
        me = await client.get_me()
        name = " ".join(part for part in (me.first_name, me.last_name) if part) or me.username or str(me.id)
        await self.store.accounts.update(account_id, {"display_name": name})
        if account_id in self.accounts:
            self.accounts[account_id]["display_name"] = name
        return name

    async def set_profile_photo(self, account_id: str, owner_id: int | None, data: bytes) -> None:
        client = self._client_for(account_id, owner_id)
        buffer = io.BytesIO(data)
        buffer.name = "profile.jpg"
        uploaded = await client.upload_file(buffer)
        await client(UploadProfilePhotoRequest(file=uploaded))
        logger.info("Changed the profile photo of account %s", mask_id(account_id))
