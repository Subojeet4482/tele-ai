"""AI tools for account security and housekeeping: 2-step password, other devices, channels and groups,
blocked users, privacy switches, profile photos. Registered into tools.TOOLS by tools.py (import tools, not this).

Rules that matter here:
- Passwords are NEVER passed through the AI. two_factor opens a form (a wizard) where the admin types them; that
  message is deleted at once and goes straight to Telegram.
- Ending devices, deleting channels and changing the 2-step password always ask first, even in auto mode.
"""
from __future__ import annotations

import logging
from typing import Any

from telegram_manager.ai.tools import (
    Prepared,
    Tool,
    _ACCOUNTS,
    _STR,
    _direct,
    _ids,
    _obj,
    label,
    labels,
    resolve_accounts,
    resolve_one,
)
from telegram_manager.core import security
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)

MAX_ACCOUNTS = 20


async def _targets(ctx, spec: Any, *, one: bool = False) -> list[dict]:
    accounts = [await resolve_one(ctx, spec)] if one else await resolve_accounts(ctx, spec)
    if len(accounts) > MAX_ACCOUNTS:
        raise ValueError(f"At most {MAX_ACCOUNTS} accounts at a time.")
    offline = [a for a in accounts if not ctx.manager.is_online(str(a["id"]))]
    if offline and len(offline) == len(accounts):
        raise ValueError(f"{labels(offline)} offline hai: connected account chahiye.")
    return accounts


async def _each(ctx, account_ids: list[str], job) -> tuple[dict[str, Any], list[str]]:
    """Run `await job(account_id)` for every account: ({id: result}, ["3. Name: what went wrong"])."""
    rows = {str(r["id"]): r for r in await ctx.store.accounts.list(ctx.owner_id)}
    done: dict[str, Any] = {}
    errors: list[str] = []
    for account_id in account_ids:
        try:
            done[account_id] = await job(account_id)
        except Exception as exc:
            errors.append(f"{label(rows.get(account_id, {}))}: {describe_error(exc)}")
    return done, errors


def _result(done_text: str, count: int, total: int, errors: list[str]) -> dict:
    lines = [f"{done_text} ({count} of {total} account(s))."]
    if errors:
        lines += ["⚠️ " + e for e in errors[:6]]
    return {"status": "done" if not errors else "partly_done", "message": "\n".join(lines)}


# --- looking (runs at once) -----------------------------------------------------------------------------
async def _prep_read(ctx, args) -> Prepared:
    accounts = await _targets(ctx, args.get("accounts"))
    return _direct("", accounts=_ids(accounts))


async def _account_security(ctx, data) -> dict:
    async def one(account_id: str) -> dict:
        two = await ctx.manager.two_fa_status(account_id, ctx.owner_id)
        sessions = await ctx.manager.list_sessions(account_id, ctx.owner_id)
        return {"two_fa": two, "sessions": sessions}

    done, errors = await _each(ctx, data["accounts"], one)
    rows = {str(r["id"]): r for r in await ctx.store.accounts.list(ctx.owner_id)}
    out = []
    lines = ["🔐 Account security"]
    for account_id, info in done.items():
        name = label(rows.get(account_id, {}))
        others = [s for s in info["sessions"] if not s["current"]]
        two = "ON ✅" if info["two_fa"]["enabled"] else "OFF ⚠️"
        lines.append(f"{name} · 2FA {two} · {len(others)} other device(s)")
        out.append({"account": name, "two_fa_enabled": info["two_fa"]["enabled"], "hint": info["two_fa"]["hint"],
                    "other_devices": len(others)})
    lines += ["⚠️ " + e for e in errors[:6]]
    ctx.outcome.display.append("\n".join(lines))
    ctx.outcome.notes.append("showed the security overview of " + ", ".join(o["account"] for o in out))
    return {"status": "shown_to_the_admin", "accounts": out, "errors": errors[:6]}


