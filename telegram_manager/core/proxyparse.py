"""Reading proxy lists: one proxy per line, any of the usual formats. Pure functions, no network.

    socks5://user:pass@1.2.3.4:1080      http://1.2.3.4:8080         socks4://1.2.3.4:1080
    1.2.3.4:1080                         1.2.3.4:1080:user:pass      user:pass@1.2.3.4:1080
    tg://proxy?server=H&port=443&secret=S   https://t.me/proxy?server=H&port=443&secret=S   (MTProxy)

A line with no scheme gets `default_scheme`. A proxy is {"scheme", "host", "port", "user", "password", "secret"}."""
from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

SCHEMES = ("socks5", "socks4", "http", "mtproxy")
MAX_PROXIES = 300
_HOST = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_ALIASES = {"socks": "socks5", "socks5h": "socks5", "socks4a": "socks4", "https": "http", "mtproto": "mtproxy"}
_SECRET = re.compile(r"^[0-9A-Fa-f]{32,}$|^[A-Za-z0-9_-]{22,}={0,2}$")


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return bool(_HOST.match(host)) and "." in host or host == "localhost"


def _make(scheme: str, host: str, port: Any, user: str = "", password: str = "", secret: str = "") -> dict[str, Any] | None:
    host = (host or "").strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if not _valid_host(host) or not 1 <= port <= 65535:
        return None
    if scheme == "mtproxy" and not _SECRET.match(secret or ""):
        return None
    return {"scheme": scheme, "host": host.strip("[]"), "port": port, "user": user or "", "password": password or "", "secret": secret or ""}


def normalize_scheme(name: str | None, fallback: str = "socks5") -> str:
    name = (name or "").strip().lower()
    name = _ALIASES.get(name, name)
    return name if name in SCHEMES else fallback


def parse_proxy_line(line: str, default_scheme: str = "socks5") -> dict[str, Any] | None:
    """One proxy from one line, or None when the line is not a proxy."""
    text = (line or "").strip().strip(",;")
    if not text or text.startswith("#") or len(text) > 400:
        return None
    default_scheme = normalize_scheme(default_scheme)
    lowered = text.lower()
    if lowered.startswith(("tg://proxy", "tg://socks", "https://t.me/proxy", "http://t.me/proxy", "t.me/proxy", "https://t.me/socks", "t.me/socks")):
        url = urlsplit(text if "://" in text else "https://" + text)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        is_socks = "socks" in lowered.split("?")[0]
        return _make("socks5" if is_socks else "mtproxy", params.get("server", ""), params.get("port", ""),
                     params.get("user", ""), params.get("pass", ""), params.get("secret", ""))
    scheme = default_scheme
    if "://" in text:
        head, _, text = text.partition("://")
        scheme = normalize_scheme(head, "")
        if not scheme:
            return None
    user = password = ""
    if "@" in text:
        creds, _, text = text.rpartition("@")
        user, _, password = creds.partition(":")
        user, password = unquote(user), unquote(password)
    parts = text.strip("/").split(":")
    if len(parts) == 2:
        host, port = parts
    elif len(parts) == 4 and not user:
        host, port, user, password = parts
    elif len(parts) == 3 and scheme == "mtproxy":
        host, port, secret = parts
        return _make("mtproxy", host, port, secret=secret)
    else:
        return None
    return _make(scheme, host, port, user, password)


def parse_proxy_text(text: str, default_scheme: str = "socks5") -> tuple[list[dict[str, Any]], int]:
    """(proxies without duplicates, number of lines that were not proxies). Raises ValueError for too many."""
    found: dict[tuple, dict[str, Any]] = {}
    bad = 0
    for line in (text or "").replace("\r", "\n").split("\n"):
        if not line.strip() or line.strip().startswith("#"):
            continue
        proxy = parse_proxy_line(line, default_scheme)
        if proxy is None:
            bad += 1
            continue
        found.setdefault((proxy["scheme"], proxy["host"], proxy["port"], proxy["user"]), proxy)
    if len(found) > MAX_PROXIES:
        raise ValueError(f"At most {MAX_PROXIES} proxies at a time.")
    return list(found.values()), bad


def looks_like_proxy_file(text: str) -> bool:
    """Is this .txt a proxy list (and not a list of session strings)? Most non-empty lines must be proxies."""
    lines = [ln for ln in (text or "").replace("\r", "\n").split("\n") if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return False
    good = sum(1 for ln in lines if parse_proxy_line(ln) is not None)
    return good > 0 and good >= 0.6 * len(lines)


def describe(proxy: dict[str, Any]) -> str:
    """'socks5 1.2.3.4:1080 (login)' - never the password or the MTProxy secret."""
    extra = " (login)" if proxy.get("user") else ""
    return f"{proxy['scheme']} {proxy['host']}:{proxy['port']}{extra}"


def telethon_kwargs(proxy: dict[str, Any]) -> dict[str, Any]:
    """The `proxy=` (and `connection=`) arguments of telethon.TelegramClient for one proxy."""
    if proxy["scheme"] == "mtproxy":
        from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate

        return {"proxy": (proxy["host"], int(proxy["port"]), proxy["secret"]), "connection": ConnectionTcpMTProxyRandomizedIntermediate}
    spec: dict[str, Any] = {"proxy_type": proxy["scheme"], "addr": proxy["host"], "port": int(proxy["port"]), "rdns": True}
    if proxy.get("user"):
        spec["username"], spec["password"] = proxy["user"], proxy.get("password", "")
    return {"proxy": spec}
