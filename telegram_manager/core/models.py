from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any


@dataclass
class LoginAttempt:
    owner_id: int
    api_id: int
    api_hash: str
    phone: str
    client: Any
    phone_code_hash: str


@dataclass
class BulkResult:
    """Outcome of an action run on all of one admin's accounts."""

    total: int = 0
    ok: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return len(self.errors)

    @property
    def done(self) -> int:
        return self.ok + self.failed


@dataclass
class DailyJob:
    target: str
    text: str
    delay_seconds: int
    start_time: dtime
    times_per_day: int
    account_ids: list[str] | None = None  # None = every connected account (also ones added later)
    buttons: list[str] = field(default_factory=list)  # inline buttons to press after the message
    drift_seconds: int = 0  # the start time moves this many seconds later every day (0 = same time daily)
    anchor_date: str | None = None  # ISO date of the first day (day 0) — set when the job starts
    drift_max_seconds: int = 0  # > drift_seconds: the daily shift is a random value in [drift_seconds, drift_max_seconds]

    def shift_total(self, days_since: int) -> int:
        """Total seconds the start time has moved after `days_since` days. A fixed shift is simple
        multiplication; a random one is the sum of one stable random value per day (the same days
        always give the same answer, so a restart never changes today's time)."""
        if days_since <= 0:
            return 0
        low, high = int(self.drift_seconds), max(int(self.drift_seconds), int(self.drift_max_seconds or 0))
        if high == low:
            return low * days_since
        import random

        seed = f"{self.anchor_date}|{self.target}|{self.start_time.isoformat()}"
        return sum(random.Random(f"{seed}|{day}").randint(low, high) for day in range(1, days_since + 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "text": self.text,
            "delay_seconds": self.delay_seconds,
            "start_time": self.start_time.strftime("%H:%M:%S" if self.start_time.second else "%H:%M"),
            "times_per_day": self.times_per_day,
            "account_ids": None if self.account_ids is None else [str(item) for item in self.account_ids],
            "buttons": list(self.buttons),
            "drift_seconds": int(self.drift_seconds),
            "anchor_date": self.anchor_date,
            "drift_max_seconds": int(self.drift_max_seconds),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DailyJob":
        hour, minute, *rest = str(data["start_time"]).split(":")
        return cls(
            target=str(data["target"]),
            text=str(data["text"]),
            delay_seconds=int(data["delay_seconds"]),
            start_time=dtime(int(hour), int(minute), int(rest[0]) if rest else 0),
            times_per_day=int(data["times_per_day"]),
            account_ids=None if data.get("account_ids") is None else [str(item) for item in data["account_ids"]],
            buttons=[str(item) for item in data.get("buttons") or []],
            drift_seconds=int(data.get("drift_seconds") or 0),
            anchor_date=data.get("anchor_date") or None,
            drift_max_seconds=int(data.get("drift_max_seconds") or 0),
        )
