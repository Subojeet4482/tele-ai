"""Human-like behaviour: read ("seen"), a typing pause that fits the text, per-account speed.

Every account gets its own stable speed (a fast typist stays fast, a slow one stays slow), so
a bulk run never looks like N identical robots. Set HUMAN_MODE=off to switch it all off."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from typing import Any

logger = logging.getLogger(__name__)

TYPING_CPS = 6.0  # characters per second for a speed-1.0 typist
TYPING_MIN, TYPING_MAX = 1.0, 14.0
READ_MIN, READ_MAX = 0.6, 3.0


def enabled() -> bool:
    return os.getenv("HUMAN_MODE", "on").strip().lower() not in {"off", "0", "false", "no"}


def account_speed(account_id: str) -> float:
    """A stable factor in 0.75 .. 1.45 for this account (bigger = slower)."""
    digest = hashlib.sha256(str(account_id).encode()).digest()
    return 0.75 + (digest[0] / 255) * 0.70


def typing_seconds(text: str, speed: float = 1.0, rng: random.Random | None = None) -> float:
    rng = rng or random
    base = len(text or "") / TYPING_CPS * speed
    return max(TYPING_MIN, min(TYPING_MAX, base * rng.uniform(0.8, 1.25)))


def read_seconds(speed: float = 1.0, rng: random.Random | None = None) -> float:
    rng = rng or random
    return max(READ_MIN, min(READ_MAX, rng.uniform(0.8, 2.2) * speed))


def jitter_delay(delay: float, account_id: str = "", rng: random.Random | None = None) -> float:
    """Spread a fixed gap (30s) into a natural one (about 21 to 52s, per account)."""
    if delay <= 0:
        return 0.0
    rng = rng or random
    return delay * rng.uniform(0.7, 1.2) * (0.9 + (account_speed(account_id) - 0.75) * 0.5)


async def seen(client: Any, entity: Any, speed: float = 1.0) -> None:
    """Open the chat and mark it read, then 'look at it' for a moment."""
    try:
        await client.send_read_acknowledge(entity)
    except Exception:
        logger.debug("mark-read skipped", exc_info=True)
    await asyncio.sleep(read_seconds(speed))


async def type_then(client: Any, entity: Any, text: str, speed: float = 1.0) -> None:
    """Show 'typing…' for as long as a person would need for `text`."""
    seconds = typing_seconds(text, speed)
    try:
        async with client.action(entity, "typing"):
            await asyncio.sleep(seconds)
    except Exception:
        logger.debug("typing action skipped", exc_info=True)
        await asyncio.sleep(seconds)


async def before_send(client: Any, entity: Any, text: str, account_id: str = "") -> None:
    """Everything a person does before pressing send: look at the chat, then type."""
    if not enabled():
        return
    speed = account_speed(account_id)
    await seen(client, entity, speed)
    if text:
        await type_then(client, entity, text, speed)
