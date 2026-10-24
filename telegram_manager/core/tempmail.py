"""Temporary mailboxes on 1secmail (https://www.1secmail.com/api): make an address, look at its inbox, find the OTP.

Pure helpers (`extract_otp`, `html_to_text`, `split_address`) plus a few small async calls. The network goes through
`_api`, which the tests replace. A mailbox needs no account: the address itself is the key, so only addresses the bot
made for the admin are ever read."""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import string
from typing import Any

logger = logging.getLogger(__name__)

API_URL = "https://www.1secmail.com/api/v1/"
FALLBACK_DOMAINS = ("1secmail.com", "1secmail.org", "1secmail.net", "esiix.com", "wwjmp.com", "xojxe.com", "yoggm.com")
API_TIMEOUT = 20
API_TRIES = 2
MAX_SEEN = 60
MAX_NEW_SHOWN = 5
_ADDRESS = re.compile(r"^[a-z0-9][a-z0-9._-]{2,40}@([a-z0-9-]+\.)+[a-z]{2,10}$")


class TempMailError(Exception):
    """1secmail could not be used; the text is meant for the admin."""


# --- addresses ---------------------------------------------------------------------------------------------------
def split_address(address: str) -> tuple[str, str]:
    text = (address or "").strip().lower()
    if not _ADDRESS.match(text):
        raise TempMailError("That is not a valid mail address.")
    login, _, domain = text.partition("@")
    return login, domain


def random_address(domain: str | None = None) -> str:
    login = random.choice(string.ascii_lowercase) + "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
    return f"{login}@{domain or random.choice(FALLBACK_DOMAINS)}"


# --- OTP ---------------------------------------------------------------------------------------------------------
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_CODE = r"(\d{3}[ -]\d{3}|\d{4,8})"
_AFTER_WORD = re.compile(
    r"(?i)(?:code|otp|pin|passcode|verification|verify|confirm\w*|token|one[- ]time|kod|login)\D{0,40}?" + _CODE + r"(?!\d)")
_BEFORE_WORD = re.compile(r"(?i)(?<![\d.,-])" + _CODE + r"(?!\d)\s*(?:is|as|:)?\s*(?:your|the|a)?\s*(?:code|otp|pin|passcode|verification)")
_ANY = re.compile(r"(?<![\w.,/-])(\d{4,8})(?![\w,/-]|\.\d)")


def html_to_text(raw: str) -> str:
    text = _TAGS.sub(" ", raw or "")
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(text)).strip()


def _clean(code: str) -> str:
    return re.sub(r"[ -]", "", code)


def extract_otp(subject: str, body: str) -> str | None:
    """The one-time code in a mail, or None. Looks for a number next to words like code / OTP / verification first
    (the subject before the text), and only then for a lone 4-8 digit number (never a year, never inside a link)."""
    for chunk in (subject or "", html_to_text(_URL.sub(" ", body or ""))):
        chunk = _URL.sub(" ", chunk)
        for pattern in (_AFTER_WORD, _BEFORE_WORD):
            match = pattern.search(chunk)
            if match:
                return _clean(match.group(1))
    for chunk in (subject or "", html_to_text(_URL.sub(" ", body or ""))):
        for match in _ANY.finditer(_URL.sub(" ", chunk)):
            code = match.group(1)
            if len(code) == 4 and code[:2] in ("19", "20"):
                continue
            return code
    return None


# --- the 1secmail API --------------------------------------------------------------------------------------------
async def _api(params: dict[str, Any]) -> Any:
    """One API call, retried once. Returns the decoded JSON."""
    try:
        import httpx  # imported here like the other network modules, so importing the bot never needs it
    except ImportError:
        raise TempMailError("The httpx package is missing on the server (pip install -r requirements.txt).")
    last: Exception | None = None
    for attempt in range(API_TRIES):
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(API_URL, params=params)
            if response.status_code >= 400:
                raise TempMailError(f"1secmail answered with error {response.status_code}.")
            return response.json()
        except TempMailError as exc:
            last = exc
        except (httpx.HTTPError, ValueError) as exc:
            last = TempMailError(f"Could not reach 1secmail ({type(exc).__name__}).")
        if attempt + 1 < API_TRIES:
            await asyncio.sleep(1)
    raise last or TempMailError("1secmail did not answer.")


