from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretDecryptError(ValueError):
    """Stored data could not be decrypted with the configured key."""


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            # InvalidToken has an empty message, which used to show up as a blank
            # reason in the admin alert.
            raise SecretDecryptError(
                "Could not decrypt the saved session: SESSION_ENCRYPTION_KEY does not match the key "
                "this account was saved with (or the stored data is corrupted). Put the original key back, "
                "or log the account out and connect it again."
            ) from exc
