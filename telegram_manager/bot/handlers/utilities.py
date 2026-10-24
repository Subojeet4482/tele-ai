"""🌐 Proxies (/proxy, /getproxy, /proxyurl) and 📧 Temp Mail (/tempmail): the screens, the buttons and the commands.

Proxies from the admin's URL and proxies added by hand are kept apart. Up to PROXY_INLINE_MAX proxies are handed over as
a chat message, more than that as a .txt file. Temp mail uses 1secmail: an address with a 📥 Get button under it; Get looks at
the inbox and sends the OTP (the newest mail first)."""
from __future__ import annotations

import logging
import time
from contextlib import suppress
from html import escape
from urllib.parse import urlsplit

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.constants import PROXY_GIVE_DEFAULT, PROXY_INLINE_MAX, TEMPMAIL_POLL_GAP, TEMPMAIL_POLL_ROUNDS
from telegram_manager.core import tempmail
from telegram_manager.core.proxy import PROXY_TEST_MAX
from telegram_manager.core.proxysource import GIVE_MAX, USE_MODES, ProxySourceError, render_give
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)

_SCHEME_WORDS = {"http": "http", "https": "http", "socks4": "socks4", "socks5": "socks5", "socks": "socks5"}
_SOURCE_WORDS = ("url", "manual", "all")
_PX_BACK = keyboards.back("px|home", "🌐 Proxies")
_USE_TEXT = {"both": "Manual + URL", "manual": "Manual only", "url": "URL only"}


def _ago(stamp: float) -> str:
    if not stamp:
        return "never"
    seconds = max(0, int(time.time() - stamp))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def parse_getproxy_args(raw: str) -> tuple[int, str, str | None]:
    """'/getproxy 30 socks5 manual' -> (30, 'manual', 'socks5'). Any order; a missing number is PROXY_GIVE_DEFAULT."""
    count, source, scheme = PROXY_GIVE_DEFAULT, "url", None
    for word in raw.lower().replace(",", " ").split():
        if word.isdigit():
            count = max(1, min(int(word), GIVE_MAX))
        elif word in _SCHEME_WORDS:
            scheme = _SCHEME_WORDS[word]
        elif word in _SOURCE_WORDS:
            source = word
    return count, source, scheme


