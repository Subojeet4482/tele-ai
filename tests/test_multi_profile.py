"""/multi, live progress, timed messages and profile changes."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.fakes import buttons
from tests.harness import BOB, OWNER, Harness, texts

from telegram_manager.bot import bulk_runner
from telegram_manager.core.models import BulkResult
from telegram_manager.parsing import parse_minutes


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_progress_text_shows_done_failed_and_left(self):
        result = BulkResult(total=4, ok=2, errors=["B: boom"])
        text = bulk_runner.progress_text("all", "@x", result, "Account 4")
        self.assertIn("3/4", text)
        self.assertIn("✅ Done: 2", text)
        self.assertIn("❌ Failed: 1", text)
        self.assertIn("⏳ Left: 1", text)
        self.assertIn("Now: Account 4", text)

    async def test_progress_message_is_edited_as_accounts_finish(self):
        h = Harness()
        h.record_telethon(self)
        for name in ("A", "B", "C"):
            await h.add_account(OWNER, name)
        with mock.patch.object(bulk_runner, "PROGRESS_INTERVAL", 0):
            await h.command(OWNER, "/all @mandal4482 hi 3sec Y")
            await h.finish_bulk(OWNER)
        edits = [text for _, _, text in h.bot.edits]
        self.assertTrue(any("✅ Done: 1" in text for text in edits))  # mid-run update
        self.assertTrue(any("✅ Done: 2" in text for text in edits))
        self.assertIn("3/3 accounts", edits[-1])  # the last edit is the result, in the same message
        self.assertEqual(len({message_id for _, message_id, _ in h.bot.edits}), 1)

    async def test_failures_are_counted_in_the_result(self):
        h = Harness()
        h.record_telethon(self)
        a = await h.add_account(OWNER, "A")
        await h.add_account(OWNER, "B", online=False)
        await h.command(OWNER, "/all @mandal4482 hi 0sec Y")
        await h.finish_bulk(OWNER)
        self.assertIn("1/2 accounts", h.bot.sent[-1][1])
        self.assertIn("not connected", h.bot.sent[-1][1])
        self.assertEqual([c[1] for c in h.calls if c[0] == "send_existing"], [a["id"]])


class MultiFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.h.record_telethon(self)
        await self.h.access.add(BOB, OWNER, "Bob")
        self.a1 = await self.h.add_account(OWNER, "Owner A")
        self.a2 = await self.h.add_account(OWNER, "Owner B")
        self.a3 = await self.h.add_account(OWNER, "Owner C")
        self.b1 = await self.h.add_account(BOB, "Bob A")

    async def test_command_asks_which_accounts_with_ticks(self):
        h = self.h
        u = await h.command(OWNER, "/multi @mandal4482 hi")
        self.assertIn("Choose the accounts", texts(u))
        labels = [b.text for b in buttons(u.last()[1])]
        self.assertEqual(sum(1 for t in labels if "⬜" in t and "Owner" in t), 3)  # only the owner's three accounts, none ticked
        self.assertNotIn("Bob A", " ".join(labels))
        u = await h.tap(OWNER, f"wz|multi|tog|{self.a2['id']}")
        labels = [b.text for b in buttons(u.last()[1])]
        self.assertTrue(any("☑️" in t and "Owner B" in t for t in labels))
        self.assertTrue(any("Done (1 picked)" in t for t in labels))
        u = await h.tap(OWNER, f"wz|multi|tog|{self.a2['id']}")  # tap again: unticked
        self.assertTrue(any("Done (0 picked)" in b.text for b in buttons(u.last()[1])))

    async def test_done_without_a_pick_asks_again(self):
        h = self.h
        await h.command(OWNER, "/multi @mandal4482 hi")
        u = await h.tap(OWNER, "wz|multi|done")
        self.assertIn("Pick at least one account", texts(u))

    async def test_one_account_skips_the_delay_and_runs_after_confirm(self):
        h = self.h
        await h.command(OWNER, "/multi @mandal4482 hi")
        await h.tap(OWNER, f"wz|multi|tog|{self.a1['id']}")
        u = await h.tap(OWNER, "wz|multi|done")
        self.assertIn("When should it be sent", texts(u))  # no delay question for one account
        u = await h.tap(OWNER, "wz|multi|pick|0")
        self.assertIn("Check and confirm", texts(u))
        self.assertIn("1 account(s): 1. Owner A", texts(u))
        u = await h.tap(OWNER, "wz|multi|pick|yes")
        self.assertIn("Working on 1 account(s)", texts(u))
        await h.finish_bulk(OWNER)
        self.assertEqual(h.calls, [("send_any", self.a1["id"], "@mandal4482", "hi", OWNER)])
        self.assertEqual(h.sleeps, [])
        self.assertIn("1/1 accounts", h.bot.sent[-1][1])

    async def test_several_accounts_ask_the_delay_and_keep_the_chosen_ones_only(self):
        h = self.h
        await h.command(OWNER, "/multi @mandal4482 hello")
        await h.tap(OWNER, f"wz|multi|tog|{self.a1['id']}")
        await h.tap(OWNER, f"wz|multi|tog|{self.a3['id']}")
        u = await h.tap(OWNER, "wz|multi|done")
        self.assertIn("Delay between accounts", texts(u))
        self.assertIn("3–100", texts(u))  # opens chats: at least 3 seconds apart
        await h.tap(OWNER, "wz|multi|pick|5")
        await h.tap(OWNER, "wz|multi|pick|0")
        await h.tap(OWNER, "wz|multi|pick|yes")
        await h.finish_bulk(OWNER)
        self.assertEqual({c[1] for c in h.calls}, {self.a1["id"], self.a3["id"]})  # a2 was not ticked
        self.assertEqual(h.sleeps, [5])

    async def test_typed_numbers_and_all(self):
        h = self.h
        await h.command(OWNER, "/multi @mandal4482 hi")
        u = await h.say(OWNER, "1 9")
        self.assertIn("There is no account '9'", texts(u))
        u = await h.say(OWNER, "1 3")
        self.assertIn("Delay between accounts", texts(u))
        await h.command(OWNER, "/multi @mandal4482 hi")
        u = await h.say(OWNER, "all")
        self.assertIn("Delay between accounts", texts(u))

    async def test_select_all_button_and_one_line_confirmed_skips_the_timer(self):
        h = self.h
        await h.command(OWNER, "/multi @mandal4482 hi 3sec Y")
        u = await h.tap(OWNER, "wz|multi|all")
        self.assertTrue(any("Done (3 picked)" in b.text for b in buttons(u.last()[1])))
        u = await h.tap(OWNER, "wz|multi|done")
        self.assertIn("Working on 3 account(s)", texts(u))  # delay, timer and confirmation were already given
        await h.finish_bulk(OWNER)
        self.assertEqual(len(h.calls), 3)
        self.assertEqual(h.sleeps, [3, 3])

    async def test_delay_below_three_seconds_is_refused_for_multi(self):
        u = await self.h.command(OWNER, "/multi @mandal4482 hi 1sec")
        self.assertIn("between 3 and 100", texts(u))

    async def test_menu_button_starts_the_flow(self):
        u = await self.h.tap(OWNER, "bulk|multi")
        self.assertIn("Enter the user name", texts(u))


class TimedMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_minutes(self):
        self.assertEqual(parse_minutes("now"), 0)
        self.assertEqual(parse_minutes("0"), 0)
        self.assertEqual(parse_minutes("15"), 15)
        self.assertEqual(parse_minutes("30m"), 30)
        self.assertEqual(parse_minutes("2h"), 120)
        self.assertEqual(parse_minutes("24h"), 1440)
        for bad in ("soon", "-5", "25h", "1.5"):
            with self.assertRaises(ValueError):
                parse_minutes(bad)

    async def test_single_account_message_waits_then_sends(self):
        h = Harness()
        h.record_telethon(self)
        acc = await h.add_account(OWNER, "Owner A")
        waits: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds, *args):
            waits.append(seconds)
            await real_sleep(0)

        patcher = mock.patch.object(
            bulk_runner, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": fake_sleep})
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        await h.tap(OWNER, "msg|start")
        await h.tap(OWNER, f"wz|message|pick|{acc['id']}")
        await h.say(OWNER, "@mandal4482")
        await h.say(OWNER, "later hello")
        await h.tap(OWNER, "wz|message|pick|-")
        u = await h.tap(OWNER, "wz|message|pick|5")
        self.assertIn("Starts in 5m", texts(u))
        self.assertEqual(h.calls, [])  # nothing sent yet
        await h.finish_bulk(OWNER)
        self.assertEqual(waits, [300])
        self.assertEqual(h.calls, [("send_existing", acc["id"], "@mandal4482", "later hello", OWNER)])
        self.assertIn("1/1 accounts", h.bot.sent[-1][1])

    async def test_all_accounts_flow_asks_when_and_waits(self):
        h = Harness()
        h.record_telethon(self)
        await h.add_account(OWNER, "A")
        await h.add_account(OWNER, "B")
        u = await h.command(OWNER, "/all")
        await h.say(OWNER, "@mandal4482")
        await h.tap(OWNER, "wz|bulk_all|pick|yes")
        await h.tap(OWNER, "wz|bulk_all|pick|0")
        await h.say(OWNER, "hello")
        await h.tap(OWNER, "wz|bulk_all|pick|-")  # no buttons
        u = await h.say(OWNER, "10m")  # typed wait
        self.assertIn("Starts in 10m", texts(u))
        task = h.deps.bulk._tasks[OWNER]
        await asyncio.sleep(0)  # let it start waiting
        self.assertTrue(h.deps.bulk.cancel(OWNER))  # Stop cancels a message that is still waiting
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(h.calls, [])
        self.assertIn("Stopped", h.bot.sent[-1][1])


class ProfileFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        await self.h.access.add(BOB, OWNER, "Bob")
        self.acc = await self.h.add_account(OWNER, "Owner A")
        self.bob = await self.h.add_account(BOB, "Bob A")
        self.changes: list[tuple] = []

        async def update_profile(account_id, owner_id=None, first_name=None, last_name=None, about=None):
            self.changes.append(("profile", account_id, owner_id, first_name, last_name, about))
            return f"{first_name or ''} {last_name or ''}".strip()

        async def set_photo(account_id, owner_id, data):
            self.changes.append(("photo", account_id, owner_id, data))

        self.h.manager.update_profile = update_profile
        self.h.manager.set_profile_photo = set_photo

    async def test_account_screen_has_name_photo_bio_buttons(self):
        u = await self.h.tap(OWNER, f"acc|{self.acc['id']}")
        data = [b.callback_data for b in buttons(u.last()[1])]
        for what in ("name", "photo", "bio"):
            self.assertIn(f"pf|{what}|{self.acc['id']}", data)

    async def test_change_name(self):
        h = self.h
        u = await h.tap(OWNER, f"pf|name|{self.acc['id']}")
        self.assertIn("new first name", texts(u))
        self.assertIn("Owner A", texts(u))
        u = await h.say(OWNER, "Rahul")
        self.assertIn("last name", texts(u))
        u = await h.say(OWNER, "-")  # - = no last name
        self.assertIn("Name changed to Rahul", texts(u))
        self.assertEqual(self.changes, [("profile", self.acc["id"], OWNER, "Rahul", "", None)])

    async def test_change_bio_and_limits(self):
        h = self.h
        await h.tap(OWNER, f"pf|bio|{self.acc['id']}")
        u = await h.say(OWNER, "x" * 71)
        self.assertIn("at most 70", texts(u))
        u = await h.say(OWNER, "Hello world")
        self.assertIn("Bio changed", texts(u))
        self.assertEqual(self.changes, [("profile", self.acc["id"], OWNER, None, None, "Hello world")])
        await h.tap(OWNER, f"pf|bio|{self.acc['id']}")
        u = await h.say(OWNER, "-")
        self.assertIn("Bio cleared", texts(u))
        self.assertEqual(self.changes[-1][-1], "")

    async def test_change_photo_needs_a_photo(self):
        h = self.h
        u = await h.tap(OWNER, f"pf|photo|{self.acc['id']}")
        self.assertIn("Send the new profile photo", texts(u))
        u = await h.say(OWNER, "not a photo")
        self.assertIn("Send a photo", texts(u))
        u = await h.send_photo(OWNER, b"\xff\xd8jpegbytes")
        self.assertIn("Profile photo changed", u.effective_message.replies[-1][0])
        self.assertEqual(self.changes, [("photo", self.acc["id"], OWNER, b"\xff\xd8jpegbytes")])

    async def test_a_photo_outside_a_flow_is_kept_for_the_ai_and_changes_nothing(self):
        u = await self.h.send_photo(OWNER, b"abc")
        self.assertIn("Photo mil gayi", texts(u))
        self.assertEqual(self.changes, [])  # nothing happens to any account until the admin says what to do

    async def test_a_photo_inside_a_text_step_is_ignored(self):
        await self.h.tap(OWNER, f"pf|name|{self.acc['id']}")  # a flow that waits for TEXT
        u = await self.h.send_photo(OWNER, b"abc")
        self.assertEqual(u.effective_message.replies, [])
        self.assertEqual(self.changes, [])

    async def test_other_admins_account_and_offline_account_are_refused(self):
        h = self.h
        u = await h.tap(BOB, f"pf|name|{self.acc['id']}")
        self.assertIn("Account not found", texts(u))
        offline = await h.add_account(OWNER, "Off", online=False)
        u = await h.tap(OWNER, f"pf|name|{offline['id']}")
        self.assertIn("offline", texts(u))
        self.assertEqual(self.changes, [])

    async def test_telegram_error_is_shown(self):
        h = self.h

        async def boom(*args, **kwargs):
            raise RuntimeError("FIRSTNAME_INVALID")

        h.manager.update_profile = boom
        await h.tap(OWNER, f"pf|name|{self.acc['id']}")
        await h.say(OWNER, "Rahul")
        u = await h.say(OWNER, "-")
        self.assertIn("Could not change it", texts(u))


class ProfileCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_profile_refreshes_the_stored_name(self):
        h = Harness()
        acc = await h.add_account(OWNER, "Old Name")
        calls = []

        class Client:
            def is_connected(self):
                return True

            async def __call__(self, request):
                calls.append(request)

            async def get_me(self):
                return SimpleNamespace(first_name="Rahul", last_name=None, username="rahul", id=5)

        h.manager.clients[acc["id"]] = Client()
        name = await h.manager.update_profile(acc["id"], OWNER, first_name="Rahul", last_name="")
        self.assertEqual(name, "Rahul")
        self.assertEqual((await h.store.accounts.get(acc["id"]))["display_name"], "Rahul")
        self.assertEqual(h.manager.accounts[acc["id"]]["display_name"], "Rahul")
        with self.assertRaises(ValueError):  # another admin cannot change it
            await h.manager.update_profile(acc["id"], BOB, first_name="X")
        self.assertEqual(len(calls), 1)

    async def test_set_profile_photo_uploads_then_sets_it(self):
        h = Harness()
        acc = await h.add_account(OWNER, "A")
        uploaded = []

        class Client:
            def is_connected(self):
                return True

            async def upload_file(self, buffer):
                uploaded.append(buffer.read())
                return "FILE"

            async def __call__(self, request):
                uploaded.append(request)

        h.manager.clients[acc["id"]] = Client()
        await h.manager.set_profile_photo(acc["id"], OWNER, b"imgbytes")
        self.assertEqual(uploaded[0], b"imgbytes")
        self.assertEqual(len(uploaded), 2)


if __name__ == "__main__":
    unittest.main()
