"""Access to the logs page without a secret in the URL.

The owner asks the bot for /logs and gets a link with a one-time key. Opening it shows a page
with a button; pressing the button trades the key for a browser session (an HttpOnly cookie).
The key works once and expires after a few minutes, and the URL that stays in the browser
history is `/logs` with nothing secret in it."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Callable

from telegram_manager.constants import LOGIN_KEY_TTL, SESSION_TTL


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class LogAccess:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()  # the web server runs in threads, the bot in the event loop
        self._keys: dict[str, float] = {}  # sha256(key) -> expires at
        self._sessions: dict[str, float] = {}  # sha256(session id) -> expires at

    def _purge(self) -> None:
        now = self._clock()
        for table in (self._keys, self._sessions):
            for key in [k for k, expires in table.items() if expires <= now]:
                del table[key]

    def new_login_key(self) -> str:
        key = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            self._keys[_digest(key)] = self._clock() + LOGIN_KEY_TTL
        return key

    def peek(self, key: str) -> bool:
        """Is the key valid? Does not use it up (a link preview or a scanner may open the page)."""
        with self._lock:
            self._purge()
            return _digest(key or "") in self._keys

    def redeem(self, key: str) -> str | None:
        """Use the key once. Returns a new session id, or None if the key is unknown or used."""
        with self._lock:
            self._purge()
            if self._keys.pop(_digest(key or ""), None) is None:
                return None
            session_id = secrets.token_urlsafe(32)
            self._sessions[_digest(session_id)] = self._clock() + SESSION_TTL
            return session_id

    def session_valid(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            self._purge()
            return _digest(session_id) in self._sessions

    def end_session(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(_digest(session_id), None)