async def _list_sessions(ctx, data) -> dict:
    done, errors = await _each(ctx, data["accounts"], lambda i: ctx.manager.list_sessions(i, ctx.owner_id))
    rows = {str(r["id"]): r for r in await ctx.store.accounts.list(ctx.owner_id)}
    lines = []
    for account_id, sessions in done.items():
        lines.append(f"📱 {label(rows.get(account_id, {}))} — {len(sessions)} session(s)")
        lines += [security.session_line(s) for s in sessions[:25]]
        lines.append("")
    lines += ["⚠️ " + e for e in errors[:6]]
    ctx.outcome.display.append("\n".join(lines).strip() or "Koi session nahi mila.")
    return {"status": "shown_to_the_admin",
            "sessions": {label(rows.get(i, {})): len(s) for i, s in done.items()}, "errors": errors[:6]}


# --- 2-step password: a form, never the chat ------------------------------------------------------------
async def _prep_two_factor(ctx, args) -> Prepared:
    action = str(args.get("action") or "status").strip().lower()
    if action not in ("status", "enable", "change", "disable"):
        raise ValueError("action must be status, enable, change or disable")
    accounts = await _targets(ctx, args.get("accounts"))
    return _direct("", accounts=_ids(accounts), action=action)


async def _two_factor(ctx, data) -> dict:
    if data["action"] == "status":
        done, errors = await _each(ctx, data["accounts"], lambda i: ctx.manager.two_fa_status(i, ctx.owner_id))
        rows = {str(r["id"]): r for r in await ctx.store.accounts.list(ctx.owner_id)}
        lines = ["🔐 2-step verification"] + [
            f"{label(rows.get(i, {}))} · {'ON ✅' + (' (hint: ' + s['hint'] + ')' if s['hint'] else '') if s['enabled'] else 'OFF ⚠️'}"
            for i, s in done.items()
        ] + ["⚠️ " + e for e in errors[:6]]
        ctx.outcome.display.append("\n".join(lines))
        return {"status": "shown_to_the_admin", "enabled": {label(rows.get(i, {})): s["enabled"] for i, s in done.items()}}
    ctx.outcome.wizard = "tfa"
    ctx.outcome.wizard_answers = {"accounts": data["accounts"], "action": data["action"]}
    return {"status": "password_form_opened",
            "note": "A form opened where the admin types the password; it is deleted from the chat and you never see it. "
                    "Do NOT ask for the password in chat and do not say it is done: say the form is open."}


# --- devices ---------------------------------------------------------------------------------------------
async def _prep_terminate(ctx, args) -> Prepared:
    accounts = await _targets(ctx, args.get("accounts"))
    match = str(args.get("device") or "").strip()
    counts = []
    for account in accounts[:8]:
        try:
            sessions = await ctx.manager.list_sessions(str(account["id"]), ctx.owner_id)
            others = [s for s in sessions if not s["current"]]
            counts.append(f"{label(account)}: {len(others)} other device(s)")
        except Exception as exc:
            counts.append(f"{label(account)}: {describe_error(exc)}")
    what = f"devices matching “{match}”" if match else "ALL other devices"
    return Prepared(
        f"🚫 End {what} (the bot's own session stays) on {len(accounts)} account(s):\n" + "\n".join(counts),
        {"accounts": _ids(accounts), "match": match},
    )


async def _terminate(ctx, data) -> dict:
    done, errors = await _each(ctx, data["accounts"], lambda i: ctx.manager.terminate_sessions(i, ctx.owner_id, data["match"]))
    ended = sum(r["ended"] for r in done.values())
    for r in done.values():
        errors += r["errors"]
    return _result(f"🚫 {ended} device(s) ended", len(done), len(data["accounts"]), list(dict.fromkeys(errors)))


# --- channels and groups ----------------------------------------------------------------------------------
async def _prep_list_chats(ctx, args) -> Prepared:
    account = await _targets(ctx, args.get("account"), one=True)
    kind = str(args.get("kind") or "all").strip().lower()
    if kind not in ("all", "channel", "group", "supergroup", "groups"):
        raise ValueError("kind must be all, channel, group or supergroup")
    return _direct("", account=str(account[0]["id"]), kind=kind, owned_only=bool(args.get("owned_only")))


