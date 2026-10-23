"""Pure text parsing for commands and wizard answers (no Telegram imports)."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime

from telegram_manager.constants import (
    AUTOCLICK_MAX_BUTTONS,
    BULK_DELAY_MAX,
    BULK_DELAY_MIN,
    DRIFT_MAX,
    MAX_TIMES_PER_DAY,
    WHEN_MAX_MINUTES,
)

_USERNAME = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")
_CHAT_ID = re.compile(r"^-?\d{5,20}$")
_PHONE = re.compile(r"^\+\d{7,15}$")
_TAIL_CONFIRM = re.compile(r"(?:^|\s)(y|yes|n|no)\s*$", re.I)
_TAIL_DELAY = re.compile(r"(?:^|\s)(\d{1,4})(?:s|sec|secs)\s*$", re.I)
_TAIL_BUTTON = re.compile(r"(?:^|\s)(?:\[([^\[\]]{1,40})\]|\(([^()]{1,40})\))\s*$")
_ANY_BUTTON = re.compile(r"\[([^\[\]]{1,40})\]|\(([^()]{1,40})\)")


def mask_phone(phone: str) -> str:
    """+919876543210 -> +91••••••3210: enough to tell accounts apart without showing the number."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return ""
    if len(digits) < 8:
        return "•" * (len(digits) - 2) + digits[-2:]
    return f"+{digits[:2]}{'•' * (len(digits) - 6)}{digits[-4:]}"


def looks_like_telethon_session(value: str) -> bool:
    """Rough shape check before spending a Telegram request on it: a Telethon StringSession is
    base64 and reasonably long. This cannot prove the session is valid or even that it is a
    Telethon session (only Telegram's own answer can) — it only catches obvious paste mistakes
    (an API hash, a phone number, an empty string) so the auto-detect step can fail fast."""
    value = (value or "").strip()
    if len(value) < 50 or any(ch.isspace() for ch in value):
        return False
    try:
        base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception:
        return False
    return True


def parse_target(text: str) -> str:
    """@username, numeric chat id or +phone. Returns it normalised."""
    value = (text or "").strip()
    if _USERNAME.match(value):
        return "@" + value.lstrip("@")
    if _CHAT_ID.match(value) or _PHONE.match(value):
        return value
    raise ValueError("Send a @username (5-32 letters, digits or _), a chat id, or a +phone number.")