class UtilityHandlers(HandlerBase):
    start = None  # StartHandlers, set by the registry (the /manual and /aimode switches live there)

    # ==== 🌐 proxies ==============================================================================================
    async def proxy_command(self, update, context) -> None:
        if await self.guard.allow(update):
            await self.hub_screen(update, context)

    async def hub(self, update, context) -> None:
        """px|home, and the 🌐 Proxies button."""
        if await self.entry(update):
            await self.hub_screen(update, context)

    async def hub_screen(self, update, context=None) -> None:
        uid = self.uid(update)
        source = await self.manager.proxy_source_get(uid)
        counts = await self.manager.proxy_counts(uid)
        rows = await self.store.accounts.list(uid)
        auto = sum(1 for r in rows if r.get("proxy_auto"))
        fixed = sum(1 for r in rows if r.get("proxy_id") and not r.get("proxy_auto"))
        host = urlsplit(source["url"]).netloc or source["url"]
        link = f"{escape(host)} ({'built-in' if source['is_default'] else 'yours'})"
        text = (
            "🌐 <b>Proxies</b>\n\n"
            f"✋ Manual: <b>{counts['manual']}</b>   🔗 From URL: <b>{counts['url']}</b>\n"
            f"🔗 Link: {link}\n"
            f"🔄 Last fetch: {_ago(source['fetched_at'])}" + (f" ({source['last_count']} proxies)" if source["last_count"] else "") + "\n"
            f"🔀 Accounts rotate through: <b>{_USE_TEXT[source['use']]}</b>\n"
            f"📱 Accounts: auto {auto} · fixed {fixed} · direct {len(rows) - auto - fixed}\n\n"
            f"<i>🎁 Get: up to {PROXY_INLINE_MAX} come as a message, more than that as a .txt file. "
            "The URL works with no setup.</i>"
        )
        await ui.show(update, text, keyboards.proxy_hub(source["use"]), "HTML")

    async def _send_lines(self, update, lines: list[str], title: str, markup) -> None:
        """Hand the proxies over: a message up to PROXY_INLINE_MAX, a .txt file beyond."""
        message = update.effective_message
        out = render_give(lines, PROXY_INLINE_MAX)
        if out["kind"] == "text":
            await message.reply_text(f"{title}\n{out['html']}", reply_markup=markup, parse_mode="HTML")
        else:
            await message.reply_document(document=out["data"], filename=out["filename"], caption=f"{title}\nFile mein {len(lines)} proxies hain.",
                                         reply_markup=markup, parse_mode="HTML")

    async def give(self, update, context, count: int, source: str = "url", scheme: str | None = None) -> None:
        message = update.effective_message
        status = None
        with suppress(Exception):
            status = await message.reply_text("⏳ Proxies la raha hoon…")
        try:
            result = await self.manager.proxy_give(self.uid(update), count, source, scheme)
        except (ProxySourceError, ValueError) as exc:
            await self._finish_status(status, message, f"❌ {exc}", _PX_BACK)
            return
        except Exception as exc:
            logger.exception("proxy give failed")
            await self._finish_status(status, message, f"❌ {describe_error(exc)}", _PX_BACK)
            return
        if status is not None:
            with suppress(Exception):
                await status.delete()
        lines = result["lines"]
        title = f"🎁 <b>{len(lines)} proxies</b> ({escape(result['source'])})"
        if len(lines) < result["requested"]:
            title += f" — {result['requested']} maange the, itni hi mili"
        if result["skipped_login"]:
            title += f"\n🔒 {result['skipped_login']} login wali proxies nahi di (password kabhi share nahi hota)"
        if result["note"]:
            title += f"\n⚠️ {escape(result['note'])}"
        await self._send_lines(update, lines, title, keyboards.proxy_after_give(count))

    @staticmethod
    async def _finish_status(status, message, text: str, markup) -> None:
        if status is not None:
            with suppress(Exception):
                await status.edit_text(text, reply_markup=markup)
                return
        await message.reply_text(text, reply_markup=markup)

    async def getproxy_command(self, update, context) -> None:
        """/getproxy [number] [http|socks5|socks4] [url|manual|all]"""
        if not await self.guard.allow(update):
            return
        count, source, scheme = parse_getproxy_args(self.command_args(update))
        await self.give(update, context, count, source, scheme)

    async def proxyurl_command(self, update, context) -> None:
        """/proxyurl <link> sets the link and fetches at once; /proxyurl default resets it; /proxyurl shows it."""
        if not await self.guard.allow(update):
            return
        raw = self.command_args(update).strip()
        uid = self.uid(update)
        if not raw:
            source = await self.manager.proxy_source_get(uid)
            await ui.show(update, f"🔗 Proxy link now ({'built-in' if source['is_default'] else 'yours'}):\n{source['url']}\n\n"
                                  "/proxyurl <link> to change it · /proxyurl default to reset.", _PX_BACK)
            return
        try:
            await self.manager.proxy_source_set(uid, url=raw)
        except ValueError as exc:
            await ui.show(update, f"❌ {exc}", _PX_BACK)
            return
        await self._refresh(update)

    async def _refresh(self, update) -> None:
        await ui.show(update, "⏳ URL se proxies la raha hoon… (free server ko jagne mein 30-60 sec lag sakte hain)")
        try:
            result = await self.manager.proxy_url_refresh(self.uid(update))
        except (ProxySourceError, ValueError) as exc:
            await ui.show(update, f"❌ {exc}", _PX_BACK)
            return
        await ui.show(update, f"✅ {result['fetched']} proxies mili · {result['added']} nayi · {result['dropped']} purani hata di\n"
                              f"🔗 URL pool: {result['url_total']}   ✋ Manual: {result['manual_total']}", _PX_BACK)

    async def proxy_button(self, update, context) -> None:
        """px|get|<n> · gm · ref · use · test · clean · clrurl · add · url · acc|<auto|off> · go|<auto|off>"""
        if not await self.entry(update):
            return
        parts = (update.callback_query.data or "").split("|")[1:]
        action, value = (parts + ["", ""])[:2]
        uid = self.uid(update)
        if action == "home":
            await self.hub_screen(update, context)
        elif action == "get":
            await self.give(update, context, int(value) if value.isdigit() else PROXY_GIVE_DEFAULT)
        elif action == "gm":
            await self.give(update, context, GIVE_MAX, "manual")
        elif action == "ref":
            await self._refresh(update)
        elif action == "use":
            current = (await self.manager.proxy_source_get(uid))["use"]
            await self.manager.proxy_source_set(uid, use=USE_MODES[(USE_MODES.index(current) + 1) % len(USE_MODES)])
            await self.hub_screen(update, context)
        elif action == "add":
            await self.engine.start(update, context, "px_add")
        elif action == "url":
            await self.engine.start(update, context, "px_url")
        elif action == "test":
            await self._test(update)
        elif action == "clean":
            await self._clean(update)
        elif action == "clrurl":
            count = await self.manager.proxy_url_clear(uid)
            await ui.show(update, f"🗑 {count} URL proxies deleted. Manual ones are untouched.", _PX_BACK)
        elif action == "acc" and value in ("auto", "off"):
            await self._confirm_accounts(update, value)
        elif action == "go" and value in ("auto", "off"):
            await self._apply_accounts(update, value)

    async def _test(self, update) -> None:
        uid = self.uid(update)
        pool = await self.manager.proxy_pool(uid)
        if not pool:
            await ui.show(update, "The pool is empty: tap 🔄 Refresh URL list or ➕ Add manual first.", _PX_BACK)
            return
        await ui.show(update, f"🧪 Testing {min(len(pool), PROXY_TEST_MAX)} proxies through Telegram… (1-2 minutes)")
        try:
            results = await self.manager.proxy_test(uid, [r["id"] for r in pool[:PROXY_TEST_MAX]])
        except Exception as exc:
            await ui.show(update, f"❌ {describe_error(exc)}", _PX_BACK)
            return
        good = sorted((r for r in results if r["ok"]), key=lambda r: r["ms"])
        lines = [f"🧪 {len(good)} of {len(results)} work."] + [f"✅ {r['label']} · {r['ms']} ms" for r in good[:10]]
        if len(good) < len(results):
            lines.append("Tap 🗑 Remove dead to clean the rest.")
        await ui.show(update, "\n".join(lines), _PX_BACK)

    async def _clean(self, update) -> None:
        uid = self.uid(update)
        dead = [r["id"] for r in await self.manager.proxy_pool(uid) if (self.manager.proxy_health(uid, r["id"]) or {}).get("ok") is False]
        if not dead:
            await ui.show(update, "No dead proxy known yet. Tap 🧪 Test first.", _PX_BACK)
            return
        await ui.show(update, f"🗑 {await self.manager.proxy_remove(uid, dead)} dead proxies deleted.", _PX_BACK)

    async def _confirm_accounts(self, update, choice: str) -> None:
        uid = self.uid(update)
        rows = await self.store.accounts.list(uid)
        if not rows:
            await ui.show(update, "You have no connected accounts yet.", _PX_BACK)
            return
        what = ("use the proxy pool with automatic retry (a failing proxy is skipped, the next working one is used)" if choice == "auto"
                else "go back to a direct connection (no proxy)")
        await ui.show(update, f"🌐 {len(rows)} account(s) will {what}. They reconnect now.\n\nContinue?",
                      keyboards.confirm_pair(f"px|go|{choice}", "px|home", "✅ Yes"))

    async def _apply_accounts(self, update, choice: str) -> None:
        uid = self.uid(update)
        ids = [str(r["id"]) for r in await self.store.accounts.list(uid)]
        await ui.show(update, "⏳ Applying…")
        try:
            result = await self.manager.proxy_assign(uid, ids, choice)
        except (ProxySourceError, ValueError) as exc:
            await ui.show(update, f"❌ {exc}", _PX_BACK)
            return
        lines = [f"🌐 Done on {result.ok} of {result.total} account(s)."]
        if result.errors:
            lines += ["", "⚠️ Problems:"] + result.errors[:10]
        await ui.show(update, "\n".join(lines), _PX_BACK)

    # ==== 📧 temp mail =============================================================================================
    async def tempmail_command(self, update, context) -> None:
        """/tempmail shows (or makes) your address · /tempmail new makes another one."""
        if not await self.guard.allow(update):
            return
        await self._mail_card(update, new=self.command_args(update).strip().lower() in ("new", "naya", "nayi"))

    async def tm_home(self, update, context) -> None:
        if await self.entry(update):
            await self._mail_card(update, new=False)

    async def _mail_card(self, update, new: bool, status: str | None = None) -> None:
        try:
            cfg = await tempmail.mailbox_for(self.store.settings, self.uid(update), new=new)
        except tempmail.TempMailError as exc:
            await ui.show(update, f"❌ {exc}", keyboards.back("menu", "⚙️ Menu"))
            return
        note = status or ("🆕 Naya address ban gaya. Isse kisi site par use karo, phir <b>📥 Get</b> dabao." if new
                          else "Mail aane ke baad <b>📥 Get OTP / Inbox</b> dabao.")
        await ui.show(update, tempmail.card_html(cfg["address"], note), keyboards.tempmail_keyboard(), "HTML")

    async def tm_button(self, update, context) -> None:
        """tm|home · new · get"""
        if not await self.entry(update):
            return
        action = self.arg(update)
        if action == "new":
            await self._mail_card(update, new=True)
        elif action == "get":
            await self._get_mail(update, context)
        else:
            await self._mail_card(update, new=False)

    async def _get_mail(self, update, context) -> None:
        """Look at the inbox (a few times if it is empty). New mails are sent as messages, the OTP first; the buttons
        move to the last message so Get is always at the bottom of the chat."""
        uid = self.uid(update)
        query = update.callback_query
        address = (await self.store.settings.get_tempmail(uid)).get("address") or ""
        if address:
            await ui.show(update, tempmail.card_html(address, "⏳ Inbox dekh raha hoon…"), None, "HTML")
        try:
            address, mails = await tempmail.fetch_new(self.store.settings, uid, TEMPMAIL_POLL_ROUNDS, TEMPMAIL_POLL_GAP)
        except tempmail.TempMailError as exc:
            await ui.show(update, tempmail.card_html(address or "—", f"❌ {escape(str(exc))}"), keyboards.tempmail_keyboard(), "HTML")
            return
        if not mails:
            stamp = time.strftime("%H:%M:%S")
            await ui.show(update, tempmail.card_html(address, f"📭 Abhi koi mail nahi aaya ({stamp}). Thodi der baad dobara <b>📥 Get</b> dabao."),
                          keyboards.tempmail_keyboard(), "HTML")
            return
        # the card above loses its buttons; the newest message below gets them
        with suppress(Exception):
            await query.edit_message_text(tempmail.card_html(address, f"📬 {len(mails)} naya mail aaya — neeche dekho 👇"), parse_mode="HTML")
        chat_id = update.effective_chat.id
        for index, mail in enumerate(mails):
            last = index == len(mails) - 1
            await context.bot.send_message(chat_id, tempmail.mail_html(mail), parse_mode="HTML",
                                           reply_markup=keyboards.tempmail_keyboard() if last else None)

    # ==== 🤖 / 🖐 instant mode switch ================================================================================
    async def switch_command(self, update, context, mode: str) -> None:
        """/aimode and /manual."""
        if await self.guard.allow(update) and self.start is not None:
            await self.start.switch_mode(update, context, mode)