async def _list_chats(ctx, data) -> dict:
    rows = await ctx.manager.list_chats_of(data["account"], ctx.owner_id, data["kind"], data["owned_only"])
    icon = {"channel": "📢", "supergroup": "👥", "group": "👥"}
    lines = [f"{icon[r['kind']]} {r['title']}" + (f" (@{r['username']})" if r["username"] else "")
             + (" · 👑 owner" if r["owner"] else "") for r in rows[:60]]
    ctx.outcome.display.append(("📋 Channels / groups: " + str(len(rows)) + "\n" + "\n".join(lines)) if rows else "Koi channel / group nahi mila.")
    ctx.outcome.notes.append(f"listed {len(rows)} chats of one account")
    return {"status": "shown_to_the_admin", "count": len(rows),
            "chats": [{"title": r["title"], "kind": r["kind"], "owner": r["owner"]} for r in rows[:40]]}


async def _prep_delete_channel(ctx, args) -> Prepared:
    account = (await _targets(ctx, args.get("account"), one=True))[0]
    account_id = str(account["id"])
    if args.get("all_owned"):
        kind = str(args.get("kind") or "all").strip().lower()
        rows = await ctx.manager.list_chats_of(account_id, ctx.owner_id, kind, owned_only=True)
        if not rows:
            raise ValueError("This account owns no such channel / group.")
        names = "\n".join(f"• {r['title']} ({r['kind']})" for r in rows[:15]) + (f"\n… +{len(rows) - 15} more" if len(rows) > 15 else "")
        return Prepared(f"🗑 DELETE {len(rows)} channel(s)/group(s) owned by {label(account)} for EVERYONE (cannot be undone):\n{names}",
                        {"account": account_id, "refs": [str(r["id"]) for r in rows], "titles": [r["title"] for r in rows]})
    ref = str(args.get("chat") or "").strip()
    entity = await ctx.manager._chat_entity(account_id, ctx.owner_id, ref)
    row = security.chat_row(entity)
    if not row["owner"]:
        raise ValueError(f"{label(account)} is not the owner of “{row['title']}”: it can only leave it (delete_chat).")
    return Prepared(f"🗑 DELETE the {row['kind']} “{row['title']}” owned by {label(account)} for EVERYONE (cannot be undone).",
                    {"account": account_id, "refs": [str(row["id"])], "titles": [row["title"]]})


async def _delete_channel(ctx, data) -> dict:
    gone, errors = [], []
    for ref, title in zip(data["refs"], data["titles"]):
        try:
            await ctx.manager.delete_owned_chat(data["account"], ctx.owner_id, ref)
            gone.append(title)
        except Exception as exc:
            errors.append(f"{title}: {describe_error(exc)}")
    text = f"🗑 {len(gone)} deleted" + (": " + ", ".join(gone[:10]) if gone else "")
    return {"status": "done" if not errors else "partly_done", "message": text + ("\n⚠️ " + "; ".join(errors[:5]) if errors else "")}


async def _prep_create_channel(ctx, args) -> Prepared:
    accounts = await _targets(ctx, args.get("accounts"))
    title = str(args.get("title") or "").strip()
    if not title:
        raise ValueError("What should the title be? Ask the admin.")
    kind = str(args.get("kind") or "channel").strip().lower()
    if kind not in ("channel", "group", "supergroup"):
        raise ValueError("kind must be channel or group")
    username = str(args.get("username") or "").strip().lstrip("@")
    if username and len(accounts) > 1:
        raise ValueError("A public @username can belong to only one chat: use one account.")
    return Prepared(f"➕ Create the {kind} “{title}”" + (f" (@{username})" if username else "") + f" on {len(accounts)} account(s): {labels(accounts)}",
                    {"accounts": _ids(accounts), "title": title, "about": str(args.get("about") or ""), "kind": kind, "username": username})


