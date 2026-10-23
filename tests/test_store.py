import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import FakeDB  # noqa: E402

from telegram_manager.store import Store  # noqa: E402

OWNER, ADMIN2 = 100, 200


def run(coro):
    return asyncio.run(coro)


def make(db=None):
    db = db or FakeDB()
    return Store("", None, OWNER, db=db), db


class AccountIsolationTests(unittest.TestCase):
    def test_each_admin_has_own_accounts_and_numbers(self):
        async def go():
            store, _ = make()
            a1 = await store.accounts.create(OWNER, {"display_name": "A", "telegram_user_id": 1})
            a2 = await store.accounts.create(OWNER, {"display_name": "B", "telegram_user_id": 2})
            b1 = await store.accounts.create(ADMIN2, {"display_name": "C", "telegram_user_id": 3})
            return store, a1, a2, b1

        store, a1, a2, b1 = run(go())
        self.assertEqual((a1["number"], a2["number"], b1["number"]), (1, 2, 1))  # numbers restart per admin

        async def check():
            self.assertEqual({r["id"] for r in await store.accounts.list(OWNER)}, {a1["id"], a2["id"]})
            self.assertEqual([r["id"] for r in await store.accounts.list(ADMIN2)], [b1["id"]])
            self.assertIsNone(await store.accounts.get(a1["id"], owner_id=ADMIN2))  # cannot reach it
            self.assertIsNotNone(await store.accounts.get(a1["id"], owner_id=OWNER))
            self.assertIsNone(await store.accounts.find_by_alias(ADMIN2, "2"))  # A's number 2 is not B's
            self.assertEqual((await store.accounts.find_by_alias(ADMIN2, "1"))["id"], b1["id"])
            self.assertEqual((await store.accounts.find_by_alias(OWNER, "@x" if False else "b"))["id"], a2["id"])
            self.assertEqual(len(await store.accounts.list()), 3)

        run(check())

    def test_legacy_accounts_belong_to_the_owner(self):
        db = FakeDB()
        col = db.collection("telegram_accounts")
        col.docs["old1"] = {"display_name": "Old", "created_at": 1}
        col.docs["old2"] = {"display_name": "Old2", "created_at": 2, "number": 5}
        store, _ = make(db)
        rows = run(store.accounts.list(OWNER))
        self.assertEqual({r["id"] for r in rows}, {"old1", "old2"})
        self.assertEqual(col.docs["old1"]["owner_id"], OWNER)  # saved back
        self.assertEqual(sorted(r["number"] for r in rows), [5, 6])
        self.assertEqual(run(store.accounts.list(ADMIN2)), [])

    def test_counter_never_reuses_a_number(self):
        async def go():
            store, _ = make()
            first = await store.accounts.create(OWNER, {"x": 1})
            await store.accounts.delete(first["id"])
            second = await store.accounts.create(OWNER, {"x": 2})
            return first["number"], second["number"]

        self.assertEqual(run(go()), (1, 2))


class SettingsTests(unittest.TestCase):
    def test_settings_are_per_admin(self):
        async def go():
            store, _ = make()
            await store.settings.set_forwarding(OWNER, "forward_user_messages", True)
            return await store.settings.get_forwarding(OWNER), await store.settings.get_forwarding(ADMIN2)

        owner, other = run(go())
        self.assertTrue(owner["forward_user_messages"])
        self.assertFalse(other["forward_user_messages"])

    def test_legacy_global_settings_move_to_the_owner(self):
        db = FakeDB()
        db.collection("telegram_settings").docs["global"] = {
            "forward_bot_messages": True, "next_account_number": 7, "bulk_delay_seconds": 9,
        }
        store, _ = make(db)
        self.assertTrue(run(store.settings.get_forwarding(OWNER))["forward_bot_messages"])
        self.assertEqual(run(store.settings.get_counter(OWNER)), 7)
        self.assertFalse(run(store.settings.get_forwarding(ADMIN2))["forward_bot_messages"])
        self.assertNotIn("bulk_delay_seconds", db.collection("telegram_admin_settings").docs[str(OWNER)])


