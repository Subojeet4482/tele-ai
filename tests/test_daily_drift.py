import os
import sys
import unittest
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import buttons  # noqa: E402
from tests.harness import OWNER, Harness, texts  # noqa: E402

from telegram_manager.core.models import DailyJob  # noqa: E402
from telegram_manager.parsing import format_clock, parse_drift, parse_time  # noqa: E402

UTC = ZoneInfo("UTC")


def at(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


class ParsingTests(unittest.TestCase):
    def test_time_with_seconds(self):
        self.assertEqual(parse_time("6:42:30pm"), dtime(18, 42, 30))
        self.assertEqual(parse_time("18:42:30"), dtime(18, 42, 30))
        self.assertEqual(format_clock(dtime(18, 42, 30)), "6:42:30 PM")
        self.assertEqual(format_clock(dtime(18, 42)), "6:42 PM")

    def test_drift_range(self):
        self.assertEqual(parse_drift("0"), 0)
        self.assertEqual(parse_drift("30 sec"), 30)
        self.assertEqual(parse_drift("9999999999999"), 9_999_999_999_999)
        for bad in ("-1", "abc", "", "10000000000000", "1.5"):
            with self.assertRaises(ValueError):
                parse_drift(bad)


class ModelTests(unittest.TestCase):
    def test_roundtrip_and_old_jobs(self):
        job = DailyJob("@x_user1", "hi", 5, dtime(18, 42, 30), 1, drift_seconds=30, anchor_date="2026-10-15")
        again = DailyJob.from_dict(job.to_dict())
        self.assertEqual((again.start_time, again.drift_seconds, again.anchor_date), (dtime(18, 42, 30), 30, "2026-10-15"))
        old = DailyJob.from_dict({"target": "@x_user1", "text": "hi", "delay_seconds": 5, "start_time": "09:00", "times_per_day": 1})
        self.assertEqual((old.start_time, old.drift_seconds, old.anchor_date), (dtime(9, 0), 0, None))
        self.assertEqual(old.to_dict()["start_time"], "09:00")


class DriftScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_example_from_the_request(self):
        """6:42:30 PM today, then +30 sec every day: 6:43:00 PM, 6:43:30 PM ..."""
        h = Harness()
        job = DailyJob("@x_user1", "hi", 5, dtime(18, 42, 30), 1, drift_seconds=30, anchor_date="2026-10-15")
        nxt = h.manager._next_run(job, at(15, 9, 0))
        self.assertEqual(nxt, at(15, 18, 42, 30))
        nxt = h.manager._next_run(job, at(15, 19, 0))
        self.assertEqual(nxt, at(16, 18, 43, 0))
        nxt = h.manager._next_run(job, at(16, 19, 0))
        self.assertEqual(nxt, at(17, 18, 43, 30))
        nxt = h.manager._next_run(job, at(26, 19, 0))  # Oct 27 is day 12: 18:42:30 + 12 * 30 s
        self.assertEqual(nxt, at(27, 18, 48, 30))

    async def test_no_drift_is_unchanged(self):
        h = Harness()
        job = DailyJob("@x_user1", "hi", 5, dtime(10, 30), 1)
        self.assertEqual(h.manager._next_run(job, at(20, 11, 0)), at(21, 10, 30))

    async def test_huge_drift_wraps_inside_the_day(self):
        h = Harness()
        drift = 9_999_999_999_999
        job = DailyJob("@x_user1", "hi", 5, dtime(0, 0), 1, drift_seconds=drift, anchor_date="2026-10-15")
        nxt = h.manager._next_run(job, at(15, 23, 59))  # day 1 = Oct 16: the drift wraps inside that day
        self.assertEqual(nxt, at(16, 0, 0) + timedelta(seconds=drift % 86400))
        self.assertEqual(nxt.date(), date(2026, 10, 16))

    async def test_multiple_runs_per_day_all_drift(self):
        h = Harness()
        job = DailyJob("@x_user1", "hi", 5, dtime(10, 0), 2, drift_seconds=60, anchor_date="2026-10-15")  # 10:00 + 22:00
        self.assertEqual(h.manager._next_run(job, at(16, 0, 0)), at(16, 10, 1))
        self.assertEqual(h.manager._next_run(job, at(16, 11, 0)), at(16, 22, 1))

    async def test_anchor_is_first_real_run(self):
        h = Harness()
        job = DailyJob("@x_user1", "hi", 5, dtime(0, 0, 1), 1, drift_seconds=30)
        await h.add_account(OWNER, "A")
        await h.manager.start_daily_job(OWNER, job)
        today = datetime.now(h.manager.tz).date()
        # 00:00:01 has passed today (unless the test runs in the first second of the day): first run is tomorrow
        self.assertIn(job.anchor_date, (today.isoformat(), date.fromordinal(today.toordinal() + 1).isoformat()))
        saved = h.db.collection("telegram_daily_jobs").docs[f"{OWNER}_1"]
        self.assertEqual((saved["drift_seconds"], saved["anchor_date"]), (30, job.anchor_date))
        await h.manager.stop_daily_job(OWNER)

    async def test_status_text_mentions_drift(self):
        h = Harness()
        job = DailyJob("@x_user1", "hi", 5, dtime(18, 42, 30), 1, drift_seconds=30, anchor_date="2026-10-15")
        text = h.manager._describe_job(job)
        self.assertIn("6:42:30 PM", text)
        self.assertIn("+30 sec later every day", text)
        self.assertIn("same time every day", h.manager._describe_job(DailyJob("@x_user1", "hi", 5, dtime(9, 0), 1)))


class DriftWizardTests(unittest.IsolatedAsyncioTestCase):
    async def _until_time(self, h):
        await h.add_account(OWNER, "A")
        await h.tap(OWNER, "sched|new")
        await h.say(OWNER, "@mandal4482")
        await h.tap(OWNER, "wz|schedule|pick|yes")
        await h.say(OWNER, "Good morning")
        await h.tap(OWNER, "wz|schedule|pick|-")
        await h.say(OWNER, "all")
        await h.tap(OWNER, "wz|schedule|pick|5")

    async def test_buttons_and_typed_seconds(self):
        h = Harness()
        await self._until_time(h)
        u = await h.say(OWNER, "6:42:30pm")
        self.assertIn("move later every day", texts(u))
        self.assertIn("Day 2: 6:43 PM", texts(u))
        labels = " ".join(b.text for b in buttons(u.last()[1]))
        self.assertIn("Same time daily", labels)
        self.assertIn("+30 sec", labels)
        u = await h.say(OWNER, "abc")
        self.assertIn("whole number of seconds", texts(u))
        u = await h.say(OWNER, "99999999999999")  # one digit too many
        self.assertIn("whole number of seconds", texts(u))
        u = await h.tap(OWNER, "wz|schedule|pick|30")
        self.assertIn("times per day", texts(u))
        u = await h.tap(OWNER, "wz|schedule|pick|1")
        self.assertIn("+30 sec later every day", texts(u))
        self.assertIn("6:42:30 PM", texts(u))
        u = await h.tap(OWNER, "wz|schedule|pick|yes")
        self.assertIn("scheduled", texts(u))
        job = h.manager.daily_job_for(OWNER)
        self.assertEqual((job.start_time, job.drift_seconds), (dtime(18, 42, 30), 30))
        self.assertIsNotNone(job.anchor_date)
        await h.manager.stop_daily_job(OWNER)

    async def test_max_value_accepted(self):
        h = Harness()
        await self._until_time(h)
        await h.say(OWNER, "10:00am")
        await h.say(OWNER, "9999999999999")
        u = await h.tap(OWNER, "wz|schedule|pick|1")
        self.assertIn("+9999999999999 sec later every day", texts(u))
        await h.tap(OWNER, "wz|schedule|pick|yes")
        self.assertEqual(h.manager.daily_job_for(OWNER).drift_seconds, 9_999_999_999_999)
        await h.manager.stop_daily_job(OWNER)


if __name__ == "__main__":
    unittest.main()
