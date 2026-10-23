"""Runs step-by-step flows. Every step shows Back and Cancel; answers come from buttons or text.

A flow is a WizardSpec (see spec.py). Its state lives in context.user_data["wz"]. Starting a
flow with some answers already filled in (from a command like `/all @user hello Y`) skips
straight to the first step that is still unanswered."""
from __future__ import annotations

import logging
import math
from typing import Any, Awaitable, Callable

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.keyboards import Btn, Markup
from telegram_manager.bot.wizard.spec import Step, WizardAbort, WizardContext, WizardSpec
from telegram_manager.bot.wizard.state import WizardState
from telegram_manager.constants import PAGE_SIZE
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)

STATE_KEY = "wz"
ATTEMPT_KEY = "login_attempt"  # a half-finished phone login (holds a Telethon client)

Screen = Callable[[Any, Any], Awaitable[None]]


class WizardEngine:
    def __init__(self, deps) -> None:
        self.deps = deps
        self.specs: dict[str, WizardSpec] = {}
        self.screens: dict[str, Screen] = {}  # where Back/Cancel lead: "menu", "home", "adm|home", ...

    def register(self, *specs: WizardSpec) -> None:
        for spec in specs:
            self.specs[spec.name] = spec

    # --- state -------------------------------------------------------------------------
    def active(self, context) -> WizardState | None:
        state = context.user_data.get(STATE_KEY)
        if state is None:
            return None
        if state.expired():
            context.user_data.pop(STATE_KEY, None)
            return None
        return state

    async def reset(self, context) -> None:
        """Drop any running flow and close a half-finished phone login."""
        context.user_data.pop(STATE_KEY, None)
        attempt = context.user_data.pop(ATTEMPT_KEY, None)
        if attempt is not None:
            await self.deps.manager.abort_login(attempt)

    def _wc(self, update, context, state: WizardState) -> WizardContext:
        return WizardContext(update, context, self.specs[state.name], state, self.deps)

    @staticmethod
    def _visible(wc: WizardContext) -> list[Step]:
        return [step for step in wc.spec.steps if not (step.skip and step.skip(wc))]

    @staticmethod
    def _step(wc: WizardContext) -> Step:
        return next(step for step in wc.spec.steps if step.key == wc.state.step)

    # --- entry points ---------------------------------------------------------------------
    async def start(self, update, context, name: str, answers: dict[str, Any] | None = None) -> None:
        spec = self.specs[name]
        if spec.owner_only and not await self.deps.guard.allow_owner(update):
            return
        await self.reset(context)
        state = WizardState(name=name, answers=dict(answers or {}))
        context.user_data[STATE_KEY] = state
        await self._advance(self._wc(update, context, state))

    async def on_text(self, update, context, text: str) -> bool:
        """Feed a typed message to the running flow. False if no flow is waiting for text."""
        state = self.active(context)
        if state is None or state.name not in self.specs:
            return False
        wc = self._wc(update, context, state)
        if wc.spec.owner_only and not self.deps.access.is_owner(wc.user_id):
            return False
        if state.step is None:
            return False
        step = self._step(wc)
        state.touch()
        if step.sensitive:
            try:
                await update.effective_message.delete()  # do not leave secrets in the chat
            except Exception:
                logger.debug("Could not delete a sensitive message")
        await self._answer(wc, text)
        return True

    async def on_photo(self, update, context, data: bytes, file_id: str | None = None) -> bool:
        """Feed a received photo to the running flow. False if no flow is waiting for one."""
        state = self.active(context)
        if state is None or state.name not in self.specs or state.step is None:
            return False
        wc = self._wc(update, context, state)
        if not self._step(wc).photo:
            return False
        state.touch()
        state.media = data
        state.media_id = file_id
        state.media_kind = "photo"
        await self._answer(wc, "photo")
        return True

    async def on_media(self, update, context, kind: str, file_id: str) -> bool:
        """Feed a received GIF or video to the running flow. False if the current step does not take one."""
        state = self.active(context)
        if state is None or state.name not in self.specs or state.step is None:
            return False
        wc = self._wc(update, context, state)
        step = self._step(wc)
        if not (step.photo and step.media):
            return False
        state.touch()
        state.media = b""  # nothing to download: the file id is enough to send it again
        state.media_id = file_id
        state.media_kind = kind
        await self._answer(wc, kind)
        return True

    async def on_callback(self, update, context) -> None:
        """Buttons inside a flow: wz|<name>|pick|<value>, back, cancel, page|<n>."""
        query = update.callback_query
        parts = (query.data or "").split("|", 3)
        name = parts[1] if len(parts) > 1 else ""
        spec = self.specs.get(name)
        allowed = await (self.deps.guard.allow_public(update) if spec is not None and spec.public else self.deps.guard.allow(update))
        if not allowed:
            return
        await query.answer()
        action = parts[2] if len(parts) > 2 else ""
        arg = parts[3] if len(parts) > 3 else ""
        state = self.active(context)
        if state is None or state.name != name or name not in self.specs:
            await ui.show(
                update, "⌛ This step expired. Open the menu and start again.", keyboards.back("menu", "⚙️ Menu")
            )
            return
        wc = self._wc(update, context, state)
        if wc.spec.owner_only and not self.deps.access.is_owner(wc.user_id):
            await self.deps.guard.allow_owner(update)
            return
        state.touch()
        if action == "pick":
            await self._answer(wc, arg)
        elif action in ("tog", "all", "none", "done"):
            await self._multi(wc, action, arg)
        elif action == "back":
            await self._back(wc)
        elif action == "cancel":
            await self._leave(wc)
        elif action == "page":
            try:
                state.page = max(0, int(arg))
            except ValueError:
                state.page = 0
            await self._render(wc)

    async def _multi(self, wc: WizardContext, action: str, arg: str) -> None:
        """Tick / untick choices of a multi-choice step, then Done answers with the ticked ones."""
        step = self._step(wc)
        if not step.multi or step.choices is None:
            return
        if wc.state.cache is None:
            wc.state.cache = await step.choices(wc)
        values = [choice.value for choice in wc.state.cache]
        selected = wc.state.selected
        if action == "tog" and arg in values:
            if arg in selected:
                selected.remove(arg)
            else:
                selected.append(arg)
        elif action == "all":
            wc.state.selected = list(values)
        elif action == "none":
            wc.state.selected = []
        elif action == "done":
            ordered = [value for value in values if value in selected]
            await self._answer(wc, ",".join(ordered))
            return
        await self._render(wc)

    # --- moving between steps -------------------------------------------------------------------
    async def _advance(self, wc: WizardContext) -> None:
        for step in self._visible(wc):
            if step.key not in wc.answers:
                wc.state.step = step.key
                wc.state.page = 0
                wc.state.cache = None
                await self._render(wc)
                return
        wc.context.user_data.pop(STATE_KEY, None)  # done: the flow ends before the final action runs
        await wc.spec.finish(wc)

    async def _answer(self, wc: WizardContext, raw: str) -> None:
        step = self._step(wc)
        try:
            value = await step.parse(wc, raw)
        except WizardAbort as exc:
            await self._abort(wc, str(exc) or "Cancelled.")
            return
        except ValueError as exc:
            await self._render(wc, error=str(exc))
            return
        except Exception as exc:
            logger.warning("Step %s of %s failed: %s", step.key, wc.spec.name, describe_error(exc))
            await self._render(wc, error=describe_error(exc))
            return
        wc.answers[step.key] = value
        await self._advance(wc)

    async def _back(self, wc: WizardContext) -> None:
        visible = self._visible(wc)
        keys = [step.key for step in visible]
        current = self._step(wc)
        index = keys.index(current.key) if current.key in keys else len(keys)
        target_index = keys.index(current.back_key) if current.back_key in keys else index - 1
        if target_index < 0:
            await self._leave(wc)
            return
        target_key = keys[target_index]
        order = [step.key for step in wc.spec.steps]
        for key in order[order.index(target_key):]:
            wc.answers.pop(key, None)  # going back means answering again
        if wc.spec.on_rewind is not None:
            await wc.spec.on_rewind(wc, target_key)
        await self._advance(wc)

    async def _leave(self, wc: WizardContext) -> None:
        await self.reset(wc.context)
        await self.go(wc.update, wc.context, wc.spec.back_to)

    async def _abort(self, wc: WizardContext, message: str) -> None:
        await self.reset(wc.context)
        await wc.show(message, keyboards.back(wc.spec.back_to))

    async def go(self, update, context, screen: str) -> None:
        handler = self.screens.get(screen)
        if handler is None:
            await ui.show(update, "Use /menu to continue.")
            return
        await handler(update, context)

    # --- drawing ------------------------------------------------------------------------------------
    async def _render(self, wc: WizardContext, error: str | None = None) -> None:
        step = self._step(wc)
        text = await step.prompt(wc)
        if error:
            text = f"❌ {error}\n\n{text}"
        choices = []
        if step.choices is not None:
            try:
                if wc.state.cache is None:
                    wc.state.cache = await step.choices(wc)
                choices = wc.state.cache
            except Exception as exc:
                logger.warning("Could not load choices for %s: %s", step.key, describe_error(exc))
                text += f"\n\n⚠️ {describe_error(exc)}"
        await wc.show(f"{wc.spec.title}\n\n{text}", self._keyboard(wc, step, choices))

    @staticmethod
    def _keyboard(wc: WizardContext, step: Step, choices: list) -> Markup:
        name = wc.spec.name
        shown = choices
        pages = 1
        if step.paged and len(choices) > PAGE_SIZE:
            pages = math.ceil(len(choices) / PAGE_SIZE)
            wc.state.page = min(wc.state.page, pages - 1)
            shown = choices[wc.state.page * PAGE_SIZE : (wc.state.page + 1) * PAGE_SIZE]
        rows: list[list[Btn]] = []
        row: list[Btn] = []
        for choice in shown:
            if step.multi:
                data = f"wz|{name}|tog|{choice.value}"
                text = f"{'☑️' if choice.value in wc.state.selected else '⬜'} {choice.label}"
            else:
                data = f"wz|{name}|pick|{choice.value}"
                text = choice.label
            if len(data.encode()) > 64:
                continue  # Telegram would reject the whole keyboard
            row.append(Btn(text, callback_data=data))
            if len(row) == step.columns:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        if pages > 1:
            page = wc.state.page
            nav = []
            if page > 0:
                nav.append(Btn("◀️ Prev", callback_data=f"wz|{name}|page|{page - 1}", style="primary"))
            nav.append(Btn(f"{page + 1}/{pages}", callback_data=f"wz|{name}|page|{page}"))
            if page < pages - 1:
                nav.append(Btn("Next ▶️", callback_data=f"wz|{name}|page|{page + 1}", style="primary"))
            rows.append(nav)
        if step.multi:
            rows.append([
                Btn("☑️ Select all", callback_data=f"wz|{name}|all"),
                Btn("⬜ Clear", callback_data=f"wz|{name}|none"),
            ])
            rows.append([Btn(f"✅ Done ({len(wc.state.selected)} picked)", callback_data=f"wz|{name}|done")])
        rows.append(
            [Btn("⬅️ Back", callback_data=f"wz|{name}|back"), Btn("❌ Cancel", callback_data=f"wz|{name}|cancel")]
        )
        return Markup(rows)
