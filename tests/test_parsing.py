import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram_manager.parsing import (  # noqa: E402
    parse_bulk_args,
    parse_count,
    parse_delay,
    parse_target,
    parse_time,
)


class TargetTests(unittest.TestCase):
    def test_valid_targets(self):
        self.assertEqual(parse_target("mandal4482"), "@mandal4482")
        self.assertEqual(parse_target(" @mandal4482 "), "@mandal4482")
        self.assertEqual(parse_target("-1001234567890"), "-1001234567890")
        self.assertEqual(parse_target("+919876543210"), "+919876543210")

    def test_invalid_targets(self):
        for bad in ("", "@ab", "hello world", "@1abcde", "12", "@a-b-c-d"):
            with self.assertRaises(ValueError, msg=bad):
                parse_target(bad)


class DelayTimeTests(unittest.TestCase):
    def test_delay(self):
        self.assertEqual(parse_delay("0"), 0)
        self.assertEqual(parse_delay("3sec"), 3)
        self.assertEqual(parse_delay(" 100 "), 100)
        with self.assertRaises(ValueError):
            parse_delay("101")
        with self.assertRaises(ValueError):
            parse_delay("2", minimum=3)
        with self.assertRaises(ValueError):
            parse_delay("abc")

    def test_time(self):
        self.assertEqual(parse_time("10:30am").hour, 10)
        self.assertEqual(parse_time("10:30 PM").hour, 22)
        self.assertEqual(parse_time("22:15").minute, 15)
        self.assertEqual(parse_time("9am").hour, 9)
        with self.assertRaises(ValueError):
            parse_time("25:00")

    def test_count(self):
        self.assertEqual(parse_count("4"), 4)
        for bad in ("0", "-1", "x", "99999"):
            with self.assertRaises(ValueError):
                parse_count(bad)


class BulkArgsTests(unittest.TestCase):
    def test_empty(self):
        args = parse_bulk_args("all", "")
        self.assertEqual((args.target, args.text, args.delay, args.confirmed), (None, None, None, None))

    def test_full_form(self):
        args = parse_bulk_args("all", "@mandal4482 hello there 3sec Y")
        self.assertEqual((args.target, args.text, args.delay, args.confirmed), ("@mandal4482", "hello there", 3, True))

    def test_confirm_and_delay_any_order(self):
        args = parse_bulk_args("all", "@mandal4482 hello Y 3sec")
        self.assertEqual((args.text, args.delay, args.confirmed), ("hello", 3, True))

    def test_confirm_without_delay(self):
        args = parse_bulk_args("all", "@mandal4482 hello Y")
        self.assertEqual((args.text, args.delay, args.confirmed), ("hello", None, True))

    def test_no(self):
        self.assertIs(parse_bulk_args("all", "@mandal4482 hello 3sec N").confirmed, False)

    def test_plain_number_is_part_of_the_message(self):
        args = parse_bulk_args("all", "@mandal4482 call me at 5")
        self.assertEqual((args.text, args.delay), ("call me at 5", None))

    def test_multiline_message_is_kept(self):
        args = parse_bulk_args("all", "@mandal4482 line one\nline two Y")
        self.assertEqual(args.text, "line one\nline two")

    def test_target_only(self):
        args = parse_bulk_args("alll", "@mandal4482")
        self.assertEqual((args.target, args.text), ("@mandal4482", None))

    def test_block_has_no_message(self):
        args = parse_bulk_args("block", "@mandal4482 3sec Y")
        self.assertEqual((args.target, args.text, args.delay, args.confirmed), ("@mandal4482", None, 3, True))
        with self.assertRaises(ValueError):
            parse_bulk_args("block", "@mandal4482 some words")

    def test_bad_target(self):
        with self.assertRaises(ValueError):
            parse_bulk_args("all", "notausername!! hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ExplainSendErrorTests(unittest.TestCase):
    def test_discussion_group_error_gets_a_plain_hint(self):
        from telegram_manager.errors import explain_send_error

        class ChatGuestSendForbiddenError(Exception):
            pass

        text = explain_send_error(ChatGuestSendForbiddenError("You join the discussion group before commenting"))
        self.assertIn("discussion group", text)
        self.assertIn("join", text)
        self.assertIn("(You join the discussion group before commenting)", text)
        # An unknown error is shown as before, without a hint.
        self.assertEqual(explain_send_error(ValueError("boom")), "boom")
