from __future__ import annotations

import time
from typing import Any


class StatusMixin:
    def status_summary(self) -> dict[str, Any]:
        """Small dict for /status and /health. Reads in-memory state only."""
        online = [aid for aid in self.clients if self.is_online(aid)]
        offline = [
            self.label_for_account(self.accounts.get(aid, {"display_name": aid}))
            for aid in self.clients
            if aid not in online
        ]
        watchdog_age = None
        if self._watchdog_task is not None and not self._watchdog_task.done() and self._watchdog_last:
            watchdog_age = time.time() - self._watchdog_last
        return {
            "accounts_saved": self._known_total,
            "accounts_online": len(online),
            "accounts_offline": offline,
            "accounts_dead_sessions": len(self._dead_accounts),
            "daily_jobs_scheduled": len(self._daily),
            "instance_superseded": self._superseded,
            "watchdog_age_seconds": watchdog_age,
        }
