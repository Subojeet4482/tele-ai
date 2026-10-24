"""Proxies from a URL (a public proxy API), kept apart from the proxies the admin adds by hand.

Every saved proxy carries a `src` tag: "manual" (typed / pasted / a .txt file; also every proxy saved before this
feature existed) or "url" (fetched from the admin's proxy URL). The URL works with no setup: DEFAULT_PROXY_URL is used
until the admin changes it. `use` says which of the two pools the accounts rotate through: both (default) / manual / url.

Giving proxies away (/getproxy, the AI) never touches the accounts: it fetches a fresh list from the URL (or reads the
saved manual ones that have no login) and hands the lines over. Proxies with a password or an MTProxy secret are never given.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from telegram_manager.core.proxyparse import MAX_PROXIES, normalize_scheme, parse_proxy_line

logger = logging.getLogger(__name__)

DEFAULT_PROXY_URL = "https://proxy-api-zfat.onrender.com/get?type=http&sec=2&count=100"
USE_MODES = ("both", "manual", "url")
FETCH_TIMEOUT = 60  # a free Render service may need a while to wake up
FETCH_TRIES = 2
FETCH_MAX_BYTES = 2_000_000
GIVE_MAX = 500
URL_POOL_MAX = 300
_IP_PORT = re.compile(r"(?:(socks5h?|socks4a?|https?)://)?((?:\d{1,3}\.){3}\d{1,3}):(\d{2,5})")


class ProxySourceError(Exception):
    """The proxy URL could not be used; the text is meant for the admin."""


# --- the URL -----------------------------------------------------------------------------------------------------
def check_source_url(url: str) -> str:
    """The cleaned URL, or ValueError. Only public http(s) addresses (this server must not be pointed at itself)."""
    text = (url or "").strip().strip("<>")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Send a full link that starts with http:// or https://")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("That address is not a public proxy API.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
        raise ValueError("That address is not a public proxy API.")
    if len(text) > 500:
        raise ValueError("That link is too long.")
    return text


def with_params(url: str, **params: Any) -> str:
    """The URL with these query parameters set (None = leave as it is)."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items() if value is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def url_scheme(url: str) -> str:
    """The proxy type the URL asks for (`type=http`), http when it does not say."""
    query = dict(parse_qsl(urlsplit(url).query))
    return normalize_scheme(query.get("type"), "http")


# --- reading what the API sends ------------------------------------------------------------------------------------
def _json_lines(data: Any):
    if isinstance(data, str):
        yield data
    elif isinstance(data, list):
        for item in data:
            yield from _json_lines(item)
    elif isinstance(data, dict):
        host = next((data[k] for k in ("ip", "host", "address", "addr") if data.get(k)), None)
        port = data.get("port")
        if host and port:
            proto = data.get("type") or data.get("protocol") or data.get("scheme") or ""
            proto = proto[0] if isinstance(proto, list) and proto else proto
            proto = normalize_scheme(str(proto), "") if proto else ""
            yield f"{proto}://{host}:{port}" if proto else f"{host}:{port}"
            return
        if isinstance(data.get("proxy"), str):
            yield data["proxy"]
            return
        for value in data.values():
            if isinstance(value, (list, dict)):
                yield from _json_lines(value)


def extract_proxies(payload: str, default_scheme: str = "http") -> list[dict[str, Any]]:
    """Proxies out of whatever the API returned: plain lines, a JSON list, a JSON object with a list inside, or messy text."""
    payload = (payload or "")[:FETCH_MAX_BYTES]
    lines: list[str] = []
    try:
        data = json.loads(payload)
    except ValueError:
        data = None
    if data is not None:
        lines = list(_json_lines(data))
    if not lines:
        lines = payload.replace("\r", "\n").split("\n")
    found: dict[tuple, dict[str, Any]] = {}
    for line in lines:
        proxy = parse_proxy_line(line, default_scheme)
        if proxy is not None and proxy["scheme"] != "mtproxy":
            found.setdefault((proxy["scheme"], proxy["host"], proxy["port"], proxy["user"]), proxy)
        if len(found) >= MAX_PROXIES:
            break
    if not found:  # a page with the proxies somewhere inside
        for scheme, host, port in _IP_PORT.findall(payload):
            proxy = parse_proxy_line(f"{normalize_scheme(scheme, default_scheme)}://{host}:{port}", default_scheme)
            if proxy is not None:
                found.setdefault((proxy["scheme"], proxy["host"], proxy["port"], ""), proxy)
            if len(found) >= MAX_PROXIES:
                break
    return list(found.values())