def parse_int(text: str, label: str) -> int:
    value = (text or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return int(value)


def parse_delay(text: str, minimum: int = BULK_DELAY_MIN, maximum: int = BULK_DELAY_MAX) -> int:
    match = re.fullmatch(r"\s*(\d{1,4})\s*(?:s|sec|secs|seconds?)?\s*", text or "", re.I)
    if not match:
        raise ValueError("Send the delay as a whole number of seconds, for example 3.")
    value = int(match.group(1))
    if not minimum <= value <= maximum:
        raise ValueError(f"Delay must be between {minimum} and {maximum} seconds.")
    return value


def parse_minutes(text: str, maximum: int = WHEN_MAX_MINUTES) -> int:
    """\"now\", \"0\", \"15\", \"15m\", \"2h\" -> minutes to wait before sending (0 = now)."""
    value = (text or "").strip().lower()
    if value in ("now", "0"):
        return 0
    match = re.fullmatch(r"(\d{1,4})\s*(m|min|mins|minutes?|h|hr|hrs|hours?)?", value)
    if not match:
        raise ValueError("Send how long to wait, like 10 (minutes), 30m or 2h. Send 0 for now.")
    minutes = int(match.group(1)) * (60 if (match.group(2) or "m").startswith("h") else 1)
    if minutes > maximum:
        raise ValueError(f"The longest wait is {maximum // 60} hours.")
    return minutes


def parse_time(text: str) -> dtime:
    cleaned = (text or "").strip().replace(" ", "").upper()
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%I:%M:%S%p", "%H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    raise ValueError("Use a time like 10:30am, 22:15 or 6:42:30pm.")


def format_clock(value: dtime) -> str:
    """6:42 PM, or 6:42:30 PM when the start time has seconds."""
    return value.strftime("%I:%M:%S %p" if value.second else "%I:%M %p").lstrip("0")


def parse_drift(text: str, maximum: int = DRIFT_MAX) -> int:
    """Seconds the daily start time moves later every day: "0" (same time), "30", "30s", "90 sec"."""
    match = re.fullmatch(r"\s*(\d{1,13})\s*(?:s|sec|secs|seconds?)?\s*", text or "", re.I)
    if not match:
        raise ValueError(f"Send a whole number of seconds from 0 to {maximum}, for example 30.")
    value = int(match.group(1))
    if value > maximum:
        raise ValueError(f"The most is {maximum} seconds.")
    return value


def parse_count(text: str, maximum: int = MAX_TIMES_PER_DAY) -> int:
    value = (text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= maximum:
        raise ValueError(f"Send a number between 1 and {maximum}.")
    return int(value)


def parse_button_names(raw: str) -> list[str]:
    """Button names to click: "[follow] (join)", "follow, join" or one per line -> ["follow", "join"]."""
    text = (raw or "").strip()
    found = [(a or b).strip() for a, b in _ANY_BUTTON.findall(text)]
    names = found or [part.strip() for part in re.split(r"[,\n]+", text)]
    names = [name for name in names if name]
    if not names:
        raise ValueError("Send at least one button name, like follow or join.")
    if len(names) > AUTOCLICK_MAX_BUTTONS:
        raise ValueError(f"At most {AUTOCLICK_MAX_BUTTONS} button names at a time.")
    if any(len(name) > 40 for name in names):
        raise ValueError("A button name can be at most 40 characters.")
    return names


def split_trailing_buttons(raw: str) -> tuple[str, list[str]]:
    """"hello [follow] (join)" -> ("hello", ["follow", "join"]): button names written at the end."""
    remainder = (raw or "").rstrip()
    buttons: list[str] = []
    while True:
        match = _TAIL_BUTTON.search(remainder)
        if not match:
            break
        buttons.insert(0, (match.group(1) or match.group(2)).strip())
        remainder = remainder[: match.start()]
    if len(buttons) > AUTOCLICK_MAX_BUTTONS:
        raise ValueError(f"At most {AUTOCLICK_MAX_BUTTONS} button names at a time.")
    return remainder.strip(), buttons


@dataclass
class BulkArgs:
    target: str | None = None
    text: str | None = None
    delay: int | None = None
    confirmed: bool | None = None
    buttons: list[str] = field(default_factory=list)


def parse_bulk_args(kind: str, raw: str) -> BulkArgs:
    """Parse what follows /all, /alll, /multi, /click or /allblock.

    Shape:  <target> [message] [[button] (button) ...] [3sec] [Y|N]     (block has no message)
    Trailing pieces can come in any order: Y/N answers the confirmation, "3sec" is the delay
    between accounts, and each trailing [name] or (name) is the name of an inline button to
    click after the message (the bot's reply is searched for a button containing that name).
    Everything is optional and missing pieces are asked step by step.
    """
    rest = (raw or "").strip()
    args = BulkArgs()
    if not rest:
        return args
    parts = rest.split(None, 1)
    args.target = parse_target(parts[0])
    remainder = parts[1] if len(parts) > 1 else ""
    while True:
        confirm = _TAIL_CONFIRM.search(remainder) if args.confirmed is None else None
        if confirm:
            args.confirmed = confirm.group(1).lower().startswith("y")
            remainder = remainder[: confirm.start()]
            continue
        delay = _TAIL_DELAY.search(remainder) if args.delay is None else None
        if delay:
            args.delay = int(delay.group(1))
            remainder = remainder[: delay.start()]
            continue
        button = _TAIL_BUTTON.search(remainder)
        if button:
            args.buttons.insert(0, (button.group(1) or button.group(2)).strip())
            remainder = remainder[: button.start()]
            continue
        break
    if len(args.buttons) > AUTOCLICK_MAX_BUTTONS:
        raise ValueError(f"At most {AUTOCLICK_MAX_BUTTONS} button names at a time.")
    message = remainder.strip()
    if kind == "block":
        if message or args.buttons:
            raise ValueError("/allblock only takes a user, an optional delay like 3sec, and Y/N.")
    elif kind == "click":
        if message:
            raise ValueError("/click only takes a user and button names like [follow] (join), then an optional delay and Y/N.")
    elif message:
        args.text = message
    return args


# --- account selectors: "all", "1,2,5", "1-5", "1 2 3", "all except 3" ---------------------
_SELECTOR = re.compile(r"^[\d,\-\s]+$")


def looks_like_selector(token: str) -> bool:
    """True for the first word of /send: all, 1, 1,2,5 or 1-5 (names are not accepted here: they may contain spaces)."""
    value = (token or "").strip().lower()
    return value == "all" or bool(value and _SELECTOR.match(value) and any(ch.isdigit() for ch in value))


def parse_selector(token: str, available: list[int]) -> list[int]:
    """Turn "1,2,5-7" or "all" into account numbers, keeping only numbers the admin really has.

    Raises ValueError for a malformed selector or a number that does not exist, so a typo
    never silently sends from the wrong accounts."""
    value = (token or "").strip().lower()
    have = sorted(set(available))
    if value == "all":
        if not have:
            raise ValueError("You have no accounts.")
        return have
    if not looks_like_selector(value):
        raise ValueError("Use all, or account numbers like 1,2,5 or 1-5.")
    wanted: list[int] = []
    for part in re.split(r"[,\s]+", value):
        if not part:
            continue
        if "-" in part:
            bits = part.split("-")
            if len(bits) != 2 or not bits[0].isdigit() or not bits[1].isdigit():
                raise ValueError(f"'{part}' is not a valid range. Example: 1-5")
            low, high = int(bits[0]), int(bits[1])
            if low > high or high - low > 500:
                raise ValueError(f"'{part}' is not a valid range. Example: 1-5")
            wanted.extend(range(low, high + 1))
        elif part.isdigit():
            wanted.append(int(part))
        else:
            raise ValueError(f"'{part}' is not an account number.")
    missing = sorted({n for n in wanted if n not in have})
    if missing:
        raise ValueError("No account with number: " + ", ".join(str(n) for n in missing) + ". Use /list to see yours.")
    seen: set[int] = set()
    return [n for n in wanted if not (n in seen or seen.add(n))]


# --- session file (.txt): one session per line --------------------------------------------
@dataclass
class SessionLine:
    session: str
    api_id: int | None = None
    api_hash: str | None = None


def parse_session_file(text: str, limit: int = 100) -> tuple[list[SessionLine], int]:
    """Read a .txt full of sessions. Understood per line:
        <session>
        <api_id>:<api_hash>:<session>        (also separated by space, comma or |)
    Lines starting with # (the export file's account labels) and blank lines are skipped.
    Returns (sessions, number of lines that were neither comments nor sessions)."""
    found: list[SessionLine] = []
    seen: set[str] = set()
    ignored = 0
    for raw in (text or "").splitlines():
        line = raw.strip().strip("\ufeff")
        if not line or line.startswith("#"):
            continue
        parts = [p for p in re.split(r"[\s,|:;]+", line) if p]
        api_id = api_hash = None
        if len(parts) == 3 and parts[0].isdigit() and len(parts[1]) >= 20:
            api_id, api_hash, session = int(parts[0]), parts[1], parts[2]
        elif len(parts) == 1:
            session = parts[0]
        else:
            ignored += 1
            continue
        if not looks_like_telethon_session(session):
            ignored += 1
            continue
        if session in seen:
            continue
        seen.add(session)
        found.append(SessionLine(session, api_id, api_hash))
    if len(found) > limit:
        raise ValueError(f"That file has {len(found)} sessions. Send at most {limit} at a time.")
    return found, ignored


# --- random daily shift: "30" (fixed) or "10-60" (random between, new value every day) -------
def parse_shift_range(text: str, maximum: int = DRIFT_MAX) -> tuple[int, int]:
    """"30" -> (30, 30); "10-60" or "10..60" -> (10, 60). Used for the daily shift of a broadcast."""
    value = (text or "").strip().lower().replace("sec", "").replace("s", "").replace("..", "-").replace(" ", "")
    match = re.fullmatch(r"(\d{1,13})(?:-(\d{1,13}))?", value)
    if not match:
        raise ValueError(f"Send a whole number of seconds like 30, or a random range like 10-60 (up to {maximum}).")
    low = int(match.group(1))
    high = int(match.group(2)) if match.group(2) else low
    if low > high:
        low, high = high, low
    if high > maximum:
        raise ValueError(f"The most is {maximum} seconds.")
    return low, high
