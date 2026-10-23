"""What the AI can do. Every tool wraps something the bot already does for its admin.

Reading and looking things up runs straight away. Sending, blocking and deleting stop and ask
the admin to tap ✅ first (confirm mode), or run at once in auto mode. Changes to the AI's own prompt,
giving accounts away and removing admins ALWAYS ask first, even in auto mode (nothing a chat message
says may quietly rewrite the AI's own rules). When the AI itself needs a
choice it calls ask_user and the admin answers with a button. An admin's tools only ever see that
admin's own accounts."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any, Awaitable, Callable

from telegram_manager.ai import account_tools, knowledge
from telegram_manager.ai.config import (
    DEFAULT_PROMPT,
    DEFAULT_REPLIES,
    MAX_KEYS,
    MAX_REPLIES,
    MODE_AUTO,
    MODE_CONFIRM,
    MODEL_NAME_RE,
    NAME_MAX,
    PROMPT_MAX,
    PROVIDER_OPENROUTER,
    PROVIDERS,
    AISettings,
    mask_key,
)
from telegram_manager.constants import BULK_DELAY_MAX, MAX_TIMES_PER_DAY, OPEN_CHAT_MIN_DELAY, START_TEXT_MAX
from telegram_manager.core.accountctl import parse_username
from telegram_manager.core.models import DailyJob
from telegram_manager.errors import describe_error
from telegram_manager.parsing import (
    looks_like_selector,
    parse_int,
    parse_selector,
    parse_target,
    parse_time,
)
from telegram_manager.store.settings import FORWARD_KEYS

logger = logging.getLogger(__name__)

PENDING_KEY = "ai_pending"
PENDING_LIMIT = 20
QUESTION_KEY = "ai_questions"
QUESTION_LIMIT = 10
ALL_WORDS = {"all", "sab", "sabhi", "saare", "sare", "everyone", "everything"}
FORWARD_KINDS = {"user": FORWARD_KEYS[0], "bot": FORWARD_KEYS[1], "channel": FORWARD_KEYS[2]}


@dataclass
class Outcome:
    """What the tools want shown to the admin besides the AI's own words."""

    pending: list[tuple[str, str]] = field(default_factory=list)  # (token, what needs approval)
    choices: list[tuple[str, str]] = field(default_factory=list)  # (button label, callback data)
    choices_title: str = "Kaunsa?"
    display: list[str] = field(default_factory=list)  # ready-made blocks (a list of accounts) sent to the admin as they are
    notes: list[str] = field(default_factory=list)  # one-line facts kept in the AI's memory ("showed accounts 1,4,5")
    questions: list[tuple[str, str, list[str]]] = field(default_factory=list)  # (question, token, option labels)
    wizard: str | None = None  # a step-by-step flow to start (adding an account, the 2-step password form)
    wizard_answers: dict = field(default_factory=dict)  # answers that flow starts with (which accounts, which action)


@dataclass
class ToolContext:
    deps: Any
    update: Any
    context: Any
    owner_id: int
    mode: str
    outcome: Outcome = field(default_factory=Outcome)

    @property
    def manager(self):
        return self.deps.manager

    @property
    def store(self):
        return self.deps.store


