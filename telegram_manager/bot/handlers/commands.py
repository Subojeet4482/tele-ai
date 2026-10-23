"""/all, /alll, /allblock and /message: from a command (all at once) or step by step."""
from __future__ import annotations

import re

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.flows.bulk import KINDS
from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.constants import BULK_DELAY_MAX, OPEN_CHAT_MIN_DELAY
from telegram_manager.errors import describe_error
from telegram_manager.parsing import parse_bulk_args, parse_target, split_trailing_buttons

_USAGE = {
    "all": "/all @user your message 3sec Y",
    "alll": "/alll @user your message 5sec Y",
    "block": "/allblock @user 3sec Y",
    "click": "/click @user [follow] (join) 3sec Y",
}


def _prefill(answers: dict, args) -> None:
    """Put what a one-liner already says into the wizard's answers, so those steps are skipped."""
    if args.target:
        answers["target"] = args.target
    if args.text:
        answers["text"] = args.text
    elif args.buttons:
        answers["text"] = ""  # buttons only: nothing to send, just press them
    if args.text or args.buttons:
        answers["buttons"] = list(args.buttons)
    if args.delay is not None:
        answers["delay"] = args.delay


class CommandHandlers(HandlerBase):
    # --- bulk ---------------------------------------------------------------------------
    async def bulk_button(self, update, context) -> None:
        """Menu button bulk|<kind>."""
        if not await self.entry(update):
            return
        await self.open_bulk(update, context, self.arg(update))

    async def open_bulk(self, update, context, kind: str) -> None:
        """Start the step-by-step flow of one bulk action (from a button or from the bottom menu)."""
        if (kind not in KINDS and kind != "multi") or await self.accounts_or_notice(update) is None:
            return
        await self.engine.start(update, context, "multi" if kind == "multi" else f"bulk_{kind}")

    async def multi_command(self, update, context) -> None:
        """/multi @user hello [3sec] [Y]: then pick which accounts send it."""
        if not await self.guard.allow(update):
            return
        if await self.accounts_or_notice(update) is None:
            return
        try:
            args = parse_bulk_args("alll", self.command_args(update))
        except ValueError as exc:
            await ui.show(update, f"❌ {exc}\n\nExample: /multi @user your message", keyboards.back("menu", "⚙️ Menu"))
            return
        if args.delay is not None and not OPEN_CHAT_MIN_DELAY <= args.delay <= BULK_DELAY_MAX:
            await ui.show(
                update,
                f"❌ The delay must be between {OPEN_CHAT_MIN_DELAY} and {BULK_DELAY_MAX} seconds.",
                keyboards.back("menu", "⚙️ Menu"),
            )
            return
        if args.confirmed is False:
            await ui.show(update, "❌ Cancelled.", keyboards.back("menu", "⚙️ Menu"))
            return
        answers: dict = {}
        _prefill(answers, args)
        if args.confirmed:
            answers["confirmed"] = True
            answers["when"] = 0  # fully specified one-liner: no timer question
        await self.engine.start(update, context, "multi", answers)

    async def bulk_command(self, update, context, kind: str) -> None:
        if not await self.guard.allow(update):
            return
        if await self.accounts_or_notice(update) is None:
            return
        try:
            args = parse_bulk_args(kind, self.command_args(update))
        except ValueError as exc:
            await ui.show(update, f"❌ {exc}\n\nExample: {_USAGE[kind]}", keyboards.back("menu", "⚙️ Menu"))
            return
        minimum = KINDS[kind][2]
        if args.delay is not None and not minimum <= args.delay <= BULK_DELAY_MAX:
            await ui.show(
                update,
                f"❌ The delay must be between {minimum} and {BULK_DELAY_MAX} seconds.",
                keyboards.back("menu", "⚙️ Menu"),
            )
            return
        if args.confirmed is False:
            await ui.show(update, "❌ Cancelled.", keyboards.back("menu", "⚙️ Menu"))
            return
        answers: dict = {}
        _prefill(answers, args)
        if kind == "click":
            answers.pop("text", None)  # /click has no message step
            if args.buttons:
                answers["when"] = 0  # a command that already names its buttons runs now
        elif "text" in answers:
            answers["when"] = 0  # a command that already carries its message sends now
        if args.confirmed:
            answers["confirmed"] = True
        await self.engine.start(update, context, f"bulk_{kind}", answers)

    async def stop_bulk(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        stopped = self.deps.bulk.cancel(self.uid(update))
        await update.callback_query.answer("Stopping…" if stopped else "Nothing is running.")

    # --- one account --------------------------------------------------------------------
    async def message_button(self, update, context) -> None:
        """Menu button msg|start."""
        if not await self.entry(update):
            return
        if await self.accounts_or_notice(update) is None:
            return
        await self.engine.start(update, context, "message")

    async def message_for_account(self, update, context) -> None:
        """Account screen button msg_acc|<id>."""
        if not await self.entry(update):
            return
        account = await self.own_account(update, self.arg(update))
        if account is not None:
            await self.engine.start(update, context, "message", {"account": str(account["id"])})

    async def message_command(self, update, context) -> None:
        """/message                → pick account, chat, text
        /message 1                → pick chat, text
        /message 1 @user          → text
        /message 1 @user hello    → sends now
        /message 1 @user hello [follow] (join)   → sends, then presses those inline buttons"""
        if not await self.guard.allow(update):
            return
        raw = self.command_args(update).strip()
        if not raw:
            if await self.accounts_or_notice(update) is not None:
                await self.engine.start(update, context, "message")
            return
        match = re.match(r"^(\S+)(?:\s+(\S+))?(?:\s+(.+))?$", raw, re.S)
        account = await self.store.accounts.find_by_alias(self.uid(update), match.group(1))
        if account is None:
            await ui.show(
                update, "❌ Account not found. Use /list to see your accounts.", keyboards.back("menu", "⚙️ Menu")
            )
            return
        answers = {"account": str(account["id"])}
        if match.group(2):
            try:
                answers["chat"] = parse_target(match.group(2))
            except ValueError as exc:
                await ui.show(update, f"❌ {exc}", keyboards.back("menu", "⚙️ Menu"))
                return
        if match.group(3):
            try:
                text, buttons = split_trailing_buttons(match.group(3))
            except ValueError as exc:
                await ui.show(update, f"❌ {exc}", keyboards.back("menu", "⚙️ Menu"))
                return
            if text:
                answers["text"] = text
            if buttons:
                answers["buttons"] = buttons
        if "text" in answers:  # everything given: send right away
            answers.setdefault("buttons", [])
            try:
                extra = {"buttons": answers["buttons"]} if answers["buttons"] else {}
                await self.manager.send_to_existing_dialog(
                    answers["account"], answers["chat"], answers["text"], self.uid(update), **extra
                )
            except Exception as exc:
                await ui.show(update, f"❌ Could not send: {describe_error(exc)}", keyboards.back("menu", "⚙️ Menu"))
                return
            await ui.show(update, "✅ Message sent.", keyboards.after_action("💬 Send another", "msg|start"))
            return
        await self.engine.start(update, context, "message", answers)
