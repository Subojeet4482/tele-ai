from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Deps:
    """Everything the bot handlers need, passed around as one object."""

    settings: Any
    store: Any
    manager: Any
    access: Any  # telegram_manager.access.AccessControl
    guard: Any  # telegram_manager.bot.guard.Guard
    bulk: Any  # telegram_manager.bot.bulk_runner.BulkRunner
    log_buffer: Any = None
    log_access: Any = None
    health: Any = None
    chats: Any = None  # telegram_manager.ai.conversation.Conversations
    gemini: Any = None  # telegram_manager.ai.gemini.GeminiClient
    openrouter: Any = None  # telegram_manager.ai.openrouter.OpenRouterClient

    def ai_client(self, settings) -> Any:
        """The client of the provider the admin picked (both answer the same way)."""
        return self.openrouter if settings.is_openrouter else self.gemini
