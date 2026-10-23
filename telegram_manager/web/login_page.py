from __future__ import annotations

import html

_STYLE = (
    "body{margin:0;background:#0e1116;color:#d7dde5;font:16px/1.5 system-ui,sans-serif;display:flex;"
    "min-height:100vh;align-items:center;justify-content:center}main{max-width:420px;padding:24px;text-align:center}"
    "button{background:#238636;color:#fff;border:0;border-radius:8px;padding:14px 28px;font-size:17px}"
    "p{color:#8b96a5}"
)


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow"><meta name="referrer" content="no-referrer">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main><h2>{html.escape(title)}</h2>{body}</main></body></html>"
    )


def render_login_form(key: str) -> str:
    return _page(
        "📜 Open the bot logs",
        '<p>This link works once.</p><form method="post" action="/logs/login">'
        f'<input type="hidden" name="key" value="{html.escape(key, quote=True)}">'
        "<button>Open logs</button></form>",
    )


def render_notice(title: str, message: str) -> str:
    return _page(title, f"<p>{html.escape(message)}</p>")