async def _create_channel(ctx, data) -> dict:
    done, errors = await _each(ctx, data["accounts"], lambda i: ctx.manager.create_chat(
        i, ctx.owner_id, data["title"], data["about"], data["kind"], data["username"]))
    links = [r["link"] for r in done.values() if r["link"]]
    return _result(f"➕ “{data['title']}” created" + (" · " + ", ".join(links) if links else ""), len(done), len(data["accounts"]), errors)


async def _prep_edit_chat(ctx, args) -> Prepared:
    account = (await _targets(ctx, args.get("account"), one=True))[0]
    title = None if args.get("title") is None else str(args["title"]).strip()
    about = None if args.get("about") is None else str(args["about"]).strip()
    if title is None and about is None:
        raise ValueError("What to change: title or about?")
    entity = await ctx.manager._chat_entity(str(account["id"]), ctx.owner_id, str(args.get("chat") or ""))
    row = security.chat_row(entity)
    changes = ", ".join(x for x, v in (("title", title), ("about", about)) if v is not None)
    return Prepared(f"✏️ Change {changes} of “{row['title']}” on {label(account)}",
                    {"account": str(account["id"]), "chat": str(row["id"]), "title": title, "about": about})


async def _edit_chat(ctx, data) -> dict:
    name = await ctx.manager.edit_chat_info(data["account"], ctx.owner_id, data["chat"], data["title"], data["about"])
    return {"status": "done", "message": f"✅ “{name}” updated."}


# --- blocked users, privacy, photos --------------------------------------------------------------------------
async def _prep_list_blocked(ctx, args) -> Prepared:
    account = (await _targets(ctx, args.get("account"), one=True))[0]
    return _direct("", account=str(account["id"]))


async def _list_blocked(ctx, data) -> dict:
    rows = await ctx.manager.blocked_users(data["account"], ctx.owner_id)
    lines = [f"⛔ {r['name']}" + (f" (@{r['username']})" if r["username"] else "") for r in rows[:60]]
    ctx.outcome.display.append((f"⛔ Blocked users: {len(rows)}\n" + "\n".join(lines)) if rows else "Is account ne kisi ko block nahi kiya.")
    return {"status": "shown_to_the_admin", "count": len(rows)}


async def _prep_unblock(ctx, args) -> Prepared:
    accounts = await _targets(ctx, args.get("accounts"))
    target = str(args.get("target") or "").strip()
    if not target:
        raise ValueError("Whom to unblock? Ask for the @username.")
    return Prepared(f"✅ Unblock {target} on {len(accounts)} account(s): {labels(accounts)}", {"accounts": _ids(accounts), "target": target})


async def _unblock(ctx, data) -> dict:
    done, errors = await _each(ctx, data["accounts"], lambda i: ctx.manager.unblock_on_account(i, data["target"], ctx.owner_id))
    return _result("✅ Unblocked", len(done), len(data["accounts"]), errors)


async def _prep_privacy(ctx, args) -> Prepared:
    accounts = await _targets(ctx, args.get("accounts"))
    key, who = security.resolve_privacy(args.get("setting"), args.get("who"))
    return Prepared(f"🔒 Privacy: {security.PRIVACY_KEYS[key][1]} → {who} on {len(accounts)} account(s): {labels(accounts)}",
                    {"accounts": _ids(accounts), "setting": key, "who": who})


async def _privacy(ctx, data) -> dict:
    done, errors = await _each(ctx, data["accounts"], lambda i: ctx.manager.set_privacy(i, ctx.owner_id, data["setting"], data["who"]))
    return _result(f"🔒 {security.PRIVACY_KEYS[data['setting']][1]} → {data['who']}", len(done), len(data["accounts"]), errors)


async def _prep_photos(ctx, args) -> Prepared:
    accounts = await _targets(ctx, args.get("accounts"))
    return Prepared(f"🖼 DELETE all profile photos of {len(accounts)} account(s): {labels(accounts)}", {"accounts": _ids(accounts)})


