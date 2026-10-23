"""The account list the way an admin asks for it: filter, find, sort, and lay out in neat lines.

Pure functions over plain dicts (no Telegram, no database), so every rule here is easy to test. The AI's
tools in tools.py call these and put the result on the admin's screen; the model never has to retype a list.
Panel numbers (#1, #2 ...) are fixed: sorting and filtering only change the ORDER they appear in.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from telegram_manager.account_status import ICON, PROBLEMS, STATUSES, UNUSABLE, WORD
from telegram_manager.parsing import mask_phone

MAX_BLOCK = 3800  # a Telegram message holds 4096 characters; keep a margin
MAX_FOR_MODEL = 80  # accounts handed to the model in one answer

# Words an admin may use (Hindi / Hinglish / English) -> the status filter we understand.
STATUS_ALIASES = {
    "all": "all", "sab": "all", "sabhi": "all", "everything": "all", "": "all",
    "online": "online", "on": "online", "active": "online", "chalu": "online", "connected": "online",
    "offline": "offline", "disconnected": "offline", "band": "problem", "closed": "problem",
    "ban": "banned", "banned": "banned", "block": "banned", "blocked": "banned", "frozen": "banned", "freeze": "banned",
    "deleted": "deleted", "deactivated": "deleted", "delete": "deleted",
    "logged_out": "logged_out", "logout": "logged_out", "expired": "logged_out", "revoked": "logged_out", "session": "logged_out",
    "limited": "limited", "limit": "limited", "spam": "limited", "spambot": "limited", "restricted": "limited",
    "disabled": "disabled", "off": "disabled",
    "unusable": "unusable", "dead": "unusable", "mara": "unusable",
    "problem": "problem", "problems": "problem", "issue": "problem", "bad": "problem", "kharab": "problem", "faulty": "problem",
    "healthy": "healthy", "ok": "healthy", "good": "healthy", "theek": "healthy", "working": "healthy",
}
STATUS_FILTERS = ("all", "online", "offline", "banned", "deleted", "logged_out", "limited", "disabled", "unusable", "problem", "healthy")

SORT_ALIASES = {
    "": "number", "number": "number", "no": "number", "num": "number", "id": "number", "serial": "number", "default": "number",
    "name": "name", "naam": "name", "alphabet": "name", "alphabetical": "name", "az": "name", "a_z": "name", "z_a": "name",
    "username": "username", "user": "username",
    "phone": "phone", "mobile": "phone",
    "status": "status", "health": "status", "state": "status",
    "added": "added", "date": "added", "time": "added", "new": "added", "latest": "added",
}
SORT_LABEL = {
    "number": "number ke hisaab se", "name": "naam ke hisaab se (A-Z)", "username": "@username ke hisaab se",
    "phone": "phone number ke hisaab se", "status": "status ke hisaab se (problem wale pehle)", "added": "jodne ki date ke hisaab se",
}
DESC_WORDS = {"desc", "descending", "reverse", "ulta", "z_a", "za", "down", "latest", "newest", "naye", "last"}


def _key(value: Any) -> str:
    return re.sub(r"[\s\-]+", "_", str(value or "").strip().lower())


def resolve_status(value: Any) -> str:
    wanted = STATUS_ALIASES.get(_key(value))
    if wanted is None:
        raise ValueError("status must be one of: " + ", ".join(STATUS_FILTERS))
    return wanted


def resolve_sort(value: Any) -> str:
    wanted = SORT_ALIASES.get(_key(value))
    if wanted is None:
        raise ValueError("sort_by must be one of: number, name, username, phone, status, added")
    return wanted


def is_descending(value: Any) -> bool:
    return _key(value) in DESC_WORDS


def view_of(row: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    """One account as the admin and the AI see it. Keys starting with _ are private (sorting/search only)."""
    phone = str(row.get("phone") or "")
    created = row.get("created_at")
    added = created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else str(created or "")[:10]
    return {
        "number": row.get("number"),
        "name": row.get("display_name") or "Unnamed",
        "username": row.get("username") or "",
        "phone": row.get("phone_masked") or (mask_phone(phone) if phone else ""),
        "status": health["status"],
        "online": bool(health.get("online")),
        "note": health.get("note") or "",
        "checked": str(health.get("checked_at") or "")[:10],
        "added": added,
        "_digits": re.sub(r"\D", "", phone),
        "_added": str(created or ""),
    }


def public(view: dict[str, Any]) -> dict[str, Any]:
    """The same view without the private keys (what goes to the model, never a full phone number)."""
    return {k: v for k, v in view.items() if not k.startswith("_")}


def matches_status(view: dict[str, Any], wanted: str) -> bool:
    status = view["status"]
    return {
        "all": True,
        "unusable": status in UNUSABLE,
        "problem": status != "online",
        "healthy": status == "online",
    }.get(wanted, status == wanted)


def counts(views: list[dict[str, Any]]) -> dict[str, int]:
    found = {status: sum(1 for v in views if v["status"] == status) for status in STATUSES}
    return {status: n for status, n in found.items() if n}


def search(views: list[dict[str, Any]], query: str) -> tuple[list[dict[str, Any]], bool]:
    """Accounts matching every word of `query` in the name, @username, phone digits or number.
    Short digit words (up to 3 digits) mean the account NUMBER; 4+ digits are searched inside the phone.
    Nothing matched? A near-spelling ("Rahull" for "Rahul") is tried. Returns (matches, was_fuzzy)."""
    tokens = [t.lstrip("@") for t in re.split(r"[\s,]+", query.strip().lower()) if t.lstrip("@")]
    if not tokens:
        return list(views), False

    def hit(view: dict[str, Any], token: str) -> bool:
        if token in view["name"].lower() or token in view["username"].lower():
            return True
        if token.isdigit():
            return view["number"] == int(token) or (len(token) >= 4 and token in view["_digits"])
        return False

    found = [v for v in views if all(hit(v, t) for t in tokens)]
    if found:
        return found, False
    near = []
    for v in views:
        words = v["name"].lower().split() + [v["username"].lower()]
        if all(any(difflib.SequenceMatcher(None, t, w).ratio() >= 0.75 for w in words if w) for t in tokens):
            near.append(v)
    return near, bool(near)


def sort_views(views: list[dict[str, Any]], sort_by: str, descending: bool = False) -> list[dict[str, Any]]:
    rank = {status: i for i, status in enumerate(STATUSES)}
    keys = {
        "number": lambda v: (v["number"] or 0,),
        "name": lambda v: (v["name"].lower(), v["number"] or 0),
        "username": lambda v: (v["username"] == "", v["username"].lower(), v["number"] or 0),
        "phone": lambda v: (v["_digits"] == "", v["_digits"], v["number"] or 0),
        "status": lambda v: (rank.get(v["status"], 99), v["number"] or 0),
        "added": lambda v: (v["_added"], v["number"] or 0),
    }
    return sorted(views, key=keys[sort_by], reverse=descending)


def line(view: dict[str, Any]) -> str:
    """#3 Rahul (@rahul) · +91••••••12 · 🟢 online   (a note follows only when something is wrong)"""
    user = f" (@{view['username']})" if view["username"] else ""
    phone = f" · {view['phone']}" if view["phone"] else ""
    status = view["status"]
    note = f" — {view['note'][:70]}" if view["note"] and status not in ("online", "offline") else ""
    return f"{view['number'] or '?'}. {view['name']}{user}{phone} · {ICON[status]} {WORD[status]}{note}"


