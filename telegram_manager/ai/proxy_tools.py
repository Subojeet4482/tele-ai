"""AI tools for proxies (see core/proxy.py). Registered into tools.TOOLS by tools.py.

The admin can send a .txt with one proxy per line to the bot (saved at once), paste proxies in the chat, or say
"proxy use karo": the accounts then connect through the pool and, when a proxy fails, retry on the next working one.
The AI never sees a proxy password or MTProxy secret."""
from __future__ import annotations

from typing import Any

from telegram_manager.ai.tools import Prepared, Tool, _ACCOUNTS, _STR, _bulk_message, _direct, _obj, resolve_accounts
from telegram_manager.core.proxy import PROXY_TEST_MAX
from telegram_manager.core.proxyparse import SCHEMES, describe, normalize_scheme, parse_proxy_text

MAX_SHOWN = 40


def _numbers(spec: Any) -> list[int]:
    items = spec if isinstance(spec, list) else [spec]
    out: list[int] = []
    for item in items:
        for part in str(item).replace(",", " ").split():
            if part.isdigit():
                out.append(int(part))
    return out


def _pick(pool: list[dict], spec: Any, health) -> list[dict]:
    """Proxies named by the AI: [3, 4] · 'all' · 'dead' (failed its last test) · 'working'."""
    items = spec if isinstance(spec, list) else [spec]
    words = {str(i).strip().lower() for i in items}
    if "all" in words or "sab" in words:
        return list(pool)
    if "dead" in words or "failed" in words:
        return [r for r in pool if (health(r["id"]) or {}).get("ok") is False]
    if "working" in words or "ok" in words:
        return [r for r in pool if (health(r["id"]) or {}).get("ok") is True]
    nums = _numbers(items)
    chosen = [pool[n - 1] for n in nums if 1 <= n <= len(pool)]
    if not chosen:
        raise ValueError("Which proxies? Give their numbers from proxy_list, or all / dead / working.")
    return chosen


def _state(ctx, row: dict) -> str:
    health = ctx.manager.proxy_health(ctx.owner_id, row["id"])
    if health is None:
        return "untested"
    if health["ok"]:
        return f"✅ {health['ms']} ms"
    return "⏸ resting" if ctx.manager.proxy_cooling(ctx.owner_id, row["id"]) else "❌ failed"


# --- looking ---------------------------------------------------------------------------------------------------
async def _prep_list(ctx, args) -> Prepared:
    return _direct("")


async def _list(ctx, data) -> dict:
    pool = await ctx.manager.proxy_pool(ctx.owner_id)
    accounts = [a for a in ctx.manager.accounts.values() if ctx.manager._owner_of(a) == ctx.owner_id]
    rows = await ctx.store.accounts.list(ctx.owner_id)
    on_auto = [r for r in rows if r.get("proxy_auto")]
    on_fixed: dict[str, int] = {}
    for r in rows:
        if r.get("proxy_id") and not r.get("proxy_auto"):
            on_fixed[r["proxy_id"]] = on_fixed.get(r["proxy_id"], 0) + 1
    lines = [f"🌐 Proxies: {len(pool)}  ·  accounts: auto {len(on_auto)}, fixed {sum(on_fixed.values())}, direct {len(rows) - len(on_auto) - sum(on_fixed.values())}"]
    for number, row in enumerate(pool[:MAX_SHOWN], 1):
        used = f" · {on_fixed[row['id']]} acct" if row["id"] in on_fixed else ""
        lines.append(f"{number}. {describe(ctx.manager._plain(row))} · {_state(ctx, row)}{used}")
    if len(pool) > MAX_SHOWN:
        lines.append(f"… and {len(pool) - MAX_SHOWN} more")
    if not pool:
        lines.append("Koi proxy nahi. .txt file bhejo (har line mein ek proxy) ya chat mein paste karo.")
    ctx.outcome.display.append("\n".join(lines))
    ctx.outcome.notes.append(f"showed the proxy list ({len(pool)} proxies)")
    return {"status": "shown_to_the_admin", "proxies": len(pool),
            "accounts_on_auto": len(on_auto), "accounts_on_fixed": sum(on_fixed.values()), "accounts_total": len(accounts),
            "note": "Passwords are never shown. To use them: proxy_use."}


# --- adding ----------------------------------------------------------------------------------------------------
async def _prep_add(ctx, args) -> Prepared:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("No proxy text. The admin can paste proxies here or send a .txt file to the bot.")
    scheme = normalize_scheme(args.get("scheme"), "socks5")
    proxies, bad = parse_proxy_text(text, scheme)
    if not proxies:
        raise ValueError(f"None of the {bad} line(s) looks like a proxy. Formats: socks5://user:pass@host:port · host:port · host:port:user:pass · tg://proxy?server=..")
    return _direct("", proxies=proxies, bad=bad)


async def _add(ctx, data) -> dict:
    result = await ctx.manager.proxy_add(ctx.owner_id, data["proxies"])
    note = f"🌐 {result['added']} proxy saved ({result['duplicates']} already there, {data['bad']} lines ignored). Pool: {result['total']}."
    ctx.outcome.display.append(note)
    return {"status": "saved", **result, "ignored_lines": data["bad"],
            "note": "Suggest proxy_test, then proxy_use if the admin wants accounts on them."}


