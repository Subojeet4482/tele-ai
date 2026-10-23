"""Multi daily broadcasts, giving accounts away, account control, session export, start screen."""
import asyncio
import os
import sys
import unittest
from datetime import time as dtime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import buttons  # noqa: E402
from tests.harness import BOB, OWNER, STRANGER, Harness, texts  # noqa: E402

from telegram_manager.bot import keyboards  # noqa: E402
from telegram_manager.bot.flows import export as export_flow  # noqa: E402
from telegram_manager.constants import MAX_DAILY_JOBS  # noqa: E402
from telegram_manager.core.accountctl import parse_username  # noqa: E402
from telegram_manager.core.models import DailyJob  # noqa: E402
from telegram_manager.parsing import split_trailing_buttons  # noqa: E402
from telegram_manager.store import Store  # noqa: E402
from tests.fakes import FakeDB  # noqa: E402


def job(target="@x_user1", text="hi", count=1):
    return DailyJob(target, text, 5, dtime(9, 0), count)


class MultiBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_several_broadcasts_get_their_own_slots(self):
        h = Harness()
        a = await h.manager.start_daily_job(OWNER, job("@a_user1"))
        b = await h.manager.start_daily_job(OWNER, job("@b_user1"))
        self.assertEqual((a, b), (1, 2))
        self.assertEqual([slot for slot, _ in h.manager.daily_jobs_for(OWNER)], [1, 2])
        docs = h.db.collection("telegram_daily_jobs").docs
        self.assertEqual(set(docs), {f"{OWNER}_1", f"{OWNER}_2"})
        status = h.manager.daily_job_status(OWNER)
        self.assertIn("Broadcast #1", status)
        self.assertIn("@b_user1", status)
        h.manager._cancel_all_daily()

    async def test_limit_replace_and_delete_one(self):
        h = Harness()
        for n in range(MAX_DAILY_JOBS):
            await h.manager.start_daily_job(OWNER, job(f"@t{n}_user1"))
        with self.assertRaises(ValueError):
            await h.manager.start_daily_job(OWNER, job("@over_user1"))
        await h.manager.start_daily_job(OWNER, job("@new_user1"), slot=3)  # replaces #3
        self.assertEqual(h.manager.daily_job_for(OWNER, 3).target, "@new_user1")
        self.assertEqual(len(h.manager.daily_jobs_for(OWNER)), MAX_DAILY_JOBS)
        self.assertTrue(await h.manager.stop_daily_job(OWNER, 3))
        self.assertEqual(h.manager.free_daily_slot(OWNER), 3)
        self.assertEqual(len(h.manager.daily_jobs_for(OWNER)), MAX_DAILY_JOBS - 1)
        self.assertTrue(await h.manager.stop_daily_job(OWNER))  # all
        self.assertEqual(h.manager.daily_jobs_for(OWNER), [])
        self.assertEqual(h.db.collection("telegram_daily_jobs").docs, {})

    async def test_restore_keeps_slots_and_old_bare_ids_become_slot_one(self):
        h = Harness()
        data = job("@old_user1").to_dict()
        h.db.collection("telegram_daily_jobs").docs[str(OWNER)] = dict(data)  # single-job era
        await h.store.jobs.save(OWNER, {**data, "target": "@two_user1"}, 2)
        await h.manager.restore_daily_jobs()
        self.assertEqual(h.manager.daily_job_for(OWNER, 1).target, "@old_user1")
        self.assertEqual(h.manager.daily_job_for(OWNER, 2).target, "@two_user1")
        self.assertNotIn(str(OWNER), h.db.collection("telegram_daily_jobs").docs)
        h.manager._cancel_all_daily()

    async def _make(self, h, text):
        await h.tap(OWNER, "sched|new")
        await h.say(OWNER, "@mandal4482")
        await h.tap(OWNER, "wz|schedule|pick|yes")
        await h.say(OWNER, text)
        await h.tap(OWNER, "wz|schedule|pick|-")
        await h.say(OWNER, "all")
        await h.tap(OWNER, "wz|schedule|pick|5")
        await h.say(OWNER, "10:30am")
        await h.tap(OWNER, "wz|schedule|pick|0")  # same time every day
        await h.tap(OWNER, "wz|schedule|pick|1")
        return await h.tap(OWNER, "wz|schedule|pick|yes")

    async def test_buttons_flow_view_edit_delete(self):
        h = Harness()
        await h.add_account(OWNER, "A")
        u = await self._make(h, "first")
        self.assertIn("#1 scheduled", texts(u))
        u = await self._make(h, "second")
        self.assertIn("#2 scheduled", texts(u))
        home = await h.tap(OWNER, "sched|home")
        labels = [b.callback_data for b in buttons(home.last()[1])]
        self.assertIn("sched|view|1", labels)
        self.assertIn("sched|view|2", labels)
        u = await h.tap(OWNER, "sched|view|2")
        self.assertTrue(texts(u).startswith("⏰ Broadcast #2"))
        self.assertNotIn("first", texts(u))
        # replace #2
        await h.tap(OWNER, "sched|edit|2")
        await h.say(OWNER, "@other_user1")
        await h.tap(OWNER, "wz|schedule|pick|yes")
        await h.say(OWNER, "changed")
        await h.tap(OWNER, "wz|schedule|pick|-")
        await h.say(OWNER, "all")
        await h.tap(OWNER, "wz|schedule|pick|5")
        await h.say(OWNER, "10:30am")
        await h.tap(OWNER, "wz|schedule|pick|0")  # same time every day
        await h.tap(OWNER, "wz|schedule|pick|1")
        u = await h.tap(OWNER, "wz|schedule|pick|yes")
        self.assertIn("#2 scheduled", texts(u))
        self.assertEqual(h.manager.daily_job_for(OWNER, 2).text, "changed")
        self.assertEqual(h.manager.daily_job_for(OWNER, 1).text, "first")
        u = await h.tap(OWNER, "sched|del|1")
        self.assertIsNone(h.manager.daily_job_for(OWNER, 1))
        self.assertIsNotNone(h.manager.daily_job_for(OWNER, 2))
        h.manager._cancel_all_daily()


class GiveAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        await self.h.access.add(BOB, OWNER, "Bob")
        self.o1 = await self.h.add_account(OWNER, "Own 1")
        self.o2 = await self.h.add_account(OWNER, "Own 2")
        self.b1 = await self.h.add_account(BOB, "Bob 1")

    async def test_owner_gives_chosen_accounts_to_an_admin(self):
        h = self.h
        u = await h.tap(OWNER, "give|start")
        self.assertIn("Who should get", texts(u))
        self.assertIn(f"wz|give|pick|{BOB}", [b.callback_data for b in buttons(u.last()[1])])
        await h.tap(OWNER, f"wz|give|pick|{BOB}")
        await h.tap(OWNER, f"wz|give|tog|{self.o2['id']}")
        u = await h.tap(OWNER, "wz|give|done")
        self.assertIn("Give 1 account(s)", texts(u))
        u = await h.tap(OWNER, "wz|give|pick|yes")
        self.assertIn("1 account(s) given", texts(u))
        mine = [r["display_name"] for r in await h.store.accounts.list(OWNER)]
        bobs = await h.store.accounts.list(BOB)
        self.assertEqual(mine, ["Own 1"])
        self.assertEqual([(r["display_name"], r["number"]) for r in bobs], [("Bob 1", 1), ("Own 2", 2)])
        self.assertEqual(h.manager.accounts[self.o2["id"]]["owner_id"], BOB)
        self.assertTrue(any(chat == BOB and "gave you" in text for chat, text in h.bot.sent))

    async def test_owner_can_give_everything(self):
        h = self.h
        await h.tap(OWNER, "give|start")
        await h.tap(OWNER, f"wz|give|pick|{BOB}")
        await h.tap(OWNER, "wz|give|all")
        await h.tap(OWNER, "wz|give|done")
        await h.tap(OWNER, "wz|give|pick|yes")
        self.assertEqual(await h.store.accounts.list(OWNER), [])
        self.assertEqual(len(await h.store.accounts.list(BOB)), 3)

    async def test_admin_gives_an_account_to_the_owner_from_its_screen(self):
        h = self.h
        u = await h.tap(BOB, f"give|acc|{self.b1['id']}")  # no "who" question: always the owner
        self.assertIn("Give 1 account(s) to 👑 the owner", texts(u))
        u = await h.tap(BOB, "wz|give|pick|yes")
        self.assertIn("given", texts(u))
        self.assertEqual(await h.store.accounts.list(BOB), [])
        self.assertEqual([r["number"] for r in await h.store.accounts.list(OWNER)], [1, 2, 3])

    async def test_cannot_give_someone_elses_account(self):
        u = await self.h.tap(BOB, f"give|acc|{self.o1['id']}")
        self.assertIn("Account not found", texts(u))

    async def test_no_admins_yet_for_the_owner(self):
        h = Harness()
        await h.add_account(OWNER, "A")
        u = await h.tap(OWNER, "give|start")
        self.assertIn("no admin to give accounts to", texts(u))

    async def test_a_given_account_leaves_the_old_owners_broadcasts(self):
        h = self.h
        await h.manager.start_daily_job(OWNER, DailyJob("@x_user1", "t", 5, dtime(9, 0), 1, account_ids=[self.o1["id"], self.o2["id"]]))
        await h.manager.transfer_account(self.o2["id"], OWNER, BOB)
        self.assertEqual(h.manager.daily_job_for(OWNER).account_ids, [self.o1["id"]])
        await h.manager.transfer_account(self.o1["id"], OWNER, BOB)
        self.assertIsNone(h.manager.daily_job_for(OWNER))  # nothing left to send from
        self.assertEqual(h.db.collection("telegram_daily_jobs").docs, {})

    async def test_new_holder_controls_it_old_holder_does_not(self):
        h = self.h
        await h.manager.transfer_account(self.o2["id"], OWNER, BOB)
        self.assertIsNotNone(await h.store.accounts.get(self.o2["id"], owner_id=BOB))
        self.assertIsNone(await h.store.accounts.get(self.o2["id"], owner_id=OWNER))
        with self.assertRaises(ValueError):
            await h.manager.transfer_account(self.o2["id"], OWNER, BOB)  # no longer his


class AccountControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.acc = await self.h.add_account(OWNER, "Own 1")
        await self.h.store.accounts.update(self.acc["id"], {"phone": "+919876543210", "username": "oldname"})
        self.h.manager.accounts[self.acc["id"]].update({"phone": "+919876543210", "username": "oldname"})
        self.set_calls = []

        async def set_username(account_id, owner_id, username):
            self.set_calls.append((account_id, owner_id, username))
            await self.h.store.accounts.update(account_id, {"username": username})
            return username

        self.h.manager.set_username = set_username

    async def test_account_screen_shows_number_username_and_all_controls(self):
        u = await self.h.tap(OWNER, f"acc|{self.acc['id']}")
        text = texts(u)
        self.assertIn("+919876543210", text)
        self.assertIn("@oldname", text)
        data = {b.callback_data for b in buttons(u.last()[1])}
        for expected in (f"un|set|{self.acc['id']}", f"un|rm|{self.acc['id']}", f"exp|ask|{self.acc['id']}", f"give|acc|{self.acc['id']}"):
            self.assertIn(expected, data)

    async def test_account_without_username_has_no_remove_button(self):
        await self.h.store.accounts.update(self.acc["id"], {"username": ""})
        u = await self.h.tap(OWNER, f"acc|{self.acc['id']}")
        self.assertIn("No username", texts(u))
        data = {b.callback_data for b in buttons(u.last()[1])}
        self.assertNotIn(f"un|rm|{self.acc['id']}", data)

    async def test_set_username_flow(self):
        h = self.h
        u = await h.tap(OWNER, f"un|set|{self.acc['id']}")
        self.assertIn("@oldname", texts(u))
        u = await h.say(OWNER, "ab")
        self.assertIn("5–32", texts(u))
        u = await h.say(OWNER, "@new_name1")
        self.assertIn("@new_name1", texts(u))
        self.assertEqual(self.set_calls, [(self.acc["id"], OWNER, "new_name1")])

    async def test_remove_username(self):
        u = await self.h.tap(OWNER, f"un|rm|{self.acc['id']}")
        self.assertIn("Username removed", texts(u))
        self.assertEqual(self.set_calls, [(self.acc["id"], OWNER, "")])

    async def test_cannot_touch_another_admins_account(self):
        await self.h.access.add(BOB, OWNER, "Bob")
        u = await self.h.tap(BOB, f"un|rm|{self.acc['id']}")
        self.assertIn("Account not found", texts(u))
        self.assertEqual(self.set_calls, [])

    def test_parse_username(self):
        self.assertEqual(parse_username("@Hello_World"), "Hello_World")
        self.assertEqual(parse_username("-"), "")
        for bad in ("abcd", "1abcde", "ab cde", "abc__de", "abcde_", "x" * 33):
            with self.assertRaises(ValueError):
                parse_username(bad)

    async def test_export_session_of_own_account(self):
        h = self.h

        async def export_session(account_id, owner_id):
            return "SESSION-STRING-123"

        h.manager.export_session = export_session
        u = await h.tap(OWNER, f"exp|ask|{self.acc['id']}")
        self.assertIn("full login", texts(u))
        u = await h.tap(OWNER, f"exp|go|{self.acc['id']}")
        self.assertIn("in the message below", texts(u))
        sent = [text for chat, text in h.bot.sent if chat == OWNER and "SESSION-STRING-123" in text]
        self.assertEqual(len(sent), 1)
        self.assertIn("<code>SESSION-STRING-123</code>", sent[0])

    async def test_export_session_refuses_other_accounts(self):
        await self.h.access.add(BOB, OWNER, "Bob")
        u = await self.h.tap(BOB, f"exp|go|{self.acc['id']}")
        self.assertIn("Account not found", texts(u))

    async def test_export_picker_lists_only_own_accounts(self):
        await self.h.access.add(BOB, OWNER, "Bob")
        await self.h.add_account(BOB, "Bob 1")
        u = await self.h.tap(BOB, "exp|pick")
        picks = [b.callback_data for b in buttons(u.last()[1]) if b.callback_data.startswith("exp|ask|")]
        self.assertEqual(len(picks), 1)
        self.assertNotIn(self.acc["id"], picks[0])


class MultiSessionExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.acc1 = await self.h.add_account(OWNER, "One")
        self.acc2 = await self.h.add_account(OWNER, "Two")

        async def export_session(account_id, owner_id):
            return f"SESSION-{account_id}"

        self.h.manager.export_session = export_session

    async def test_exports_several_accounts_into_one_txt_file(self):
        h = self.h
        u = await h.tap(OWNER, "exp|multi")
        self.assertIn("Which accounts", texts(u))
        await h.tap(OWNER, f"wz|exp_multi|tog|{self.acc1['id']}")
        u = await h.tap(OWNER, f"wz|exp_multi|tog|{self.acc2['id']}")
        done_btn = next(b for b in buttons(u.last()[1]) if b.callback_data == "wz|exp_multi|done")
        self.assertIn("2 picked", done_btn.text)
        await h.tap(OWNER, "wz|exp_multi|done")
        self.assertEqual(len(h.bot.documents), 1)
        chat_id, filename, content = h.bot.documents[0]
        self.assertEqual(chat_id, OWNER)
        self.assertTrue(filename.endswith(".txt"))
        self.assertIn(f"SESSION-{self.acc1['id']}", content)
        self.assertIn(f"SESSION-{self.acc2['id']}", content)
        sent_captions = [text for chat, text in h.bot.sent if chat == OWNER and "2 session string(s)" in text]
        self.assertEqual(len(sent_captions), 1)

    async def test_failed_account_is_reported_but_does_not_block_the_rest(self):
        h = self.h

        async def export_session(account_id, owner_id):
            if account_id == self.acc1["id"]:
                raise RuntimeError("boom")
            return f"SESSION-{account_id}"

        h.manager.export_session = export_session
        await h.tap(OWNER, "exp|multi")
        await h.tap(OWNER, f"wz|exp_multi|tog|{self.acc1['id']}")
        await h.tap(OWNER, f"wz|exp_multi|tog|{self.acc2['id']}")
        await h.tap(OWNER, "wz|exp_multi|done")
        self.assertEqual(len(h.bot.documents), 1)
        _, _, content = h.bot.documents[0]
        self.assertIn("FAILED", content)
        self.assertIn(f"SESSION-{self.acc2['id']}", content)

    async def test_bob_only_sees_his_own_accounts_to_export(self):
        h = self.h
        await h.access.add(BOB, OWNER, "Bob")
        bob_acc = await h.add_account(BOB, "Bob 1")
        u = await h.tap(BOB, "exp|multi")
        picks = [b.callback_data for b in buttons(u.last()[1]) if b.callback_data.startswith("wz|exp_multi|tog|")]
        self.assertEqual(picks, [f"wz|exp_multi|tog|{bob_acc['id']}"])


class ExportNotifyAndRevokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        export_flow._last_request.clear()
        export_flow._revoke_pending.clear()
        self.h = Harness()
        h = self.h

        async def begin(owner_id, api_id, api_hash, phone):
            return SimpleNamespace(client="c")

        async def after_code(attempt, code):
            return "EXPORTED-" + code, {"api_id": 12345, "api_hash": "a" * 32, "label": "Jane"}

        async def abort(attempt):
            pass

        h.manager.begin_login, h.manager.export_after_code = begin, after_code
        h.manager.abort_login = abort

    async def _export_as_stranger(self):
        h = self.h
        await h.tap(STRANGER, "ex|start")
        await h.say(STRANGER, "12345")
        await h.say(STRANGER, "a" * 32)
        await h.say(STRANGER, "+919876543210")
        await h.say(STRANGER, "1 2 3 4 5")

    async def test_owner_gets_notified_with_a_revoke_button(self):
        h = self.h
        await self._export_as_stranger()
        owner_msgs = [(c, t) for c, t in h.bot.sent if c == OWNER and "session was exported" in t]
        self.assertEqual(len(owner_msgs), 1)
        self.assertIn("Jane", owner_msgs[0][1])

    async def test_revoke_button_ends_the_session(self):
        h = self.h
        await self._export_as_stranger()
        self.assertEqual(len(export_flow._revoke_pending), 1)
        token = next(iter(export_flow._revoke_pending))

        revoked = {}

        async def revoke_exported_session(session, api_id, api_hash):
            revoked["args"] = (session, api_id, api_hash)
            return True

        h.manager.revoke_exported_session = revoke_exported_session
        u = await h.tap(OWNER, f"expr|{token}")
        self.assertIn("revoked", texts(u))
        self.assertEqual(revoked["args"], ("EXPORTED-12345", 12345, "a" * 32))
        self.assertEqual(export_flow._revoke_pending, {})  # one-time use

    async def test_revoke_button_twice_says_it_expired(self):
        h = self.h
        await self._export_as_stranger()
        token = next(iter(export_flow._revoke_pending))
        h.manager.revoke_exported_session = lambda *a: asyncio.sleep(0, result=True)
        await h.tap(OWNER, f"expr|{token}")
        u = await h.tap(OWNER, f"expr|{token}")
        self.assertIn("expired", texts(u))

    async def test_stranger_cannot_tap_revoke(self):
        h = self.h
        await self._export_as_stranger()
        token = next(iter(export_flow._revoke_pending))
        u = await h.tap(STRANGER, f"expr|{token}")
        self.assertEqual(u.callback_query.answers[-1], ("🚫 This bot is only for its admin.", True))
        self.assertEqual(len(export_flow._revoke_pending), 1)  # untouched


class ConnectAutoDetectTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_obviously_wrong_text_as_a_session(self):
        h = Harness()
        await h.tap(OWNER, "connect")
        u = await h.tap(OWNER, "cx|session")
        self.assertIn("auto-detect", texts(u))
        await h.say(OWNER, "12345")
        await h.say(OWNER, "a" * 32)
        u = await h.say(OWNER, "not a real session")
        self.assertIn("doesn't look like a Telethon session", texts(u))

    async def test_accepts_a_plausible_looking_session_string(self):
        h = Harness()

        async def connect_with_session(owner_id, api_id, api_hash, session):
            return {"id": "acc1", "display_name": "Auto", "username": ""}

        h.manager.connect_with_session = connect_with_session
        await h.tap(OWNER, "connect")
        await h.tap(OWNER, "cx|session")
        await h.say(OWNER, "12345")
        await h.say(OWNER, "a" * 32)
        plausible = "QQ" + "A" * 300  # long enough, base64-safe
        u = await h.say(OWNER, plausible)
        self.assertIn("Connected", texts(u))


class MessageButtonsTests(unittest.IsolatedAsyncioTestCase):
    def test_split_trailing_buttons(self):
        self.assertEqual(split_trailing_buttons("hello there [follow] (join)"), ("hello there", ["follow", "join"]))
        self.assertEqual(split_trailing_buttons("plain"), ("plain", []))
        self.assertEqual(split_trailing_buttons("[go]"), ("", ["go"]))

    async def test_message_command_presses_buttons(self):
        h = Harness()
        h.record_telethon(self)
        acc = await h.add_account(OWNER, "A")
        u = await h.command(OWNER, "/message 1 @mandal4482 hello [follow] (join)")
        self.assertIn("Message sent", texts(u))
        self.assertEqual(h.calls, [("send_existing", acc["id"], "@mandal4482", "hello", OWNER, ["follow", "join"])])

    async def test_message_wizard_asks_for_buttons(self):
        h = Harness()
        h.record_telethon(self)
        acc = await h.add_account(OWNER, "A")
        await h.command(OWNER, "/message 1 @mandal4482")
        await h.say(OWNER, "hi")
        u = await h.say(OWNER, "[follow]")
        self.assertIn("When should it be sent", texts(u))
        await h.tap(OWNER, "wz|message|pick|0")
        self.assertEqual(h.calls[-1][-1], ["follow"])


class PublicExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        export_flow._last_request.clear()
        self.h = Harness()
        self.log = []
        h = self.h

        async def begin(owner_id, api_id, api_hash, phone):
            self.log.append(("begin", owner_id, api_id, phone))
            return SimpleNamespace(client="c")

        async def after_code(attempt, code):
            if code == "999":
                from telethon.errors import SessionPasswordNeededError
                raise SessionPasswordNeededError()
            return "EXPORTED-" + code, {"api_id": 12345, "api_hash": "a" * 32, "label": "Stranger's account"}

        async def after_password(attempt, password):
            if password != "pw":
                from telethon.errors import PasswordHashInvalidError
                raise PasswordHashInvalidError()
            return "EXPORTED-PW", {"api_id": 12345, "api_hash": "a" * 32, "label": "Stranger's account"}

        async def abort(attempt):
            self.log.append(("abort",))

        h.manager.begin_login, h.manager.export_after_code = begin, after_code
        h.manager.export_after_password, h.manager.abort_login = after_password, abort

    async def _to_code(self):
        h = self.h
        u = await h.tap(STRANGER, "ex|start")
        self.assertIn("API ID", texts(u))
        await h.say(STRANGER, "12345")
        u = await h.say(STRANGER, "a" * 32)
        self.assertTrue(u.effective_message.deleted)  # the API hash is not left in the chat
        u = await h.say(STRANGER, "+91 98765 43210")
        self.assertIn("login code", texts(u))

    async def test_stranger_exports_only_their_own_session(self):
        h = self.h
        await self._to_code()
        u = await h.say(STRANGER, "1 2 3 4 5")
        self.assertTrue(u.effective_message.deleted)
        sent = [text for chat, text in h.bot.sent if chat == STRANGER and "EXPORTED-12345" in text]
        self.assertEqual(len(sent), 1)
        self.assertIn("did not save", texts(u))
        self.assertEqual(h.db.collection("telegram_accounts").docs, {})  # nothing stored
        self.assertEqual(await h.store.accounts.list(), [])
        # the owner is told a session was exported, with a Revoke button
        notice = [text for chat, text in h.bot.sent if chat == OWNER and "session was exported" in text]
        self.assertEqual(len(notice), 1)

    async def test_two_step_password_path(self):
        h = self.h
        await self._to_code()
        u = await h.say(STRANGER, "9 9 9")
        self.assertIn("two-step", texts(u))
        u = await h.say(STRANGER, "wrong")
        self.assertIn("Wrong password", texts(u))
        await h.say(STRANGER, "pw")
        self.assertTrue(any("EXPORTED-PW" in text for _, text in h.bot.sent))

    async def test_stranger_still_cannot_use_anything_else(self):
        h = self.h
        await h.tap(STRANGER, "ex|start")
        u = await h.command(STRANGER, "/list")
        self.assertIn("only for its admin", texts(u))
        u = await h.tap(STRANGER, "sched|home")
        self.assertEqual(u.callback_query.answers[-1], ("🚫 This bot is only for its admin.", True))
        u = await h.say(STRANGER, "hello")  # no flow running any more (the command closed it)
        self.assertEqual(u.effective_message.replies, [])

    async def test_wizard_buttons_work_for_strangers_but_not_admin_wizards(self):
        h = self.h
        await h.tap(STRANGER, "ex|start")
        u = await h.tap(STRANGER, "wz|ex_otp|cancel")
        self.assertIn("export the session string", texts(u))  # back on the public start screen
        # an admin-only wizard name is refused for a stranger
        u = await h.tap(STRANGER, "wz|message|pick|x")
        self.assertEqual(u.callback_query.answers[-1], ("🚫 This bot is only for its admin.", True))

    async def test_cooldown_between_code_requests(self):
        h = self.h
        await self._to_code()
        await h.tap(STRANGER, "wz|ex_otp|back")
        u = await h.say(STRANGER, "+919876543210")
        self.assertIn("Please wait", texts(u))
        self.assertEqual([entry for entry in self.log if entry[0] == "begin"].__len__(), 1)

    async def test_owner_can_switch_it_off(self):
        h = self.h
        u = await h.tap(OWNER, "sc|pub")
        self.assertIn("Public session export: OFF", texts(u))
        u = await h.command(STRANGER, "/start")
        self.assertIn("only for its admin", texts(u))
        self.assertEqual(u.last()[1], None)
        u = await h.tap(STRANGER, "ex|start")
        self.assertIn("switched off", texts(u))
        await h.tap(OWNER, "sc|pub")
        u = await h.tap(STRANGER, "ex|start")
        self.assertIn("API ID", texts(u))

    async def test_export_command_for_admin_and_stranger(self):
        h = self.h
        await h.add_account(OWNER, "A")
        u = await h.command(OWNER, "/export")
        self.assertIn("Which account's session", texts(u))
        u = await h.command(STRANGER, "/export")
        self.assertIn("API ID", texts(u))


class StartScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_text_and_image_are_shown_to_everybody(self):
        h = Harness()
        await h.tap(OWNER, "sc|home")
        u = await h.tap(OWNER, "sc|text")
        self.assertIn("{name}", texts(u))
        u = await h.say(OWNER, "x" * 900)
        self.assertIn("At most 800", texts(u))
        u = await h.say(OWNER, "Hey {name}, welcome to my bot!")
        self.assertIn("Welcome text: custom", texts(u))
        await h.tap(OWNER, "sc|photo")
        u = await h.say(OWNER, "not a photo")
        self.assertIn("Send a photo", texts(u))
        u = await h.send_photo(OWNER, b"\xff\xd8jpegbytes")
        self.assertIn("Media: photo", texts(u))
        cfg = await h.store.settings.get_start_screen()
        self.assertTrue(cfg["photo"].startswith("photo"))
        # the owner sees the custom text + role + the usual buttons; a stranger the text + export button
        await h.command(OWNER, "/start")  # goes out as a photo message with the text as caption
        captions = [text for chat, text in h.bot.sent if chat == OWNER and "welcome to my bot" in text]
        self.assertEqual(len(captions), 1)
        self.assertIn("Hey there", captions[0])  # {name} -> first name ("there" when Telegram gave none)
        self.assertIn("You are the 👑 owner", captions[0])
        stranger = await h.command(STRANGER, "/start")
        self.assertTrue(any("welcome to my bot" in text for chat, text in h.bot.sent if chat == STRANGER))
        self.assertEqual(stranger.effective_message.replies, [])  # went out as a photo message instead

    async def test_text_only_screen_and_reset(self):
        h = Harness()
        await h.store.settings.set_start_screen(text="Hi {name}!")
        u = await h.command(STRANGER, "/start")
        self.assertTrue(texts(u).startswith("Hi User 999"[:3]))
        self.assertIn("Hi", texts(u))
        u = await h.command(OWNER, "/start")
        self.assertIn("You are the 👑 owner", texts(u))
        u = await h.tap(OWNER, "sc|reset")
        self.assertIn("Welcome text: default", texts(u))
        u = await h.command(OWNER, "/start")
        self.assertIn("Telegram Account Manager", texts(u))

    async def test_remove_image_and_preview(self):
        h = Harness()
        await h.store.settings.set_start_screen(photo="photoX", text="Hello")
        u = await h.tap(OWNER, "sc|home")
        self.assertIn("🗑 Remove media", [b.text for b in buttons(u.last()[1])])
        await h.tap(OWNER, "sc|preview")
        self.assertIn((OWNER, "Hello"), h.bot.sent)  # preview = the public view as a new message
        u = await h.tap(OWNER, "sc|rmphoto")
        self.assertIn("Media: none", texts(u))

    async def test_any_admin_may_edit_but_strangers_may_not(self):
        h = Harness()
        await h.access.add(BOB, OWNER, "Bob")
        u = await h.tap(BOB, "sc|home") if False else await h.command(BOB, "/startscreen")
        self.assertIn("Start screen", texts(u))
        u = await h.command(STRANGER, "/startscreen")
        self.assertIn("only for its admin", texts(u))
        u = await h.tap(STRANGER, "sc|reset")
        self.assertEqual(u.callback_query.answers[-1][1], True)

    async def test_help_screen_has_a_button_for_every_command(self):
        h = Harness()
        u = await h.command(OWNER, "/help")
        data = {b.callback_data for b in buttons(u.last()[1])}
        for expected in ("bulk|all", "bulk|alll", "bulk|multi", "bulk|click", "bulk|block", "msg|start", "acc_list",
                         "acc_refresh", "fw|home", "sched|home", "give|start", "exp|pick", "sc|home", "connect", "adm|home", "logs|link"):
            self.assertIn(expected, data)


class ButtonStyleTests(unittest.TestCase):
    def test_style_follows_the_leading_emoji(self):
        self.assertEqual(keyboards._auto_style("🗑 Delete"), "danger")
        self.assertEqual(keyboards._auto_style("✅ Confirm"), "success")
        self.assertEqual(keyboards._auto_style("📋 My accounts"), "primary")
        self.assertIsNone(keyboards._auto_style("⬅️ Back"))
        self.assertIsNone(keyboards._auto_style("💬 Message"))


class JobStoreMigrationTests(unittest.TestCase):
    def test_delete_all_of_one_owner_only(self):
        async def go():
            db = FakeDB()
            store = Store("", None, OWNER, db=db)
            await store.jobs.save(OWNER, {"x": 1}, 1)
            await store.jobs.save(OWNER, {"x": 2}, 2)
            await store.jobs.save(BOB, {"x": 3}, 1)
            await store.jobs.delete(OWNER)
            return [(o, s) for o, s, _ in await store.jobs.list_all()]

        self.assertEqual(asyncio.run(go()), [(BOB, 1)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
