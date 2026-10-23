"""Real health for uptime monitors: is the bot answering, and are the accounts connected?"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

STARTED_AT = time.time()


class HealthMonitor:
    def __init__(
        self,
        startup_grace: float = 180,
        bot_stale: float = 240,
        watchdog_stale: float = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._started = clock()
        self._grace = startup_grace
        self._bot_stale = bot_stale
        self._watchdog_stale = watchdog_stale
        self._bot_seen: float | None = None
        self.status_provider: Callable[[], dict[str, Any]] | None = None

    def beat(self) -> None:
        """Called every time the bot successfully talked to Telegram."""
        self._bot_seen = self._clock()

    def check(self) -> tuple[str, str]:
        """(status, reason). status is ok, degraded or down; only down should alarm a monitor."""
        now = self._clock()
        try:
            summary = self.status_provider() if self.status_provider else {}
        except Exception:
            logger.exception("Status provider failed")
            summary = {}
        if summary.get("instance_superseded"):
            return "ok", "standby"  # another copy runs the accounts on purpose
        if now - self._started < self._grace:
            return "ok", "starting"
        if self._bot_seen is None or now - self._bot_seen > self._bot_stale:
            return "down", "bot_not_responding"
        watchdog_age = summary.get("watchdog_age_seconds")
        if watchdog_age is not None and watchdog_age > self._watchdog_stale:
            return "down", "watchdog_stalled"
        saved = int(summary.get("accounts_saved") or 0)
        online = int(summary.get("accounts_online") or 0)
        if saved and online == 0:
            return "down", "no_accounts_online"
        if summary.get("accounts_offline"):
            return "degraded", "some_accounts_offline"
        return "ok", "ok"


async def heartbeat_loop(bot: Any, health: HealthMonitor, interval: int = 60) -> None:
    """Ask Telegram who we are once a minute; a success proves the bot connection is alive."""
    while True:
        try:
            await bot.get_me()
            health.beat()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Bot heartbeat failed: %s", str(exc) or exc.__class__.__name__)
        await asyncio.sleep(interval)
