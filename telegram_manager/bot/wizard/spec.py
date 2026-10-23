"""Declarative description of a step-by-step flow."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from telegram_manager.bot import ui


@dataclass
class Choice:
    label: str
    value: str  # goes into callback data: keep it short (max ~40 bytes)


@dataclass
class WizardContext:
    update: Any
    context: Any
    spec: "WizardSpec"
    state: Any  # WizardState
    deps: Any

    @property
    def answers(self) -> dict[str, Any]:
        return self.state.answers

    @property
    def user_id(self) -> int:
        return self.update.effective_user.id

    async def show(self, text: str, markup=None, parse_mode: str | None = None):
        return await ui.show(self.update, text, markup, parse_mode)


class WizardAbort(Exception):
    """Stop the flow and show this message (a dead end such as 'account already connected')."""


@dataclass
class Step:
    key: str
    prompt: Callable[[WizardContext], Awaitable[str]]
    parse: Callable[[WizardContext, str], Awaitable[Any]]  # text or button value -> answer; raise ValueError to re-ask
    choices: Callable[[WizardContext], Awaitable[list[Choice]]] | None = None
    columns: int = 1
    paged: bool = False
    sensitive: bool = False  # the user's message is deleted right after it is read
    skip: Callable[[WizardContext], bool] | None = None
    back_key: str | None = None  # where Back goes, if not simply the previous step
    multi: bool = False  # several choices can be ticked; Done answers with the ticked values
    photo: bool = False  # the answer is a photo sent to the chat (its bytes land in state.media)
    media: bool = False  # a photo step that also takes a GIF or a video (state.media_id / media_kind)


@dataclass
class WizardSpec:
    name: str  # short: it is part of callback data
    title: str
    steps: list[Step]
    finish: Callable[[WizardContext], Awaitable[None]]
    owner_only: bool = False
    public: bool = False  # anybody may run it (the session export), not just admins
    back_to: str = "menu"  # screen shown on Back from the first step and on Cancel
    on_rewind: Callable[[WizardContext, str], Awaitable[None]] | None = None
