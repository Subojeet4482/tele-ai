import random

from telegram_manager.core import humanize
from telegram_manager.core.joinparse import extract_join_targets, pick_verify_button


def test_speed_is_stable_and_in_range():
    assert humanize.account_speed("a1") == humanize.account_speed("a1")
    assert all(0.75 <= humanize.account_speed(str(i)) <= 1.45 for i in range(200))


def test_typing_time_grows_with_text_and_is_capped():
    rng = random.Random(1)
    assert humanize.typing_seconds("hi", 1.0, rng) >= humanize.TYPING_MIN
    assert humanize.typing_seconds("x" * 5000, 1.0, rng) <= humanize.TYPING_MAX
    assert humanize.typing_seconds("x" * 60, 1.0, random.Random(2)) > humanize.typing_seconds("x" * 6, 1.0, random.Random(2))


def test_jitter_varies_and_zero_stays_zero():
    assert humanize.jitter_delay(0, "a") == 0
    assert len({round(humanize.jitter_delay(30, str(i)), 2) for i in range(20)}) > 10


def test_extract_targets():
    texts = ["Join https://t.me/+AbCdEfGhIjKl and t.me/mychannel first, @other_chan and @helper_bot", "https://t.me/mychannel"]
    assert extract_join_targets(texts) == ["https://t.me/+AbCdEfGhIjKl", "@mychannel", "@other_chan"]


def test_verify_button():
    assert pick_verify_button(["Join", "✅ Joined"]) == "✅ Joined"
    assert pick_verify_button(["Menu", "Help"]) is None
