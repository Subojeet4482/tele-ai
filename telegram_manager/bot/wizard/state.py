from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from telegram_manager.constants import WIZARD_TTL


@dataclass
class WizardState:
    name: str
    answers: dict[str, Any] = field(default_factory=dict)
    step: str | None = None
    page: int = 0
    cache: list | None = None
    selected: list[str] = field(default_factory=list)  # ticked values of a multi-choice step
    media: bytes | None = None  # a photo received by a photo step
    media_id: str | None = None  # Telegram file id of that photo / GIF / video
    media_kind: str | None = None  # photo | animation | video
    touched: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.touched = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self.touched > WIZARD_TTL