async def new_mailbox() -> str:
    """A fresh address. The API picks one; when it is down a random one is made locally (any 1secmail address works)."""
    try:
        data = await _api({"action": "genRandomMailbox", "count": 1})
        if isinstance(data, list) and data and _ADDRESS.match(str(data[0]).lower()):
            return str(data[0]).lower()
    except TempMailError as exc:
        logger.warning("1secmail could not make an address, making one locally: %s", exc)
    return random_address()


async def list_messages(address: str) -> list[dict[str, Any]]:
    login, domain = split_address(address)
    data = await _api({"action": "getMessages", "login": login, "domain": domain})
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


async def read_message(address: str, message_id: int | str) -> dict[str, Any]:
    login, domain = split_address(address)
    data = await _api({"action": "readMessage", "login": login, "domain": domain, "id": message_id})
    if not isinstance(data, dict):
        raise TempMailError("That mail could not be read.")
    return data


async def check_inbox(address: str, seen: set[str]) -> list[dict[str, Any]]:
    """The mails that are new since `seen` (ids as text), newest first, at most MAX_NEW_SHOWN, each with its OTP if there
    is one: {'id', 'from', 'subject', 'date', 'otp', 'preview'}. Mails that cannot be read are skipped."""
    rows = [row for row in await list_messages(address) if str(row.get("id")) not in seen]
    rows.sort(key=lambda row: int(row.get("id") or 0), reverse=True)
    found: list[dict[str, Any]] = []
    for row in rows[:MAX_NEW_SHOWN]:
        try:
            full = await read_message(address, row["id"])
        except TempMailError:
            continue
        body = full.get("textBody") or full.get("body") or full.get("htmlBody") or ""
        subject = str(full.get("subject") or row.get("subject") or "")
        text = html_to_text(body)
        found.append({
            "id": str(row["id"]), "from": str(full.get("from") or row.get("from") or ""), "subject": subject,
            "date": str(full.get("date") or row.get("date") or ""), "otp": extract_otp(subject, body),
            "preview": " ".join(text.split())[:300],
        })
    return found


# --- the admin's current mailbox (kept in their settings, so a restart does not lose it) -----------------------------
async def mailbox_for(settings, owner_id: int, new: bool = False) -> dict[str, Any]:
    """{'address', 'created', 'seen'}: the saved mailbox, or a fresh one when none is saved / `new` is set."""
    import time

    saved = await settings.get_tempmail(owner_id)
    if saved.get("address") and not new:
        try:
            split_address(saved["address"])
            return saved
        except TempMailError:
            pass
    cfg = {"address": await new_mailbox(), "created": time.time(), "seen": []}
    await settings.set_tempmail(owner_id, cfg)
    return cfg


async def fetch_new(settings, owner_id: int, rounds: int = 1, gap: float = 0) -> tuple[str, list[dict[str, Any]]]:
    """(address, new mails). Looks `rounds` times, `gap` seconds apart, until something arrives; what is returned is
    remembered as seen, so the same mail is never shown twice."""
    cfg = await mailbox_for(settings, owner_id)
    seen = {str(i) for i in cfg.get("seen", [])}
    mails: list[dict[str, Any]] = []
    for attempt in range(max(1, rounds)):
        mails = await check_inbox(cfg["address"], seen)
        if mails or attempt + 1 >= rounds:
            break
        await asyncio.sleep(gap)
    if mails:
        cfg["seen"] = ([str(i) for i in cfg.get("seen", [])] + [m["id"] for m in mails])[-MAX_SEEN:]
        await settings.set_tempmail(owner_id, cfg)
    return cfg["address"], mails


def mail_html(mail: dict[str, Any]) -> str:
    """One received mail as a chat message (HTML): the OTP first when there is one."""
    esc = html.escape
    head = f"🔑 <b>OTP: <code>{esc(mail['otp'])}</code></b>" if mail.get("otp") else "📬 <b>New mail</b>"
    lines = [head, f"👤 {esc(mail['from'][:80])}", f"📝 {esc(mail['subject'][:120] or '(no subject)')}"]
    if not mail.get("otp") and mail.get("preview"):
        lines += ["", esc(mail["preview"])]
    return "\n".join(lines)


def card_html(address: str, status: str) -> str:
    return (f"📧 <b>Temp Mail</b> · 1secmail\n<code>{html.escape(address)}</code>\n"
            f"<i>Address par tap karo = copy</i>\n\n{status}")