def summary_line(views: list[dict[str, Any]]) -> str:
    return " · ".join(f"{ICON[s]} {n} {WORD[s]}" for s, n in counts(views).items())


def render(views: list[dict[str, Any]], *, title: str, sort_by: str = "number", descending: bool = False,
           group_by: str = "", total: int | None = None, icon: str = "📋") -> list[str]:
    """The list as ready-to-send text blocks (each under Telegram's message limit), one account per line."""
    if not views:
        return []
    shown = f"{len(views)}" if total is None or total == len(views) else f"{len(views)} / {total}"
    order = f"{SORT_LABEL[sort_by]} {'↓' if descending else '↑'}"
    note = " · status ke group mein" if group_by == "status" else ""
    note += " · number = panel wala number" if sort_by != "number" else ""
    lines = [f"{icon} {title}: {shown}", f"↕️ {order}{note}", ""]
    if group_by == "status":
        for status in STATUSES:
            part = [v for v in views if v["status"] == status]
            if part:
                lines.append(f"{ICON[status]} {WORD[status].capitalize()} ({len(part)})")
                lines.extend(line(v) for v in sort_views(part, sort_by, descending))
                lines.append("")
    else:
        lines.extend(line(v) for v in views)
        lines.append("")
    lines.append(summary_line(views))
    blocks, current = [], ""
    for text in lines:
        if current and len(current) + len(text) + 1 > MAX_BLOCK:
            blocks.append(current.rstrip())
            current = ""
        current += text + "\n"
    blocks.append(current.rstrip())
    return blocks


__all__ = [
    "PROBLEMS", "STATUS_FILTERS", "MAX_FOR_MODEL", "counts", "is_descending", "line", "matches_status", "public",
    "render", "resolve_sort", "resolve_status", "search", "sort_views", "summary_line", "view_of",
]
