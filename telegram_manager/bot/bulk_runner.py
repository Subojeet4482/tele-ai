"""Runs /all, /alll and /allblock in the background and reports the result. One run per admin."""
from __future__ import annotations

import asyncio
import logging
import time

from telegram_manager.bot import keyboards, ui
from telegram_manager.constants import OPEN_CHAT_MIN_DELAY
from telegram_manager.core.models import BulkResult
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)

_VERBS = {
    "all": "Message sent", "alll": "Message sent", "multi": "Message sent", "one": "Message sent",
    "block": "Blocked", "click": "Buttons clicked",
}
_DOING = {
    "all": "📨 Sending", "alll": "📤 Sending", "multi": "👥 Sending", "one": "💬 Sending",
    "block": "🚫 Blocking", "click": "🖱 Clicking",
}
PROGRESS_INTERVAL = 2.0  # seconds between edits of the progress message (Telegram limits edits)


def summarize(kind: str, target: str, result: BulkResult, buttons: list[str] | None = None, text: str | None = None) -> str:
    if result.total == 0:
        return "You have no connected accounts."
    if result.ok == 0:
        head = f"❌ Nothing went through ({target})."
    else:
        verb = "Buttons clicked" if buttons and not text else _VERBS[kind]
        head = f"✅ {verb}: {result.ok}/{result.total} accounts ({target})"
    if buttons:
        head += f"\n🖱 Buttons: {' → '.join(buttons)}"
    if not result.errors:
        return head
    lines = "\n".join(f"• {line}" for line in result.errors[:15])
    more = f"\n… and {len(result.errors) - 15} more" if len(result.errors) > 15 else ""
    return f"{head}\n\n⚠️ Problems:\n{lines}{more}"


def progress_text(kind: str, target: str, result: BulkResult, current: str = "") -> str:
    """Live status: a bar, how many went through, how many did not, and who is being worked on."""
    total = max(result.total, 1)
    filled = round(10 * result.done / total)
    lines = [
        f"{_DOING[kind]} ({target})",
        f"{'█' * filled}{'░' * (10 - filled)}  {result.done}/{result.total}",
        f"✅ Done: {result.ok}    ❌ Failed: {result.failed}    ⏳ Left: {result.total - result.done}",
    ]
    if current:
        lines.append(f"➡️ Now: {current}")
    return "\n".join(lines)


def format_wait(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours}h" if hours else "", f"{minutes}m" if minutes else "", f"{secs}s" if secs and not hours else ""]
    return " ".join(part for part in parts if part) or "0s"


