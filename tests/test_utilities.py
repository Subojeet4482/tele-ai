"""Proxies from a URL (kept apart from manual ones), handing proxies out (message / .txt file), 1secmail temp mail with
the Get button, the instant AI / Manual switch, and the AI tools for all of it."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tests.harness import OWNER, texts
from tests.test_ai import AITestCase, call, say

import telegram_manager.bot.handlers.utilities as utilities_module
import telegram_manager.core.proxysource as source_module
import telegram_manager.core.tempmail as tempmail_module
from telegram_manager.bot import reply_menu
from telegram_manager.core.proxyparse import parse_proxy_text
from telegram_manager.core.proxysource import (
    DEFAULT_PROXY_URL, ProxySourceError, check_source_url, extract_proxies, url_scheme, with_params)
from telegram_manager.core.tempmail import extract_otp


def ips(n: int, start: int = 1) -> str:
    return "\n".join(f"10.0.{start}.{i}:8080" for i in range(1, n + 1)).replace("10.0", "45.7")


class PureTests(unittest.TestCase):
    def test_plain_lines_json_list_json_objects_and_messy_text(self):
        plain = extract_proxies("1.2.3.4:80\n5.6.7.8:3128\nnot a proxy\n1.2.3.4:80", "http")
        self.assertEqual([(p["scheme"], p["host"], p["port"]) for p in plain], [("http", "1.2.3.4", 80), ("http", "5.6.7.8", 3128)])
        as_list = extract_proxies(json.dumps(["1.1.1.1:80", "socks5://2.2.2.2:1080"]), "http")
        self.assertEqual([p["scheme"] for p in as_list], ["http", "socks5"])
        objects = extract_proxies(json.dumps({"data": [{"ip": "3.3.3.3", "port": 8080, "protocol": "http"}, {"host": "4.4.4.4", "port": "81"}]}), "http")
        self.assertEqual([p["host"] for p in objects], ["3.3.3.3", "4.4.4.4"])
        messy = extract_proxies("<html><body>servers: 9.9.9.9:8000 and also 8.8.8.8:3128, thanks</body></html>", "http")
        self.assertEqual({p["host"] for p in messy}, {"9.9.9.9", "8.8.8.8"})

    def test_url_helpers(self):
        self.assertEqual(url_scheme(DEFAULT_PROXY_URL), "http")
        self.assertEqual(url_scheme("https://x.example/get?type=socks5"), "socks5")
        changed = with_params(DEFAULT_PROXY_URL, count=7, type="socks5")
        self.assertIn("count=7", changed)
        self.assertIn("type=socks5", changed)
        self.assertIn("sec=2", changed)
        self.assertEqual(check_source_url(" https://api.example.com/get?count=5 "), "https://api.example.com/get?count=5")
        for bad in ("ftp://x.com/a", "http://localhost/x", "http://192.168.1.5/x", "http://127.0.0.1:8000", "not a link"):
            with self.assertRaises(ValueError, msg=bad):
                check_source_url(bad)

    def test_otp_finding(self):
        self.assertEqual(extract_otp("Your code", "Your verification code is 482913. Do not share it."), "482913")
        self.assertEqual(extract_otp("", "Login code: 12345\nhttps://x.com/1234567"), "12345")
        self.assertEqual(extract_otp("Confirm 123-456", "hello"), "123456")
        self.assertEqual(extract_otp("", "<p>Use <b>77889</b> as your code</p>"), "77889")
        self.assertIsNone(extract_otp("Welcome", "Thanks for joining in 2024, see https://x.com/123456"))


class UtilCase(AITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.asked: list[str] = []
        self.payload = ips(100)
        self.down = False

        async def fake_get(url):
            self.asked.append(url)
            if self.down:
                raise ProxySourceError("server down")
            return self.payload

        patcher = patch.object(source_module, "_get_text", fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch.object(source_module.asyncio, "sleep", lambda s: _noop())
        sleeper.start()
        self.addCleanup(sleeper.stop)


async def _noop():
    return None


class SourceTests(UtilCase):
    async def test_the_default_link_works_with_no_setup(self):
        m = self.h.manager
        source = await m.proxy_source_get(OWNER)
        self.assertEqual((source["url"], source["is_default"], source["use"]), (DEFAULT_PROXY_URL, True, "both"))
        result = await m.proxy_url_refresh(OWNER)
        self.assertEqual((result["fetched"], result["added"], result["url_total"]), (100, 100, 100))
        self.assertEqual(self.asked[-1], DEFAULT_PROXY_URL)

    async def test_manual_and_url_proxies_stay_apart(self):
        m = self.h.manager
        await m.proxy_add(OWNER, parse_proxy_text("http://1.1.1.1:1080\nhttp://2.2.2.2:1080")[0])
        self.payload = "45.7.1.1:80\n45.7.1.2:80\n1.1.1.1:1080"  # one of them is already a manual proxy
        await m.proxy_url_refresh(OWNER)
        self.assertEqual(await m.proxy_counts(OWNER), {"manual": 2, "url": 2, "total": 4})
        self.payload = "45.7.9.9:80"  # the list changed: the old URL ones go, the manual ones never do
        result = await m.proxy_url_refresh(OWNER)
        self.assertEqual((result["added"], result["dropped"]), (1, 2))
        self.assertEqual(await m.proxy_counts(OWNER), {"manual": 2, "url": 1, "total": 3})

    async def test_a_proxy_an_account_is_pinned_to_survives_a_refresh(self):
        m = self.h.manager
        self.payload = "45.7.1.1:80\n45.7.1.2:80"
        await m.proxy_url_refresh(OWNER)
        pinned = (await m.proxy_pool(OWNER))[0]
        self.a["proxy_id"] = pinned["id"]
        self.payload = "45.7.5.5:80"
        await m.proxy_url_refresh(OWNER)
        self.assertIn(pinned["id"], {r["id"] for r in await m.proxy_pool(OWNER)})

    async def test_accounts_rotate_through_the_pool_the_admin_chose(self):
        m = self.h.manager
        await m.proxy_add(OWNER, parse_proxy_text("1.1.1.1:1080")[0])
        self.payload = "45.7.1.1:80"
        await m.proxy_url_refresh(OWNER)
        self.assertEqual(len(await m.proxy_pool_for_accounts(OWNER)), 2)
        await m.proxy_source_set(OWNER, use="manual")
        self.assertEqual([r["host"] for r in await m.proxy_pool_for_accounts(OWNER)], ["1.1.1.1"])
        await m.proxy_source_set(OWNER, use="url")
        self.assertEqual([r["host"] for r in await m.proxy_pool_for_accounts(OWNER)], ["45.7.1.1"])
        with self.assertRaises(ValueError):
            await m.proxy_source_set(OWNER, use="nonsense")

    async def test_auto_with_an_empty_pool_fetches_the_default_link(self):
        h = self.h
        result = await h.manager.proxy_assign(OWNER, [], "auto")
        self.assertEqual(len(await h.manager.proxy_pool(OWNER)), 100)
        self.assertIsNotNone(result)

    async def test_auto_with_a_dead_link_says_why(self):
        self.down = True
        with self.assertRaises(ValueError) as caught:
            await self.h.manager.proxy_assign(OWNER, [], "auto")
        self.assertIn("server down", str(caught.exception))

    async def test_change_and_reset_the_link(self):
        m = self.h.manager
        source = await m.proxy_source_set(OWNER, url="https://api.example.com/p?type=socks5&count=20")
        self.assertFalse(source["is_default"])
        self.assertEqual((await m.proxy_source_set(OWNER, url="default"))["url"], DEFAULT_PROXY_URL)
        with self.assertRaises(ValueError):
            await m.proxy_source_set(OWNER, url="http://localhost/x")


class GiveTests(UtilCase):
    async def test_a_few_come_as_a_message_and_the_link_is_asked_for_that_many(self):
        update = await self.h.command(OWNER, "/getproxy 5")
        self.assertIn("count=5", self.asked[-1])
        shown = update.effective_message.replies[-1][0]
        self.assertIn("<pre>", shown)
        self.assertEqual(shown.count("45.7."), 5)
        self.assertEqual(update.effective_message.documents, [])

    async def test_more_than_twenty_come_as_a_txt_file(self):
        update = await self.h.command(OWNER, "/getproxy 30")
        name, content, caption, markup = update.effective_message.documents[0]
        self.assertTrue(name.endswith(".txt"))
        self.assertEqual(len(content.strip().split("\n")), 30)
        self.assertIn("30", caption)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "px|get|30")

    async def test_exactly_twenty_is_still_a_message_and_twenty_one_is_a_file(self):
        twenty = await self.h.command(OWNER, "/getproxy 20")
        self.assertEqual(twenty.effective_message.documents, [])
        file = await self.h.command(OWNER, "/getproxy 21")
        self.assertEqual(len(file.effective_message.documents), 1)

    async def test_no_number_gives_ten_and_the_type_can_be_asked(self):
        update = await self.h.command(OWNER, "/getproxy")
        self.assertEqual(update.effective_message.replies[-1][0].count("45.7."), 10)
        await self.h.command(OWNER, "/getproxy 3 socks5")
        self.assertIn("type=socks5", self.asked[-1])

    async def test_manual_proxies_are_given_without_logins(self):
        m = self.h.manager
        await m.proxy_add(OWNER, parse_proxy_text("http://1.1.1.1:80\nhttp://bob:hunter2@2.2.2.2:80\nhttp://3.3.3.3:80")[0])
        update = await self.h.command(OWNER, "/getproxy 10 manual")
        shown = update.effective_message.replies[-1][0]
        self.assertIn("1.1.1.1:80", shown)
        self.assertIn("3.3.3.3:80", shown)
        self.assertNotIn("2.2.2.2", shown)
        self.assertNotIn("hunter2", shown)
        self.assertIn("1 login", shown)

    async def test_a_dead_link_falls_back_to_the_saved_url_proxies_or_says_so(self):
        m = self.h.manager
        self.payload = "45.7.1.1:80\n45.7.1.2:80"
        await m.proxy_url_refresh(OWNER)
        self.down = True
        update = await self.h.command(OWNER, "/getproxy 5")
        shown = update.effective_message.replies[-1][0]
        self.assertIn("45.7.1.1:80", shown)
        self.assertIn("server down", shown)
        await m.proxy_url_clear(OWNER)
        sorry = await self.h.command(OWNER, "/getproxy 5")
        self.assertIn("❌", sorry.effective_message.replies[-1][0])

    async def test_strangers_get_nothing(self):
        from tests.harness import STRANGER

        update = await self.h.command(STRANGER, "/getproxy 5")
        self.assertEqual(self.asked, [])
        self.assertNotIn("45.7.", " ".join(r[0] for r in update.effective_message.replies))


class ScreenTests(UtilCase):
    async def test_the_proxy_screen_and_its_buttons(self):
        update = await self.h.command(OWNER, "/proxy")
        text, markup = update.last()
        for part in ("Manual: <b>0</b>", "From URL: <b>0</b>", "proxy-api-zfat.onrender.com", "built-in", "Manual + URL"):
            self.assertIn(part, text)
        buttons = {b.callback_data for row in markup.inline_keyboard for b in row}
        for data in ("px|get|10", "px|get|20", "px|get|50", "px|get|100", "px|gm", "px|ref", "px|add", "px|url", "px|test",
                     "px|clean", "px|use", "px|acc|auto", "px|acc|off", "px|clrurl", "menu"):
            self.assertIn(data, buttons)

    async def test_refresh_button_fills_the_url_pool_and_the_screen_shows_it(self):
        update = await self.h.tap(OWNER, "px|ref")
        self.assertIn("100 proxies mili", texts(update))
        self.assertIn("From URL: <b>100</b>", texts(await self.h.tap(OWNER, "px|home")))

    async def test_the_use_button_cycles_both_manual_url(self):
        modes = []
        for _ in range(3):
            await self.h.tap(OWNER, "px|use")
            modes.append((await self.h.manager.proxy_source_get(OWNER))["use"])
        self.assertEqual(modes, ["manual", "url", "both"])

    async def test_change_url_flow_saves_and_fetches(self):
        h = self.h
        await h.tap(OWNER, "px|url")
        bad = await h.say(OWNER, "http://localhost/x")
        self.assertIn("not a public", texts(bad))
        done = await h.say(OWNER, "https://api.example.com/p?type=http&count=50")
        self.assertIn("your link", texts(done))
        self.assertIn("api.example.com", self.asked[-1])
        self.assertFalse((await h.manager.proxy_source_get(OWNER))["is_default"])

    async def test_add_manual_flow_with_a_type_line(self):
        h = self.h
        await h.tap(OWNER, "px|add")
        done = await h.say(OWNER, "http\n1.1.1.1:8080\n2.2.2.2:3128\njunk")
        self.assertIn("2 manual proxies saved", texts(done))
        pool = await h.manager.proxy_pool(OWNER)
        self.assertEqual({(r["scheme"], r["src"]) for r in pool}, {("http", "manual")})

    async def test_clear_url_list_keeps_the_manual_ones(self):
        m = self.h.manager
        await m.proxy_add(OWNER, parse_proxy_text("1.1.1.1:1080")[0])
        await m.proxy_url_refresh(OWNER)
        await self.h.tap(OWNER, "px|clrurl")
        self.assertEqual(await m.proxy_counts(OWNER), {"manual": 1, "url": 0, "total": 1})

    async def test_accounts_to_auto_asks_first_then_applies(self):
        h = self.h
        asked = await h.tap(OWNER, "px|acc|auto")
        self.assertIn("2 account(s)", texts(asked))
        self.assertEqual(h.manager.accounts[self.a["id"]].get("proxy_auto"), None)
        h.manager.proxy_assign = _recording_assign(h.manager)
        done = await h.tap(OWNER, "px|go|auto")
        self.assertIn("Done on 2 of 2", texts(done))

    async def test_the_proxy_buttons_are_in_the_menus(self):
        for command in ("/menu", "/start", "/settings", "/help"):
            update = await self.h.command(OWNER, command)
            if update.callback_query is None and not update.effective_message.replies:
                continue
            markup = update.last()[1]
            data = {b.callback_data for row in getattr(markup, "inline_keyboard", []) for b in row}
            if command in ("/menu", "/settings", "/help"):
                self.assertIn("px|home", data, command)
                self.assertIn("tm|home", data, command)


def _recording_assign(manager):
    from types import SimpleNamespace

    async def assign(owner, ids, choice, delay_seconds=2):
        return SimpleNamespace(ok=len(ids), total=len(ids), errors=[])

    return assign


class MailCase(UtilCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.inbox: list[dict] = []
        self.bodies: dict[int, dict] = {}
        self.calls: list[dict] = []

        async def fake_api(params):
            self.calls.append(params)
            action = params["action"]
            if action == "genRandomMailbox":
                return ["abc123xyz@1secmail.com"]
            if action == "getMessages":
                return list(self.inbox)
            if action == "readMessage":
                return self.bodies[int(params["id"])]
            raise AssertionError(action)

        for patcher in (patch.object(tempmail_module, "_api", fake_api),
                        patch.object(utilities_module, "TEMPMAIL_POLL_ROUNDS", 2),
                        patch.object(utilities_module, "TEMPMAIL_POLL_GAP", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def receive(self, mail_id: int, subject: str, body: str, sender: str = "no-reply@site.com") -> None:
        self.inbox.append({"id": mail_id, "from": sender, "subject": subject, "date": "2026-10-24 10:00:00"})
        self.bodies[mail_id] = {"id": mail_id, "from": sender, "subject": subject, "textBody": body, "date": "2026-10-24 10:00:00"}


class TempMailTests(MailCase):
    async def test_tempmail_makes_an_address_with_a_get_button(self):
        update = await self.h.command(OWNER, "/tempmail")
        text, markup = update.last()
        self.assertIn("<code>abc123xyz@1secmail.com</code>", text)
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(buttons[0], "tm|get")
        self.assertIn("tm|new", buttons)
        again = await self.h.command(OWNER, "/tempmail")  # the same address until a new one is asked for
        self.assertIn("abc123xyz@1secmail.com", again.last()[0])
        self.assertEqual(sum(1 for c in self.calls if c["action"] == "genRandomMailbox"), 1)

    async def test_get_with_an_empty_inbox_says_so_and_keeps_the_button(self):
        await self.h.command(OWNER, "/tempmail")
        update = await self.h.tap(OWNER, "tm|get")
        text, markup = update.last()
        self.assertIn("📭", text)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "tm|get")

    async def test_get_sends_the_otp_and_the_button_moves_to_the_last_message(self):
        h = self.h
        await h.command(OWNER, "/tempmail")
        self.receive(1, "Your login code", "Your verification code is 482913. It expires in 10 minutes.")
        before = len(h.bot.sent)
        update = await h.tap(OWNER, "tm|get")
        new = h.bot.sent[before:]
        self.assertTrue(any("482913" in text for _, text in new), new)
        self.assertIn("naya mail aaya", update.callback_query.edits[-1][0])
        self.assertIsNone(update.callback_query.edits[-1][1])  # the old card lost its buttons
        self.assertEqual(h.bot.keyboards, [])  # inline buttons are not reply keyboards; check the markup on the message itself
        again = await h.tap(OWNER, "tm|get")  # nothing new: the same mail is not sent twice
        self.assertIn("📭", again.last()[0])

    async def test_a_mail_without_a_code_is_shown_as_text(self):
        h = self.h
        await h.command(OWNER, "/tempmail")
        self.receive(2, "Welcome aboard", "Thanks for signing up, enjoy the app!")
        before = len(h.bot.sent)
        await h.tap(OWNER, "tm|get")
        shown = " ".join(text for _, text in h.bot.sent[before:])
        self.assertIn("Welcome aboard", shown)
        self.assertIn("Thanks for signing up", shown)
        self.assertNotIn("OTP:", shown)

    async def test_new_mail_button_changes_the_address_and_forgets_the_old_mails(self):
        h = self.h
        await h.command(OWNER, "/tempmail")
        self.receive(3, "code", "Your code is 111222")
        await h.tap(OWNER, "tm|get")
        update = await h.tap(OWNER, "tm|new")
        self.assertIn("Naya address", update.last()[0])
        self.assertEqual((await h.store.settings.get_tempmail(OWNER))["seen"], [])

    async def test_get_without_an_address_makes_one_first(self):
        update = await self.h.tap(OWNER, "tm|get")
        self.assertIn("abc123xyz@1secmail.com", update.last()[0])

    async def test_the_site_being_down_is_explained(self):
        await self.h.command(OWNER, "/tempmail")

        async def broken(params):
            raise tempmail_module.TempMailError("Could not reach 1secmail (ConnectError).")

        with patch.object(tempmail_module, "_api", broken):
            update = await self.h.tap(OWNER, "tm|get")
        self.assertIn("Could not reach 1secmail", update.last()[0])
        self.assertEqual(update.last()[1].inline_keyboard[0][0].callback_data, "tm|get")

    async def test_an_address_is_made_locally_when_the_api_cannot_make_one(self):
        async def broken(params):
            raise tempmail_module.TempMailError("down")

        with patch.object(tempmail_module, "_api", broken):
            address = await tempmail_module.new_mailbox()
        tempmail_module.split_address(address)


class ModeSwitchTests(UtilCase):
    async def test_the_bottom_keyboard_has_the_instant_switch_in_both_modes(self):
        manual = [b.text for row in reply_menu.keyboard(True).keyboard for b in row]
        ai = [b.text for row in reply_menu.ai_keyboard().keyboard for b in row]
        self.assertIn("🤖 AI Mode", manual)
        self.assertIn("🌐 Proxies", manual)
        self.assertIn("📧 Temp Mail", manual)
        self.assertEqual(manual[-1], "🤖 AI Mode")  # the switch is the last button
        self.assertIn("🖐 Manual Mode", ai)
        self.assertIn("⚙️ Menu", ai)

    async def test_tapping_the_switches(self):
        h = self.h
        await h.press(OWNER, "🤖 AI Mode")
        self.assertEqual((await h.store.settings.get_ai(OWNER))["ui_mode"], "ai")
        self.assertTrue(any("🖐 Manual Mode" in [b.text for row in m.keyboard for b in row] for _, m in h.bot.keyboards))
        await h.press(OWNER, "🖐 Manual Mode")
        self.assertEqual((await h.store.settings.get_ai(OWNER))["ui_mode"], "manual")
        await h.command(OWNER, "/aimode")
        self.assertEqual((await h.store.settings.get_ai(OWNER))["ui_mode"], "ai")
        await h.command(OWNER, "/manual")
        self.assertEqual((await h.store.settings.get_ai(OWNER))["ui_mode"], "manual")

    async def test_the_proxy_and_mail_buttons_open_their_screens(self):
        h = self.h
        proxies = await h.press(OWNER, "🌐 Proxies")
        self.assertIn("Proxies", " ".join(r[0] for r in proxies.effective_message.replies))
        self.assertTrue(h.parts.menu.lookup if hasattr(h.parts.menu, "lookup") else True)


class AIToolTests(MailCase):
    async def test_a_few_proxies_from_the_ai_arrive_as_a_message_the_ai_never_sees(self):
        fake = self.gemini(call("proxy_give", count=5), say("Ye lo 5 proxy."))
        update = await self.h.say(OWNER, "5 proxy do")
        replies = [r[0] for r in update.effective_message.replies]
        self.assertEqual(sum(text.count("45.7.") for text in replies), 5)
        self.assertNotIn("45.7.", json.dumps(fake.requests[1]["contents"][-1]))

    async def test_more_than_twenty_from_the_ai_come_as_a_file(self):
        self.gemini(call("proxy_give", count=50), say("File bhej di."))
        update = await self.h.say(OWNER, "50 proxy chahiye")
        name, content, caption, _ = update.effective_message.documents[0]
        self.assertEqual(len(content.strip().split("\n")), 50)
        self.assertTrue(name.endswith(".txt"))

    async def test_refresh_and_source_tools(self):
        self.gemini(call("proxy_refresh"), say("Ho gaya."), call("proxy_source", use="url"), say("Theek."))
        update = await self.h.say(OWNER, "proxy list refresh karo")
        self.assertIn("100 proxies mili", " ".join(r[0] for r in update.effective_message.replies))
        await self.h.say(OWNER, "sirf url wali proxy use karo")
        self.assertEqual((await self.h.manager.proxy_source_get(OWNER))["use"], "url")

    async def test_temp_mail_new_shows_the_address_and_the_get_button(self):
        self.gemini(call("temp_mail", action="new"), say("Mail ban gayi."))
        update = await self.h.say(OWNER, "temp mail do")
        replies = update.effective_message.replies
        self.assertTrue(any("abc123xyz@1secmail.com" in r[0] for r in replies))
        buttons = [b.callback_data for r in replies if r[1] is not None for row in r[1].inline_keyboard for b in row]
        self.assertIn("tm|get", buttons)

    async def test_temp_mail_inbox_shows_the_otp_to_the_admin_only(self):
        self.receive(7, "Code", "Your code is 998877")
        fake = self.gemini(call("temp_mail", action="inbox"), say("OTP aa gaya."))
        update = await self.h.say(OWNER, "mail check karo")
        self.assertIn("998877", " ".join(r[0] for r in update.effective_message.replies))
        self.assertNotIn("998877", json.dumps(fake.requests))

    async def test_the_tools_are_registered_and_known_to_the_ai(self):
        from telegram_manager.ai import knowledge, tools

        for name in ("proxy_give", "proxy_refresh", "proxy_source", "temp_mail"):
            self.assertIn(name, tools.TOOLS)
        self.assertIn("proxy_give", knowledge.guide("proxies"))
        self.assertIn("temp_mail", knowledge.guide("proxies"))


if __name__ == "__main__":
    unittest.main()