@dataclass
class Prepared:
    summary: str
    data: dict[str, Any]
    direct: bool = False  # run at once even in confirm mode (nothing is sent, or the admin picks by button)
    force_confirm: bool = False  # asks first even in auto mode (this call only)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    prepare: Callable[[ToolContext, dict], Awaitable[Prepared]]
    run: Callable[[ToolContext, dict], Awaitable[dict]]
    risky: bool = False
    always_confirm: bool = False  # asks first even in auto mode (cannot be undone)

    def declaration(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


# --- helpers -----------------------------------------------------------------------------
def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_STATUS = {"type": "string", "description": (
    "all (default) · online · offline · banned (Telegram ban / 'block') · deleted · logged_out · limited (spam limit) · disabled · "
    "unusable (banned+deleted+logged_out) · problem (everything not healthy) · healthy")}
_VIEW_PROPS = {
    "status": _STATUS,
    "query": {"type": "string", "description": "only accounts whose name / @username / number / phone digits match"},
    "sort_by": {"type": "string", "description": "number (default, panel order) · name (A-Z) · username · phone · status (problems first) · added (date added)"},
    "order": {"type": "string", "description": "asc (default) or desc (reverse, Z-A, newest / online first)"},
}
_ACCOUNTS = {
    "type": "array",
    "items": _STR,
    "description": "Account numbers, names or @usernames, or [\"all\"] for every account. Example: [\"1\",\"3\"] or [\"Rahul\"].",
}


def label(account: dict) -> str:
    return f"{account.get('number')}. {account.get('display_name') or 'Unnamed'}"


def labels(accounts: list[dict], limit: int = 6) -> str:
    shown = ", ".join(label(a) for a in accounts[:limit])
    return shown + (f" +{len(accounts) - limit} more" if len(accounts) > limit else "")


async def resolve_accounts(ctx: ToolContext, spec: Any, *, empty_means_all: bool = False) -> list[dict]:
    rows = [a for a in await ctx.store.accounts.list(ctx.owner_id) if a.get("enabled", True)]
    if not rows:
        raise ValueError("No account is connected yet.")
    items = spec if isinstance(spec, list) else ([spec] if spec else [])
    items = [str(item).strip() for item in items if str(item).strip()]
    if not items:
        if empty_means_all:
            return rows
        raise ValueError("Which account? Ask the admin for the account name or number.")
    if any(item.lower() in ALL_WORDS for item in items):
        return rows
    chosen: list[dict] = []
    for item in items:
        if looks_like_selector(item):
            numbers = parse_selector(item, [r["number"] for r in rows if r.get("number")])
            found = [r for r in rows if r.get("number") in numbers]
        else:
            row = await ctx.store.accounts.find_by_alias(ctx.owner_id, item) or await _guess_account(ctx, item)
            found = [row]
        chosen += [r for r in found if r not in chosen]
    return chosen


async def _account_views(ctx: ToolContext) -> tuple[list[dict], list[dict]]:
    """(rows, views) of EVERY account of this admin in the database: online, offline, banned, disabled, all."""
    rows = await ctx.store.accounts.list(ctx.owner_id)
    return rows, [account_tools.view_of(row, ctx.manager.account_health(row)) for row in rows]


async def _guess_account(ctx: ToolContext, item: str) -> dict:
    """An account named loosely ("rahul", a part of the name or the phone): the one that fits, or a clear question."""
    rows, views = await _account_views(ctx)
    found, _fuzzy = account_tools.search(views, item)
    if len(found) == 1:
        return next(row for row in rows if row.get("number") == found[0]["number"])
    if not found:
        raise ValueError(f"No account is called '{item}'. Call find_accounts or show_accounts to see them.")
    options = ", ".join(f"{v['number']} {v['name']}" for v in found[:8])
    raise ValueError(f"'{item}' fits {len(found)} accounts: {options}. Ask the admin which one with ask_user, or use its number.")


async def resolve_one(ctx: ToolContext, spec: Any) -> dict:
    accounts = await resolve_accounts(ctx, spec)
    if len(accounts) != 1:
        raise ValueError("Pick exactly one account for this.")
    return accounts[0]


def _target(value: Any) -> str:
    return parse_target(str(value or ""))


def _text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("The message text is empty. Ask the admin what to send.")
    if len(text) > 4000:
        raise ValueError("That message is too long (max 4000 characters).")
    return text


def _direct(summary: str, **data) -> Prepared:
    return Prepared(summary, data, direct=True)


def _ids(accounts: list[dict]) -> list[str]:
    return [str(a["id"]) for a in accounts]


# --- reading / looking up (run immediately) -------------------------------------------------
async def _prep_none(ctx, args) -> Prepared:
    return _direct("", **args)


def _parse_view_args(args: dict) -> dict:
    """status / query / sort_by / order / group_by as the tools below understand them (clear errors for the model)."""
    sort_by = account_tools.resolve_sort(args.get("sort_by"))
    group_by = "status" if str(args.get("group_by") or "").strip().lower() in ("status", "state", "health") else ""
    return {
        "status": account_tools.resolve_status(args.get("status")),
        "query": str(args.get("query") or "").strip(),
        "sort_by": sort_by,
        "desc": account_tools.is_descending(args.get("order")) or account_tools.is_descending(args.get("sort_by")),  # "Z-A" may come as either
        "group_by": group_by,
    }


async def _prep_view(ctx, args) -> Prepared:
    return _direct("", **_parse_view_args(args))


async def _selection(ctx, data) -> tuple[list[dict], list[dict], bool]:
    """(everything, the wanted accounts sorted, was the search fuzzy) for the view arguments."""
    _rows, views = await _account_views(ctx)
    chosen, fuzzy = (account_tools.search(views, data["query"]) if data["query"] else (list(views), False))
    chosen = [v for v in chosen if account_tools.matches_status(v, data["status"])]
    return views, account_tools.sort_views(chosen, data["sort_by"], data["desc"]), fuzzy


def _numbers(views: list[dict]) -> str:
    return ",".join(str(v["number"]) for v in views[:60]) + ("…" if len(views) > 60 else "")


async def _list_accounts(ctx, data) -> dict:
    """Data for the model: every account in the database (not shown to the admin)."""
    everything, chosen, fuzzy = await _selection(ctx, data)
    return {
        "total": len(everything), "matching": len(chosen), "counts": account_tools.counts(everything), "fuzzy_match": fuzzy,
        "accounts": [account_tools.public(v) for v in chosen[:account_tools.MAX_FOR_MODEL]],
        "truncated": len(chosen) > account_tools.MAX_FOR_MODEL,
    }


async def _show_accounts(ctx, data) -> dict:
    """Print the list on the admin's screen: filtered, sorted, one account per line."""
    everything, chosen, fuzzy = await _selection(ctx, data)
    if not chosen:
        return {"matching": 0, "total": len(everything), "counts": account_tools.counts(everything),
                "note": "Nothing matches. Tell the admin; the counts show what exists."}
    title = "Accounts"
    if data["status"] != "all":
        title += f" ({data['status'].replace('_', ' ')})"
    if data["query"]:
        title += f" “{data['query']}”"
    ctx.outcome.display += account_tools.render(
        chosen, title=title, sort_by=data["sort_by"], descending=data["desc"], group_by=data["group_by"], total=len(everything))
    ctx.outcome.notes.append(
        f"showed accounts (status={data['status']}{', search=' + data['query'] if data['query'] else ''}, sorted by {data['sort_by']}"
        f"{' desc' if data['desc'] else ''}): numbers {_numbers(chosen)}")
    return {"shown_to_admin": True, "matching": len(chosen), "total": len(everything), "counts": account_tools.counts(chosen),
            "numbers": [v["number"] for v in chosen[:account_tools.MAX_FOR_MODEL]],
            "note": "The list is already on the admin's screen. Do not retype it; add at most one short sentence."}


async def _prep_find_accounts(ctx, args) -> Prepared:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("What should I look for? A name, @username, a number, or the last digits of a phone.")
    return _direct("", **{**_parse_view_args(args), "query": query})


async def _find_accounts(ctx, data) -> dict:
    everything, chosen, fuzzy = await _selection(ctx, data)
    if not chosen:
        return {"matches": [], "total": len(everything), "note": "No account matches. Say so; offer show_accounts to see them all."}
    title = f"“{data['query']}”" + (" (milte-julte naam)" if fuzzy else "")
    ctx.outcome.display += account_tools.render(
        chosen, title=title, icon="🔎", sort_by=data["sort_by"], descending=data["desc"], total=len(everything))
    ctx.outcome.notes.append(f"searched “{data['query']}”: numbers {_numbers(chosen)}")
    return {"matches": [account_tools.public(v) for v in chosen[:account_tools.MAX_FOR_MODEL]], "fuzzy_match": fuzzy,
            "note": "The matches are already on the admin's screen; use their numbers for the next step."}


async def _prep_check(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"), empty_means_all=True)
    spam = args.get("spam_check") is not False
    data = {"accounts": _ids(accounts), "spam": spam}
    if not spam:
        return _direct("", **data)  # only asks each account who it is: reads, sends nothing
    return Prepared(
        f"🔍 {len(accounts)} account ki health check: {labels(accounts)}\n"
        "Har online account se @SpamBot ko /start jayega (spam-limit pata karne ke liye).", data)


async def _check_accounts(ctx, data) -> dict:
    message = ctx.update.effective_message
    if message is not None:
        with suppress(Exception):
            await message.reply_text(f"🔍 {len(data['accounts'])} account check ho rahe hain… thoda waqt lag sakta hai.")
    gate = asyncio.Semaphore(5)

    async def one(account_id: str) -> None:
        async with gate:
            try:
                await ctx.manager.check_account(ctx.owner_id, account_id, data["spam"])
            except Exception as exc:
                logger.warning("Health check failed for %s: %s", account_id, exc)

    await asyncio.gather(*(one(i) for i in data["accounts"]))
    rows, views = await _account_views(ctx)
    numbers = {row.get("number") for row in rows if row["id"] in data["accounts"]}
    checked = [v for v in views if v["number"] in numbers]
    ordered = account_tools.sort_views(checked, "status")
    ctx.outcome.display += account_tools.render(ordered, title="Health check", sort_by="status", group_by="status", total=len(views))
    bad = [v for v in ordered if v["status"] != "online"]
    ctx.outcome.notes.append(f"health check on {len(checked)} accounts; not fine: " + (_numbers(bad) or "none"))
    return {"status": "done", "message": f"✅ {len(checked)} account check hue: {account_tools.summary_line(checked)}",
            "counts": account_tools.counts(checked),
            "problems": [{k: v[k] for k in ("number", "name", "status", "note")} for v in bad][:40]}


async def _prep_guide(ctx, args) -> Prepared:
    return _direct("", topic=str(args.get("topic") or ""))


async def _panel_guide(ctx, data) -> dict:
    text = knowledge.guide(data["topic"])
    if text is None:
        return {"topics": knowledge.topic_names(), "note": "Pick one of these topics."}
    return {"topic": data["topic"], "guide": text}


async def _prep_read_chat(ctx, args) -> Prepared:
    account = await resolve_one(ctx, args.get("account"))
    return _direct("", account=str(account["id"]), chat=_target(args.get("chat")), limit=int(args.get("limit") or 10))


async def _read_chat(ctx, data) -> dict:
    return {"messages": await ctx.manager.read_chat(ctx.owner_id, data["account"], data["chat"], data["limit"])}


async def _prep_unread(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"), empty_means_all=True)
    return _direct("", accounts=_ids(accounts)[:12])


async def _unread(ctx, data) -> dict:
    out, skipped = [], 0
    for account_id in data["accounts"]:
        row = await ctx.store.accounts.get(account_id, owner_id=ctx.owner_id)
        if row is None or not ctx.manager.is_online(account_id):
            skipped += 1
            continue
        try:
            chats = await ctx.manager.unread_chats(ctx.owner_id, account_id)
        except Exception as exc:
            out.append({"account": label(row), "error": describe_error(exc)})
            continue
        if chats:
            out.append({"account": label(row), "chats": chats})
    return {"accounts_with_unread": out, "offline_skipped": skipped}


async def _prep_find(ctx, args) -> Prepared:
    account = await resolve_one(ctx, args.get("account"))
    return _direct("", account=str(account["id"]), query=str(args.get("query") or ""))


async def _find(ctx, data) -> dict:
    return {"chats": await ctx.manager.find_chats(ctx.owner_id, data["account"], data["query"])}


async def _prep_whois(ctx, args) -> Prepared:
    return _direct("", target=_target(args.get("target")))


async def _whois(ctx, data) -> dict:
    return await ctx.manager.lookup_user(ctx.owner_id, data["target"])


# --- sending and the risky ones -----------------------------------------------------------------
async def _prep_send(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    target, text = _target(args.get("target")), _text(args.get("text"))
    delay = max(OPEN_CHAT_MIN_DELAY, min(int(args.get("delay_seconds") or 5), BULK_DELAY_MAX))
    preview = text if len(text) <= 80 else text[:77] + "…"
    return Prepared(
        f"📨 Send to {target} from {len(accounts)} account(s) ({labels(accounts)}), {delay}s apart:\n“{preview}”",
        {"accounts": _ids(accounts), "target": target, "text": text, "delay": delay},
    )


async def _send(ctx, data) -> dict:
    await ctx.deps.bulk.launch(
        ctx.update, ctx.context, ctx.owner_id, "multi", data["target"], data["text"], data["delay"],
        account_ids=data["accounts"],
    )
    return {"status": "started", "message": None}  # the bulk runner shows its own live progress


async def _prep_block(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    target = _target(args.get("target"))
    return Prepared(f"🚫 Block {target} on {len(accounts)} account(s): {labels(accounts)}", {"accounts": _ids(accounts), "target": target})


async def _block(ctx, data) -> dict:
    result = await ctx.manager.block_selected(ctx.owner_id, data["accounts"], data["target"])
    return _bulk_message(f"🚫 {data['target']} blocked", result)


async def _prep_delchat(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    target = _target(args.get("target"))
    return Prepared(
        f"🗑 Delete the chat with {target} on {len(accounts)} account(s): {labels(accounts)} (their side stays)",
        {"accounts": _ids(accounts), "target": target},
    )


async def _delchat(ctx, data) -> dict:
    result = await ctx.manager.delete_chat_selected(ctx.owner_id, data["accounts"], data["target"])
    return _bulk_message(f"🗑 Chat with {data['target']} deleted", result)


async def _prep_unsend(ctx, args) -> Prepared:
    minutes = int(args.get("minutes") or 0)
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("Say how many minutes back (1 to 1440).")
    accounts = await resolve_accounts(ctx, args.get("accounts"), empty_means_all=True)
    count = ctx.manager.sent_count(ctx.owner_id, minutes, _ids(accounts))
    if not count:
        raise ValueError(f"The bot sent nothing in the last {minutes} minute(s) from those accounts.")
    return Prepared(
        f"↩️ Delete {count} message(s) sent in the last {minutes} min from {len(accounts)} account(s)",
        {"accounts": _ids(accounts), "minutes": minutes},
    )


async def _unsend(ctx, data) -> dict:
    result, deleted = await ctx.manager.unsend_recent(ctx.owner_id, data["minutes"], data["accounts"])
    return _bulk_message(f"↩️ {deleted} message(s) deleted", result)


def _bulk_message(done: str, result) -> dict:
    lines = [f"✅ {done} on {result.ok} of {result.total} account(s)."]
    if result.errors:
        lines += ["⚠️ Problems:"] + result.errors[:10]
    return {"status": "done" if not result.errors else "partly_done", "message": "\n".join(lines)}


# --- settings and broadcasts ---------------------------------------------------------------------
async def _prep_forwarding(ctx, args) -> Prepared:
    kind = str(args.get("kind") or "").lower()
    if kind not in FORWARD_KINDS:
        raise ValueError("kind must be user, bot or channel.")
    enabled = bool(args.get("enabled"))
    accounts = await resolve_accounts(ctx, args["account"]) if args.get("account") else None
    return _direct("", kind=kind, enabled=enabled, accounts=_ids(accounts) if accounts else None)


async def _forwarding(ctx, data) -> dict:
    key = FORWARD_KINDS[data["kind"]]
    state = "ON" if data["enabled"] else "OFF"
    if data["accounts"] is None:
        await ctx.store.settings.set_forwarding(ctx.owner_id, key, data["enabled"])
        return {"status": "done", "message": f"📥 {data['kind']} message forwarding is now {state} for all accounts."}
    for account_id in data["accounts"]:
        await ctx.manager.set_forward_override(account_id, key, data["enabled"])
    return {"status": "done", "message": f"📥 {data['kind']} message forwarding is now {state} on {len(data['accounts'])} account(s)."}


def _job_line(slot: int, job: DailyJob) -> dict:
    return {
        "slot": slot, "to": job.target, "text": job.text[:60], "time": job.start_time.strftime("%H:%M"),
        "per_day": job.times_per_day, "accounts": "all" if job.account_ids is None else len(job.account_ids),
    }


async def _list_broadcasts(ctx, data) -> dict:
    return {"broadcasts": [_job_line(slot, job) for slot, job in ctx.manager.daily_jobs_for(ctx.owner_id)]}


async def _prep_create_broadcast(ctx, args) -> Prepared:
    target, text = _target(args.get("target")), _text(args.get("text"))
    clock = parse_time(str(args.get("time") or ""))
    spec = args.get("accounts")
    everyone = [a for a in await ctx.store.accounts.list(ctx.owner_id) if a.get("enabled", True)]
    accounts = await resolve_accounts(ctx, spec, empty_means_all=True)
    delay = max(OPEN_CHAT_MIN_DELAY, min(int(args.get("delay_seconds") or 5), BULK_DELAY_MAX))
    per_day = max(1, min(int(args.get("times_per_day") or 1), MAX_TIMES_PER_DAY))
    low = max(0, int(args.get("shift_min_seconds") or 0))
    high = max(low, int(args.get("shift_max_seconds") or 0))
    shift = "same time every day" if not high else (f"+{low}s later every day" if low == high else f"random +{low}-{high}s later every day")
    covers_all = len(accounts) == len(everyone)
    return Prepared(
        f"🕒 Daily broadcast to {target} at {clock.strftime('%I:%M %p').lstrip('0')} IST, {per_day}×/day, "
        f"{'all accounts' if covers_all else labels(accounts)}, {shift}:\n“{text[:80]}”",
        {"target": target, "text": text, "time": clock.strftime("%H:%M:%S"), "delay": delay, "per_day": per_day,
         "accounts": None if covers_all else _ids(accounts), "low": low, "high": high},
    )


async def _create_broadcast(ctx, data) -> dict:
    hour, minute, second = (int(x) for x in data["time"].split(":"))
    job = DailyJob(
        data["target"], data["text"], data["delay"], dtime(hour, minute, second), data["per_day"],
        account_ids=data["accounts"], drift_seconds=data["low"], drift_max_seconds=data["high"],
    )
    slot = await ctx.manager.start_daily_job(ctx.owner_id, job)
    return {"status": "done", "slot": slot, "message": f"🕒 Daily broadcast #{slot} is set.\n\n{ctx.manager.daily_job_status(ctx.owner_id, slot)}"}


async def _prep_delete_broadcast(ctx, args) -> Prepared:
    jobs = ctx.manager.daily_jobs_for(ctx.owner_id)
    if not jobs:
        raise ValueError("There is no daily broadcast to delete.")
    slot = args.get("slot")
    if slot in (None, "", 0):
        return _direct("", choose=True)
    slot = int(slot)
    if not any(s == slot for s, _ in jobs):
        raise ValueError(f"There is no broadcast #{slot}. Call list_broadcasts.")
    job = next(j for s, j in jobs if s == slot)
    return Prepared(f"🗑 Delete daily broadcast #{slot} (to {job.target}, “{job.text[:40]}”)", {"slot": slot})


async def _delete_broadcast(ctx, data) -> dict:
    if data.get("choose"):
        for slot, job in ctx.manager.daily_jobs_for(ctx.owner_id):
            ctx.outcome.choices.append((f"🗑 #{slot} → {job.target}: {job.text[:24]}", f"aib|del|{slot}"))
        return {"status": "asked_the_admin_to_pick_one_with_buttons"}
    await ctx.manager.stop_daily_job(ctx.owner_id, data["slot"])
    return {"status": "done", "message": f"🗑 Daily broadcast #{data['slot']} deleted."}


async def _prep_add_account(ctx, args) -> Prepared:
    method = "session" if str(args.get("method") or "").lower().startswith("session") else "phone"
    return _direct("", method=method)


async def _add_account(ctx, data) -> dict:
    ctx.outcome.wizard = "cx_session" if data["method"] == "session" else "cx_otp"
    return {"status": "login_steps_will_start_in_the_chat"}



# --- chatting by itself ------------------------------------------------------------------------------
async def _prep_start_chat(ctx, args) -> Prepared:
    account = await resolve_one(ctx, args.get("account"))
    target = _target(args.get("target"))
    goal = str(args.get("goal") or "").strip()
    if len(goal) < 3:
        raise ValueError("What should the chat be about? Ask the admin for the goal.")
    opener = str(args.get("first_message") or "").strip()[:4000]
    autonomy = "auto" if str(args.get("autonomy") or "").lower() == "auto" else "draft"
    limit = max(1, min(int(args.get("max_replies") or DEFAULT_REPLIES), MAX_REPLIES))
    wanted = target.lower().lstrip("@")
    for running in await ctx.deps.chats.sessions(ctx.owner_id):
        if running["state"] != "ended" and running["account_id"] == str(account["id"]) and running["label"].lower().lstrip("@") == wanted:
            raise ValueError(f"An AI chat with {target} from {label(account)} is already {running['state']} ({running['replies_left']} replies left). "
                             "Do not start it again: tell the admin it is running, or call end_chat first if they want a new one.")
    how = "replies by itself" if autonomy == "auto" else "writes drafts, you approve each"
    return Prepared(
        f"💬 AI chat with {target} from {label(account)}: {how}, up to {limit} replies.\nGoal: {goal[:200]}"
        + (f"\nFirst message: “{opener[:100]}”" if opener else "\n(waits for them to write first)"),
        {"account": str(account["id"]), "target": target, "goal": goal, "opener": opener, "autonomy": autonomy, "limit": limit},
    )


async def _start_chat(ctx, data) -> dict:
    client = ctx.manager._client_for(data["account"], ctx.owner_id)
    entity = await ctx.manager._resolve_any_entity(client, data["target"])
    if getattr(entity, "first_name", None) is None:
        raise ValueError("The AI can only chat privately with a person, not in a group or channel.")
    peer_id = int(entity.id)
    session = await ctx.deps.chats.start(
        ctx.owner_id, data["account"], peer_id, data["target"], data["goal"], data["autonomy"], data["limit"], data["opener"]
    )
    if data["opener"]:
        await ctx.manager.send_to_any(data["account"], data["target"], data["opener"], ctx.owner_id)
    return {"status": "chat_started", "id": session["id"],
            "message": f"💬 AI chat shuru: {data['target']} (reply limit {session['replies_left']}). Band karne ke liye /aichats."}


async def _prep_end_chat(ctx, args) -> Prepared:
    return _direct("", target=str(args.get("target") or ""))


async def _end_chat(ctx, data) -> dict:
    ended = await ctx.deps.chats.end(ctx.owner_id, label=data["target"] or None)
    return {"status": "done", "ended": [s["label"] for s in ended],
            "message": f"⛔ {len(ended)} AI chat band." if ended else "Koi chalti hui AI chat nahi mili."}


async def _prep_resume_chat(ctx, args) -> Prepared:
    return _direct("", target=str(args.get("target") or ""))


async def _resume_chat(ctx, data) -> dict:
    resumed = await ctx.deps.chats.resume(ctx.owner_id, data["target"])
    return {"status": "done", "resumed": [s["label"] for s in resumed]} if resumed else {"error": "No paused chat matches that."}


async def _list_chats(ctx, data) -> dict:
    out = []
    for s in await ctx.deps.chats.sessions(ctx.owner_id):
        if s["state"] == "ended":
            continue
        row = await ctx.store.accounts.get(s["account_id"], owner_id=ctx.owner_id) or {}
        out.append({"with": s["label"], "account": row.get("number"), "state": s["state"], "autonomy": s["autonomy"],
                    "replies_left": s["replies_left"], "goal": s["goal"][:100]})
    return {"chats": out}


# --- more control ----------------------------------------------------------------------------------------
async def _prep_join(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    link = str(args.get("link") or "").strip()
    if not link:
        raise ValueError("Which group/channel? Ask for the invite link or @username.")
    return Prepared(f"➕ Join {link} with {len(accounts)} account(s): {labels(accounts)}", {"accounts": _ids(accounts), "link": link})


async def _join(ctx, data) -> dict:
    statuses: dict[str, str] = {}

    async def one(account_id: str) -> None:
        statuses[account_id] = await ctx.manager.join_with_status(account_id, data["link"], ctx.owner_id)

    result = await ctx.manager._for_each_account(ctx.owner_id, one, 3, "join", only_ids=data["accounts"])
    rows = {str(r["id"]): r for r in await ctx.store.accounts.list(ctx.owner_id)}
    out = _bulk_message("➕ Joined", result)
    out["message"] += "\n" + "\n".join(f"{label(rows.get(i, {}))}: {text}" for i, text in list(statuses.items())[:20])
    return out


async def _prep_force_join(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    bot = str(args.get("bot") or "").strip()
    if not bot:
        raise ValueError("Which bot asked to join first? Ask for its @username.")
    return Prepared(f"🔗 Force-join + verify via {bot} on {len(accounts)} account(s): {labels(accounts)}", {"accounts": _ids(accounts), "bot": bot})


async def _force_join(ctx, data) -> dict:
    result, reports = await ctx.manager.force_join_selected(ctx.owner_id, data["accounts"], data["bot"])
    lines = [f"{account_id}: " + "; ".join(steps) for account_id, steps in reports.items()]
    out = _bulk_message("🔗 Force-join done", result)
    out["message"] += "\n" + "\n".join(lines[:20])
    return out


async def _prep_send_later(ctx, args) -> Prepared:
    prepared = await _prep_send(ctx, args)
    minutes = int(args.get("minutes") or 0)
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("Say after how many minutes (1 to 1440).")
    prepared.data["minutes"] = minutes
    prepared.summary = prepared.summary.replace("📨 Send", f"⏰ In {minutes} min, send", 1)
    return prepared


async def _send_later(ctx, data) -> dict:
    await ctx.deps.bulk.launch(
        ctx.update, ctx.context, ctx.owner_id, "multi", data["target"], data["text"], data["delay"],
        account_ids=data["accounts"], start_in=data["minutes"] * 60,
    )
    return {"status": "scheduled", "message": None}


async def _prep_press(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    target = _target(args.get("target"))
    names = [str(b).strip() for b in (args.get("buttons") or []) if str(b).strip()]
    if not names:
        raise ValueError("Which button(s) to press? Ask for the button text.")
    return Prepared(f"🔘 Press {', '.join(names)} in {target} on {len(accounts)} account(s): {labels(accounts)}",
                    {"accounts": _ids(accounts), "target": target, "buttons": names})


async def _press(ctx, data) -> dict:
    await ctx.deps.bulk.launch(ctx.update, ctx.context, ctx.owner_id, "click", data["target"], None, OPEN_CHAT_MIN_DELAY,
                               account_ids=data["accounts"], buttons=data["buttons"])
    return {"status": "started", "message": None}


async def _prep_profile(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    fields = {k: str(args[k]).strip() for k in ("first_name", "last_name", "about") if args.get(k) is not None}
    if not fields:
        raise ValueError("What to change: first_name, last_name or about?")
    if any(len(fields.get(k, "")) > 64 for k in ("first_name", "last_name")) or len(fields.get("about", "")) > 70:
        raise ValueError("Names can be 64 characters, the bio 70.")
    return Prepared(f"🪪 Change profile ({', '.join(fields)}) on {len(accounts)} account(s): {labels(accounts)}",
                    {"accounts": _ids(accounts), **fields})


async def _profile(ctx, data) -> dict:
    fields = {k: data[k] for k in ("first_name", "last_name", "about") if k in data}
    done, errors = 0, []
    for account_id in data["accounts"]:
        try:
            await ctx.manager.update_profile(account_id, ctx.owner_id, **fields)
            done += 1
        except Exception as exc:
            errors.append(describe_error(exc))
    return {"status": "done" if not errors else "partly_done",
            "message": f"🪪 Profile changed on {done} of {len(data['accounts'])} account(s)." + ("\n⚠️ " + "; ".join(errors[:5]) if errors else "")}


async def _prep_logout(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    return Prepared(f"⚠️ LOG OUT and remove {len(accounts)} account(s): {labels(accounts)}", {"accounts": _ids(accounts)})


async def _logout(ctx, data) -> dict:
    done = 0
    for account_id in data["accounts"]:
        if await ctx.manager.remove_account(account_id, ctx.owner_id):
            done += 1
    return {"status": "done", "message": f"🚪 {done} account(s) logged out and removed."}


# --- asking the admin with buttons -------------------------------------------------------------------
async def _prep_ask(ctx, args) -> Prepared:
    question = str(args.get("question") or "").strip()
    if not question:
        raise ValueError("What is the question?")
    options: list[str] = []
    for item in args.get("options") or []:
        text = str(item).strip()[:60]
        if text and text not in options:
            options.append(text)
    if not 2 <= len(options) <= 8:
        raise ValueError("Give 2 to 8 different short options (for yes/no use two options).")
    return _direct("", question=question[:300], options=options)


async def _ask(ctx, data) -> dict:
    waiting = ctx.context.user_data.setdefault(QUESTION_KEY, {})
    while len(waiting) >= QUESTION_LIMIT:
        waiting.pop(next(iter(waiting)))
    token = secrets.token_hex(4)
    waiting[token] = (data["question"], list(data["options"]))
    ctx.outcome.questions.append((data["question"], token, list(data["options"])))
    return {"status": "question_shown_with_buttons",
            "note": "The admin will tap a button and it comes back as their next message. Stop now; do not answer for them."}


def take_answer(context, token: str, index: int) -> tuple[str, str] | None:
    """(question, chosen option) for a tapped question button; None when it expired or was used already."""
    entry = context.user_data.get(QUESTION_KEY, {}).pop(token, None)
    if entry is None or not 0 <= index < len(entry[1]):
        return None
    return entry[0], entry[1][index]


# --- the whole picture, and the AI's own settings -------------------------------------------------------
async def _load_ai(ctx) -> AISettings:
    return AISettings.from_dict(await ctx.store.settings.get_ai(ctx.owner_id))


async def _overview(ctx, data) -> dict:
    accounts = await ctx.store.accounts.list(ctx.owner_id)
    online = sum(1 for a in accounts if ctx.manager.is_online(str(a["id"])))
    forwarding = await ctx.store.settings.get_forwarding(ctx.owner_id)
    chats = ctx.deps.chats
    running = [s for s in (await chats.sessions(ctx.owner_id) if chats else []) if s["state"] != "ended"]
    settings = await _load_ai(ctx)
    return {
        "role": "owner" if ctx.deps.access.is_owner(ctx.owner_id) else "admin",
        "accounts": {"total": len(accounts), "online": online, "offline": len(accounts) - online},
        "forwarding": {kind: bool(forwarding[key]) for kind, key in FORWARD_KINDS.items()},
        "daily_broadcasts": len(ctx.manager.daily_jobs_for(ctx.owner_id)),
        "ai_chats_running": len(running),
        "ai": {"name": settings.name, "address": settings.address, "provider": settings.provider, "model": settings.active_model, "mode": settings.mode,
               "panel_mode": settings.ui_mode or "not_chosen", "keys_saved": len(settings.active_keys_enc)},
    }


async def _get_ai_settings(ctx, data) -> dict:
    s = await _load_ai(ctx)
    return {"name": s.name, "address": s.address, "provider": s.provider, "model": s.active_model, "mode": s.mode, "enabled": s.enabled,
            "keys_saved": len(s.active_keys_enc), "max_keys": MAX_KEYS, "prompt": s.prompt, "can_undo_prompt": bool(s.prompt_prev)}


async def _prep_change_settings(ctx, args) -> Prepared:
    changes: dict[str, str] = {}
    for field_name in ("name", "address"):
        if args.get(field_name) is not None:
            value = str(args[field_name]).strip()
            if not 1 <= len(value) <= NAME_MAX or "\n" in value:
                raise ValueError(f"{field_name} must be 1 to {NAME_MAX} characters on one line.")
            changes[field_name] = value
    current = await _load_ai(ctx)
    provider = current.provider
    if args.get("provider") is not None:
        provider = str(args["provider"]).strip().lower().replace("google gemini", "google").replace("gemini", "google")
        if provider not in PROVIDERS:
            raise ValueError("provider must be openrouter or google.")
        changes["provider"] = provider
    if args.get("model") is not None:
        model = str(args["model"]).strip().removeprefix("models/")
        if not re.fullmatch(MODEL_NAME_RE, model):
            raise ValueError("A model name has only letters, digits, . - _ / : (for example openrouter/free or gemini-flash-latest).")
        changes["or_model" if provider == PROVIDER_OPENROUTER else "model"] = model  # each provider keeps its own model
    if args.get("mode") is not None:
        mode = str(args["mode"]).strip().lower()
        if mode not in (MODE_CONFIRM, MODE_AUTO):
            raise ValueError("mode must be confirm or auto.")
        changes["mode"] = mode
    if not changes:
        raise ValueError("Say what to change: name, address, provider, model or mode.")
    shown = ", ".join(f"{k} → {v}" for k, v in changes.items())
    return Prepared(f"⚙️ AI settings: {shown}", changes)


async def _change_settings(ctx, data) -> dict:
    await ctx.store.settings.set_ai(ctx.owner_id, **data)
    return {"status": "done", "message": "✅ AI settings saved: " + ", ".join(f"{k} = {v}" for k, v in data.items())}


async def _prep_edit_prompt(ctx, args) -> Prepared:
    s = await _load_ai(ctx)
    action = str(args.get("action") or "").strip().lower().replace("-", "_")
    text, find = str(args.get("text") or "").strip(), str(args.get("find") or "")
    if action == "append":
        if len(text) < 3:
            raise ValueError("What should be added to the prompt?")
        new, summary = s.prompt.rstrip() + "\n" + text, f"➕ Prompt mein yeh jodna:\n“{text[:300]}”"
    elif action == "replace_text":
        if not find:
            raise ValueError("Which part of the prompt should change (find)? Call get_ai_settings to read the prompt.")
        if find not in s.prompt:
            raise ValueError("That text is not in the prompt. Call get_ai_settings and copy it exactly.")
        new = s.prompt.replace(find, text)
        summary = f"🔎 Prompt mein “{find[:80]}” ko “{text[:80]}” karna ({s.prompt.count(find)} jagah)"
    elif action == "replace_all":
        if len(text) < 20:
            raise ValueError("The new prompt is too short (at least 20 characters).")
        new, summary = text, f"✏️ Poora prompt badalna ({len(text)} akshar):\n“{text[:300]}”"
    elif action == "reset":
        new, summary = DEFAULT_PROMPT, "♻️ Prompt wapas default par"
    elif action == "undo":
        if not s.prompt_prev:
            raise ValueError("There is no earlier prompt to go back to.")
        new, summary = s.prompt_prev, "↩️ Pichhla prompt wapas lana"
    else:
        raise ValueError("action must be append, replace_text, replace_all, reset or undo.")
    if not 20 <= len(new) <= PROMPT_MAX:
        raise ValueError(f"The prompt must stay between 20 and {PROMPT_MAX} characters (this would be {len(new)}).")
    return Prepared(summary, {"prompt": new}, force_confirm=True)  # a prompt change always needs a tap


async def _edit_prompt(ctx, data) -> dict:
    old = (await _load_ai(ctx)).prompt
    await ctx.store.settings.set_ai(ctx.owner_id, prompt=data["prompt"], prompt_prev=old)
    return {"status": "done", "message": f"📝 Prompt badal gaya ({len(data['prompt'])} akshar). Wapas lene ke liye: “pichhla prompt wapas lao”."}


async def _list_keys(ctx, data) -> dict:
    s = await _load_ai(ctx)
    status = getattr(ctx.deps.ai_client(s), "status", None)
    rows = []
    for number, token in enumerate(s.active_keys_enc, start=1):
        try:
            key = ctx.manager.secret_box.decrypt(token)
        except Exception:
            rows.append({"number": number, "key": "unreadable"})
            continue
        rows.append({"number": number, "ends_with": mask_key(key), "status": status(key) if status else "unknown"})
    return {"provider": s.provider, "keys": rows, "max_keys": MAX_KEYS,
            "how_to_add": "You never see or add keys. The admin pastes them in this chat (saved at once, message deleted) or uses AI Settings → Keys."}


# --- screens that need a tap, a file or a secret ---------------------------------------------------------------
SCREENS: dict[str, tuple[str, str, bool]] = {  # name -> (button label, callback data, owner only)
    "accounts": ("📋 My accounts", "acc_list", False),
    "connect": ("🔗 Connect account", "connect", False),
    "export": ("📤 Export session", "exp|pick", False),
    "export_many": ("📦 Export multiple (.txt)", "exp|multi", False),
    "give": ("🎁 Give accounts", "give|start", False),
    "startscreen": ("🎨 Start screen (photo / GIF / video)", "sc|home", False),
    "forwarding": ("📥 Forwarding", "fw|home", False),
    "broadcasts": ("🕒 Daily broadcasts", "sched|home", False),
    "ai_settings": ("⚙️ AI settings", "ai|home", False),
    "keys": ("🔑 AI keys", "ai|keys", False),
    "prompt": ("📝 Prompt", "ai|prompts", False),
    "manual": ("🖐 Manual panel", "mode|manual", False),
    "admins": ("👑 Admins", "adm|home", True),
    "logs": ("📜 Live logs", "logs|link", True),
    "settings": ("⚙️ Settings", "set|home", False),
    "security": ("🔐 Account security", "sec|home", False),
    "channels": ("📢 Channels / groups", "sch|home", False),
}


async def _prep_open(ctx, args) -> Prepared:
    name = str(args.get("screen") or "").strip().lower()
    if name not in SCREENS:
        raise ValueError("screen must be one of: " + ", ".join(SCREENS))
    if SCREENS[name][2] and not ctx.deps.access.is_owner(ctx.owner_id):
        raise ValueError("Only the owner can open that screen.")
    return _direct("", screen=name)


async def _open(ctx, data) -> dict:
    title, callback, _ = SCREENS[data["screen"]]
    ctx.outcome.choices.append((title, callback))
    ctx.outcome.choices_title = "Yahan se khol lo 👇"
    return {"status": "button_shown_to_the_admin"}


# --- more of the panel: names, usernames, giving accounts, start screen, admins ----------------------------------
async def _prep_refresh(ctx, args) -> Prepared:
    if not await ctx.store.accounts.list(ctx.owner_id):
        raise ValueError("No account is connected yet.")
    return _direct("")


async def _refresh(ctx, data) -> dict:
    changed, failed, offline = await ctx.manager.refresh_accounts(ctx.owner_id)
    return {"status": "done", "message": f"🔄 {changed} name(s) changed, {failed} failed, {offline} offline (not read)."}


async def _prep_username(ctx, args) -> Prepared:
    account = await resolve_one(ctx, args.get("account"))
    name = parse_username(str(args.get("username") or ""))  # "" or "-" removes it
    what = f"@{name}" if name else "remove the username"
    return Prepared(f"🔤 {label(account)}: {what}", {"account": str(account["id"]), "username": name})


async def _username(ctx, data) -> dict:
    await ctx.manager.set_username(data["account"], ctx.owner_id, data["username"])
    return {"status": "done", "message": f"✅ Username {'@' + data['username'] if data['username'] else 'removed'}."}


def _who(access, user_id: int) -> str:
    if access.is_owner(user_id):
        return "👑 the owner"
    return f"🛡 {(access.admin(user_id) or {}).get('name') or 'Admin'} ({user_id})"


async def _prep_give(ctx, args) -> Prepared:
    access = ctx.deps.access
    accounts = await resolve_accounts(ctx, args.get("accounts"))
    if access.is_owner(ctx.owner_id):
        raw = str(args.get("to") or "").strip()
        target = int(raw) if raw.isdigit() else 0
        if access.admin(target) is None:
            raise ValueError("Which admin gets them? 'to' must be the Telegram id of one of the admins (call list_admins).")
    else:
        target = access.owner_id  # an admin can only give back to the owner
    return Prepared(
        f"🎁 Give {len(accounts)} account(s) to {_who(access, target)}: {labels(accounts)}\nUnke paas poora control chala jayega.",
        {"accounts": _ids(accounts), "to": target}, force_confirm=True,
    )


async def _give(ctx, data) -> dict:
    moved, failed = [], []
    for account_id in data["accounts"]:
        try:
            moved.append(await ctx.manager.transfer_account(account_id, ctx.owner_id, data["to"]))
        except Exception as exc:
            failed.append(describe_error(exc))
    text = f"🎁 {len(moved)} account(s) given to {_who(ctx.deps.access, data['to'])}."
    if failed:
        text += f"\n⚠️ {len(failed)} could not be moved: {failed[0]}"
    if moved:
        names = ", ".join(ctx.manager.label_for_account(row) for row in moved)
        try:
            await ctx.context.bot.send_message(
                data["to"], f"🎁 {_who(ctx.deps.access, ctx.owner_id)} gave you {len(moved)} account(s): {names}\nOpen 📋 My accounts to see them.")
        except Exception:
            text += "\n(Could not message them; ask them to open the bot.)"
    return {"status": "done" if not failed else "partly_done", "message": text}


async def _prep_start_text(ctx, args) -> Prepared:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("What should the welcome text say?")
    if len(text) > START_TEXT_MAX:
        raise ValueError(f"At most {START_TEXT_MAX} characters (this one has {len(text)}). {{name}} = the person's first name.")
    return Prepared(f"🎨 Start screen text (everybody sees it):\n“{text[:200]}”", {"text": text})


async def _start_text(ctx, data) -> dict:
    await ctx.store.settings.set_start_screen(text=data["text"])
    return {"status": "done", "message": "🎨 Welcome text saved."}


async def _prep_public_export(ctx, args) -> Prepared:
    enabled = bool(args.get("enabled"))
    return Prepared(f"📤 Public session export: {'ON' if enabled else 'OFF'}", {"enabled": enabled})


async def _public_export(ctx, data) -> dict:
    await ctx.store.settings.set_start_screen(public_export=data["enabled"])
    return {"status": "done", "message": f"📤 Public session export is now {'ON' if data['enabled'] else 'OFF'}."}


async def _prep_reset_start(ctx, args) -> Prepared:
    return Prepared("♻️ Start screen: default welcome text and no picture / GIF / video", {})


async def _reset_start(ctx, data) -> dict:
    await ctx.store.settings.clear_start_screen()
    return {"status": "done", "message": "♻️ Start screen is back to default."}


def _need_owner(ctx) -> None:
    if not ctx.deps.access.is_owner(ctx.owner_id):
        raise ValueError("Only the owner can manage admins.")


async def _prep_list_admins(ctx, args) -> Prepared:
    _need_owner(ctx)
    return _direct("")


async def _list_admins(ctx, data) -> dict:
    access = ctx.deps.access
    return {"owner": access.owner_id, "admins": [
        {"id": row["id"], "name": row.get("name") or "", "accounts": len(await ctx.store.accounts.list(int(row["id"])))}
        for row in access.admins()
    ]}


async def _prep_add_admin(ctx, args) -> Prepared:
    _need_owner(ctx)
    access = ctx.deps.access
    user_id = parse_int(str(args.get("user_id") or ""), "Telegram user ID")
    if access.is_owner(user_id):
        raise ValueError("That is the owner.")
    if access.is_admin(user_id):
        raise ValueError("That user is already an admin.")
    return Prepared(f"➕ Add {user_id} as an admin (they see only their own accounts)", {"user_id": user_id})


async def _add_admin(ctx, data) -> dict:
    user_id, bot, name = data["user_id"], ctx.context.bot, ""
    try:
        chat = await bot.get_chat(user_id)
        name = getattr(chat, "full_name", None) or getattr(chat, "username", None) or ""
    except Exception:
        logger.debug("Could not look up the new admin's name")
    await ctx.deps.access.add(user_id, ctx.owner_id, name)
    note = ""
    try:
        await bot.send_message(user_id, "👋 You were added as an admin. Send /start to begin.")
    except Exception:
        note = " I could not message them yet; ask them to open the bot and press Start."
    return {"status": "done", "message": f"✅ {name or user_id} is now an admin.{note}"}


async def _prep_remove_admin(ctx, args) -> Prepared:
    _need_owner(ctx)
    user_id = parse_int(str(args.get("user_id") or ""), "Telegram user ID")
    if ctx.deps.access.admin(user_id) is None:
        raise ValueError("That user is not one of the admins. Call list_admins.")
    count = len(await ctx.store.accounts.list(user_id))
    return Prepared(f"➖ Remove admin {user_id}: their {count} account(s) are logged out and deleted, their broadcasts stop.", {"user_id": user_id})


async def _remove_admin(ctx, data) -> dict:
    user_id, failed = data["user_id"], 0
    for account in await ctx.store.accounts.list(user_id):
        try:
            await ctx.manager.remove_account(str(account["id"]))
        except Exception:
            logger.exception("Could not remove an account while removing an admin")
            failed += 1
    if failed:
        return {"error": f"{failed} account(s) could not be logged out, so the admin was not removed. Try again."}
    await ctx.manager.stop_daily_job(user_id)
    await ctx.deps.access.remove(user_id)
    try:
        await ctx.context.bot.send_message(user_id, "You are no longer an admin of this bot.")
    except Exception:
        logger.debug("Could not tell the removed admin")
    return {"status": "done", "message": f"✅ Admin {user_id} was removed."}


# --- the registry -------------------------------------------------------------------------------------
TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("list_accounts",
         "Data for YOU about every account saved in the database (online, offline, banned, deleted, logged out, limited, disabled): number, name, "
         "@username, masked phone, status, note, last check, date added. Optional filter / search / sort. Nothing is shown to the admin; "
         "use show_accounts when the admin wants to SEE the list.",
         _obj(_VIEW_PROPS), _prep_view, _list_accounts),
    Tool("show_accounts",
         "SHOW the admin their accounts on screen, one per line (#number, name, @username, masked phone, status), filtered and sorted as they asked. "
         "Use it for: list dikhao, sajao / line mein / naam se / A-Z / online pehle / naye pehle, ban / block / limited / kharab / offline accounts. "
         "Reads the saved status; it never talks to Telegram. The list is printed for you, do not retype it.",
         _obj({**_VIEW_PROPS, "group_by": {"type": "string", "description": "status = one section per status (banned, limited, offline, online ...)"}}),
         _prep_view, _show_accounts),
    Tool("find_accounts",
         "Find accounts by a part of the name, @username, panel number, or the last 4+ digits of the phone. Forgives small spelling mistakes. "
         "The matches are shown to the admin and returned to you with their numbers.",
         _obj({**_VIEW_PROPS, "query": {"type": "string", "description": "e.g. rahul, @rahul_x, 12, 98765"}}, ["query"]), _prep_find_accounts, _find_accounts),
    Tool("check_accounts",
         "LIVE health check: asks Telegram if each account still works and (spam_check) asks @SpamBot if it is limited. Finds banned, deleted, "
         "logged-out and limited accounts and saves the result. accounts empty = all. Use it when the admin wants the real, current state.",
         _obj({"accounts": _ACCOUNTS, "spam_check": {"type": "boolean", "description": "default true; false only asks Telegram who the account is"}}),
         _prep_check, _check_accounts, risky=True),
    Tool("panel_guide",
         "Read your own manual about this bot for one topic before doing something big or unusual. "
         "Topics: accounts, sending, broadcasts, forwarding, ai_chats, health, admins, start_screen, modes, security, channels, bots_media.",
         _obj({"topic": _STR}, ["topic"]), _prep_guide, _panel_guide),
    Tool("read_chat", "Read the latest messages of one chat on one account.",
         _obj({"account": _STR, "chat": {"type": "string", "description": "@username or numeric chat id"}, "limit": _INT}, ["account", "chat"]),
         _prep_read_chat, _read_chat),
    Tool("unread_chats", "Show chats with unread messages. Leave accounts empty for all accounts.",
         _obj({"accounts": _ACCOUNTS}), _prep_unread, _unread),
    Tool("find_chat", "Search an account's chats by name or @username to get the chat id.",
         _obj({"account": _STR, "query": _STR}, ["account", "query"]), _prep_find, _find),
    Tool("whois", "Look up the id, name and type of a @username.", _obj({"target": _STR}, ["target"]), _prep_whois, _whois),
    Tool("send_message", "Send a text message to a user/chat from one or more accounts (opens the chat if needed).",
         _obj({"accounts": _ACCOUNTS, "target": {"type": "string", "description": "@username, chat id or +phone"}, "text": _STR,
               "delay_seconds": {"type": "integer", "description": "pause between accounts, min 3"}}, ["accounts", "target", "text"]),
         _prep_send, _send, risky=True),
    Tool("block_user", "Block a user on the named accounts.", _obj({"accounts": _ACCOUNTS, "target": _STR}, ["accounts", "target"]),
         _prep_block, _block, risky=True),
    Tool("delete_chat", "Delete a chat on the named accounts (only their side). For a group or channel this leaves it.", _obj({"accounts": _ACCOUNTS, "target": _STR}, ["accounts", "target"]),
         _prep_delchat, _delchat, risky=True),
    Tool("unsend_recent", "Delete the messages this bot sent in the last N minutes (for everybody). Leave accounts empty for all.",
         _obj({"minutes": _INT, "accounts": _ACCOUNTS}, ["minutes"]), _prep_unsend, _unsend, risky=True),
    Tool("set_forwarding", "Switch forwarding of incoming messages on or off, for all accounts or one.",
         _obj({"kind": {"type": "string", "description": "user, bot or channel"}, "enabled": {"type": "boolean"}, "account": _STR},
              ["kind", "enabled"]), _prep_forwarding, _forwarding),
    Tool("list_broadcasts", "List the daily broadcasts.", _obj({}), _prep_none, _list_broadcasts),
    Tool("create_daily_broadcast",
         "Schedule a message that goes out every day at a time (India time, 24h like 18:30; for every 6 hours use times_per_day=4). The daily shift moves it later every day, fixed or random between min and max seconds.",
         _obj({"target": _STR, "text": _STR, "time": {"type": "string", "description": "HH:MM India time"}, "accounts": _ACCOUNTS,
               "delay_seconds": _INT, "times_per_day": _INT, "shift_min_seconds": _INT, "shift_max_seconds": _INT}, ["target", "text", "time"]),
         _prep_create_broadcast, _create_broadcast, risky=True),
    Tool("delete_broadcast", "Delete a daily broadcast. Without a slot the admin is shown buttons to choose.",
         _obj({"slot": _INT}), _prep_delete_broadcast, _delete_broadcast, risky=True),
    Tool("start_chat",
         "Start an AI-run private chat with ONE person from ONE account. From then on the AI answers whatever that person writes back. "
         "autonomy=auto replies by itself; autonomy=draft (default) shows each reply to the admin first. first_message is optional.",
         _obj({"account": _STR, "target": _STR, "goal": {"type": "string", "description": "what the chat is about / what to achieve"},
               "first_message": _STR, "autonomy": {"type": "string", "description": "auto or draft"}, "max_replies": _INT},
              ["account", "target", "goal"]), _prep_start_chat, _start_chat, risky=True),
    Tool("end_chat", "Stop AI chats: with one person (target) or all when target is empty.", _obj({"target": _STR}), _prep_end_chat, _end_chat),
    Tool("resume_chat", "Continue an AI chat that paused to ask the admin.", _obj({"target": _STR}, ["target"]), _prep_resume_chat, _resume_chat),
    Tool("list_chats", "List the AI's running private chats.", _obj({}), _prep_none, _list_chats),
    Tool("force_join_verify", "A bot says 'join our channel(s) first, then press Verify': read the bot's last message, join every channel it names "
         "(with human pauses) and press its Verify/I-joined button, on the named accounts.",
         _obj({"accounts": _ACCOUNTS, "bot": _STR}, ["accounts", "bot"]), _prep_force_join, _force_join, risky=True),
    Tool("join_chat", "Join a group or channel with the named accounts: an invite link (t.me/+abc, joinchat, tg://join), a t.me/name link or a public @username. "
         "It says per account: joined · already in · request sent (the admin of the group must approve). Only a name, no link? use search_public first.",
         _obj({"accounts": _ACCOUNTS, "link": _STR}, ["accounts", "link"]), _prep_join, _join, risky=True),
    Tool("send_later", "Send a message after N minutes (1 to 1440) from the named accounts.",
         _obj({"accounts": _ACCOUNTS, "target": _STR, "text": _STR, "minutes": _INT, "delay_seconds": _INT}, ["accounts", "target", "text", "minutes"]),
         _prep_send_later, _send_later, risky=True),
    Tool("press_buttons", "Press inline buttons (by their text) under a bot's message in a chat, on the named accounts.",
         _obj({"accounts": _ACCOUNTS, "target": _STR, "buttons": {"type": "array", "items": _STR}}, ["accounts", "target", "buttons"]),
         _prep_press, _press, risky=True),
    Tool("set_profile", "Change the first name, last name or bio of the named accounts.",
         _obj({"accounts": _ACCOUNTS, "first_name": _STR, "last_name": _STR, "about": _STR}, ["accounts"]), _prep_profile, _profile, risky=True),
    Tool("logout_account", "Log an account out and remove it from the bot. Always asks the admin first.",
         _obj({"accounts": _ACCOUNTS}, ["accounts"]), _prep_logout, _logout, risky=True, always_confirm=True),
    Tool("add_account", "Start adding a new Telegram account (login steps run in the chat). method: phone or session.",
         _obj({"method": _STR}), _prep_add_account, _add_account),
    Tool("ask_user",
         "Ask the admin a question with TAP BUTTONS instead of typing it as text. Use it whenever you need a choice or a yes/no "
         "(which account, which person, which time, 'should I go ahead?'). 2 to 8 short options. The tap comes back as their next "
         "message. After calling it, stop and wait.",
         _obj({"question": _STR, "options": {"type": "array", "items": _STR, "description": "2-8 short button labels, e.g. [\"✅ Haan\", \"❌ Nahi\"]"}},
              ["question", "options"]), _prep_ask, _ask),
    Tool("get_overview", "One look at everything: role, accounts online/offline, forwarding switches, daily broadcasts, running AI chats, AI settings.",
         _obj({}), _prep_none, _overview),
    Tool("refresh_accounts", "Re-read every online account's name and @username from Telegram.", _obj({}), _prep_refresh, _refresh),
    Tool("set_username", "Set (or remove, with an empty username) the @username of one account.",
         _obj({"account": _STR, "username": {"type": "string", "description": "without @; empty string removes it"}}, ["account", "username"]),
         _prep_username, _username, risky=True),
    Tool("give_accounts",
         "Give accounts to another admin (the owner picks the admin by Telegram id) or, as an admin, back to the owner. Always asks first.",
         _obj({"accounts": _ACCOUNTS, "to": {"type": "string", "description": "admin's Telegram id (owner only; admins give to the owner)"}}, ["accounts"]),
         _prep_give, _give, risky=True, always_confirm=True),
    Tool("set_start_screen_text", "Set the welcome text everybody sees on /start ({name} = first name, max 800 characters).",
         _obj({"text": _STR}, ["text"]), _prep_start_text, _start_text, risky=True),
    Tool("set_public_export", "Switch on/off that anybody (not only admins) can export the session of their own account.",
         _obj({"enabled": {"type": "boolean"}}, ["enabled"]), _prep_public_export, _public_export, risky=True),
    Tool("reset_start_screen", "Back to the default welcome text and no picture/GIF/video.", _obj({}), _prep_reset_start, _reset_start, risky=True),
    Tool("list_admins", "Owner only: list the admins and how many accounts each has.", _obj({}), _prep_list_admins, _list_admins),
    Tool("add_admin", "Owner only: make a Telegram user an admin by numeric user id.", _obj({"user_id": _STR}, ["user_id"]),
         _prep_add_admin, _add_admin, risky=True),
    Tool("remove_admin", "Owner only: remove an admin (logs out and deletes THEIR accounts). Always asks first.",
         _obj({"user_id": _STR}, ["user_id"]), _prep_remove_admin, _remove_admin, risky=True, always_confirm=True),
    Tool("get_ai_settings", "Read your own settings: name, how you address the admin, provider (openrouter / google), model, mode, key count and the full prompt text.",
         _obj({}), _prep_none, _get_ai_settings),
    Tool("change_ai_settings", "Change your own name, how you address the admin (address), provider (openrouter or google), model (of the provider in use, or of the provider given together with it; OpenRouter takes any model id like openrouter/free), or mode (confirm = asks before risky actions, auto = does them at once).",
         _obj({"name": _STR, "address": _STR, "provider": {"type": "string", "description": "openrouter or google"}, "model": _STR, "mode": {"type": "string", "description": "confirm or auto"}}),
         _prep_change_settings, _change_settings, risky=True),
    Tool("edit_prompt",
         "Change your own prompt (instructions). action: append (add text at the end), replace_text (replace the exact text in `find` with `text`), "
         "replace_all (whole new prompt), reset (default prompt), undo (go back to the prompt before the last change). Always asks the admin first. "
         "Call get_ai_settings first to read the current prompt before editing it.",
         _obj({"action": _STR, "text": _STR, "find": _STR}, ["action"]), _prep_edit_prompt, _edit_prompt, risky=True),
    Tool("list_ai_keys", "Show how many API keys of the provider in use (OpenRouter or Gemini) are saved and whether each works (only the last 4 characters). You can never see or add keys.",
         _obj({}), _prep_none, _list_keys),
    Tool("open_screen",
         "Show a button that opens a panel screen. Use it for what needs a tap, a file or a secret: session export, giving accounts by list, start screen "
         "photo/GIF/video, connecting, admins, logs, forwarding, broadcasts, AI settings, keys, prompt, or the manual panel.",
         _obj({"screen": {"type": "string", "description": ", ".join(SCREENS)}}, ["screen"]), _prep_open, _open),
]}


def declarations() -> list[dict]:
    return [tool.declaration() for tool in TOOLS.values()]


async def _execute(tool: Tool, ctx: ToolContext, data: dict) -> dict:
    try:
        return await tool.run(ctx, data)
    except Exception as exc:
        logger.warning("AI tool %s failed: %s", tool.name, describe_error(exc))
        return {"error": describe_error(exc)}


async def call(ctx: ToolContext, name: str, args: dict | None) -> dict:
    """Run one tool the model asked for (or park it for approval). Always returns a dict for the model."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"There is no tool called {name}."}
    try:
        prepared = await tool.prepare(ctx, dict(args or {}))
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": describe_error(exc)}
    if tool.risky and not prepared.direct and (ctx.mode == MODE_CONFIRM or tool.always_confirm or prepared.force_confirm):
        pending = ctx.context.user_data.setdefault(PENDING_KEY, {})
        for waiting_name, waiting_data, _summary in pending.values():
            if waiting_name == name and waiting_data == prepared.data:  # the very same request is already on the admin's screen
                return {"status": "waiting_for_the_admin_to_approve",
                        "note": "This exact approval is ALREADY waiting on the admin's screen (an earlier card). Do not ask again and do not "
                                "call the tool again: say once, briefly, that you are waiting for their tap on that card."}
        while len(pending) >= PENDING_LIMIT:
            pending.pop(next(iter(pending)))
        token = secrets.token_hex(4)
        pending[token] = (name, prepared.data, prepared.summary)
        ctx.outcome.pending.append((token, prepared.summary))
        return {"status": "waiting_for_the_admin_to_approve",
                "note": "Approval buttons were shown. Do not say it is done; say you are waiting for approval."}
    return await _execute(tool, ctx, prepared.data)


async def execute_pending(ctx: ToolContext, token: str) -> dict | None:
    """The admin tapped ✅. None if the request is gone (expired or already used)."""
    entry = ctx.context.user_data.get(PENDING_KEY, {}).pop(token, None)
    if entry is None:
        return None
    name, data, _summary = entry
    return await _execute(TOOLS[name], ctx, data)


def pending_summary(ctx: ToolContext, token: str) -> str | None:
    entry = ctx.context.user_data.get(PENDING_KEY, {}).get(token)
    return entry[2] if entry else None


# The security / channel tools live in their own module; they import the helpers above, so they are added last.
from telegram_manager.ai import security_tools as _security_tools  # noqa: E402

TOOLS.update({tool.name: tool for tool in _security_tools.TOOLS})

from telegram_manager.ai import control_tools as _control_tools  # noqa: E402

TOOLS.update({tool.name: tool for tool in _control_tools.TOOLS})
