"""What state is an account in? Names, icons and the two judgements that need a little text reading.

Pure functions (no Telegram, no database), shared by the manager, the panel and the AI.
"""
from __future__ import annotations

# Worst first: this is also the order of "problems first" when a list is sorted by status.
STATUSES = ("banned", "deleted", "logged_out", "limited", "offline", "disabled", "online")
UNUSABLE = ("banned", "deleted", "logged_out")  # the session can never work again
PROBLEMS = UNUSABLE + ("limited",)

ICON = {"banned": "⛔", "deleted": "🗑", "logged_out": "🚪", "limited": "⚠️", "offline": "🔴", "disabled": "⏸", "online": "🟢"}
WORD = {
    "banned": "banned", "deleted": "deleted", "logged_out": "logged out", "limited": "limited (spam)",
    "offline": "offline", "disabled": "disabled", "online": "online",
}


def classify_dead(kind: str, reason: str) -> str:
    """banned / deleted / logged_out for a session Telegram refused. `kind` is the error class name."""
    text = f"{kind} {reason}".lower()
    if "ban" in text:
        return "banned"
    if "deactivated" in text or "deleted" in text:
        return "deleted"
    return "logged_out"


def parse_spambot(text: str) -> tuple[str, str]:
    """What @SpamBot answered -> ("ok" | "limited" | "unknown", short note). The bot's own wording decides."""
    lowered = (text or "").lower()
    note = " ".join((text or "").split())[:160]
    if "no limits" in lowered or "free as a bird" in lowered or "good news" in lowered:
        return "ok", ""
    if any(word in lowered for word in ("limited", "restrict", "unfortunately", "violat", "spam")):
        return "limited", note
    return "unknown", note


def badge(status: str) -> str:
    """The short marker the accounts list prints after an account (offline keeps its old wording)."""
    return f"{ICON.get(status, '')} {WORD.get(status, status)}".strip() if status != "online" else ICON["online"]