# --- testing ---------------------------------------------------------------------------------------------------------
async def _prep_test(ctx, args) -> Prepared:
    pool = await ctx.manager.proxy_pool(ctx.owner_id)
    if not pool:
        raise ValueError("The pool is empty.")
    chosen = _pick(pool, args.get("proxies") or "all", lambda i: ctx.manager.proxy_health(ctx.owner_id, i))
    return _direct("", ids=[r["id"] for r in chosen][:PROXY_TEST_MAX])


async def _test(ctx, data) -> dict:
    results = await ctx.manager.proxy_test(ctx.owner_id, data["ids"])
    pool = {r["id"]: i for i, r in enumerate(await ctx.manager.proxy_pool(ctx.owner_id), 1)}
    good = [r for r in results if r["ok"]]
    lines = [f"🧪 Proxy test: {len(good)}/{len(results)} chal rahe hain"]
    for r in results[:MAX_SHOWN]:
        lines.append(f"{pool.get(r['id'], '?')}. {r['label']} · " + (f"✅ {r['ms']} ms" if r["ok"] else f"❌ {r['error']}"))
    ctx.outcome.display.append("\n".join(lines))
    ctx.outcome.notes.append(f"tested {len(results)} proxies: {len(good)} work")
    return {"status": "shown_to_the_admin", "tested": len(results), "working": len(good), "failed": len(results) - len(good),
            "note": "Offer proxy_remove dead to clean up, and proxy_use auto to put accounts on the working ones."}


# --- using / removing -----------------------------------------------------------------------------------------------------
async def _prep_use(ctx, args) -> Prepared:
    accounts = await resolve_accounts(ctx, args.get("accounts"), empty_means_all=False)
    pool = await ctx.manager.proxy_pool(ctx.owner_id)
    choice = str(args.get("proxy") or "auto").strip().lower()
    if choice in ("off", "direct", "none", "band"):
        choice, what = "off", "NO proxy (direct)"
    elif choice == "auto":
        if not pool:
            raise ValueError("The proxy pool is empty: ask the admin for proxies (.txt file or paste) first.")
        what = f"the proxy pool ({len(pool)}) with automatic retry: a failing proxy is skipped and the next working one is used"
    else:
        nums = _numbers(choice)
        if len(nums) != 1 or not 1 <= nums[0] <= len(pool):
            raise ValueError("proxy must be auto, off or one number from proxy_list.")
        choice, what = pool[nums[0] - 1]["id"], f"proxy #{nums[0]} ({describe(ctx.manager._plain(pool[nums[0] - 1]))})"
    names = ", ".join(str(a.get("number") or a["id"]) for a in accounts[:8])
    return Prepared(f"🌐 {len(accounts)} account(s) [{names}] will use {what}. They reconnect now.",
                    {"accounts": [str(a["id"]) for a in accounts], "choice": choice})


async def _use(ctx, data) -> dict:
    return _bulk_message("🌐 Proxy set", await ctx.manager.proxy_assign(ctx.owner_id, data["accounts"], data["choice"]))


async def _prep_remove(ctx, args) -> Prepared:
    pool = await ctx.manager.proxy_pool(ctx.owner_id)
    chosen = _pick(pool, args.get("proxies"), lambda i: ctx.manager.proxy_health(ctx.owner_id, i))
    everything = len(chosen) == len(pool) and len(pool) > 1
    return Prepared(f"🗑 Delete {len(chosen)} proxy(ies): " + ", ".join(describe(ctx.manager._plain(r)) for r in chosen[:5]) + ("…" if len(chosen) > 5 else ""),
                    {"ids": [r["id"] for r in chosen]}, force_confirm=everything)


async def _remove(ctx, data) -> dict:
    count = await ctx.manager.proxy_remove(ctx.owner_id, data["ids"])
    return {"status": "done", "message": f"🗑 {count} proxy deleted.", "removed": count}


TOOLS: list[Tool] = [
    Tool("proxy_list", "Show the admin's proxy pool: number, type, host:port, last test result, which accounts use proxies. Passwords are never shown.",
         _obj({}), _prep_list, _list),
    Tool("proxy_add",
         "Save proxies the admin pasted into the chat (one per line, any format: socks5://user:pass@host:port, host:port, host:port:user:pass, "
         "http://..., tg://proxy?server=..&port=..&secret=.. for MTProxy). A .txt FILE sent to the bot is saved by the bot itself. "
         f"scheme = socks5 (default) · socks4 · http · mtproxy for lines without a scheme ({', '.join(SCHEMES)}).",
         _obj({"text": _STR, "scheme": _STR}, ["text"]), _prep_add, _add),
    Tool("proxy_test",
         "Open a real Telegram connection through proxies (no login) and report which work and how fast. proxies = numbers from proxy_list, or all (default) / dead / working.",
         _obj({"proxies": {"type": "array", "items": _STR}}), _prep_test, _test),
    Tool("proxy_use",
         "Make accounts connect through proxies. proxy=auto (default; for 'proxy use karo') uses the pool with RETRY: if a proxy fails the next working one is tried, "
         "also later when a connection drops; proxy=<number> pins one proxy; proxy=off goes back to direct. The accounts reconnect now; a failed switch leaves them online as before.",
         _obj({"accounts": _ACCOUNTS, "proxy": {"type": "string", "description": "auto · off · a number from proxy_list"}}, ["accounts"]),
         _prep_use, _use, risky=True),
    Tool("proxy_remove", "Delete proxies from the pool: numbers from proxy_list, or all / dead (failed their last test).",
         _obj({"proxies": {"type": "array", "items": _STR}}, ["proxies"]), _prep_remove, _remove, risky=True),
]
