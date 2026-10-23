"""/setapi, /send, /unsend, /delchat, /whois, .txt import and the random daily shift."""
from __future__ import annotations

import time
import unittest
from datetime import date, datetime, time as dtime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from tests.harness import BOB, OWNER, STRANGER, Harness, texts

from telegram_manager.core.models import DailyJob
from telegram_manager.crypto import SecretBox
from telegram_manager.parsing import (
    looks_like_selector,
    parse_selector,
    parse_session_file,
    parse_shift_range,
)

SESSION_A = "1" + "A" * 123  # real sessions are base64: a multiple of 4 characters
SESSION_B = "1" + "B" * 123
API_HASH = "0123456789abcdef0123456789abcdef"


class SelectorTests(unittest.TestCase):
    def test_all_numbers_and_ranges(self):
        have = [1, 2, 3, 4, 5, 6, 7]
        self.assertEqual(parse_selector("all", have), have)
        self.assertEqual(parse_selector("1,2,5", have), [1, 2, 5])
        self.assertEqual(parse_selector("1-3,6", have), [1, 2, 3, 6])
        self.assertEqual(parse_selector("3,3,1", have), [3, 1])

    def test_unknown_or_bad_selectors_are_refused(self):
        with self.assertRaisesRegex(ValueError, "No account with number: 9"):
            parse_selector("1,9", [1, 2])
        for bad in ("1-", "5-1", "a,b", ""):
            with self.assertRaises(ValueError):
                parse_selector(bad, [1, 2, 3, 4, 5])

    def test_what_looks_like_a_selector(self):
        self.assertTrue(all(looks_like_selector(x) for x in ("all", "1", "1,2", "1-5")))
        self.assertFalse(any(looks_like_selector(x) for x in ("@user", "hello", "", "-")))


class SessionFileTests(unittest.TestCase):
    def test_export_file_format_is_read(self):
        text = f"# 1. Alice\n{SESSION_A}\n\n# 2. Bob (@bob)\n{SESSION_B}\n"
        sessions, ignored = parse_session_file(text)
        self.assertEqual([s.session for s in sessions], [SESSION_A, SESSION_B])
        self.assertEqual(ignored, 0)

    def test_api_per_line_duplicates_and_junk(self):
        text = f"12345:{API_HASH}:{SESSION_A}\n{SESSION_A}\nnot a session\n"
        sessions, ignored = parse_session_file(text)
        self.assertEqual(len(sessions), 1)  # the duplicate is dropped
        self.assertEqual((sessions[0].api_id, sessions[0].api_hash), (12345, API_HASH))
        self.assertEqual(ignored, 1)

    def test_too_many_sessions(self):
        text = "\n".join("1" + chr(65 + i) * 59 + "AAAA" for i in range(5))
        with self.assertRaisesRegex(ValueError, "at most 3"):
            parse_session_file(text, limit=3)


class RandomShiftTests(unittest.TestCase):
    def test_range_parsing(self):
        self.assertEqual(parse_shift_range("30"), (30, 30))
        self.assertEqual(parse_shift_range("10-60"), (10, 60))
        self.assertEqual(parse_shift_range("60..10"), (10, 60))
        with self.assertRaises(ValueError):
            parse_shift_range("abc")

    def _job(self, low, high):
        return DailyJob("@x", "hi", 3, dtime(9, 0), 1, drift_seconds=low, drift_max_seconds=high, anchor_date="2026-10-20")

    def test_fixed_shift_is_multiplication(self):
        self.assertEqual(self._job(30, 0).shift_total(4), 120)

    def test_random_shift_is_stable_and_in_range(self):
        job = self._job(10, 60)
        first = [job.shift_total(d) for d in range(0, 8)]
        again = [self._job(10, 60).shift_total(d) for d in range(0, 8)]
        self.assertEqual(first, again)  # a restart gives the same times
        steps = [b - a for a, b in zip(first, first[1:])]
        self.assertTrue(all(10 <= step <= 60 for step in steps))
        self.assertGreater(len(set(steps)), 1)  # not the same value every day

    def test_job_roundtrip_keeps_the_range(self):
        job = self._job(10, 60)
        back = DailyJob.from_dict(job.to_dict())
        self.assertEqual((back.drift_seconds, back.drift_max_seconds), (10, 60))

    def test_next_run_moves_by_the_random_amount(self):
        h = Harness()
        h.manager.tz = ZoneInfo("Asia/Kolkata")
        job = self._job(10, 60)
        day2 = h.manager._next_run(job, datetime(2026, 10, 21, 0, 0, tzinfo=h.manager.tz))
        self.assertEqual(day2.hour * 3600 + day2.minute * 60 + day2.second, 9 * 3600 + job.shift_total(1))


class ExtraCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.h.manager.secret_box = SecretBox(Fernet.generate_key().decode())
        self.h.record_telethon(self)

    async def test_setapi_saves_encrypted_and_deletes_the_message(self):
        h = self.h
        update = await h.command(OWNER, f"/setapi 1234567 {API_HASH}")
        self.assertTrue(update.effective_message.deleted)
        self.assertIn("saved", texts(update))
        self.assertEqual(await h.manager.saved_api(OWNER), (1234567, API_HASH))
        stored = h.db.collection("telegram_admin_settings").docs[str(OWNER)]
        self.assertNotIn(API_HASH, str(stored))  # only the encrypted form is stored
        self.assertIsNone(await h.manager.saved_api(BOB))  # per admin
        await h.command(OWNER, "/setapi clear")
        self.assertIsNone(await h.manager.saved_api(OWNER))

    async def test_setapi_rejects_a_short_hash(self):
        update = await self.h.command(OWNER, "/setapi 1234567 abc")
        self.assertIn("too short", texts(update))
        self.assertIsNone(await self.h.manager.saved_api(OWNER))

    async def test_connect_skips_the_api_questions_when_saved(self):
        h = self.h
        await h.manager.save_api(OWNER, 1234567, API_HASH)
        update = await h.tap(OWNER, "cx|otp")
        self.assertIn("phone number", texts(update).lower())

    async def test_send_uses_only_the_named_accounts(self):
        h = self.h
        rows = [await h.add_account(OWNER, n) for n in ("A", "B", "C", "D")]
        await h.command(OWNER, "/send 1,3 @mandal4482 hello 3sec Y")
        await h.finish_bulk(OWNER)
        sent = sorted(c[1] for c in h.calls if c[0] == "send_any")
        self.assertEqual(sent, sorted([rows[0]["id"], rows[2]["id"]]))
        self.assertTrue(all(c[3] == "hello" for c in h.calls if c[0] == "send_any"))

    async def test_send_all_and_ranges(self):
        h = self.h
        for n in ("A", "B", "C"):
            await h.add_account(OWNER, n)
        await h.command(OWNER, "/send all @mandal4482 hi 3sec Y")
        await h.finish_bulk(OWNER)
        self.assertEqual(len([c for c in h.calls if c[0] == "send_any"]), 3)

    async def test_send_asks_to_confirm_without_y(self):
        h = self.h
        await h.add_account(OWNER, "A")
        update = await h.command(OWNER, "/send 1 @mandal4482 hello")
        self.assertEqual([c for c in h.calls if c[0] == "send_any"], [])  # nothing is sent before the confirm step
        self.assertIn("when should it be sent", texts(update).lower())

    async def test_send_refuses_unknown_account_numbers(self):
        h = self.h
        await h.add_account(OWNER, "A")
        update = await h.command(OWNER, "/send 1,7 @mandal4482 hi Y")
        self.assertIn("No account with number: 7", texts(update))
        self.assertEqual(h.calls, [])

    async def test_send_never_touches_another_admins_accounts(self):
        h = self.h
        await h.access.add(BOB, OWNER, "Bob")
        mine = await h.add_account(OWNER, "Mine")
        await h.add_account(BOB, "Bobs")
        await h.command(OWNER, "/send all @mandal4482 hi 3sec Y")
        await h.finish_bulk(OWNER)
        self.assertEqual([c[1] for c in h.calls if c[0] == "send_any"], [mine["id"]])

    async def test_strangers_are_refused(self):
        update = await self.h.command(STRANGER, "/send all @mandal4482 hi Y")
        self.assertIn("only for its admin", texts(update))

    async def test_whois(self):
        h = self.h
        row = await h.add_account(OWNER, "A")

        async def get_entity(target):
            return SimpleNamespace(id=777, username="mandal4482", first_name="Mandal", last_name=None, bot=False)

        h.manager.clients[row["id"]].get_entity = get_entity
        update = await h.command(OWNER, "/whois @mandal4482")
        self.assertIn("777", texts(update))
        self.assertIn("@mandal4482", texts(update))


class UnsendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.deleted: list[tuple] = []

    async def _account(self, name):
        h = self.h
        row = await h.add_account(OWNER, name)
        client = h.manager.clients[row["id"]]

        async def get_entity(target):
            return target

        async def delete_messages(entity, ids, revoke=True):
            self.deleted.append((row["id"], entity, list(ids), revoke))

        async def delete_dialog(entity, revoke=False):
            self.deleted.append((row["id"], entity, "dialog", revoke))

        client.get_entity, client.delete_messages, client.delete_dialog = get_entity, delete_messages, delete_dialog
        return row

    async def test_unsend_deletes_only_recent_messages_after_confirming(self):
        h = self.h
        a, b = await self._account("A"), await self._account("B")
        h.manager._remember_sent(a["id"], "@userx", 11, OWNER)
        h.manager._remember_sent(b["id"], "@userx", 22, OWNER)
        h.manager._sent_log[OWNER][0]["at"] = time.time() - 3 * 3600  # an old one
        update = await h.command(OWNER, "/unsend 30")
        self.assertIn("Delete 1 message", texts(update))
        self.assertEqual(self.deleted, [])  # nothing yet: it asks first
        done = await h.tap(OWNER, "unsend|go")
        self.assertEqual(self.deleted, [(b["id"], "@userx", [22], True)])
        self.assertIn("Deleted 1 message", texts(done))
        again = await h.command(OWNER, "/unsend 30")
        self.assertIn("Nothing to delete", texts(again))

    async def test_unsend_can_be_limited_to_accounts_and_cancelled(self):
        h = self.h
        a, b = await self._account("A"), await self._account("B")
        h.manager._remember_sent(a["id"], "@userx", 1, OWNER)
        h.manager._remember_sent(b["id"], "@userx", 2, OWNER)
        await h.command(OWNER, "/unsend 30 1")
        await h.tap(OWNER, "unsend|no")
        self.assertEqual(self.deleted, [])
        await h.command(OWNER, "/unsend 30 1")
        await h.tap(OWNER, "unsend|go")
        self.assertEqual([d[0] for d in self.deleted], [a["id"]])

    async def test_sent_messages_are_recorded_by_the_real_send_path(self):
        h = self.h
        row = await self._account("A")
        sent = []

        async def send_message(entity, text, parse_mode=None):
            sent.append(text)
            return SimpleNamespace(id=555)

        h.manager.clients[row["id"]].send_message = send_message
        await h.manager.send_to_any(row["id"], "@userx", "hi", OWNER)
        self.assertEqual(h.manager.sent_count(OWNER, 5), 1)
        self.assertEqual(h.manager._sent_log[OWNER][0]["msg_id"], 555)

    async def test_delchat_asks_then_deletes_only_my_side(self):
        h = self.h
        a, _ = await self._account("A"), await self._account("B")
        await h.command(OWNER, "/delchat 1 @userx")
        self.assertEqual(self.deleted, [])
        done = await h.tap(OWNER, "delchat|go")
        self.assertEqual(self.deleted, [(a["id"], "@userx", "dialog", False)])
        self.assertIn("deleted on 1 of 1", texts(done))


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.h.manager.secret_box = SecretBox(Fernet.generate_key().decode())
        self.connected: list[tuple] = []

        async def connect_with_session(owner_id, api_id, api_hash, session):
            self.connected.append((owner_id, api_id, api_hash, session))
            if session == SESSION_B:
                raise ValueError("This session string is not authorized.")
            return {"display_name": f"Acc{len(self.connected)}", "username": ""}

        self.h.manager.connect_with_session = connect_with_session
        import telegram_manager.core.extras as extras

        extras.IMPORT_GAP_SECONDS = 0

    async def test_txt_without_api_asks_for_setapi(self):
        update = await self.h.send_txt(OWNER, SESSION_A)
        self.assertIn("/setapi", update.effective_message.replies[-1][0])
        self.assertEqual(self.connected, [])

    async def test_txt_adds_every_session_with_the_saved_api(self):
        h = self.h
        await h.manager.save_api(OWNER, 1234567, API_HASH)
        update = await h.send_txt(OWNER, f"# 1. x\n{SESSION_A}\n{SESSION_B}\n")
        self.assertTrue(update.effective_message.deleted)  # the secrets file is removed from the chat
        self.assertEqual([c[:3] for c in self.connected], [(OWNER, 1234567, API_HASH)] * 2)
        report = update.effective_message.replies[-1][0]
        self.assertIn("Added 1 of 2", report)
        self.assertIn("not authorized", report)

    async def test_per_line_api_needs_no_setapi(self):
        await self.h.send_txt(OWNER, f"777:{API_HASH}:{SESSION_A}")
        self.assertEqual(self.connected[0][1:3], (777, API_HASH))

    async def test_other_file_types_are_ignored(self):
        from tests.fakes import FakeUpdate

        update = FakeUpdate(OWNER, text=None)
        update.effective_message.document = SimpleNamespace(file_size=10, file_name="photo.pdf", mime_type="application/pdf")
        await self.h._run(OWNER, update, self.h.parts.extras.txt_input)
        self.assertEqual((update.effective_message.replies, self.connected), ([], []))

    async def test_a_stranger_cannot_import(self):
        update = await self.h.send_txt(STRANGER, SESSION_A)
        self.assertEqual(self.connected, [])
        self.assertIn("only for its admin", update.effective_message.replies[-1][0])


if __name__ == "__main__":
    unittest.main()
