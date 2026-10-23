"""Button auto-click ([follow] (join)), /click, Refresh and the multi-account daily broadcast."""
import asyncio
import os
import sys
import unittest
from datetime import time as dtime
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import buttons  # noqa: E402
from tests.harness import OWNER, Harness, texts  # noqa: E402

import telegram_manager.core.autoclick as autoclick  # noqa: E402
from telegram_manager.core.models import DailyJob  # noqa: E402
from telegram_manager.parsing import parse_bulk_args, parse_button_names  # noqa: E402


class ParsingTests(unittest.TestCase):
    def test_buttons_at_the_end_of_a_command(self):
        a = parse_bulk_args("all", "@mandal4482 hello there [follow] (join) 3sec Y")
        self.assertEqual((a.target, a.text, a.buttons, a.delay, a.confirmed), ("@mandal4482", "hello there", ["follow", "join"], 3, True))

    def test_buttons_without_a_message(self):
        a = parse_bulk_args("all", "@mandal4482 [follow] (join)")
        self.assertEqual((a.text, a.buttons), (None, ["follow", "join"]))

    def test_brackets_inside_the_message_stay_in_the_message(self):
        a = parse_bulk_args("all", "@mandal4482 see (this) now [join]")
        self.assertEqual((a.text, a.buttons), ("see (this) now", ["join"]))

    def test_no_buttons_is_unchanged(self):
        a = parse_bulk_args("all", "@mandal4482 hello 3sec Y")
        self.assertEqual((a.text, a.buttons), ("hello", []))

    def test_block_and_click_rules(self):
        with self.assertRaises(ValueError):
            parse_bulk_args("block", "@mandal4482 [join]")
        with self.assertRaises(ValueError):
            parse_bulk_args("click", "@mandal4482 hello [join]")
        self.assertEqual(parse_bulk_args("click", "@mandal4482 [join] y").buttons, ["join"])

    def test_button_names(self):
        self.assertEqual(parse_button_names("[follow] (join)"), ["follow", "join"])
        self.assertEqual(parse_button_names("follow, join"), ["follow", "join"])
        with self.assertRaises(ValueError):
            parse_button_names("  ")
        with self.assertRaises(ValueError):
            parse_button_names("a,b,c,d,e,f")


class FakeButton:
    def __init__(self, text, url=None):
        self.text, self.url = text, url


class FakeMsg:
    def __init__(self, mid, text, rows, error=None):
        self.id, self.message, self.out = mid, text, False
        self.buttons = [[FakeButton(*b) if isinstance(b, tuple) else FakeButton(b) for b in row] for row in rows]
        self.clicked, self._error = [], error

    async def click(self, row, col):
        self.clicked.append((row, col))
        if self._error:
            raise self._error


class FakeTg:
    """The bit of a Telethon client that the clicker uses."""

    def __init__(self, messages):
        self.messages, self.requests = messages, []

    async def get_messages(self, entity, limit=8):
        return sorted(self.messages, key=lambda m: -m.id)[:limit]

    async def __call__(self, request):
        self.requests.append(request)

    async def get_entity(self, name):
        return autoclick.Channel.__new__(autoclick.Channel)


class ClickEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        for name in ("AUTOCLICK_WAIT", "AUTOCLICK_POLL", "AUTOCLICK_PAUSE"):
            patcher = mock.patch.object(autoclick, name, 0.05 if name != "AUTOCLICK_PAUSE" else 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_presses_matching_buttons_in_order_ignoring_case(self):
        msg = FakeMsg(11, "Please follow", [["📢 Follow"], ["✅ Joined"], ["Help"]])
        tg = FakeTg([msg])
        done = await self.h.manager.click_buttons(tg, object(), ["follow", "JOIN"], after_id=10)
        self.assertEqual(msg.clicked, [(0, 0), (1, 0)])
        self.assertEqual(len(done), 2)

    async def test_missing_button_fails_with_its_name(self):
        tg = FakeTg([FakeMsg(11, "hi", [["Help"]])])
        with self.assertRaises(ValueError) as ctx:
            await self.h.manager.click_buttons(tg, object(), ["join"], after_id=10)
        self.assertIn("join", str(ctx.exception))

    async def test_old_messages_are_ignored_after_a_send(self):
        old = FakeMsg(5, "old", [["Join"]])
        tg = FakeTg([old])
        with self.assertRaises(ValueError):
            await self.h.manager.click_buttons(tg, object(), ["join"], after_id=10)
        self.assertEqual(old.clicked, [])
        await self.h.manager.click_buttons(tg, object(), ["join"], after_id=0)  # click-only: the chat as it is
        self.assertEqual(old.clicked, [(0, 0)])

    async def test_a_bot_that_does_not_answer_the_press_still_counts(self):
        error = type("BotResponseTimeoutError", (Exception,), {})()
        msg = FakeMsg(11, "x", [["Join"]], error=error)
        await self.h.manager.click_buttons(FakeTg([msg]), object(), ["join"], after_id=10)
        self.assertEqual(msg.clicked, [(0, 0)])

    async def test_link_button_joins_the_channel(self):
        msg = FakeMsg(11, "x", [[("Follow", "https://t.me/some_channel")]])
        tg = FakeTg([msg])
        done = await self.h.manager.click_buttons(tg, object(), ["follow"], after_id=10)
        self.assertEqual(len(tg.requests), 1)
        self.assertIn("joined", done[0])
        self.assertEqual(msg.clicked, [])  # a link is not "clicked", the account joins instead

    async def test_invite_link_button_joins_with_the_invite_hash(self):
        msg = FakeMsg(11, "x", [[("Join", "https://t.me/+AbCdEfGhIjKlMn")]])
        tg = FakeTg([msg])
        await self.h.manager.click_buttons(tg, object(), ["join"], after_id=10)
        self.assertEqual(len(tg.requests), 1)

    async def test_non_channel_link_is_skipped_not_failed(self):
        msg = FakeMsg(11, "x", [[("Website", "https://example.com/page")]])
        tg = FakeTg([msg])
        done = await self.h.manager.click_buttons(tg, object(), ["website"], after_id=10)
        self.assertEqual(tg.requests, [])
        self.assertIn("skipped", done[0])


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.h.record_telethon(self)
        self.a1 = await self.h.add_account(OWNER, "One")
        self.a2 = await self.h.add_account(OWNER, "Two")

    async def test_all_with_message_and_buttons(self):
        h = self.h
        await h.command(OWNER, "/all @mandal4482 hello [follow] (join) 0sec Y")
        await h.finish_bulk(OWNER)
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.calls[0][3], "hello")
        self.assertEqual(h.calls[0][-1], ["follow", "join"])
        self.assertIn("Buttons: follow → join", h.bot.sent[-1][1])

    async def test_all_with_only_buttons_sends_no_message(self):
        h = self.h
        await h.command(OWNER, "/all @mandal4482 [follow] (join) 0sec Y")
        await h.finish_bulk(OWNER)
        self.assertEqual(len(h.calls), 2)
        self.assertEqual((h.calls[0][3], h.calls[0][-1]), ("", ["follow", "join"]))
        self.assertIn("Buttons clicked", h.bot.sent[-1][1])

    async def test_click_command_one_liner(self):
        h = self.h
        await h.command(OWNER, "/click @mandal4482 [join] 0sec Y")
        await h.finish_bulk(OWNER)
        self.assertEqual([c[0] for c in h.calls], ["send_existing"] * 2)
        self.assertEqual((h.calls[0][3], h.calls[0][-1]), ("", ["join"]))

    async def test_click_step_by_step_and_menu_button(self):
        h = self.h
        u = await h.tap(OWNER, "menu")
        self.assertTrue(any(b.callback_data == "bulk|click" for b in buttons(u.last()[1])))
        u = await h.tap(OWNER, "bulk|click")
        self.assertIn("Enter the user name", texts(u))
        await h.say(OWNER, "@mandal4482")
        await h.tap(OWNER, "wz|bulk_click|pick|yes")
        u = await h.tap(OWNER, "wz|bulk_click|pick|0")
        self.assertIn("Which inline buttons", texts(u))
        self.assertNotIn("Skip", " ".join(b.text for b in buttons(u.last()[1])))  # names are required here
        u = await h.say(OWNER, "[follow] (join)")
        self.assertIn("When should it be sent", texts(u))
        await h.tap(OWNER, "wz|bulk_click|pick|0")
        await h.finish_bulk(OWNER)
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.calls[0][-1], ["follow", "join"])

    async def test_multi_one_liner_carries_buttons(self):
        h = self.h
        await h.command(OWNER, "/multi @mandal4482 hi [join]")
        await h.tap(OWNER, f"wz|multi|tog|{self.a1['id']}")
        u = await h.tap(OWNER, "wz|multi|done")  # one account: the delay question is skipped
        self.assertIn("When should it be sent", texts(u))
        u = await h.tap(OWNER, "wz|multi|pick|0")
        self.assertIn("Click: join", texts(u))
        await h.tap(OWNER, "wz|multi|pick|yes")
        await h.finish_bulk(OWNER)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.calls[0][-1], ["join"])


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.a1 = await self.h.add_account(OWNER, "Old name")
        self.a2 = await self.h.add_account(OWNER, "Second")
        self.h.manager.clients[self.a1["id"]].me = SimpleNamespace(first_name="New", last_name="Name", username="newname", id=1)
        self.h.manager.clients[self.a2["id"]].me = SimpleNamespace(first_name="Second", last_name=None, username=None, id=2)

    async def test_refresh_button_reads_names_again(self):
        u = await self.h.tap(OWNER, "acc_list")
        self.assertIn("Refresh", " ".join(b.text for b in buttons(u.last()[1])))
        u = await self.h.tap(OWNER, "acc_refresh")
        text = texts(u)
        self.assertIn("New Name", text)
        self.assertIn("1 name(s) changed", text)
        row = await self.h.store.accounts.get(self.a1["id"])
        self.assertEqual((row["display_name"], row["username"]), ("New Name", "newname"))
        self.assertEqual(self.h.manager.accounts[self.a1["id"]]["display_name"], "New Name")

    async def test_refresh_command_and_offline_accounts(self):
        self.h.manager.clients[self.a2["id"]].connected = False
        u = await self.h.command(OWNER, "/refresh")
        self.assertIn("1 account(s) offline", texts(u))

    async def test_accounts_are_listed_in_number_order(self):
        await self.h.store.accounts.update(self.a1["id"], {"number": 9})
        u = await self.h.tap(OWNER, "acc_list")
        text = texts(u)
        self.assertLess(text.index("2. Second"), text.index("9. "))
        labels = [b.text for b in buttons(u.last()[1]) if b.callback_data.startswith("acc|")]
        self.assertTrue(labels[0].startswith("📱 2.") and labels[1].startswith("📱 9."))


class MultiBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.h.record_telethon(self)
        self.accs = [await self.h.add_account(OWNER, n) for n in ("A", "B", "C")]

    async def _schedule(self, accounts_answer):
        h = self.h
        await h.tap(OWNER, "sched|new")
        await h.say(OWNER, "@mandal4482")
        await h.tap(OWNER, "wz|schedule|pick|yes")
        await h.say(OWNER, "Good morning")
        await h.say(OWNER, "[join]")
        return await accounts_answer()

    async def test_only_chosen_accounts_broadcast(self):
        h = self.h

        async def pick():
            await h.tap(OWNER, f"wz|schedule|tog|{self.accs[0]['id']}")
            await h.tap(OWNER, f"wz|schedule|tog|{self.accs[2]['id']}")
            await h.tap(OWNER, "wz|schedule|done")
            await h.tap(OWNER, "wz|schedule|pick|5")
            await h.say(OWNER, "10:30am")
            await h.tap(OWNER, "wz|schedule|pick|0")  # same time every day
            u = await h.tap(OWNER, "wz|schedule|pick|2")
            self.assertIn("2 chosen account(s)", texts(u))
            self.assertIn("Click: join", texts(u))
            return await h.tap(OWNER, "wz|schedule|pick|yes")

        u = await self._schedule(pick)
        self.assertIn("scheduled", texts(u))
        job = h.manager.daily_job_for(OWNER)
        self.assertEqual(job.account_ids, [self.accs[0]["id"], self.accs[2]["id"]])
        self.assertEqual(job.buttons, ["join"])
        self.assertIn("2 chosen account(s)", h.manager.daily_job_status(OWNER))
        await h.manager._broadcast(OWNER, job)
        self.assertEqual({c[1] for c in h.calls}, {self.accs[0]["id"], self.accs[2]["id"]})
        self.assertEqual(h.calls[0][-1], ["join"])

    async def test_ticking_everyone_means_all_accounts(self):
        h = self.h

        async def pick():
            await h.tap(OWNER, "wz|schedule|all")
            await h.tap(OWNER, "wz|schedule|done")
            await h.tap(OWNER, "wz|schedule|pick|5")
            await h.say(OWNER, "10:30am")
            await h.tap(OWNER, "wz|schedule|pick|0")  # same time every day
            await h.tap(OWNER, "wz|schedule|pick|1")
            return await h.tap(OWNER, "wz|schedule|pick|yes")

        await self._schedule(pick)
        job = h.manager.daily_job_for(OWNER)
        self.assertIsNone(job.account_ids)
        await h.manager._broadcast(OWNER, job)
        self.assertEqual(len(h.calls), 3)

    async def test_job_round_trip_and_old_jobs_still_load(self):
        job = DailyJob("@x_user1", "hi", 5, dtime(9, 0), 2, account_ids=["a", "b"], buttons=["join"])
        again = DailyJob.from_dict(job.to_dict())
        self.assertEqual((again.account_ids, again.buttons), (["a", "b"], ["join"]))
        old = DailyJob.from_dict({"target": "@x_user1", "text": "hi", "delay_seconds": 5, "start_time": "09:00", "times_per_day": 1})
        self.assertEqual((old.account_ids, old.buttons), (None, []))


if __name__ == "__main__":
    unittest.main()
