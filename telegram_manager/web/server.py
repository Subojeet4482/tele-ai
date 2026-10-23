"""Small HTTP server on a background thread.

Public:   GET/HEAD /  and /health  -> real health (200 ok/degraded, 503 down)
          GET/HEAD /live           -> always 200 (for a platform that only needs "process is up")
Private:  /logs and /status need a browser session, obtained through a one-time link the
          owner gets from the bot (/logs/login). No secret ever sits in a query string.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from telegram_manager.log_setup import RingBufferHandler
from telegram_manager.web.access import LogAccess
from telegram_manager.web.health import STARTED_AT, HealthMonitor
from telegram_manager.web.login_page import render_login_form, render_notice
from telegram_manager.web.logs_page import (
    DEFAULT_LINES,
    LEVEL_FILTERS,
    MAX_LINES,
    int_param,
    render_logs_html,
    select_entries,
)

logger = logging.getLogger(__name__)

COOKIE = "lsid"
EXPIRED = "This link was already used or has expired. Send /logs to the bot."
TEXT = "text/plain; charset=utf-8"
HTML = "text/html; charset=utf-8"
# Nothing on these pages needs scripts, images or other sites.
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


class _Handler(BaseHTTPRequestHandler):
    server_version = "AccountManager"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    # --- plumbing ------------------------------------------------------------------
    def _send(
        self, status: int, body: bytes, content_type: str, method: str, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body)

    def _page(self, status: int, title: str, message: str, method: str) -> None:
        self._send(status, render_notice(title, message).encode(), HTML, method)

    def _session_id(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(COOKIE)
        return morsel.value if morsel else None

    def _cookie_header(self, value: str, max_age: int) -> str:
        proxy_https = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        secure = getattr(self.server, "secure_cookies", False) or proxy_https
        cookie = f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        return cookie + ("; Secure" if secure else "")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            if method in ("GET", "HEAD"):
                if path in ("/", "/health"):
                    return self._health(method)
                if path == "/live":
                    return self._send(200, b"OK", TEXT, method)
                if path == "/logs/login":
                    return self._login_page(query, method)
                if path == "/logs/logout":
                    return self._logout(method)
                if path in ("/logs", "/status"):
                    return self._private(path, query, method)
            elif method == "POST" and path == "/logs/login":
                return self._login_submit()
            self._send(404, b"Not found", TEXT, method)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("HTTP handler failed for %s", self.path.split("?", 1)[0])
            try:
                self._send(500, b"Internal error", TEXT, method)
            except Exception:
                pass

    # --- public health ---------------------------------------------------------------
    def _health(self, method: str) -> None:
        health: HealthMonitor | None = getattr(self.server, "health", None)
        status, reason = health.check() if health else ("ok", "ok")
        body = json.dumps({"status": status, "reason": reason}).encode()
        self._send(503 if status == "down" else 200, body, "application/json", method)

    # --- login with a one-time key -----------------------------------------------------
    def _access(self) -> LogAccess | None:
        return getattr(self.server, "log_access", None)

    def _login_page(self, query: dict[str, str], method: str) -> None:
        access = self._access()
        key = query.get("key", "")
        if access is None or not access.peek(key):
            return self._page(403, "Link expired", EXPIRED, method)
        self._send(200, render_login_form(key).encode(), HTML, method)

    def _login_submit(self) -> None:
        access = self._access()
        try:
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 2048))
        except ValueError:
            length = 0
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
        session_id = access.redeem(form.get("key", [""])[0]) if access else None
        if session_id is None:
            logger.warning("Rejected a logs login with an unknown or used key")
            return self._page(403, "Link expired", EXPIRED, "POST")
        logger.info("Logs session opened")
        headers = {"Location": "/logs", "Set-Cookie": self._cookie_header(session_id, 12 * 3600)}
        self._send(303, b"", TEXT, "POST", headers)

    def _logout(self, method: str) -> None:
        access = self._access()
        if access:
            access.end_session(self._session_id())
        self._send(200, render_notice("Signed out", "Send /logs to the bot for a new link.").encode(), HTML, method,
                   {"Set-Cookie": self._cookie_header("", 0)})

    # --- private pages -----------------------------------------------------------------
    def _private(self, path: str, query: dict[str, str], method: str) -> None:
        server = self.server
        buffer: RingBufferHandler | None = getattr(server, "log_buffer", None)
        access = self._access()
        if buffer is None or access is None:
            return self._send(404, b"Not found", TEXT, method)
        if not access.session_valid(self._session_id()):
            return self._page(403, "Sign in needed", "Send /logs to the bot for a fresh link.", method)

        provider = getattr(server, "status_provider", None)
        status: dict[str, Any] = {}
        if provider is not None:
            try:
                status = provider()
            except Exception:
                logger.exception("Status provider failed")
                status = {"status_error": "provider failed"}

        if path == "/status":
            health: HealthMonitor | None = getattr(server, "health", None)
            state, reason = health.check() if health else ("ok", "ok")
            payload = {
                "status": state,
                "reason": reason,
                "started_at": datetime.fromtimestamp(STARTED_AT, tz=timezone.utc).isoformat(),
                "uptime_seconds": int(time.time() - STARTED_AT),
                **status,
            }
            return self._send(200, json.dumps(payload, indent=2, default=str).encode(), "application/json", method)

        limit = int_param(query.get("lines"), DEFAULT_LINES, 1, MAX_LINES)
        level_name = query.get("level", "all").lower()
        if level_name not in LEVEL_FILTERS:
            level_name = "all"
        newest_first = query.get("order", "new").lower() != "old"
        needle = query.get("q", "")
        entries, total = select_entries(buffer.snapshot(), LEVEL_FILTERS[level_name], needle, limit, newest_first)

        if query.get("format", "html").lower() == "text":
            text = "\n".join(entry.text for entry in entries) + "\n"
            return self._send(200, text.encode("utf-8", "replace"), TEXT, method)

        params = {
            "lines": str(limit),
            "level": level_name,
            "order": "new" if newest_first else "old",
            "refresh": str(int_param(query.get("refresh"), 0, 0, 3600)),
            "q": needle,
        }
        page = render_logs_html(entries, total, params, status)
        self._send(200, page.encode("utf-8", "replace"), HTML, method)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence default access logs
        return


def start_http_server(
    port: int,
    log_buffer: RingBufferHandler | None = None,
    log_access: LogAccess | None = None,
    health: HealthMonitor | None = None,
    host: str = "0.0.0.0",
    secure_cookies: bool = False,
) -> ThreadingHTTPServer:
    """Start the server on a background thread. Set ``server.status_provider`` to a callable
    returning a dict to add live fields (accounts online, ...) to /status and /logs."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.log_buffer = log_buffer  # type: ignore[attr-defined]
    server.log_access = log_access  # type: ignore[attr-defined]
    server.health = health  # type: ignore[attr-defined]
    server.secure_cookies = secure_cookies  # type: ignore[attr-defined]
    server.status_provider = None  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True, name="http-server").start()
    logger.info("HTTP server listening on port %s", server.server_address[1])
    return server