async def _get_text(url: str) -> str:
    """One GET (the tests replace this). Raises ProxySourceError with a readable reason."""
    try:
        import httpx  # imported here like the other network modules, so importing the bot never needs it
    except ImportError:
        raise ProxySourceError("The httpx package is missing on the server (pip install -r requirements.txt).")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; telegram-manager)", "Accept": "application/json, text/plain, */*"}
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
    except httpx.TimeoutException:
        raise ProxySourceError("The proxy link did not answer in time (a free server may be waking up: try again).")
    except httpx.HTTPError as exc:
        raise ProxySourceError(f"Could not reach the proxy link ({type(exc).__name__}).")
    if response.status_code >= 400:
        raise ProxySourceError(f"The proxy link answered with error {response.status_code}.")
    return response.text[:FETCH_MAX_BYTES]


async def fetch_proxy_list(url: str, count: int | None = None, scheme: str | None = None) -> list[dict[str, Any]]:
    """Fresh proxies from the URL. `count` and `scheme` replace the link's own count= / type= values."""
    wanted = with_params(url, count=count, type=scheme)
    default_scheme = url_scheme(wanted)
    last: ProxySourceError | None = None
    for attempt in range(FETCH_TRIES):
        try:
            proxies = extract_proxies(await _get_text(wanted), default_scheme)
        except ProxySourceError as exc:
            last = exc
        else:
            if proxies:
                return proxies
            last = ProxySourceError("The proxy link answered but there was no proxy in it.")
        if attempt + 1 < FETCH_TRIES:
            await asyncio.sleep(2)
    raise last or ProxySourceError("No proxies came back.")


def line_for(proxy: dict[str, Any], plain_scheme: str = "http") -> str:
    """host:port for the usual type, scheme://host:port for any other (what people paste into a browser or an app)."""
    base = f"{proxy['host']}:{proxy['port']}"
    return base if proxy["scheme"] == plain_scheme else f"{proxy['scheme']}://{base}"


def render_give(lines: list[str], inline_max: int) -> dict[str, Any]:
    """How a list of proxies is handed over: a chat message (<pre>, tap to copy) up to `inline_max`, else a .txt file.
    {'kind': 'text', 'html': ...} or {'kind': 'file', 'data': bytes, 'filename': ...}."""
    if len(lines) <= inline_max:
        from html import escape

        return {"kind": "text", "html": "<pre>" + escape("\n".join(lines)) + "</pre>"}
    return {"kind": "file", "data": ("\n".join(lines) + "\n").encode(), "filename": f"proxies_{len(lines)}.txt"}


def _key(row: dict[str, Any]) -> tuple:
    return row["scheme"], row["host"], int(row["port"]), row.get("user", "")


class ProxySourceMixin:
    # --- settings ---------------------------------------------------------------------------------------------
    async def proxy_source_get(self, owner_id: int) -> dict[str, Any]:
        """{'url', 'use', 'fetched_at', 'last_count', 'is_default'}; a missing value is the default."""
        saved = await self.store.settings.get_proxy_source(owner_id)
        url = saved.get("url") or DEFAULT_PROXY_URL
        use = saved.get("use") if saved.get("use") in USE_MODES else "both"
        return {"url": url, "use": use, "fetched_at": float(saved.get("fetched_at") or 0),
                "last_count": int(saved.get("last_count") or 0), "is_default": url == DEFAULT_PROXY_URL}

    async def proxy_source_set(self, owner_id: int, url: str | None = None, use: str | None = None) -> dict[str, Any]:
        """Change the URL ('' / 'default' / 'reset' = the built-in one) and / or which pool the accounts use."""
        values: dict[str, Any] = {}
        if url is not None:
            word = url.strip().lower()
            values["url"] = "" if word in ("", "default", "reset", "auto") else check_source_url(url)
        if use is not None:
            if use not in USE_MODES:
                raise ValueError("use must be both, manual or url.")
            values["use"] = use
        if values:
            await self.store.settings.set_proxy_source(owner_id, **values)
        return await self.proxy_source_get(owner_id)

    # --- which saved proxies the accounts may rotate through ---------------------------------------------------
    @staticmethod
    def proxy_src(row: dict[str, Any]) -> str:
        return "url" if row.get("src") == "url" else "manual"

    async def proxy_pool_for_accounts(self, owner_id: int) -> list[dict[str, Any]]:
        use = (await self.proxy_source_get(owner_id))["use"]
        pool = await self.proxy_pool(owner_id)
        return pool if use == "both" else [row for row in pool if self.proxy_src(row) == use]

    async def proxy_counts(self, owner_id: int) -> dict[str, int]:
        pool = await self.proxy_pool(owner_id)
        url_rows = sum(1 for row in pool if self.proxy_src(row) == "url")
        return {"manual": len(pool) - url_rows, "url": url_rows, "total": len(pool)}

    # --- refreshing the URL pool --------------------------------------------------------------------------------
    async def proxy_url_refresh(self, owner_id: int) -> dict[str, Any]:
        """Fetch the URL now. New proxies are added, URL proxies that are gone from the list are dropped (unless an account
        is pinned to one), manual proxies are never touched. Raises ProxySourceError."""
        source = await self.proxy_source_get(owner_id)
        fresh = await fetch_proxy_list(source["url"])
        pool = await self.proxy_pool(owner_id)
        pinned = {a.get("proxy_id") for a in self.accounts.values() if self._owner_of(a) == owner_id and a.get("proxy_id")}
        fresh_keys = {_key(p) for p in fresh}
        kept = [r for r in pool if self.proxy_src(r) != "url" or _key(r) in fresh_keys or r["id"] in pinned]
        dropped = len(pool) - len(kept)
        if dropped:
            await self.store.settings.set_proxies(owner_id, kept)
        room = max(0, URL_POOL_MAX - sum(1 for r in kept if self.proxy_src(r) == "url"))
        added = (await self.proxy_add(owner_id, fresh[:room], src="url"))["added"] if room else 0
        counts = await self.proxy_counts(owner_id)
        await self.store.settings.set_proxy_source(owner_id, fetched_at=time.time(), last_count=len(fresh))
        return {"fetched": len(fresh), "added": added, "dropped": dropped, "url_total": counts["url"], "manual_total": counts["manual"]}

    async def proxy_url_clear(self, owner_id: int) -> int:
        """Delete every proxy that came from the URL (the manual ones stay)."""
        ids = [r["id"] for r in await self.proxy_pool(owner_id) if self.proxy_src(r) == "url"]
        return await self.proxy_remove(owner_id, ids) if ids else 0

    # --- giving proxies away ---------------------------------------------------------------------------------------
    async def proxy_give(self, owner_id: int, count: int = 10, source: str = "url", scheme: str | None = None) -> dict[str, Any]:
        """Lines to hand to somebody. source: url (a fresh list from the link, else the saved URL proxies) · manual (saved
        ones without a login) · all. Returns {'lines', 'requested', 'source', 'skipped_login', 'note'}."""
        count = max(1, min(int(count), GIVE_MAX))
        source = source if source in ("url", "manual", "all") else "url"
        scheme = normalize_scheme(scheme, "") if scheme else None
        cfg = await self.proxy_source_get(owner_id)
        plain = scheme or url_scheme(cfg["url"])
        lines: list[str] = []
        note = ""
        skipped = 0
        if source in ("url", "all"):
            try:
                fresh = await fetch_proxy_list(cfg["url"], count=count, scheme=scheme)
                lines += [line_for(p, plain) for p in fresh]
            except ProxySourceError as exc:
                saved = [r for r in await self.proxy_pool(owner_id) if self.proxy_src(r) == "url" and (not scheme or r["scheme"] == scheme)]
                if not saved and source == "url":
                    raise
                lines += [line_for(r, plain) for r in saved]
                note = f"Link problem ({exc}) - gave the last saved ones instead."
        if source in ("manual", "all"):
            for row in await self.proxy_pool(owner_id):
                if self.proxy_src(row) != "manual" or (scheme and row["scheme"] != scheme):
                    continue
                if row.get("user") or row.get("password_enc") or row.get("secret_enc"):
                    skipped += 1  # logins and secrets are never handed out
                    continue
                lines.append(line_for(row, plain))
        unique = list(dict.fromkeys(lines))[:count]
        if not unique:
            raise ValueError("No proxy to give: the link sent none and nothing is saved." if source != "manual"
                             else "No manual proxy without a login is saved.")
        return {"lines": unique, "requested": count, "source": source, "skipped_login": skipped, "note": note}