async def _photos(ctx, data) -> dict:
    done, errors = await _each(ctx, data["accounts"], lambda i: ctx.manager.delete_profile_photos(i, ctx.owner_id))
    return _result(f"🖼 {sum(done.values())} photo(s) deleted", len(done), len(data["accounts"]), errors)


_ONE = {"account": _STR}
TOOLS: list[Tool] = [
    Tool("account_security",
         "Look at the security of accounts: is 2-step verification ON or OFF, and how many other devices are logged in. Shown to the admin.",
         _obj({"accounts": _ACCOUNTS}, ["accounts"]), _prep_read, _account_security),
    Tool("list_sessions", "List the devices / apps an account is logged in on (and which one is this bot).",
         _obj({"accounts": _ACCOUNTS}, ["accounts"]), _prep_read, _list_sessions),
    Tool("two_factor",
         "2-step verification (cloud password). action: status (read), enable, change or disable. For enable / change / disable a FORM opens where "
         "the admin types the password(s) themselves: never ask for or accept a password in chat. Several accounts get the same new password.",
         _obj({"accounts": _ACCOUNTS, "action": {"type": "string", "description": "status · enable · change · disable"}}, ["accounts", "action"]),
         _prep_two_factor, _two_factor),
    Tool("terminate_sessions",
         "Log out the OTHER devices of an account (the bot's own session stays). Optional device text (e.g. 'Android', 'Chrome', a country) ends only matching ones. "
         "Always asks the admin first.",
         _obj({"accounts": _ACCOUNTS, "device": _STR}, ["accounts"]), _prep_terminate, _terminate, risky=True, always_confirm=True),
    Tool("list_my_chats", "List the channels and groups one account is in. kind: all · channel · group · supergroup. owned_only=true shows only the ones it created.",
         _obj({**_ONE, "kind": _STR, "owned_only": {"type": "boolean"}}, ["account"]), _prep_list_chats, _list_chats),
    Tool("delete_channel",
         "DELETE a channel or group for everybody (the account must be its owner): chat = its name, @username or id; or all_owned=true (with optional kind) for every "
         "one the account owns. Cannot be undone, always asks first. To merely leave a chat use delete_chat.",
         _obj({**_ONE, "chat": _STR, "all_owned": {"type": "boolean"}, "kind": _STR}, ["account"]),
         _prep_delete_channel, _delete_channel, risky=True, always_confirm=True),
    Tool("create_channel", "Create a channel or a group (kind: channel or group) with a title, optional about text and optional public @username.",
         _obj({"accounts": _ACCOUNTS, "title": _STR, "about": _STR, "kind": _STR, "username": _STR}, ["accounts", "title"]),
         _prep_create_channel, _create_channel, risky=True),
    Tool("edit_chat", "Change the title and/or about text of a channel or group the account administers.",
         _obj({**_ONE, "chat": _STR, "title": _STR, "about": _STR}, ["account", "chat"]), _prep_edit_chat, _edit_chat, risky=True),
    Tool("list_blocked", "Show the users one account has blocked.", _obj(_ONE, ["account"]), _prep_list_blocked, _list_blocked),
    Tool("unblock_user", "Unblock a user (@username / id) on the named accounts.",
         _obj({"accounts": _ACCOUNTS, "target": _STR}, ["accounts", "target"]), _prep_unblock, _unblock, risky=True),
    Tool("set_privacy",
         "Privacy of accounts. setting: phone · last_seen · photo · about · groups (who can add it to groups/channels) · calls · forwards. who: everyone · contacts · nobody.",
         _obj({"accounts": _ACCOUNTS, "setting": _STR, "who": _STR}, ["accounts", "setting", "who"]), _prep_privacy, _privacy, risky=True),
    Tool("delete_profile_photos", "Delete all profile photos of the named accounts.", _obj({"accounts": _ACCOUNTS}, ["accounts"]),
         _prep_photos, _photos, risky=True),
]
