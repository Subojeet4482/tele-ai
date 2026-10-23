"""The /logs page: filtering the log buffer and drawing it as HTML."""
from __future__ import annotations

import html
import logging
import time
from typing import Any
from urllib.parse import urlencode

from telegram_manager.web.health import STARTED_AT

DEFAULT_LINES = 300
MAX_LINES = 3000
LEVEL_FILTERS = {"all": 0, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}



def int_param(value: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def select_entries(entries: list, min_level: int, needle: str, limit: int, newest_first: bool) -> tuple[list, int]:
    needle = needle.lower()
    matching = [e for e in entries if e.levelno >= min_level and (not needle or needle in e.text.lower())]
    chosen = matching[-limit:]
    if newest_first:
        chosen.reverse()
    return chosen, len(matching)


def _level_class(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


def render_logs_html(entries: list, total: int, params: dict[str, str], status: dict[str, Any] | None) -> str:
    def link(**overrides: str) -> str:
        merged = {**params, **overrides}
        merged = {k: v for k, v in merged.items() if v not in ("", None)}
        return "/logs?" + urlencode(merged)

    refresh = int_param(params.get("refresh"), 0, 0, 3600)
    meta_refresh = f'<meta http-equiv="refresh" content="{max(3, refresh)}">' if refresh else ""
    uptime = _format_uptime(time.time() - STARTED_AT)
    status_line = ""
    if status:
        status_line = " · ".join(f"{html.escape(str(k))}: {html.escape(str(v))}" for k, v in status.items())

    rows = []
    for entry in entries:
        rows.append(f'<div class="l {_level_class(entry.levelno)}">{html.escape(entry.text)}</div>')
    body = "\n".join(rows) or '<div class="l info">No log lines match this filter yet.</div>'

    lines_options = " ".join(f'<a href="{link(lines=str(n))}">{n}</a>' for n in (50, 200, 500, 1000, 3000))
    level_options = " ".join(f'<a href="{link(level=name)}">{name}</a>' for name in LEVEL_FILTERS)
    refresh_options = " ".join(
        f'<a href="{link(refresh=str(n))}">{"off" if n == 0 else f"{n}s"}</a>' for n in (0, 5, 15, 30)
    )
    order_option = (
        f'<a href="{link(order="old")}">oldest first</a>'
        if params.get("order", "new") == "new"
        else f'<a href="{link(order="new")}">newest first</a>'
    )
    text_link = link(format="text")
    search_value = html.escape(params.get("q", ""), quote=True)
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k, quote=True)}" value="{html.escape(v, quote=True)}">'
        for k, v in params.items()
        if k != "q" and v
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
{meta_refresh}
<title>Bot logs</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; background: #0e1116; color: #d7dde5; font: 13px/1.45 ui-monospace, Menlo, Consolas, monospace; }}
  header {{ position: sticky; top: 0; background: #161b22; padding: 10px 12px; border-bottom: 1px solid #2a313c; }}
  h1 {{ font-size: 15px; margin: 0 0 4px; }}
  .meta {{ color: #8b96a5; margin-bottom: 6px; }}
  .bar {{ margin: 3px 0; }}
  .bar b {{ color: #8b96a5; font-weight: normal; margin-right: 4px; }}
  a {{ color: #58a6ff; margin-right: 8px; text-decoration: none; display: inline-block; padding: 5px 4px; }}
  input[type=text] {{ background: #0e1116; color: inherit; border: 1px solid #2a313c; padding: 7px 8px; width: 55%; }}
  button {{ background: #238636; color: #fff; border: 0; padding: 8px 14px; border-radius: 6px; }}
  main {{ padding: 8px 12px 40px; }}
  .l {{ white-space: pre-wrap; word-break: break-word; padding: 2px 0; border-bottom: 1px solid #1a2029; }}
  .debug {{ color: #7d8794; }} .info {{ color: #d7dde5; }}
  .warning {{ color: #e3b341; }} .error {{ color: #ff7b72; }}
</style>
</head>
<body>
<header>
  <h1>📜 Bot logs</h1>
  <div class="meta">uptime {html.escape(uptime)} · showing {len(entries)} of {total} matching lines
  {(' · ' + status_line) if status_line else ''}</div>
  <div class="bar"><b>lines</b>{lines_options}</div>
  <div class="bar"><b>level</b>{level_options}</div>
  <div class="bar"><b>auto-refresh</b>{refresh_options} · {order_option}
  · <a href="{text_link}">plain text</a> · <a href="/logs/logout">sign out</a></div>
  <form method="get" action="/logs" class="bar">{hidden}
  <input type="text" name="q" value="{search_value}" placeholder="search…"> <button>Search</button></form>
</header>
<main>
{body}
</main>
</body>
</html>
"""
