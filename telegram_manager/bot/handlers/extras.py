"""/setapi, /send, /unsend, /delchat, /whois and /import (a .txt of sessions)."""
from __future__ import annotations

import logging
import re
from contextlib import suppress

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.constants import BULK_DELAY_MAX, OPEN_CHAT_MIN_DELAY
from telegram_manager.core.extras import UNSEND_MAX_MINUTES
from telegram_manager.errors import describe_error
from telegram_manager.parsing import (
    looks_like_selector,
    parse_bulk_args,
    parse_int,
    parse_minutes,
    parse_selector,
    parse_session_file,
    parse_target,
)

logger = logging.getLogger(__name__)

MAX_SESSION_FILE_BYTES = 1024 * 1024
PENDING_UNSEND = "pending_unsend"
PENDING_DELCHAT = "pending_delchat"
_BACK = keyboards.back("menu", "⚙️ Menu")

SETAPI_HELP = (
    "🔐 Save your API ID and hash once, and Connect / /import will not ask again.\n\n"
    "/setapi 1234567 0123456789abcdef0123456789abcdef\n"
    "/setapi clear   → forget it\n\n"
    "I delete your message right after reading it. The hash is stored encrypted."
)
SEND_HELP = (
    "Send from the accounts you name (new chats are opened if needed):\n\n"
    "/send 1,2,5 @user hello\n/send 1-5 @user hello 5sec Y\n/send all @user hello\n\n"
    "Accounts: all, or numbers like 1,2,5 or ranges like 1-5 (see /list)."
)