class BulkRunner:
    def __init__(self, manager, store) -> None:
        self._manager = manager
        self._store = store
        self._tasks: dict[int, asyncio.Task] = {}

    def running(self, owner_id: int) -> bool:
        task = self._tasks.get(owner_id)
        return task is not None and not task.done()

    def cancel(self, owner_id: int) -> bool:
        task = self._tasks.get(owner_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def cancel_all(self) -> int:
        return sum(1 for owner_id in list(self._tasks) if self.cancel(owner_id))

    async def launch(
        self,
        update,
        context,
        owner_id: int,
        kind: str,
        target: str,
        text: str | None,
        delay: int,
        account_ids: list[str] | None = None,
        start_in: int = 0,
        buttons: list[str] | None = None,
    ) -> None:
        """kind: all | alll | block | click | multi (picked accounts, opens chat) | one (one account, existing chat).
        buttons: inline buttons to press after the message (click: only that, no message).
        start_in: wait this many seconds before starting (a timed message)."""
        accounts = [a for a in await self._store.accounts.list(owner_id) if a.get("enabled", True)]
        if account_ids is not None:
            wanted = {str(item) for item in account_ids}
            accounts = [a for a in accounts if str(a["id"]) in wanted]
        if not accounts:
            await ui.show(update, "You have no connected accounts yet.", keyboards.no_accounts())
            return
        if self.running(owner_id):
            await ui.show(update, "⏳ A bulk action is already running. Stop it first.", keyboards.stop_bulk())
            return
        if kind in ("alll", "multi"):
            delay = max(delay, OPEN_CHAT_MIN_DELAY)
        pace = "all at the same time" if delay == 0 else f"{delay} sec apart"
        estimate = ""
        if delay and len(accounts) > 1:
            seconds = (len(accounts) - 1) * delay
            estimate = f" (about {seconds // 60}m {seconds % 60}s)" if seconds >= 60 else f" (about {seconds}s)"
        wait = f"\n⏰ Starts in {format_wait(start_in)}. Tap Stop to cancel." if start_in else ""
        shown = await ui.show(
            update,
            f"⏳ Working on {len(accounts)} account(s) — {pace}{estimate}.{wait}\nThis message updates as it goes.",
            keyboards.stop_bulk(),
        )
        message_id = getattr(shown, "message_id", None)
        ids = [str(a["id"]) for a in accounts] if account_ids is not None else None
        # asyncio.create_task rather than application.create_task: PTB waits for its own tasks on
        # shutdown, and a bulk send with a long delay would block a redeploy.
        task = asyncio.create_task(
            self._run(context.bot, owner_id, kind, target, text, delay, ids, start_in, message_id, buttons)
        )
        self._tasks[owner_id] = task
        task.add_done_callback(lambda done, owner=owner_id: self._forget(owner, done))

    def _forget(self, owner_id: int, task: asyncio.Task) -> None:
        if self._tasks.get(owner_id) is task:
            del self._tasks[owner_id]

    async def _run(
        self,
        bot,
        owner_id: int,
        kind: str,
        target: str,
        text: str | None,
        delay: int,
        ids: list[str] | None,
        start_in: int,
        message_id: int | None,
        buttons: list[str] | None = None,
    ) -> None:
        again = keyboards.after_action("🔁 Again", f"bulk|{kind}" if kind != "one" else "msg|start")
        last_edit = 0.0

        async def on_progress(result: BulkResult, current: str) -> None:
            nonlocal last_edit
            if message_id is None or (result.total and result.done >= result.total):
                return  # no message to edit, or finished: the final text is written below
            now = time.monotonic()
            if now - last_edit < PROGRESS_INTERVAL:
                return
            last_edit = now
            try:
                await bot.edit_message_text(
                    chat_id=owner_id,
                    message_id=message_id,
                    text=progress_text(kind, target, result, current),
                    reply_markup=keyboards.stop_bulk(),
                )
            except Exception:
                logger.debug("Could not update the progress message", exc_info=True)

        try:
            if start_in:
                await asyncio.sleep(start_in)
            extra = {"buttons": buttons} if buttons else {}
            if kind in ("all", "click"):
                result = await self._manager.send_all(owner_id, target, text or "", delay, on_progress, **extra)
            elif kind == "alll":
                result = await self._manager.send_all_any(owner_id, target, text or "", delay, on_progress, **extra)
            elif kind in ("multi", "one"):
                result = await self._manager.send_selected(
                    owner_id, ids or [], target, text or "", delay, kind == "multi", on_progress, **extra
                )
            else:
                result = await self._manager.block_all(owner_id, target, delay, on_progress)
        except asyncio.CancelledError:
            await self._finish(bot, owner_id, message_id, "🛑 Stopped. Accounts that were already done stay done.", again)
            raise
        except Exception as exc:
            logger.exception("Bulk action %s failed", kind)
            await self._finish(bot, owner_id, message_id, f"❌ It failed: {describe_error(exc)}", again)
            return
        await self._finish(bot, owner_id, message_id, summarize(kind, target, result, buttons, text), again)

    @staticmethod
    async def _finish(bot, chat_id: int, message_id: int | None, text: str, markup) -> None:
        """Write the final result into the progress message; send a new one if that is not possible."""
        if message_id is not None:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
                return
            except Exception:
                logger.debug("Could not edit the progress message into the result", exc_info=True)
        try:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        except Exception:
            logger.warning("Could not deliver a bulk result", exc_info=True)
