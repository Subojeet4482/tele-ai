import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import buttons, find  # noqa: E402
from tests.harness import BOB, OWNER, Harness, texts  # noqa: E402

from telegram_manager.core.models import LoginAttempt  # noqa: E402
from telegram_manager.errors import AccountAlreadyConnectedError  # noqa: E402
from telethon.errors import PasswordHashInvalidError, PhoneCodeInvalidError, SessionPasswordNeededError  # noqa: E402


class MessageFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.h.record_telethon(self)
        await self.h.access.add(BOB, OWNER, "Bob")
        self.acc = await self.h.add_account(OWNER, "Owner A")
        self.bob = await self.h.add_account(BOB, "Bob A")

        async def dialogs(account_id, limit=40, owner_id=None):
            return [{"peer_id": 1000 + i, "name": f"Chat {i}", "username": ""} for i in range(20)]

        self.h.manager.list_dialogs = dialogs

    async def test_full_flow_with_pagination(self):
        h = self.h
        u = await h.command(OWNER, "/message")
        self.assertIn("Choose the account", texts(u))
        self.assertEqual(len([b for b in buttons(u.last()[1]) if "pick" in b.callback_data]), 1)  # only own account
        u = await h.tap(OWNER, f"wz|message|pick|{self.acc['id']}")
        self.assertIn("Owner A", texts(u))
        picks = [b for b in buttons(u.last()[1]) if "|pick|" in b.callback_data]
        self.assertEqual(len(picks), 8)  # first page
        u = await h.tap(OWNER, "wz|message|page|1")
        self.assertIn("Chat 8", [b.text for b in buttons(u.last()[1])])
        self.assertIn("2/3", [b.text for b in buttons(u.last()[1])])
        u = await h.tap(OWNER, "wz|message|pick|1009")
        self.assertIn("Send the message text", texts(u))
        u = await h.say(OWNER, "hello there")
        self.assertIn("Which inline buttons", texts(u))
        u = await h.tap(OWNER, "wz|message|pick|-")
        self.assertIn("When should it be sent", texts(u))
        u = await h.tap(OWNER, "wz|message|pick|0")
        self.assertIn("Message sent", texts(u))
        self.assertEqual(h.calls, [("send_existing", self.acc["id"], "1009", "hello there", OWNER)])

    async def test_type_username_instead_of_picking(self):
        h = self.h
        await h.command(OWNER, "/message 1")
        await h.say(OWNER, "@mandal4482")
        u = await h.say(OWNER, "hi")
        u = await h.say(OWNER, "-")  # no buttons to click
        u = await h.say(OWNER, "now")  # the "when" step accepts typed text too
        self.assertIn("Message sent", texts(u))
        self.assertEqual(h.calls[0][2], "@mandal4482")

    async def test_direct_command_and_partial_forms(self):
        h = self.h
        u = await h.command(OWNER, "/message 1 @mandal4482 hello world")
        self.assertIn("Message sent", texts(u))
        self.assertEqual(h.calls[0][3], "hello world")
        u = await h.command(OWNER, "/message 1 @mandal4482")
        self.assertIn("Send the message text", texts(u))
        u = await h.command(OWNER, "/message 7 @mandal4482 hi")
        self.assertIn("Account not found", texts(u))
        # Bob's "1" is Bob's own account, never the owner's
        await h.command(BOB, "/message 1 @mandal4482 yo")
        self.assertEqual(h.calls[-1][1], self.bob["id"])

    async def test_back_from_chat_to_account(self):
        h = self.h
        await h.command(OWNER, "/message")
        await h.tap(OWNER, f"wz|message|pick|{self.acc['id']}")
        u = await h.tap(OWNER, "wz|message|back")
        self.assertIn("Choose the account", texts(u))

    async def test_chat_list_error_still_allows_typing(self):
        h = self.h

        async def broken(account_id, limit=40, owner_id=None):
            raise ValueError("This account is not connected")

        h.manager.list_dialogs = broken
        await h.command(OWNER, "/message 1")
        u = await h.tap(OWNER, "wz|message|back") if False else None
        u = await h.say(OWNER, "@mandal4482")
        self.assertIn("Send the message text", texts(u))

    async def test_send_error_is_shown(self):
        h = self.h

        async def failing(account_id, target, text, owner_id=None):
            raise ValueError("That chat is not already open in this account")

        h.manager.send_to_existing_dialog = failing
        u = await h.command(OWNER, "/message 1 @mandal4482 hi")
        self.assertIn("not already open", texts(u))

    async def test_account_screen_button_prefills_account(self):
        u = await self.h.tap(OWNER, f"msg_acc|{self.acc['id']}")
        self.assertIn("Sending from Owner A", texts(u))


class ConnectFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.events = []
        m = self.h.manager

        async def begin(owner_id, api_id, api_hash, phone):
            self.events.append(("begin", owner_id, api_id, api_hash, phone))
            return LoginAttempt(owner_id, api_id, api_hash, phone, object(), "hash")

        async def abort(attempt):
            if attempt:  # the real abort_login(None) does nothing
                self.events.append(("abort",))

        async def code(attempt, value):
            self.events.append(("code", value))
            if self.need_password:
                raise SessionPasswordNeededError(None)
            if self.dup:
                raise AccountAlreadyConnectedError("This account is already connected as X.")
            return {"display_name": "New Person", "username": "np"}

        async def password(attempt, value):
            self.events.append(("password", value))
            if value == "bad":
                raise PasswordHashInvalidError(None)
            return {"display_name": "New Person", "username": "np"}

        m.begin_login, m.abort_login, m.submit_code, m.submit_password = begin, abort, code, password
        self.need_password = False
        self.dup = False

    async def go_to_code(self):
        h = self.h
        await h.tap(OWNER, "connect")
        u = await h.tap(OWNER, "cx|otp")
        self.assertIn("Send your API ID", texts(u))
        u = await h.say(OWNER, "12345")
        self.assertIn("API hash", texts(u))
        u = await h.say(OWNER, "a" * 32)
        self.assertTrue(u.effective_message.deleted)  # the secret message is removed from the chat
        self.assertIn("phone number", texts(u))
        u = await h.say(OWNER, "+91 98765-43210")
        self.assertIn("login code", texts(u))
        return u

    async def test_otp_login(self):
        h = self.h
        await self.go_to_code()
        self.assertEqual(self.events[0], ("begin", OWNER, 12345, "a" * 32, "+919876543210"))
        u = await h.say(OWNER, "1 2 3 4 5")
        self.assertTrue(u.effective_message.deleted)
        self.assertIn("Connected: New Person (@np)", texts(u))
        self.assertIn(("code", "12345"), self.events)
        self.assertIsNone(h.ctx(OWNER).user_data.get("wz"))
        self.assertIsNone(h.ctx(OWNER).user_data.get("login_attempt"))

    async def test_two_step_password(self):
        h = self.h
        self.need_password = True
        await self.go_to_code()
        u = await h.say(OWNER, "12345")
        self.assertIn("two-step verification", texts(u))
        u = await h.say(OWNER, "bad")
        self.assertIn("Wrong password", texts(u))
        u = await h.say(OWNER, "good")
        self.assertIn("Connected", texts(u))

    async def test_back_from_password_restarts_from_phone_and_aborts_login(self):
        h = self.h
        self.need_password = True
        await self.go_to_code()
        await h.say(OWNER, "12345")
        u = await h.tap(OWNER, "wz|cx_otp|back")
        self.assertIn("phone number", texts(u))
        self.assertIn(("abort",), self.events)
        self.assertIsNone(h.ctx(OWNER).user_data.get("login_attempt"))
        self.need_password = False
        await h.say(OWNER, "+919876543210")
        u = await h.say(OWNER, "54321")
        self.assertIn("Connected", texts(u))

    async def test_wrong_code_can_be_retried(self):
        h = self.h
        await self.go_to_code()

        async def bad(attempt, value):
            raise PhoneCodeInvalidError(None)

        real = h.manager.submit_code
        h.manager.submit_code = bad
        u = await h.say(OWNER, "11111")
        self.assertIn("wrong or expired", texts(u))
        h.manager.submit_code = real
        u = await h.say(OWNER, "22222")
        self.assertIn("Connected", texts(u))

    async def test_duplicate_account_ends_flow_cleanly(self):
        h = self.h
        self.dup = True
        await self.go_to_code()
        u = await h.say(OWNER, "12345")
        self.assertIn("already connected", texts(u))
        self.assertIsNone(h.ctx(OWNER).user_data.get("wz"))
        self.assertIn(("abort",), self.events)

    async def test_cancel_closes_pending_login(self):
        h = self.h
        await self.go_to_code()
        await h.tap(OWNER, "wz|cx_otp|cancel")
        self.assertIn(("abort",), self.events)

    async def test_bad_inputs_are_asked_again(self):
        h = self.h
        await h.tap(OWNER, "cx|otp")
        u = await h.say(OWNER, "abc")
        self.assertIn("API ID must be a positive number", texts(u))
        await h.say(OWNER, "12345")
        u = await h.say(OWNER, "short")
        self.assertIn("too short", texts(u))
        await h.say(OWNER, "a" * 32)
        u = await h.say(OWNER, "12345")
        self.assertIn("country code", texts(u))

    async def test_session_string(self):
        h = self.h
        got = {}

        async def connect(owner_id, api_id, api_hash, session):
            got.update(owner=owner_id, api_id=api_id, session=session)
            return {"display_name": "Sess", "username": ""}

        h.manager.connect_with_session = connect
        await h.tap(BOB if False else OWNER, "cx|session")
        await h.say(OWNER, "999")
        await h.say(OWNER, "b" * 32)
        u = await h.say(OWNER, "s" * 60)
        self.assertTrue(u.effective_message.deleted)
        self.assertIn("Connected: Sess", texts(u))
        self.assertEqual(got, {"owner": OWNER, "api_id": 999, "session": "s" * 60})


class ScheduleFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_broadcast_flow(self):
        h = Harness()
        await h.add_account(OWNER, "A")
        await h.add_account(OWNER, "B")
        u = await h.tap(OWNER, "sched|home")
        self.assertIn("No daily broadcast", texts(u))
        self.assertNotIn("Turn off", " ".join(b.text for b in buttons(u.last()[1])))
        u = await h.tap(OWNER, "sched|new")
        self.assertIn("Enter the user name", texts(u))
        await h.say(OWNER, "@mandal4482")
        await h.tap(OWNER, "wz|schedule|pick|yes")
        u = await h.say(OWNER, "Good morning")
        self.assertIn("Which inline buttons", texts(u))
        u = await h.tap(OWNER, "wz|schedule|pick|-")
        self.assertIn("Which accounts should send", texts(u))
        u = await h.say(OWNER, "all")
        self.assertIn("Delay between accounts", texts(u))
        self.assertNotIn("instant", " ".join(b.text for b in buttons(u.last()[1])))
        u = await h.say(OWNER, "1")
        self.assertIn("between 3 and 100", texts(u))
        u = await h.tap(OWNER, "wz|schedule|pick|5")
        self.assertIn("first broadcast", texts(u))
        u = await h.say(OWNER, "10:30am")
        self.assertIn("move later every day", texts(u))
        u = await h.tap(OWNER, "wz|schedule|pick|0")
        self.assertIn("times per day", texts(u))
        u = await h.tap(OWNER, "wz|schedule|pick|2")
        self.assertIn("Review", texts(u))
        self.assertIn("10:30 AM", texts(u))
        u = await h.tap(OWNER, "wz|schedule|pick|yes")
        self.assertIn("scheduled", texts(u))
        job = h.manager.daily_job_for(OWNER)
        self.assertEqual((job.target, job.text, job.delay_seconds, job.times_per_day), ("@mandal4482", "Good morning", 5, 2))
        self.assertIsNone(h.manager.daily_job_for(BOB))  # per admin
        self.assertIn(f"{OWNER}_1", h.db.collection("telegram_daily_jobs").docs)
        u = await h.tap(OWNER, "sched|home")
        self.assertIn("Good morning", texts(u))
        u = await h.tap(OWNER, "sched|off")
        self.assertIn("off", texts(u))
        self.assertIsNone(h.manager.daily_job_for(OWNER))
        self.assertEqual(h.db.collection("telegram_daily_jobs").docs, {})

    async def test_back_in_schedule_goes_step_by_step(self):
        h = Harness()
        await h.add_account(OWNER, "A")
        await h.tap(OWNER, "sched|new")
        await h.say(OWNER, "@mandal4482")
        u = await h.tap(OWNER, "wz|schedule|back")
        self.assertIn("Enter the user name", texts(u))
        u = await h.tap(OWNER, "wz|schedule|back")
        self.assertIn("Daily broadcast", texts(u))  # back on the schedule screen

    async def test_needs_an_account(self):
        u = await Harness().tap(OWNER, "sched|new")
        self.assertIn("no connected accounts", texts(u))


class AdminFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_admin_then_admin_uses_bot(self):
        h = Harness()
        u = await h.tap(OWNER, "adm|home")
        self.assertIn("No other admins", texts(u))
        await h.tap(OWNER, "adm|add")
        u = await h.say(OWNER, str(OWNER))
        self.assertIn("That is you", texts(u))
        u = await h.say(OWNER, "abc")
        self.assertIn("positive number", texts(u))
        u = await h.say(OWNER, str(BOB))
        self.assertIn("Add 200", texts(u))
        u = await h.tap(OWNER, "wz|adm_add|pick|yes")
        self.assertIn("is now an admin", texts(u))
        self.assertTrue(h.access.is_admin(BOB))
        self.assertIn(str(BOB), h.db.collection("telegram_admins").docs)
        self.assertIn((BOB, "👋 You were added as an admin. Send /start to begin."), h.bot.sent)
        u = await h.command(BOB, "/start")
        self.assertIn("admin", texts(u))
        self.assertNotIn("only for its admin", texts(u))
        await h.tap(OWNER, "adm|add")
        u = await h.say(OWNER, str(BOB))
        self.assertIn("already an admin", texts(u))

    async def test_remove_admin_logs_out_their_accounts_only(self):
        h = Harness()
        await h.access.add(BOB, OWNER, "Bob")
        mine = await h.add_account(OWNER, "Mine")
        bobs = await h.add_account(BOB, "Bobs")
        removed = []

        async def remove_account(account_id, owner_id=None):
            removed.append(account_id)
            await h.store.accounts.delete(account_id)
            return True

        h.manager.remove_account = remove_account
        u = await h.tap(OWNER, "adm|home")
        self.assertIn("Name 200" if False else "200", texts(u))
        await h.tap(OWNER, "adm|remove")
        u = await h.tap(OWNER, f"wz|adm_rm|pick|{BOB}")
        self.assertIn("1 connected account", texts(u))
        u = await h.tap(OWNER, "wz|adm_rm|pick|yes")
        self.assertIn("was removed", texts(u))
        self.assertEqual(removed, [bobs["id"]])
        self.assertFalse(h.access.is_admin(BOB))
        self.assertIsNotNone(await h.store.accounts.get(mine["id"]))
        u = await h.command(BOB, "/start")
        self.assertNotIn("Account Manager", texts(u))  # the removed admin now sees the public screen
        self.assertEqual([b.callback_data for b in buttons(u.last()[1])], ["ex|start"])

    async def test_admin_cannot_drive_the_owner_flows(self):
        h = Harness()
        await h.access.add(BOB, OWNER, "Bob")
        await h.tap(OWNER, "adm|add")  # owner starts a flow ...
        h.ctx(BOB).user_data["wz"] = h.ctx(OWNER).user_data["wz"]  # ... and a copy lands in Bob's session
        u = await h.tap(BOB, f"wz|adm_add|pick|{BOB}")
        self.assertIn("Only the owner", u.callback_query.answers[-1][0])
        self.assertEqual(await h.access.ensure_loaded(), None)
        self.assertFalse(any(row["id"] == 555 for row in h.access.admins()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
