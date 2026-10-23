"""Session export for anybody (not only admins): log in with the number + code, get the session
string of THAT account back, nothing is stored. Also the helper that shows a session string."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import uuid
from contextlib import suppress

from telegram import InputFile
from telethon.errors import PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError

from telegram_manager.bot.flows.common import accounts_step, static
from telegram_manager.bot.wizard.engine import ATTEMPT_KEY
from telegram_manager.bot.wizard.spec import Step, WizardAbort, WizardContext, WizardSpec
from telegram_manager.constants import EXPORT_AUTODELETE, EXPORT_COOLDOWN, EXPORT_FILE_AUTODELETE, EXPORT_NOTIFY_TTL, WIZARD_TTL
from telegram_manager.errors import describe_error
from telegram_manager.parsing import parse_int

logger = logging.getLogger(__name__)

_last_request: dict[int, float] = {}
_pending: set[asyncio.Task] = set()

# token -> {"session", "api_id", "api_hash", "label", "expires"}: lets the owner/admin tap
# "Revoke" on the notification about a session someone just exported. In memory only, on purpose —
# the whole point of public export is that nothing about it is persisted.
_revoke_pending: dict[str, dict] = {}


def _register_revoke(session: str, api_id: int, api_hash: str, label: str) -> str:
    now = time.monotonic()
    for key in [k for k, v in _revoke_pending.items() if v["expires"] < now]:
        _revoke_pending.pop(key, None)
    token = uuid.uuid4().hex[:16]
    _revoke_pending[token] = {
        "session": session,
        "api_id": api_id,
        "api_hash": api_hash,
        "label": label,
        "expires": now + EXPORT_NOTIFY_TTL,
    }
    return token


def pop_revoke(token: str) -> dict | None:
    """The token from an expr|<token> button, once: removes it either way so it cannot be reused."""
    pending = _revoke_pending.pop(token, None)
    if pending is None or pending["expires"] < time.monotonic():
        return None
    return pending


async def _delete_later(message, seconds: int) -> None:
    await asyncio.sleep(seconds)
    with suppress(Exception):
        await message.delete()


async def send_session(context, chat_id: int, session: str) -> None:
    """The session string as a tap-to-copy block; the message deletes itself after a few minutes."""
    minutes = max(1, EXPORT_AUTODELETE // 60)
    text = (
        "🔑 <b>Your session string</b>\n\n"
        f"<code>{html.escape(session)}</code>\n\n"
        "⚠️ Anyone who has this text can use your Telegram account. Never share it.\n"
        f"🗑 This message deletes itself in {minutes} minutes — copy it now."
    )
    message = await context.bot.send_message(chat_id, text, parse_mode="HTML")
    task = asyncio.ensure_future(_delete_later(message, EXPORT_AUTODELETE))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _expire_attempt(manager, context, attempt) -> None:
    """A phone login nobody finished must not keep a Telegram connection open."""
    await asyncio.sleep(WIZARD_TTL + 5)
    if context.user_data.get(ATTEMPT_KEY) is attempt:
        context.user_data.pop(ATTEMPT_KEY, None)
        await manager.abort_login(attempt)


async def notify_admins_of_export(deps, context, requester, meta: dict) -> None:
    """Tell the owner and every admin that someone (not necessarily an admin) just exported a
    session through the public /export flow. If the login reached far enough to know which
    Telegram account it was (api_id/api_hash + the session string), a Revoke button is attached
    so an admin can immediately end that session on Telegram's side if it was not expected."""
    from telegram_manager.bot import keyboards

    session = meta.get("session")
    label = meta.get("label") or meta.get("phone") or "an account"
    who = getattr(requester, "full_name", None) or getattr(requester, "first_name", None) or "Someone"
    username = getattr(requester, "username", None)
    who_line = f"{who} (@{username})" if username else who
    requester_id = getattr(requester, "id", None)

    lines = [
        "📤 <b>A session was exported</b>",
        f"👤 By: {html.escape(who_line)}" + (f" · id {requester_id}" if requester_id else ""),
        f"📱 Account: {html.escape(str(label))}",
        "",
        "This was created through the public session-export flow (/export). If you did not expect "
        "this, you can revoke it below — the exported string stops working immediately.",
    ]
    markup = None
    if session and meta.get("api_id") and meta.get("api_hash"):
        token = _register_revoke(session, meta["api_id"], meta["api_hash"], str(label))
        markup = keyboards.revoke_confirm(token)
        minutes = max(1, EXPORT_NOTIFY_TTL // 60)
        lines.append(f"\n⏳ This Revoke button works for {minutes} minutes.")

    text = "\n".join(lines)
    recipients = {deps.settings.owner_id}
    with suppress(Exception):
        await deps.access.ensure_loaded()
        recipients.update(a["id"] for a in deps.access.admins())
    recipients.discard(requester_id)  # exporting your own account doesn't need to notify yourself twice
    for chat_id in recipients:
        with suppress(Exception):
            await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


def build_export_specs(deps) -> list[WizardSpec]:
    manager = deps.manager

    async def parse_api_id(wc: WizardContext, raw: str) -> int:
        return parse_int(raw, "API ID")

    async def parse_api_hash(wc: WizardContext, raw: str) -> str:
        value = raw.strip()
        if len(value) < 20:
            raise ValueError("That API hash looks too short.")
        return value

    async def drop_attempt(wc: WizardContext) -> None:
        await manager.abort_login(wc.context.user_data.pop(ATTEMPT_KEY, None))

    async def parse_phone(wc: WizardContext, raw: str) -> str:
        phone = re.sub(r"[\s().-]", "", raw)
        if phone.startswith("00"):
            phone = "+" + phone[2:]
        elif phone.isdigit() and 11 <= len(phone) <= 15:
            phone = "+" + phone
        if not re.fullmatch(r"\+\d{7,15}", phone):
            raise ValueError("Send the number with country code, like +919876543210.")
        wait = EXPORT_COOLDOWN - (time.monotonic() - _last_request.get(wc.user_id, -EXPORT_COOLDOWN))
        if wait > 0:
            raise ValueError(f"Please wait {int(wait) + 1} seconds before asking for another code.")
        await drop_attempt(wc)
        attempt = await manager.begin_login(wc.user_id, wc.answers["api_id"], wc.answers["api_hash"], phone)
        _last_request[wc.user_id] = time.monotonic()
        if len(_last_request) > 2000:
            cutoff = time.monotonic() - EXPORT_COOLDOWN
            for key in [k for k, v in _last_request.items() if v < cutoff]:
                _last_request.pop(key, None)
        wc.context.user_data[ATTEMPT_KEY] = attempt
        task = asyncio.ensure_future(_expire_attempt(manager, wc.context, attempt))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
        return phone

    async def parse_code(wc: WizardContext, raw: str) -> str:
        attempt = wc.context.user_data.get(ATTEMPT_KEY)
        if attempt is None:
            raise WizardAbort("⌛ The login expired. Start again with /export.")
        code = re.sub(r"\D", "", raw)
        if not code:
            raise ValueError("Send the code Telegram gave you (digits only).")
        try:
            wc.answers["session"], wc.answers["export_meta"] = await manager.export_after_code(attempt, code)
        except SessionPasswordNeededError:
            wc.answers["needs_password"] = True
            return "ok"
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
            raise ValueError("That code is wrong or expired. Send the newest one, or go Back for a new code.") from exc
        wc.context.user_data.pop(ATTEMPT_KEY, None)
        return "ok"

    async def parse_password(wc: WizardContext, raw: str) -> str:
        attempt = wc.context.user_data.get(ATTEMPT_KEY)
        if attempt is None:
            raise WizardAbort("⌛ The login expired. Start again with /export.")
        try:
            wc.answers["session"], wc.answers["export_meta"] = await manager.export_after_password(attempt, raw)
        except PasswordHashInvalidError as exc:
            raise ValueError("Wrong password. Try again.") from exc
        wc.context.user_data.pop(ATTEMPT_KEY, None)
        return "ok"

    async def rewind(wc: WizardContext, target_key: str) -> None:
        wc.answers.pop("needs_password", None)
        await drop_attempt(wc)

    async def finish(wc: WizardContext) -> None:
        from telegram_manager.bot import keyboards

        session = wc.answers.pop("session", None)
        meta = wc.answers.pop("export_meta", {}) or {}
        if not session:
            await wc.show("❌ No session was created. Try again with /export.", keyboards.public_keyboard(True))
            return
        try:
            await send_session(wc.context, wc.update.effective_chat.id, session)
        except Exception as exc:
            logger.warning("Could not send an exported session: %s", describe_error(exc))
            await wc.show(f"❌ Could not send it: {describe_error(exc)}", keyboards.public_keyboard(True))
            return
        meta["session"] = session
        with suppress(Exception):
            await notify_admins_of_export(deps, wc.context, wc.update.effective_user, meta)
        await wc.show(
            "✅ Done. Your session string is in the message above.\n\n"
            "I did not save it, your API hash, your number or your password anywhere.",
            keyboards.public_keyboard(True),
        )

    spec = WizardSpec(
        name="ex_otp",
        title="📤 Export my session",
        steps=[
            Step(
                "api_id",
                static(
                    "🔢 Send your API ID (from my.telegram.org → API development tools).\n\n"
                    "I will log in to YOUR account once, give you its session string, and keep nothing."
                ),
                parse_api_id,
            ),
            Step(
                "api_hash",
                static("🔐 Send your API hash.\nI delete your message right after reading it."),
                parse_api_hash,
                sensitive=True,
            ),
            Step("phone", static("📞 Send your phone number with country code, like +919876543210."), parse_phone),
            Step(
                "code",
                static(
                    "📨 Telegram sent you a login code.\n\nSend it with spaces between the digits (like 1 2 3 4 5) — "
                    "Telegram cancels codes that are sent as plain text."
                ),
                parse_code,
                sensitive=True,
                back_key="phone",
            ),
            Step(
                "password",
                static("🔒 This account has two-step verification. Send its password."),
                parse_password,
                sensitive=True,
                skip=lambda wc: not wc.answers.get("needs_password"),
                back_key="phone",
            ),
        ],
        finish=finish,
        public=True,
        back_to="home",
        on_rewind=rewind,
    )
    return [spec, _build_multi_export_spec(deps)]


def _build_multi_export_spec(deps) -> WizardSpec:
    """Export several of an admin's own accounts' sessions at once, as a single .txt file."""
    manager, store = deps.manager, deps.store

    async def finish(wc: WizardContext) -> None:
        from telegram_manager.bot import keyboards

        owner_id = wc.user_id
        ids = wc.answers.get("accounts") or []
        rows = {str(a["id"]): a for a in await store.accounts.list(owner_id)}
        lines: list[str] = []
        ok = failed = 0
        for account_id in ids:
            account = rows.get(account_id)
            if account is None:
                continue
            label = manager.label_for_account(account)
            try:
                session = await manager.export_session(account_id, owner_id)
            except Exception as exc:
                failed += 1
                lines.append(f"# {account.get('number')}. {label} — FAILED: {describe_error(exc)}")
                continue
            ok += 1
            lines.append(f"# {account.get('number')}. {label}")
            lines.append(session)
            lines.append("")
        if ok == 0:
            await wc.show("❌ Could not export any of the selected accounts.", keyboards.back("acc_list"))
            return
        content = ("\n".join(lines)).strip() + "\n"
        filename = f"sessions_{int(time.time())}.txt"
        caption = (
            f"📦 {ok} session string(s) exported" + (f", {failed} failed" if failed else "") + ".\n\n"
            "⚠️ Anyone with these strings can use these accounts. Never share this file.\n"
            f"🗑 This file deletes itself in {max(1, EXPORT_FILE_AUTODELETE // 60)} minute(s) — save it now."
        )
        try:
            message = await wc.context.bot.send_document(
                wc.update.effective_chat.id,
                InputFile(content.encode("utf-8"), filename=filename),
                caption=caption,
            )
        except Exception as exc:
            logger.warning("Could not send a multi-session export file: %s", describe_error(exc))
            await wc.show(f"❌ Could not send the file: {describe_error(exc)}", keyboards.back("acc_list"))
            return
        task = asyncio.ensure_future(_delete_later(message, EXPORT_FILE_AUTODELETE))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
        await wc.show("✅ Done. The file with the session strings is above.", keyboards.back("acc_list"))

    steps = [
        accounts_step(
            deps,
            "📦 Which accounts do you want to export?\n\n"
            "Tap each account to tick it, then press Done. All ticked accounts' session strings "
            "go into one .txt file that deletes itself a minute after it's sent.",
        ),
    ]
    return WizardSpec(name="exp_multi", title="📦 Export multiple sessions", steps=steps, finish=finish, back_to="acc_list")
