"""Proxies: reading lists, the pool, connecting with retry, failover, giving accounts a proxy, and the AI tools."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.harness import OWNER, texts
from tests.test_ai import AITestCase, call, say, _token

import telegram_manager.core.proxy as proxy_module
import telegram_manager.core.proxysource as source_module
from telegram_manager.core.proxysource import ProxySourceError
from telethon import errors
from telegram_manager.core.proxyparse import describe, looks_like_proxy_file, parse_proxy_line, parse_proxy_text, telethon_kwargs

SAMPLE = """# my list
socks5://user:pa%40ss@1.2.3.4:1080
http://5.6.7.8:8080
9.9.9.9:1080
8.8.4.4:3128:bob:secret
bob:pw@proxy.example.com:1080
tg://proxy?server=mt.example.com&port=443&secret=ee1234567890abcdef1234567890abcdef
https://t.me/proxy?server=1.1.1.1&port=443&secret=dd00112233445566778899aabbccddeeff
9.9.9.9:1080
not a proxy
1.2.3.4:99999
"""


class ParseTests(unittest.TestCase):
    def test_every_format(self):
        proxies, bad = parse_proxy_text(SAMPLE)
        self.assertEqual(bad, 2)  # "not a proxy" and the port 99999
        self.assertEqual([describe(p) for p in proxies], [
            "socks5 1.2.3.4:1080 (login)", "http 5.6.7.8:8080", "socks5 9.9.9.9:1080", "socks5 8.8.4.4:3128 (login)",
            "socks5 proxy.example.com:1080 (login)", "mtproxy mt.example.com:443", "mtproxy 1.1.1.1:443"])  # the duplicate is dropped
        self.assertEqual(proxies[0]["password"], "pa@ss")  # %40 decoded
        self.assertEqual((proxies[3]["user"], proxies[3]["password"]), ("bob", "secret"))

    def test_default_scheme_for_bare_lines(self):
        self.assertEqual(parse_proxy_line("1.2.3.4:8080", "http")["scheme"], "http")
        self.assertEqual(parse_proxy_line("1.2.3.4:8080", "nonsense")["scheme"], "socks5")

    def test_not_proxies(self):
        for line in ("", "# comment", "hello", "1.2.3.4", "1.2.3.4:0", "ftp://1.2.3.4:21", "tg://proxy?server=a.b&port=1&secret=zz"):
            self.assertIsNone(parse_proxy_line(line), line)

    def test_a_proxy_file_is_told_from_a_session_file(self):
        self.assertTrue(looks_like_proxy_file(SAMPLE))
        self.assertFalse(looks_like_proxy_file("1" + "A" * 300 + "\n1" + "B" * 300))
        self.assertFalse(looks_like_proxy_file(""))

    def test_telethon_arguments(self):
        socks = telethon_kwargs({"scheme": "socks5", "host": "1.2.3.4", "port": 1080, "user": "u", "password": "p", "secret": ""})
        self.assertEqual(socks, {"proxy": {"proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080, "rdns": True, "username": "u", "password": "p"}})
        mt = telethon_kwargs({"scheme": "mtproxy", "host": "mt.example.com", "port": 443, "user": "", "password": "", "secret": "ee00"})
        self.assertEqual(mt["proxy"], ("mt.example.com", 443, "ee00"))
        self.assertIn("connection", mt)


class FakeLink:
    """A Telethon client that connects only through proxies that are not in `broken`."""

    def __init__(self, proxy, broken, authorized=True):
        self.proxy, self.broken, self.authorized = proxy, broken, authorized
        self.connected = False
        self.closed = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized


class ProxyCase(AITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        m = self.h.manager
        self.broken: set[str] = set()
        self.made: list[FakeLink] = []
        self.dead_session = False

        def new_client(session, api_id, api_hash, proxy=None):
            link = FakeLink(proxy, self.broken)
            self.made.append(link)
            return link

        async def connect(client, timeout=45):
            if self.dead_session:
                raise errors.AuthKeyUnregisteredError()
            if client.proxy and client.proxy["host"] in self.broken:
                raise ConnectionError("proxy unreachable")
            client.connected = True

        async def disconnect(client):
            client.connected = False
            client.closed = True

        m._new_client, m._connect_client, m._safe_disconnect = new_client, connect, disconnect
        self.registered: list = []
        m._register_handlers = lambda account_id, client: self.registered.append((account_id, client))
        for account in (self.a, self.b):  # a stored session and API, like a real account
            row = {"api_id": 1, "api_hash_enc": m.secret_box.encrypt("hash"), "session_enc": m.secret_box.encrypt("session")}
            account.update(row)
            await self.h.store.accounts.update(account["id"], row)
        await m.proxy_add(OWNER, parse_proxy_text("1.1.1.1:1080\n2.2.2.2:1080\n3.3.3.3:1080\n4.4.4.4:1080")[0])
        self.pool = await m.proxy_pool(OWNER)

    def host(self, number: int) -> str:
        return self.pool[number - 1]["host"]

    async def stored(self, account) -> dict:
        return await self.h.store.accounts.get(account["id"])


class PoolTests(ProxyCase):
    async def test_passwords_are_stored_encrypted_and_duplicates_skipped(self):
        m = self.h.manager
        result = await m.proxy_add(OWNER, parse_proxy_text("socks5://bob:hunter2@5.5.5.5:1080\n1.1.1.1:1080")[0])
        self.assertEqual((result["added"], result["duplicates"], result["total"]), (1, 1, 5))
        row = (await m.proxy_pool(OWNER))[-1]
        self.assertNotIn("hunter2", json.dumps(row))
        self.assertEqual(m._plain(row)["password"], "hunter2")

    async def test_remove_sends_accounts_back_to_direct(self):
        m = self.h.manager
        self.a["proxy_id"] = self.pool[0]["id"]
        self.assertEqual(await m.proxy_remove(OWNER, [self.pool[0]["id"]]), 1)
        self.assertEqual(self.a["proxy_id"], "")
        self.assertEqual(len(await m.proxy_pool(OWNER)), 3)

    async def test_the_new_client_gets_the_proxy(self):
        class Recorder:
            def __init__(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs

        from telegram_manager.core.base import ManagerBase
        with patch("telegram_manager.core.base.TelegramClient", Recorder):
            client = ManagerBase._new_client(self.h.manager, "s", 1, "h", proxy={"scheme": "http", "host": "5.6.7.8", "port": 8080, "user": "", "password": "", "secret": ""})
            direct = ManagerBase._new_client(self.h.manager, "s", 1, "h")
        self.assertEqual(client.kwargs["proxy"], {"proxy_type": "http", "addr": "5.6.7.8", "port": 8080, "rdns": True})
        self.assertNotIn("proxy", direct.kwargs)


class RetryTests(ProxyCase):
    async def test_the_next_working_proxy_is_used_and_remembered(self):
        m = self.h.manager
        self.broken |= {self.host(1), self.host(2)}
        self.a["proxy_auto"], self.a["proxy_id"] = True, self.pool[0]["id"]
        client = await m._connect_via_proxies(self.a, "session", 1, "hash")
        self.assertEqual(client.proxy["host"], self.host(3))
        self.assertEqual((await self.stored(self.a))["proxy_id"], self.pool[2]["id"])  # next start begins with the one that works
        self.assertTrue(m.proxy_cooling(OWNER, self.pool[0]["id"]))  # the failed ones rest
        self.assertTrue(all(link.closed for link in self.made if link.proxy["host"] in self.broken))  # and are closed again

    async def test_a_resting_proxy_is_tried_last_not_never(self):
        m = self.h.manager
        for number in (1, 2, 3):
            m._cool(OWNER, self.pool[number - 1]["id"])
        self.a["proxy_auto"] = True
        order = [r["host"] for r in await m._proxy_candidates(self.a)]
        self.assertEqual(order[0], self.host(4))
        self.assertEqual(set(order), {self.host(n) for n in (1, 2, 3, 4)})

    async def test_every_proxy_failing_is_an_error_and_never_a_direct_connection(self):
        m = self.h.manager
        self.broken |= {self.host(n) for n in (1, 2, 3, 4)}
        self.a["proxy_auto"] = True
        with self.assertRaises(ConnectionError) as caught:
            await m._connect_via_proxies(self.a, "session", 1, "hash")
        self.assertIn("Every proxy failed", str(caught.exception))
        self.assertEqual(len(self.made), proxy_module.PROXY_MAX_TRIES)
        self.assertTrue(all(link.proxy is not None for link in self.made))  # never a direct try: that would show the real IP

    async def test_a_fixed_proxy_is_tried_twice_and_no_other(self):
        m = self.h.manager
        self.broken.add(self.host(2))
        self.a["proxy_id"] = self.pool[1]["id"]
        with self.assertRaises(ConnectionError):
            await m._connect_via_proxies(self.a, "session", 1, "hash")
        self.assertEqual([link.proxy["host"] for link in self.made], [self.host(2)] * proxy_module.PROXY_FIXED_RETRIES)

    async def test_a_dead_session_is_not_blamed_on_the_proxy(self):
        self.dead_session = True
        self.a["proxy_auto"] = True
        with self.assertRaises(errors.AuthKeyUnregisteredError):
            await self.h.manager._connect_via_proxies(self.a, "session", 1, "hash")
        self.assertEqual(len(self.made), 1)  # no pointless round of other proxies
        self.assertFalse(self.h.manager.proxy_cooling(OWNER, self.pool[0]["id"]))

    async def test_an_empty_pool_is_explained(self):
        await self.h.manager.proxy_remove(OWNER, [r["id"] for r in self.pool])
        self.a["proxy_auto"] = True
        with self.assertRaises(ConnectionError) as caught:
            await self.h.manager._connect_via_proxies(self.a, "session", 1, "hash")
        self.assertIn("pool is empty", str(caught.exception))


class AssignTests(ProxyCase):
    async def test_auto_reconnects_the_account_through_a_proxy_and_saves_it(self):
        m = self.h.manager
        old = m.clients[self.a["id"]]
        result = await m.proxy_assign(OWNER, [self.a["id"]], "auto")
        self.assertEqual((result.ok, result.failed), (1, 0))
        new = m.clients[self.a["id"]]
        self.assertIsNot(new, old)
        self.assertIsNotNone(new.proxy)
        self.assertEqual(self.registered[-1], (self.a["id"], new))  # handlers moved to the new connection
        stored = await self.stored(self.a)
        self.assertTrue(stored["proxy_auto"])

    async def test_a_failed_switch_keeps_the_working_connection_and_setting(self):
        m = self.h.manager
        old = m.clients[self.a["id"]]
        self.broken |= {self.host(n) for n in (1, 2, 3, 4)}
        result = await m.proxy_assign(OWNER, [self.a["id"]], "auto")
        self.assertEqual(result.ok, 0)
        self.assertIs(m.clients[self.a["id"]], old)  # still online exactly as before
        self.assertFalse(self.a.get("proxy_auto"))
        self.assertFalse((await self.stored(self.a)).get("proxy_auto"))

    async def test_a_pinned_proxy_and_back_to_direct(self):
        m = self.h.manager
        await m.proxy_assign(OWNER, [self.a["id"]], self.pool[1]["id"])
        self.assertEqual(m.clients[self.a["id"]].proxy["host"], self.host(2))
        await m.proxy_assign(OWNER, [self.a["id"]], "off")
        self.assertIsNone(m.clients[self.a["id"]].proxy)
        self.assertEqual((await self.stored(self.a))["proxy_id"], "")

    async def test_unknown_choices_are_refused(self):
        with self.assertRaises(ValueError):
            await self.h.manager.proxy_assign(OWNER, [self.a["id"]], "nope")
        await self.h.manager.proxy_remove(OWNER, [r["id"] for r in self.pool])

        async def link_down(url):  # an empty pool now tries the proxy link first: keep the test off the network
            raise ProxySourceError("down")

        with patch.object(source_module, "_get_text", link_down):
            with self.assertRaises(ValueError):
                await self.h.manager.proxy_assign(OWNER, [self.a["id"]], "auto")


class FailoverTests(ProxyCase):
    async def test_a_dropped_proxy_account_moves_to_another_proxy(self):
        m = self.h.manager
        await m.proxy_assign(OWNER, [self.a["id"]], self.pool[0]["id"])
        self.assertEqual(m.clients[self.a["id"]].proxy["host"], self.host(1))
        await m.proxy_assign(OWNER, [self.a["id"]], "auto")
        self.broken.add(self.host(1))
        self.assertTrue(await m.proxy_failover(self.a["id"]))
        self.assertNotEqual(m.clients[self.a["id"]].proxy["host"], self.host(1))

    async def test_an_account_without_proxies_is_left_alone(self):
        self.assertFalse(await self.h.manager.proxy_failover(self.b["id"]))
        self.assertEqual(self.made, [])

    async def test_the_watchdog_uses_it_instead_of_giving_up(self):
        m = self.h.manager
        await m.proxy_assign(OWNER, [self.a["id"]], "auto")
        first = m.clients[self.a["id"]]
        first.connected = False  # the connection dropped
        self.broken.add(first.proxy["host"])
        m._offline_notified.add(self.a["id"])
        m.store.accounts.list = _async([])  # nothing new to pick up
        await m.watchdog_tick()
        now = m.clients[self.a["id"]]
        self.assertIsNot(now, first)
        self.assertTrue(now.connected)
        self.assertNotIn(self.a["id"], m._offline_notified)
        self.assertTrue(any("back online (through another proxy)" in text for _chat, text in self.h.bot.sent))  # the admin is told
        self.assertFalse(any("lost its connection" in text for _chat, text in self.h.bot.sent))  # and not alarmed


def _async(value):
    async def inner(*args, **kwargs):
        return value
    return inner


class TestingTests(ProxyCase):
    async def test_each_proxy_is_really_tried_and_reported(self):
        m = self.h.manager
        self.broken |= {self.host(2), self.host(4)}
        results = await m.proxy_test(OWNER)
        self.assertEqual([r["ok"] for r in results], [True, False, True, False])
        self.assertIn("unreachable", results[1]["error"])
        self.assertTrue(m.proxy_cooling(OWNER, self.pool[1]["id"]))
        self.assertFalse(m.proxy_cooling(OWNER, self.pool[0]["id"]))
        self.assertEqual(m.proxy_health(OWNER, self.pool[0]["id"])["ok"], True)

    async def test_testing_needs_an_api_id(self):
        m = self.h.manager
        m.accounts.clear()
        with self.assertRaises(ValueError) as caught:
            await m.proxy_test(OWNER)
        self.assertIn("API id", str(caught.exception))


class UploadTests(ProxyCase):
    async def test_a_txt_of_proxies_is_saved_and_not_read_as_sessions(self):
        update = await self.h.send_txt(OWNER, "socks5://u:p@7.7.7.7:1080\n8.8.8.8:1080\ngarbage line")
        self.assertIn("2 proxy save hui", texts(update))
        self.assertIn("1 line proxy nahi", texts(update))
        self.assertEqual(len(await self.h.manager.proxy_pool(OWNER)), 6)

    async def test_the_same_file_twice_adds_nothing(self):
        await self.h.send_txt(OWNER, "7.7.7.7:1080")
        update = await self.h.send_txt(OWNER, "7.7.7.7:1080")
        self.assertIn("0 proxy save hui (1 pehle se thi", texts(update))


class AIToolTests(ProxyCase):
    async def test_proxies_pasted_in_the_chat_are_saved(self):
        fake = self.gemini(call("proxy_add", text="socks5://bob:hunter2@5.5.5.5:1080\nhttp://6.6.6.6:8080"), say("Save ho gayi."))
        update = await self.h.say(OWNER, "ye proxy lo socks5://bob:hunter2@5.5.5.5:1080 http://6.6.6.6:8080")
        self.assertEqual(len(await self.h.manager.proxy_pool(OWNER)), 6)
        self.assertIn("2 proxy saved", " ".join(r[0] for r in update.effective_message.replies))
        self.assertNotIn("hunter2", json.dumps(fake.requests[1]["contents"][-1]))

    async def test_the_list_never_shows_a_password(self):
        await self.h.manager.proxy_add(OWNER, parse_proxy_text("socks5://bob:hunter2@5.5.5.5:1080")[0])
        fake = self.gemini(call("proxy_list"), say("Ye hain."))
        update = await self.h.say(OWNER, "proxy list dikhao")
        shown = " ".join(r[0] for r in update.effective_message.replies)
        self.assertIn("socks5 5.5.5.5:1080 (login)", shown)
        self.assertNotIn("hunter2", shown + json.dumps(fake.requests))

    async def test_proxy_use_karo_asks_then_puts_the_accounts_on_the_pool(self):
        h = self.h
        fake = self.gemini(call("proxy_use", accounts=["all"]), say("Ijazat chahiye."))
        update = await h.say(OWNER, "sab accounts proxy use karo")
        self.assertIn("proxy pool (4) with automatic retry", update.effective_message.replies[-1][0])
        self.assertIsNone(h.manager.clients[self.a["id"]].__dict__.get("proxy"))  # nothing changed before the tap
        done = await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertIsNotNone(h.manager.clients[self.a["id"]].proxy)
        self.assertIsNotNone(h.manager.clients[self.b["id"]].proxy)
        self.assertIn("Proxy set", texts(done))

    async def test_proxy_test_and_removing_the_dead_ones(self):
        h = self.h
        self.broken |= {self.host(2), self.host(3)}
        self.gemini(call("proxy_test"), say("Do chal rahi hain."), call("proxy_remove", proxies=["dead"]), say("Ijazat chahiye."))
        update = await h.say(OWNER, "proxy test karo")
        self.assertIn("2/4 chal rahe hain", " ".join(r[0] for r in update.effective_message.replies))
        update = await h.say(OWNER, "kharab wali hata do")
        done = await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual([r["host"] for r in await h.manager.proxy_pool(OWNER)], [self.host(1), self.host(4)])
        self.assertIn("2 proxy deleted", texts(done))

    async def test_use_with_an_empty_pool_is_explained(self):
        await self.h.manager.proxy_remove(OWNER, [r["id"] for r in self.pool])
        fake = self.gemini(call("proxy_use", accounts=["all"]), say("Pehle proxy do."))
        await self.h.say(OWNER, "proxy use karo")
        self.assertIn("pool is empty", json.dumps(fake.requests[1]["contents"][-1]))


if __name__ == "__main__":
    unittest.main()