class AdminJobReplyTests(unittest.TestCase):
    def test_admin_add_remove(self):
        async def go():
            store, _ = make()
            await store.admins.add(ADMIN2, OWNER, "Bob")
            rows = await store.admins.list()
            await store.admins.remove(ADMIN2)
            return rows, await store.admins.list()

        rows, after = run(go())
        self.assertEqual((rows[0]["id"], rows[0]["name"]), (ADMIN2, "Bob"))
        self.assertEqual(after, [])

    def test_jobs_per_admin_and_legacy_migration(self):
        db = FakeDB()
        legacy = {"target": "@a", "text": "t", "delay_seconds": 3, "start_time": "10:00", "times_per_day": 1}
        db.collection("telegram_settings").docs["daily_job"] = dict(legacy)
        store, _ = make(db)

        async def go():
            await store.jobs.save(ADMIN2, {**legacy, "target": "@b"})
            return await store.jobs.list_all()

        rows = {(owner, slot): data for owner, slot, data in run(go())}
        self.assertEqual(rows[(OWNER, 1)]["target"], "@a")
        self.assertEqual(rows[(ADMIN2, 1)]["target"], "@b")
        self.assertNotIn("daily_job", db.collection("telegram_settings").docs)

    def test_reply_targets_are_per_chat(self):
        async def go():
            store, _ = make()
            await store.replies.save(OWNER, 55, "acc1", 9, 3)
            await store.replies.save(ADMIN2, 55, "acc2", 8, 4)  # same message id, other admin's chat
            return await store.replies.get(OWNER, 55), await store.replies.get(ADMIN2, 55), await store.replies.get(300, 55)

        a, b, c = run(go())
        self.assertEqual(a, ("acc1", 9, 3))
        self.assertEqual(b, ("acc2", 8, 4))
        self.assertIsNone(c)

    def test_legacy_reply_key_for_owner(self):
        db = FakeDB()
        db.collection("telegram_reply_map").docs["77"] = {
            "account_id": "a", "peer_id": 1, "original_message_id": 2, "created_at": datetime.now(timezone.utc),
        }
        store, _ = make(db)
        self.assertEqual(run(store.replies.get(OWNER, 77)), ("a", 1, 2))
        self.assertIsNone(run(store.replies.get(ADMIN2, 77)))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PhoneAliasTests(unittest.TestCase):
    def test_find_by_phone_tail(self):
        async def go():
            store, _ = make()
            a = await store.accounts.create(OWNER, {"display_name": "A", "telegram_user_id": 1, "phone": "919876543210"})
            b = await store.accounts.create(OWNER, {"display_name": "B", "telegram_user_id": 2, "phone": "919811113210"})
            c = await store.accounts.create(OWNER, {"display_name": "C", "telegram_user_id": 3, "phone": ""})
            find = lambda alias: store.accounts.find_by_alias(OWNER, alias)  # noqa: E731
            self.assertEqual((await find("+919876543210"))["id"], a["id"])  # full number
            self.assertEqual((await find("6543210"))["id"], a["id"])  # tail
            self.assertEqual((await find("1111 3210"))["id"], b["id"])  # spaces ignored
            self.assertIsNone(await find("3210"))  # ambiguous: both end in 3210
            self.assertIsNone(await find("210"))  # too short to be a phone tail
            self.assertEqual((await find("3"))["id"], c["id"])  # short digits still mean account number
            self.assertIsNone(await store.accounts.find_by_alias(ADMIN2, "6543210"))  # other admin's account

        run(go())

    def test_phone_masked_is_backfilled_and_searchable(self):
        from telegram_manager.parsing import mask_phone

        self.assertEqual(mask_phone("+919876543210"), "+91\u2022\u2022\u2022\u2022\u2022\u20223210")
        self.assertEqual(mask_phone("1234567"), "\u2022\u2022\u2022\u2022\u202267")
        self.assertEqual(mask_phone(""), "")

        async def go():
            db = FakeDB()
            store, _ = make(db)
            a = await store.accounts.create(OWNER, {"display_name": "A", "telegram_user_id": 1, "phone": "919876543210"})
            fresh, _ = make(db)  # new process: old row has no phone_masked yet
            row = await fresh.accounts.get(a["id"])
            self.assertEqual(row["phone_masked"], mask_phone("919876543210"))
            found = await fresh.accounts.find_by_alias(OWNER, row["phone_masked"])
            self.assertEqual(found["id"], a["id"])

        run(go())
