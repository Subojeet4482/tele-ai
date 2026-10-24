"""Step-by-step flows of the proxy screen: set the proxy link, add manual proxies."""
from __future__ import annotations

from html import escape

from telegram_manager.bot import keyboards
from telegram_manager.bot.flows.common import static
from telegram_manager.bot.wizard.spec import Step, WizardContext, WizardSpec
from telegram_manager.core.proxyparse import SCHEMES, parse_proxy_text
from telegram_manager.core.proxysource import DEFAULT_PROXY_URL, ProxySourceError, check_source_url

_BACK = lambda: keyboards.back("px|home", "🌐 Proxies")  # noqa: E731


def build_utility_specs(deps) -> list[WizardSpec]:
    manager = deps.manager

    # --- the link proxies are fetched from ---------------------------------------------------------------------
    async def parse_url(wc: WizardContext, raw: str) -> str:
        if raw.strip().lower() in ("default", "reset", "auto"):
            return ""
        return check_source_url(raw)

    async def finish_url(wc: WizardContext) -> None:
        source = await manager.proxy_source_set(wc.user_id, url=wc.answers["url"])
        try:
            result = await manager.proxy_url_refresh(wc.user_id)
        except ProxySourceError as exc:
            await wc.show(f"✅ Link saved.\n⚠️ But fetching failed right now: {escape(str(exc))}\nTap 🔄 Refresh to try again.", _BACK(), "HTML")
            return
        label = "built-in link" if source["is_default"] else "your link"
        await wc.show(f"✅ Using the {label}.\n🔄 {result['fetched']} proxies came, {result['added']} new · URL pool: {result['url_total']}.", _BACK(), "HTML")

    url = WizardSpec(
        name="px_url",
        title="🔗 Proxy link",
        steps=[Step("url", static(
            "🔗 Send the proxy API link (it must answer with one proxy per line, or JSON). Tip: put count=100 and "
            "type=http / socks5 in it.\n\n"
            f"Send  default  to go back to the built-in link:\n{DEFAULT_PROXY_URL}"), parse_url)],
        finish=finish_url,
        back_to="px|home",
    )

    # --- proxies typed by hand ---------------------------------------------------------------------------------
    async def parse_manual(wc: WizardContext, raw: str) -> dict:
        lines = raw.replace("\r", "\n").split("\n")
        scheme = "socks5"
        if lines and lines[0].strip().lower() in SCHEMES:  # a first line like "http" sets the type of bare host:port lines
            scheme, lines = lines[0].strip().lower(), lines[1:]
        proxies, bad = parse_proxy_text("\n".join(lines), scheme)
        if not proxies:
            raise ValueError("I found no proxy in that. One per line: host:port · host:port:user:pass · socks5://user:pass@host:port")
        return {"proxies": proxies, "bad": bad}

    async def finish_manual(wc: WizardContext) -> None:
        data = wc.answers["proxies"]
        result = await manager.proxy_add(wc.user_id, data["proxies"], src="manual")
        await wc.show(
            f"✅ {result['added']} manual proxies saved ({result['duplicates']} already there, {data['bad']} lines ignored).\n"
            f"✋ Manual: {(await manager.proxy_counts(wc.user_id))['manual']}", _BACK())

    manual = WizardSpec(
        name="px_add",
        title="➕ Manual proxies",
        steps=[Step("proxies", static(
            "✋ Paste your proxies, one per line (host:port · host:port:user:pass · socks5://user:pass@host:port · tg://proxy…).\n"
            "Bare host:port lines are socks5; to change that, make the FIRST line just  http,  socks4  or  socks5.\n\n"
            "A .txt file works too: send it to the chat any time."), parse_manual)],
        finish=finish_manual,
        back_to="px|home",
    )
    return [url, manual]
