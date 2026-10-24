"""AI tools for handing out proxies, the proxy link, and temp mail (1secmail).

proxy_give: up to PROXY_INLINE_MAX proxies arrive as a message, more than that as a .txt file. The proxies go straight to
the admin's screen; the AI only learns how many. temp_mail: makes an address (with the 📥 Get button under it) or looks at the
inbox; an OTP is shown to the admin and never to the AI."""
from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

from telegram_manager.ai.tools import Prepared, Tool, _INT, _STR, _direct, _obj
from telegram_manager.constants import PROXY_INLINE_MAX
from telegram_manager.core import tempmail
from telegram_manager.core.proxysource import GIVE_MAX, USE_MODES, ProxySourceError, render_give

_GET_BUTTONS = [("📥 Get OTP / Inbox", "tm|get"), ("🔄 Naya mail", "tm|new")]


def _fail(exc: Exception) -> ValueError:
    return ValueError(str(exc))


# --- proxy_give -----------------------------------------------------------------------------------------------------
async def _prep_give(ctx, args) -> Prepared:
    try:
        count = int(args.get("count") or 10)
    except (TypeError, ValueError):
        raise ValueError("count must be a number.")
    source = str(args.get("source") or "url").strip().lower()
    scheme = str(args.get("scheme") or "").strip().lower() or None
    return _direct("", count=max(1, min(count, GIVE_MAX)), source=source, scheme=scheme)


async def _give(ctx, data) -> dict:
    try:
        result = await ctx.manager.proxy_give(ctx.owner_id, data["count"], data["source"], data["scheme"])
    except ProxySourceError as exc:
        raise _fail(exc)
    lines = result["lines"]
    out = render_give(lines, PROXY_INLINE_MAX)
    title = f"🎁 <b>{len(lines)} proxies</b> ({escape(result['source'])})"
    if out["kind"] == "text":
        ctx.outcome.html.append(f"{title}\n{out['html']}")
    else:
        ctx.outcome.files.append((out["filename"], out["data"], f"{title}\nFile mein {len(lines)} proxies hain."))
    ctx.outcome.notes.append(f"gave {len(lines)} proxies as a {'message' if out['kind'] == 'text' else '.txt file'}")
    return {"status": "given_to_the_admin", "count": len(lines), "requested": result["requested"],
            "sent_as": "message" if out["kind"] == "text" else "file", "skipped_login_proxies": result["skipped_login"],
            "note": result["note"] or "The proxies are already on the admin's screen. Do not repeat or invent them; just say how many and how."}


# --- proxy_refresh / proxy_source ---------------------------------------------------------------------------------------
async def _prep_refresh(ctx, args) -> Prepared:
    return _direct("")


async def _refresh(ctx, data) -> dict:
    try:
        result = await ctx.manager.proxy_url_refresh(ctx.owner_id)
    except ProxySourceError as exc:
        raise _fail(exc)
    ctx.outcome.display.append(f"🔄 URL se {result['fetched']} proxies mili · {result['added']} nayi · {result['dropped']} purani hata di · URL pool {result['url_total']}")
    return {"status": "done", **result}


async def _prep_source(ctx, args) -> Prepared:
    url, use = args.get("url"), args.get("use")
    if url is not None and not str(url).strip():
        url = "default"
    return _direct("", url=url, use=str(use).lower() if use else None)


async def _source(ctx, data) -> dict:
    source = await ctx.manager.proxy_source_set(ctx.owner_id, url=data["url"], use=data["use"])
    counts = await ctx.manager.proxy_counts(ctx.owner_id)
    return {"status": "ok", "link_host": urlsplit(source["url"]).netloc, "link_is_default": source["is_default"], "accounts_use": source["use"], **counts}


# --- temp_mail ------------------------------------------------------------------------------------------------------------
async def _prep_mail(ctx, args) -> Prepared:
    action = str(args.get("action") or "show").strip().lower()
    if action not in ("new", "show", "inbox"):
        raise ValueError("action must be new, show or inbox.")
    return _direct("", action=action)


def _buttons(ctx) -> None:
    ctx.outcome.choices = list(_GET_BUTTONS)
    ctx.outcome.choices_title = "📧 Mail aane ke baad neeche dabao 👇"


async def _mail(ctx, data) -> dict:
    settings = ctx.store.settings
    try:
        cfg = await tempmail.mailbox_for(settings, ctx.owner_id, new=data["action"] == "new")
        if data["action"] != "inbox":
            ctx.outcome.html.append(tempmail.card_html(cfg["address"], "Is address ko kisi site par use karo, phir <b>📥 Get</b> dabao."))
            _buttons(ctx)
            ctx.outcome.notes.append("made / showed the temp mail address")
            return {"status": "shown_to_the_admin", "made_new": data["action"] == "new",
                    "note": "The address and the Get button are on the admin's screen. Tell them to tap Get after the mail is sent."}
        address, mails = await tempmail.fetch_new(settings, ctx.owner_id)
    except tempmail.TempMailError as exc:
        raise _fail(exc)
    if not mails:
        ctx.outcome.html.append(tempmail.card_html(address, "📭 Abhi koi mail nahi aaya."))
        _buttons(ctx)
        return {"status": "no_new_mail", "note": "Tell the admin to try again in a moment."}
    ctx.outcome.html.extend(tempmail.mail_html(mail) for mail in mails)
    _buttons(ctx)
    ctx.outcome.notes.append(f"showed {len(mails)} new temp mail(s)")
    return {"status": "shown_to_the_admin", "new_mails": len(mails), "otp_found": any(m["otp"] for m in mails),
            "note": "The mails (and any OTP) are already on the admin's screen. You do not see the OTP; do not guess one."}


TOOLS: list[Tool] = [
    Tool("proxy_give",
         f"Hand proxies to the admin when they ASK for proxies (\"proxy do\", \"10 proxy chahiye\"). count (default 10): up to {PROXY_INLINE_MAX} arrive as a chat message, "
         "MORE than that as a .txt file (automatic). source = url (default: a fresh list from the proxy link) · manual (saved ones without a login) · all. "
         "scheme = http · socks5 · socks4 (optional). Proxies with a password are never given. Nothing is changed on the accounts.",
         _obj({"count": _INT, "source": _STR, "scheme": _STR}), _prep_give, _give),
    Tool("proxy_refresh", "Fetch the proxy link now (the built-in link works with no setup): adds new proxies to the URL pool and drops dead ones that are gone from the list. Manual proxies are never touched.",
         _obj({}), _prep_refresh, _refresh),
    Tool("proxy_source",
         "Change where URL proxies come from and which pool accounts rotate through. url = a full http(s) link that answers with one proxy per line or JSON ('default' = the built-in link). "
         f"use = {' · '.join(USE_MODES)} (both = manual + URL proxies, manual = only the ones the admin added, url = only the fetched ones). Call with no arguments to just read the setting.",
         _obj({"url": _STR, "use": _STR}), _prep_source, _source),
    Tool("temp_mail",
         "Temporary e-mail (1secmail). action=new makes an address (shown with a 📥 Get OTP / Inbox button under it), show repeats the current address, inbox looks at the inbox now. "
         "Use it when the admin asks for a temp / fake / temporary mail or wants an OTP from it. The admin taps 📥 Get after the mail was sent; the OTP is shown to them, you never see it.",
         _obj({"action": _STR}), _prep_mail, _mail),
]
