from __future__ import annotations

import asyncio
import logging

from telegram_manager.constants import SELF_PING_MIN_INTERVAL

logger = logging.getLogger(__name__)


async def self_ping_loop(url: str, interval_seconds: int) -> None:
    """Fetch our own public URL now and then so the host does not put the app to sleep.
    Never raises: any failure is logged and the loop keeps going."""
    import httpx

    interval = max(SELF_PING_MIN_INTERVAL, int(interval_seconds))
    logger.info("Self-ping loop started for %s every %ss", url, interval)
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            await asyncio.sleep(interval)
            try:
                response = await client.get(url)
                level = logging.INFO if response.status_code < 400 else logging.WARNING
                logger.log(level, "Self-ping to %s returned %s", url, response.status_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Self-ping to %s failed: %s", url, str(exc) or exc.__class__.__name__)
