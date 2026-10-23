"""Connect an account: phone + code (with optional 2-step password), or a session string."""
from __future__ import annotations

import re

from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from telegram_manager.bot import keyboards
from telegram_manager.bot.flows.common import static
from telegram_manager.bot.wizard.engine import ATTEMPT_KEY
from telegram_manager.bot.wizard.spec import Step, WizardAbort, WizardContext, WizardSpec
from telegram_manager.errors import AccountAlreadyConnectedError, describe_error
from telegram_manager.parsing import looks_like_telethon_session, parse_int

_API_HELP = "Get it at my.telegram.org → API development tools."


def build_connect_specs(deps) -> list[WizardSpec]:
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
            phone = "+" + phone  # 919876543210 -> +919876543210
        if not re.fullmatch(r"\+\d{7,15}", phone):
            raise ValueError("Send the number with country code, like +919876543210.")
        await drop_attempt(wc)
        attempt = await manager.begin_login(wc.user_id, wc.answers["api_id"], wc.answers["api_hash"], phone)
        wc.context.user_data[ATTEMPT_KEY] = attempt
        return phone

    async def parse_code(wc: WizardContext, raw: str) -> str:
        attempt = wc.context.user_data.get(ATTEMPT_KEY)
        if attempt is None:
            raise WizardAbort("⌛ The login expired. Start again with Connect account.")
        code = re.sub(r"\D", "", raw)
        if not code:
            raise ValueError("Send the code Telegram gave you (digits only).")
        try:
            account = await manager.submit_code(attempt, code)
        except SessionPasswordNeededError:
            wc.answers["needs_password"] = True
            return "ok"
        except AccountAlreadyConnectedError as exc:
            raise WizardAbort(f"❌ {exc}") from exc
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
            raise ValueError("That code is wrong or expired. Send the newest one, or go Back for a new code.") from exc
        wc.context.user_data.pop(ATTEMPT_KEY, None)
        wc.answers["connected"] = manager.label_for_account(account)
        return "ok"

    async def parse_password(wc: WizardContext, raw: str) -> str:
        attempt = wc.context.user_data.get(ATTEMPT_KEY)
        if attempt is None:
            raise WizardAbort("⌛ The login expired. Start again with Connect account.")
        try:
            account = await manager.submit_password(attempt, raw)
        except AccountAlreadyConnectedError as exc:
            raise WizardAbort(f"❌ {exc}") from exc
        except PasswordHashInvalidError as exc:
            raise ValueError("Wrong password. Try again.") from exc
        wc.context.user_data.pop(ATTEMPT_KEY, None)
        wc.answers["connected"] = manager.label_for_account(account)
        return "ok"

    async def parse_session(wc: WizardContext, raw: str) -> str:
        value = raw.strip()
        if not looks_like_telethon_session(value):
            raise ValueError(
                "That doesn't look like a Telethon session string (it should be one long base64 "
                "piece of text with no spaces, usually 300+ characters). Paste the whole string, or "
                "go Back and connect with a phone number instead."
            )
        try:
            account = await manager.connect_with_session(
                wc.user_id, wc.answers["api_id"], wc.answers["api_hash"], value
            )
        except AccountAlreadyConnectedError as exc:
            raise WizardAbort(f"❌ {exc}") from exc
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(
                f"The format looked right, but Telegram refused it: {describe_error(exc)}"
            ) from exc
        wc.answers["connected"] = manager.label_for_account(account)
        return "ok"

    async def rewind(wc: WizardContext, target_key: str) -> None:
        wc.answers.pop("needs_password", None)
        await drop_attempt(wc)  # going back means requesting a fresh code

    async def finish(wc: WizardContext) -> None:
        await wc.show(
            f"✅ Connected: {wc.answers['connected']}\n\n"
            "The session is stored encrypted. Only you can see this account.",
            keyboards.after_connect(),
        )

    api_id = Step("api_id", static(f"🔢 Send your API ID.\n{_API_HELP}"), parse_api_id)
    api_hash = Step(
        "api_hash", static("🔐 Send your API hash.\nI delete your message right after reading it."), parse_api_hash,
        sensitive=True,
    )
    otp = WizardSpec(
        name="cx_otp",
        title="📱 Connect with phone number",
        steps=[
            api_id,
            api_hash,
            Step("phone", static("📞 Send the phone number with country code, like +919876543210."), parse_phone),
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
        back_to="home",
        on_rewind=rewind,
    )
    session = WizardSpec(
        name="cx_session",
        title="🔑 Connect with any session (auto-detect)",
        steps=[
            api_id,
            api_hash,
            Step(
                "session",
                static(
                    "🔑 Paste the session string.\n\nI'll auto-detect whether it looks like a valid "
                    "Telethon session before trying to log in with it, and delete your message right "
                    "after reading it."
                ),
                parse_session,
                sensitive=True,
            ),
        ],
        finish=finish,
        back_to="home",
    )
    return [otp, session]
