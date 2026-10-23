import asyncio
import os
import sys
import unittest
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import BOB, OWNER, Harness, patch_sleep  # noqa: E402

from telegram_manager.core.models import DailyJob  # noqa: E402


class BulkTimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.accounts = [await self.h.add_account(OWNER, f"Acc{i}") for i in range(3)]

    async def test_delay_zero_runs_all_accounts_at_the_same_time(self):
        running, peak = 0, 0

        async def action(account_id):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1

        result = await self.h.manager._for_each_account(OWNER, action, 0, "test")
        self.assertEqual((result.ok, result.total, peak), (3, 3, 3))

    async def test_positive_delay_runs_one_after_another_in_order(self):
        order, sleeps = [], []
        patch_sleep(self, lambda seconds: (sleeps.append(seconds), order.append("wait")))

        async def action(account_id):
            order.append(account_id)

        result = await self.h.manager._for_each_account(OWNER, action, 7, "test")
        self.assertEqual(result.ok, 3)
        ids = [a["id"] for a in self.accounts]
        self.assertEqual(order, [ids[0], "wait", ids[1], "wait", ids[2]])
        self.assertEqual(sleeps, [7, 7])

    async def test_one_failure_does_not_stop_the_others(self):
        async def action(account_id):
            if account_id == self.accounts[1]["id"]:
                raise ValueError("boom")

        result = await self.h.manager._for_each_account(OWNER, action, 0, "test")
        self.assertEqual((result.ok, len(result.errors)), (2, 1))
        self.assertIn("boom", result.errors[0])

    async def test_delay_is_clamped_to_the_limit(self):
        seen = []
        patch_sleep(self, seen.append)

        async def action(account_id):
            return None

        await self.h.manager._for_each_account(OWNER, action, 5000, "test")
        self.assertEqual(seen, [100, 100])

    async def test_other_admins_accounts_are_never_included(self):
        await self.h.add_account(BOB, "Bob")
        hit = []

        async def action(account_id):
            hit.append(account_id)

        result = await self.h.manager._for_each_account(OWNER, action, 0, "test")
        self.assertEqual((result.total, len(hit)), (3, 3))


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_run_slots(self):
        h = Harness()
        job = DailyJob("@x_user1", "hi", 5, dtime(10, 30), 2)  # 10:30 and 22:30
        tz = ZoneInfo("UTC")
        nxt = h.manager._next_run(job, datetime(2026, 9, 30, 9, 0, tzinfo=tz))
        self.assertEqual((nxt.hour, nxt.minute, nxt.day), (10, 30, 30))
        nxt = h.manager._next_run(job, datetime(2026, 9, 30, 11, 0, tzinfo=tz))
        self.assertEqual((nxt.hour, nxt.minute, nxt.day), (22, 30, 30))
        nxt = h.manager._next_run(job, datetime(2026, 9, 30, 23, 0, tzinfo=tz))
        self.assertEqual((nxt.hour, nxt.minute, nxt.day), (10, 30, 1))

    async def test_jobs_are_restored_per_admin_and_delay_is_forced_to_three(self):
        h = Harness()
        await h.store.jobs.save(OWNER, {"target": "@a_user1", "text": "t", "delay_seconds": 3, "start_time": "10:00", "times_per_day": 1})
        await h.store.jobs.save(BOB, {"target": "@b_user1", "text": "u", "delay_seconds": 9, "start_time": "11:00", "times_per_day": 2})
        await h.manager.restore_daily_jobs()
        self.assertEqual(h.manager.daily_job_for(OWNER).target, "@a_user1")
        self.assertEqual(h.manager.daily_job_for(BOB).times_per_day, 2)
        job = DailyJob("@c_user1", "x", 0, dtime(8, 0), 1)
        await h.manager.start_daily_job(OWNER, job)
        self.assertEqual(h.manager.daily_job_for(OWNER).delay_seconds, 3)  # minimum for opening chats
        h.manager._cancel_all_daily()
        self.assertEqual(h.manager.status_summary()["daily_jobs_scheduled"], 0)

    async def test_stopping_one_admins_job_leaves_the_other(self):
        h = Harness()
        job = DailyJob("@c_user1", "x", 5, dtime(8, 0), 1)
        await h.manager.start_daily_job(OWNER, job)
        await h.manager.start_daily_job(BOB, job)
        self.assertTrue(await h.manager.stop_daily_job(BOB))
        self.assertIsNotNone(h.manager.daily_job_for(OWNER))
        self.assertFalse(await h.manager.stop_daily_job(BOB))
        h.manager._cancel_all_daily()


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_tells_the_accounts_owner_not_everyone(self):
        h = Harness()
        acc = await h.add_account(BOB, "Bob A", online=False)

        async def fail(client, timeout=45):
            raise ConnectionError("network down")

        h.manager._connect_client = fail
        await h.manager.watchdog_tick()
        self.assertEqual([chat for chat, _ in h.bot.sent], [BOB])
        self.assertIn("Bob A", h.bot.sent[0][1])
        self.assertIn(acc["id"], h.manager._offline_notified)

    async def test_status_summary_counts(self):
        h = Harness()
        await h.add_account(OWNER, "A")
        await h.add_account(OWNER, "B", online=False)
        status = h.manager.status_summary()
        self.assertEqual((status["accounts_online"], len(status["accounts_offline"])), (1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
