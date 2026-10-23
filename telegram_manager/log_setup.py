from __future__ import annotations

import hashlib
import logging
import re
import secrets
import sys
from collections import deque
from dataclasses import dataclass

MAX_LOG_ENTRIES = 3000

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

# Anything that looks like a secret is masked before it reaches stdout or the
# in-memory buffer that the /logs page serves.
_REDACTIONS = (
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    (re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}"), "<bot-token>"),
    (re.compile(r"(?i)(token=)[^&\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(?<![\w.])\+\d{7,15}(?!\d)"), "+<phone>"),
)


# Chat ids, sender ids and user ids never go into the logs as-is. A per-run random
# salt turns them into short stable tags (#a1b2c3): the same id always gets the same
# tag inside one run, so you can still follow a conversation, but the tag cannot be
# reversed into the real id.
_MASK_SALT = secrets.token_hex(8)


def mask_id(value: object) -> str:
    if value is None or value == "":
        return "-"
    digest = hashlib.sha256(f"{_MASK_SALT}:{value}".encode()).hexdigest()[:6]
    return f"#{digest}"


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


@dataclass(frozen=True)
class LogEntry:
    created: float
    levelno: int
    text: str


class RingBufferHandler(logging.Handler):
    """Keeps the most recent log lines in memory so they can be shown at /logs."""

    def __init__(self, capacity: int = MAX_LOG_ENTRIES) -> None:
        super().__init__()
        self._entries: deque[LogEntry] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001 - logging must never break the app
            self.handleError(record)
            return
        self._entries.append(LogEntry(record.created, record.levelno, text))

    def snapshot(self) -> list[LogEntry]:
        self.acquire()
        try:
            return list(self._entries)
        finally:
            self.release()


def setup_logging(level_name: str = "INFO") -> RingBufferHandler:
    """Send logs to stdout (Render's log tab) and to an in-memory buffer (/logs)."""
    level = _LEVELS.get((level_name or "INFO").upper(), logging.INFO)
    formatter = RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    buffer = RingBufferHandler()
    buffer.setFormatter(formatter)

    root.setLevel(level)
    root.addHandler(stream)
    root.addHandler(buffer)

    # httpx logs every Telegram API call (one per long-poll) and the URL contains the bot token.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
    return buffer