class ExtraHandlers(HandlerBase):
    async def _accounts(self, update, selector: str) -> tuple[list[dict], str | None]:
        """The accounts a selector names, or an error text."""
        rows = await self.store.accounts.list(self.uid(update))
        if not rows:
            return [], "You have no connected accounts yet."
        try:
            numbers = parse_selector(selector, [r["number"] for r in rows if r.get("number")])
        except ValueError as exc:
            return [], str(exc)
        return [r for r in rows if r.get("number") in numbers and r.get("enabled", True)], None

    # --- /setapi ---------------------------------------------------------------------------
    async def setapi(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        message = update.effective_message
        raw = self.command_args(update).strip()
        uid = self.uid(update)
        if raw:
            with suppress(Exception):
                await message.delete()  # the hash must not stay in the chat
        if not raw:
            saved = await self.manager.saved_api(uid)
            state = f"✅ Saved: API ID {saved[0]} (hash hidden)." if saved else "Nothing saved yet."
            await ui.show(update, f"{state}\n\n{SETAPI_HELP}", _BACK)
            return
        if raw.lower() in ("clear", "delete", "remove"):
            await self.manager.clear_api(uid)
            await ui.show(update, "🗑 Saved API ID and hash removed.", _BACK)
            return
        parts = re.split(r"[\s:,|]+", raw)
        try:
            if len(parts) != 2:
                raise ValueError("Send the API ID and the API hash, like /setapi 1234567 0123…")
            api_id = parse_int(parts[0], "API ID")
            if len(parts[1]) < 20:
                raise ValueError("That API hash looks too short.")
        except ValueError as exc:
            await ui.show(update, f"❌ {exc}\n\n{SETAPI_HELP}", _BACK)
            return
        await self.manager.save_api(uid, api_id, parts[1])
        await ui.show(update, f"✅ API ID {api_id} and its hash are saved (encrypted). Message deleted.", _BACK)

    # --- /send <accounts> <user> <text> ------------------------------------------------------
    async def send_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        first, _, rest = self.command_args(update).strip().partition(" ")
        if not looks_like_selector(first):
            await ui.show(update, SEND_HELP, _BACK)
            return
        accounts, error = await self._accounts(update, first)
        if error or not accounts:
            await ui.show(update, f"❌ {error or 'Those accounts are switched off.'}", _BACK)
            return
        try:
            args = parse_bulk_args("alll", rest)
        except ValueError as exc:
            await ui.show(update, f"❌ {exc}\n\n{SEND_HELP}", _BACK)
            return
        if args.delay is not None and not OPEN_CHAT_MIN_DELAY <= args.delay <= BULK_DELAY_MAX:
            await ui.show(update, f"❌ The delay must be between {OPEN_CHAT_MIN_DELAY} and {BULK_DELAY_MAX} seconds.", _BACK)
            return
        if args.confirmed is False:
            await ui.show(update, "❌ Cancelled.", _BACK)
            return
        answers: dict = {"accounts": [str(a["id"]) for a in accounts]}
        if args.target:
            answers["target"] = args.target
        if args.text:
            answers["text"] = args.text
        if args.text or args.buttons:
            answers["buttons"] = list(args.buttons)
        if args.delay is not None:
            answers["delay"] = args.delay
        if args.confirmed and args.target and args.text:
            answers["confirmed"] = True
            answers["when"] = 0
        await self.engine.start(update, context, "multi", answers)

    # --- /unsend <minutes> [accounts] ------------------------------------------------------------
    async def unsend(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        first, _, rest = self.command_args(update).strip().partition(" ")
        usage = (
            "↩️ Delete (for everybody) what the bot sent in the last N minutes.\n\n"
            "/unsend 30          every account\n/unsend 2h 1,2,5     only these accounts\n\n"
            f"Works for messages sent since the bot last started, up to {UNSEND_MAX_MINUTES // 60} hours back."
        )
        try:
            minutes = parse_minutes(first, UNSEND_MAX_MINUTES)
            if minutes < 1:
                raise ValueError("Say how many minutes, like 30 or 2h.")
        except ValueError as exc:
            await ui.show(update, f"❌ {exc}\n\n{usage}" if first else usage, _BACK)
            return
        uid = self.uid(update)
        account_ids = None
        if rest.strip():
            accounts, error = await self._accounts(update, rest.strip())
            if error:
                await ui.show(update, f"❌ {error}", _BACK)
                return
            account_ids = [str(a["id"]) for a in accounts]
        count = self.manager.sent_count(uid, minutes, account_ids)
        if not count:
            await ui.show(update, f"Nothing to delete: the bot sent no message in the last {minutes} minute(s).", _BACK)
            return
        context.user_data[PENDING_UNSEND] = (minutes, account_ids)
        await ui.show(
            update,
            f"↩️ Delete {count} message(s) sent in the last {minutes} minute(s)"
            f"{' from ' + str(len(account_ids)) + ' account(s)' if account_ids else ''}?\n\nThis cannot be undone.",
            keyboards.confirm_pair("unsend|go", "unsend|no", "🗑 Delete them"),
        )

    async def unsend_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        pending = context.user_data.pop(PENDING_UNSEND, None)
        if self.arg(update) != "go" or pending is None:
            await ui.show(update, "❌ Cancelled." if pending is not None else "⌛ That request expired. Send /unsend again.", _BACK)
            return
        minutes, account_ids = pending
        await ui.show(update, "⏳ Deleting…")
        try:
            result, deleted = await self.manager.unsend_recent(self.uid(update), minutes, account_ids)
        except Exception as exc:
            await ui.show(update, f"❌ {describe_error(exc)}", _BACK)
            return
        lines = [f"✅ Deleted {deleted} message(s) on {result.ok} account(s)."]
        if result.errors:
            lines += ["", "⚠️ Problems:"] + result.errors[:15]
        await ui.show(update, "\n".join(lines), _BACK)

    # --- /delchat <accounts> <user> -----------------------------------------------------------------
    async def delchat(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        first, _, rest = self.command_args(update).strip().partition(" ")
        usage = "🗑 Delete a chat on the accounts you name (the other person keeps their copy).\n\n/delchat 1,2,5 @user\n/delchat all @user"
        if not looks_like_selector(first) or not rest.strip():
            await ui.show(update, usage, _BACK)
            return
        accounts, error = await self._accounts(update, first)
        if error or not accounts:
            await ui.show(update, f"❌ {error or 'Those accounts are switched off.'}", _BACK)
            return
        try:
            target = parse_target(rest.strip().split()[0])
        except ValueError as exc:
            await ui.show(update, f"❌ {exc}\n\n{usage}", _BACK)
            return
        context.user_data[PENDING_DELCHAT] = ([str(a["id"]) for a in accounts], target)
        await ui.show(
            update,
            f"🗑 Delete the chat with {target} on {len(accounts)} account(s)?\n"
            "Only your side is removed; the other person keeps their messages.",
            keyboards.confirm_pair("delchat|go", "delchat|no", "🗑 Delete chat"),
        )

    async def delchat_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        pending = context.user_data.pop(PENDING_DELCHAT, None)
        if self.arg(update) != "go" or pending is None:
            await ui.show(update, "❌ Cancelled." if pending is not None else "⌛ That request expired. Send /delchat again.", _BACK)
            return
        ids, target = pending
        await ui.show(update, "⏳ Deleting…")
        result = await self.manager.delete_chat_selected(self.uid(update), ids, target)
        lines = [f"✅ Chat with {target} deleted on {result.ok} of {result.total} account(s)."]
        if result.errors:
            lines += ["", "⚠️ Problems:"] + result.errors[:15]
        await ui.show(update, "\n".join(lines), _BACK)

    # --- /whois ---------------------------------------------------------------------------------------
    async def whois(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        raw = self.command_args(update).strip()
        try:
            target = parse_target(raw.split()[0] if raw else "")
        except ValueError:
            await ui.show(update, "🔎 Find who a @username is.\n\n/whois @username   (or a chat id)", _BACK)
            return
        try:
            info = await self.manager.lookup_user(self.uid(update), target)
        except Exception as exc:
            await ui.show(update, f"❌ {describe_error(exc)}", _BACK)
            return
        lines = [
            f"🔎 {info['name'] or target}",
            f"🆔 ID: {info['id']}",
            f"🔗 Username: {'@' + info['username'] if info['username'] else '—'}",
            f"📦 Type: {'bot' if info['is_bot'] else info['kind']}",
            f"👁 Looked up with account {info['via']}",
        ]
        await ui.show(update, "\n".join(lines), _BACK)

    # --- /import + a .txt of sessions --------------------------------------------------------------------
    async def import_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        saved = await self.manager.saved_api(self.uid(update))
        api = "✅ Your saved API ID/hash will be used." if saved else "⚠️ No API saved yet: use /setapi first, or write api_id:api_hash:session on each line."
        await ui.show(
            update,
            "📥 Send me a .txt file now: one session string per line.\n"
            "Lines starting with # and blank lines are ignored, so the file from 📦 Export multiple works as it is.\n\n"
            f"{api}\nThe file is deleted from the chat after I read it.",
            _BACK,
        )

    async def txt_input(self, update, context) -> None:
        """Any .txt an admin sends (while no step-by-step flow is waiting) is read as a list of sessions."""
        if not await self.guard.allow(update):
            return
        if self.engine.active(context) is not None:
            return
        message = update.effective_message
        document = getattr(message, "document", None)
        if document is None:
            return
        name = (getattr(document, "file_name", "") or "").lower()
        mime = (getattr(document, "mime_type", "") or "").lower()
        if not (name.endswith(".txt") or mime == "text/plain"):
            return  # only text files are read as session lists
        if getattr(document, "file_size", 0) and document.file_size > MAX_SESSION_FILE_BYTES:
            await message.reply_text("❌ That file is too big. Send a .txt under 1 MB.")
            return
        try:
            raw = bytes(await (await document.get_file()).download_as_bytearray())
            lines, ignored = parse_session_file(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            await message.reply_text(f"❌ {exc}")
            return
        except Exception as exc:
            await message.reply_text(f"❌ Could not read that file: {describe_error(exc)}")
            return
        if not lines:
            await message.reply_text("❌ I found no session strings in that file. One session per line, please.")
            return
        uid = self.uid(update)
        api = await self.manager.saved_api(uid)
        if api is None and any(item.api_id is None for item in lines):
            await message.reply_text("❌ No API ID/hash saved. Send /setapi first, then send the file again.")
            return
        with suppress(Exception):
            await message.delete()  # sessions are secrets: do not leave the file in the chat
        status = await message.reply_text(f"⏳ Adding {len(lines)} account(s)… this takes about {len(lines) * 3} seconds.")
        report = await self.manager.import_sessions(uid, lines, api)
        out = [f"✅ Added {len(report['added'])} of {len(lines)} account(s)."]
        out += [f"  • {label}" for label in report["added"][:40]]
        if report["duplicate"]:
            out.append(f"↪️ {report['duplicate']} were already connected.")
        if ignored:
            out.append(f"ℹ️ {ignored} line(s) were not sessions and were skipped.")
        if report["failed"]:
            out += ["", "❌ Failed:"] + report["failed"][:15]
        with suppress(Exception):
            await status.edit_text("\n".join(out), reply_markup=keyboards.after_connect())
            return
        await message.reply_text("\n".join(out), reply_markup=keyboards.after_connect())
