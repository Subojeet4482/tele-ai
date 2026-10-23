"""The AI sees every saved account, lays them out as asked, finds them, and checks which are banned or limited."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tests.harness import OWNER, texts  # installs the Telegram / Telethon stand-ins first
from tests.test_ai import AITestCase, _token, call, say
from tests.test_ai_mode import response_of

from telethon import errors  # noqa: E402

from telegram_manager import account_status
from telegram_manager.ai import account_tools, knowledge


def row(number, name, username="", phone="", created="2026-09-01", **extra):
    return {"number": number, "display_name": name, "username": username, "phone": phone, "created_at": created, **extra}


def view(number, name, status="online", **kw):
    return account_tools.view_of(row(number, name, **kw), {"status": status, "online": status == "online", "note": kw.get("note", "")})


# --- pure helpers ----------------------------------------------------------------------------------------------
class StatusWordTests(unittest.TestCase):
    def test_a_ban_is_told_from_a_deleted_account_and_a_logout(self):
        self.assertEqual(account_status.classify_dead("UserDeactivatedBanError", "x"), "banned")
        self.assertEqual(account_status.classify_dead("UserDeactivatedError", "x"), "deleted")
        self.assertEqual(account_status.classify_dead("SessionRevokedError", "The authorization has been invalidated"), "logged_out")
        self.assertEqual(account_status.classify_dead("", "session expired or revoked"), "logged_out")

    def test_spambot_answers(self):
        self.assertEqual(account_status.parse_spambot("Good news, no limits are currently applied to your account. You’re free as a bird!")[0], "ok")
        state, note = account_status.parse_spambot("Unfortunately, some phone numbers may have been limited until 5 Nov.")
        self.assertEqual(state, "limited")
        self.assertIn("5 Nov", note)
        self.assertEqual(account_status.parse_spambot("Namaste, kuch aur")[0], "unknown")

    def test_the_words_an_admin_uses_map_to_a_status_filter(self):
        for word, want in {"block": "banned", "Blocked": "banned", "ban": "banned", "kharab": "problem", "logged out": "logged_out",
                           "limit": "limited", "spam": "limited", "dead": "unusable", "sab": "all", "": "all", None: "all", "theek": "healthy"}.items():
            self.assertEqual(account_tools.resolve_status(word), want, word)
        with self.assertRaises(ValueError):
            account_tools.resolve_status("purple")

    def test_sort_words(self):
        words = ("naam", "A-Z", "status", "date", None, "username")
        self.assertEqual([account_tools.resolve_sort(w) for w in words], ["name", "name", "status", "added", "number", "username"])
        self.assertEqual(account_tools.resolve_sort("naam"), "name")
        self.assertEqual(account_tools.resolve_sort(None), "number")
        self.assertTrue(account_tools.is_descending("Z-A"))
        self.assertTrue(account_tools.is_descending("ulta"))
        self.assertFalse(account_tools.is_descending(None))


class FindAndSortTests(unittest.TestCase):
    def setUp(self):
        self.views = [
            view(1, "Rahul", username="rahul_x", phone="+919876543210", created="2026-09-01"),
            view(2, "Amit Kumar", phone="+919811112222", created="2026-09-05", status="banned"),
            view(3, "Zoya", username="zoya9", phone="+14155550123", created="2026-08-20", status="limited"),
            view(12, "Rahim", username="abc", created="2026-09-10", status="offline"),
        ]

    def numbers(self, views):
        return [v["number"] for v in views]

    def test_find_by_name_username_number_and_phone_digits(self):
        find = lambda q: self.numbers(account_tools.search(self.views, q)[0])  # noqa: E731
        self.assertEqual(find("rah"), [1, 12])
        self.assertEqual(find("@zoya9"), [3])
        self.assertEqual(find("amit kumar"), [2])
        self.assertEqual(find("12"), [12])  # up to 3 digits = the account number, not a piece of a phone
        self.assertEqual(find("1111"), [2])  # 4+ digits = inside the phone
        self.assertEqual(find("3210"), [1])

    def test_a_spelling_mistake_is_forgiven_but_only_when_nothing_matches(self):
        found, fuzzy = account_tools.search(self.views, "rahull")
        self.assertTrue(fuzzy)
        self.assertEqual(1, found[0]["number"])
        self.assertFalse(account_tools.search(self.views, "zoya")[1])
        self.assertEqual(account_tools.search(self.views, "qqqqqq"), ([], False))

    def test_sorting_never_changes_the_panel_numbers(self):
        by_name = account_tools.sort_views(self.views, "name")
        self.assertEqual([v["name"] for v in by_name], ["Amit Kumar", "Rahim", "Rahul", "Zoya"])
        self.assertEqual(self.numbers(by_name), [2, 12, 1, 3])
        self.assertEqual(self.numbers(account_tools.sort_views(self.views, "name", True)), [3, 1, 12, 2])
        self.assertEqual(self.numbers(account_tools.sort_views(self.views, "number")), [1, 2, 3, 12])
        self.assertEqual(self.numbers(account_tools.sort_views(self.views, "added", True)), [12, 2, 1, 3])

    def test_problems_come_first_when_sorted_by_status(self):
        self.assertEqual([v["status"] for v in account_tools.sort_views(self.views, "status")], ["banned", "limited", "offline", "online"])
        self.assertEqual(account_tools.sort_views(self.views, "status", True)[0]["status"], "online")

    def test_usernames_sort_with_the_empty_ones_last(self):
        self.assertEqual(self.numbers(account_tools.sort_views(self.views, "username")), [12, 1, 3, 2])

    def test_status_filters(self):
        pick = lambda w: self.numbers([v for v in self.views if account_tools.matches_status(v, w)])  # noqa: E731
        self.assertEqual(pick("banned"), [2])
        self.assertEqual(pick("unusable"), [2])
        self.assertEqual(pick("problem"), [2, 3, 12])
        self.assertEqual(pick("healthy"), [1])
        self.assertEqual(pick("all"), [1, 2, 3, 12])


class RenderTests(unittest.TestCase):
    def test_one_account_per_line_with_the_panel_number_and_a_note_for_trouble(self):
        views = [view(2, "Amit", status="banned", username="a", phone="+919811112222", note="The user has been deleted"),
                 view(1, "Rahul")]
        text = "\n".join(account_tools.render(views, title="Accounts", sort_by="name"))
        self.assertIn("2. Amit (@a) · +91••••••2222 · ⛔ banned — The user has been deleted", text)
        self.assertIn("1. Rahul · 🟢 online", text)
        self.assertIn("number = panel wala number", text)
        self.assertIn("⛔ 1 banned · 🟢 1 online", text)

    def test_grouping_by_status(self):
        views = [view(1, "A"), view(2, "B", status="banned"), view(3, "C", status="limited")]
        text = "\n".join(account_tools.render(views, title="Accounts", group_by="status"))
        self.assertLess(text.index("Banned (1)"), text.index("Limited (spam) (1)"))
        self.assertLess(text.index("Limited (spam) (1)"), text.index("Online (1)"))

    def test_a_long_list_is_split_into_messages_under_the_limit(self):
        views = [view(n, f"Account number {n} with a long name", username=f"user_{n}", phone="+919811112222") for n in range(1, 201)]
        blocks = account_tools.render(views, title="Accounts")
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(len(b) <= 4096 for b in blocks))
        self.assertEqual(sum(b.count("Account number") for b in blocks), 200)

    def test_nothing_to_show(self):
        self.assertEqual(account_tools.render([], title="Accounts"), [])

    def test_the_guide_has_every_topic_and_the_numbers_of_the_code(self):
        for topic in knowledge.topic_names():
            self.assertTrue(knowledge.guide(topic))
        self.assertIn("1440", knowledge.guide("broadcasts"))
        self.assertIsNone(knowledge.guide("nothing"))
        self.assertIn("show_accounts", knowledge.OVERVIEW)


# --- the manager's view of an account ----------------------------------------------------------------------------------
class HealthTests(AITestCase):
    def health(self, row_):
        return self.h.manager.account_health(row_)

    async def test_online_offline_disabled_banned_deleted_logged_out(self):
        m = self.h.manager
        self.assertEqual(self.health(self.a)["status"], "online")
        del m.clients[self.a["id"]]
        self.assertEqual(self.health(self.a)["status"], "offline")
        self.assertEqual(self.health({**self.b, "enabled": False})["status"], "disabled")
        for kind, want in (("UserDeactivatedBanError", "banned"), ("UserDeactivatedError", "deleted"), ("SessionRevokedError", "logged_out"), ("", "logged_out")):
            m._dead_accounts[self.b["id"]], m._dead_kinds[self.b["id"]] = "reason text", kind
            self.assertEqual(self.health(self.b), {"status": want, "online": False, "note": "reason text", "checked_at": ""})

    async def test_a_dead_session_notice_says_ban_when_it_was_a_ban(self):
        await self.h.manager._drop_dead_account(self.a["id"], "Rahul", "The user has been deleted/deactivated", "UserDeactivatedBanError")
        sent = [text for chat, text in self.h.bot.sent if chat == OWNER]
        self.assertTrue(any("banned by Telegram" in t for t in sent))
        self.assertEqual(self.health(self.a)["status"], "banned")

    async def test_a_saved_limit_shows_until_the_next_check(self):
        await self.h.manager._save_health(self.a["id"], "limited", "limited until 5 Nov")
        fresh = await self.h.store.accounts.get(self.a["id"])
        self.assertEqual(self.health(fresh)["status"], "limited")
        self.assertEqual(self.health(fresh)["note"], "limited until 5 Nov")
        self.assertTrue(self.health(fresh)["checked_at"].startswith(str(datetime.now(timezone.utc).year)))
        await self.h.manager._save_health(self.a["id"], "ok", "")
        self.assertEqual(self.health(await self.h.store.accounts.get(self.a["id"]))["status"], "online")

    async def test_check_account_asks_get_me_and_spambot(self):
        m = self.h.manager
        asked = []

        async def spambot(client):
            asked.append(client)
            return "Unfortunately, some phone numbers may have been limited."

        m._ask_spambot = spambot
        result = await m.check_account(OWNER, self.a["id"], spam=True)
        self.assertEqual((result["status"], result["checked"]), ("limited", True))
        self.assertEqual(len(asked), 1)
        m._ask_spambot = lambda client: _answer("Good news, no limits are currently applied to your account.")
        result = await m.check_account(OWNER, self.a["id"], spam=True)
        self.assertEqual((result["status"], result["checked"]), ("online", True))  # a lifted limit is noticed

    async def test_without_the_spam_check_nothing_is_sent_to_spambot(self):
        self.h.manager._ask_spambot = lambda client: (_ for _ in ()).throw(AssertionError("must not ask @SpamBot"))
        result = await self.h.manager.check_account(OWNER, self.a["id"], spam=False)
        self.assertEqual((result["status"], result["checked"]), ("online", True))

    async def test_a_banned_session_is_found_by_get_me(self):
        async def get_me():
            raise errors.UserDeactivatedBanError("The user has been deleted/deactivated")

        self.h.manager.clients[self.a["id"]].get_me = get_me
        result = await self.h.manager.check_account(OWNER, self.a["id"], spam=False)
        self.assertEqual((result["status"], result["checked"]), ("banned", True))
        self.assertNotIn(self.a["id"], self.h.manager.clients)

    async def test_a_frozen_account_counts_as_banned(self):
        async def get_me():
            raise RuntimeError("FROZEN_METHOD_INVALID (caused by GetMeRequest)")

        self.h.manager.clients[self.a["id"]].get_me = get_me
        result = await self.h.manager.check_account(OWNER, self.a["id"], spam=False)
        self.assertEqual(result["status"], "banned")
        self.assertIn("frozen", result["note"])

    async def test_an_offline_account_is_probed_once_to_learn_why(self):
        m = self.h.manager
        del m.clients[self.a["id"]]

        async def connect(account):
            raise errors.SessionRevokedError("The authorization has been invalidated")

        m._connect_saved_account = connect
        result = await m.check_account(OWNER, self.a["id"], spam=True)
        self.assertEqual((result["status"], result["checked"]), ("logged_out", True))
        # a network problem is not a verdict
        del m.clients[self.b["id"]]

        async def down(account):
            raise ConnectionError("network down")

        m._connect_saved_account = down
        result = await m.check_account(OWNER, self.b["id"], spam=True)
        self.assertEqual((result["status"], result["checked"]), ("offline", False))
        self.assertIn("could not connect", result["note"])

    async def test_a_dead_account_needs_no_network_and_a_stranger_cannot_be_checked(self):
        m = self.h.manager
        m._dead_accounts[self.a["id"]], m._dead_kinds[self.a["id"]] = "gone", "UserDeactivatedBanError"
        self.assertEqual(await m.check_account(OWNER, self.a["id"]), {"status": "banned", "online": False, "note": "gone", "checked_at": "", "checked": False})
        with self.assertRaises(ValueError):
            await m.check_account(999, self.b["id"])

    async def test_the_manual_list_marks_trouble(self):
        self.h.manager._dead_accounts[self.b["id"]], self.h.manager._dead_kinds[self.b["id"]] = "x", "UserDeactivatedBanError"
        await self.h.manager._save_health(self.a["id"], "limited", "limited")
        shown = texts(await self.h.command(OWNER, "/list"))
        self.assertIn("Amit ⛔ banned", shown)
        self.assertIn("Rahul 🟢 ⚠️ limited", shown)


async def _answer(text):
    return text


# --- the AI ------------------------------------------------------------------------------------------------------------------
class AIAccountTests(AITestCase):
    async def add(self, name, **extra):
        account = await self.h.add_account(OWNER, name)
        if extra:
            await self.h.store.accounts.update(account["id"], extra)
        return account

    def shown(self, update) -> str:
        return "\n".join(text for text, _ in update.effective_message.replies)

    async def test_the_ai_knows_every_saved_account_before_it_is_asked(self):
        zoya = await self.add("Zoya", username="zoya9")
        self.h.manager._dead_accounts[self.b["id"]], self.h.manager._dead_kinds[self.b["id"]] = "x", "UserDeactivatedBanError"
        del self.h.manager.clients[zoya["id"]]
        fake = self.gemini(say("ok"))
        await self.h.say(OWNER, "hello")
        system = fake.requests[0]["system"]
        self.assertIn("ACCOUNTS (3 saved", system)
        self.assertIn("1 Rahul;", system)
        self.assertIn("2 Amit [banned]", system)
        self.assertIn("3 Zoya @zoya9 [offline]", system)
        self.assertIn("show_accounts", system)  # and how to show / sort / find / check them

    async def test_a_big_roster_is_summarised(self):
        for n in range(45):
            await self.add(f"Extra {n}")
        fake = self.gemini(say("ok"))
        await self.h.say(OWNER, "hello")
        system = fake.requests[0]["system"]
        self.assertIn("ACCOUNTS (47 saved", system)
        self.assertIn("Too many to list here", system)
        self.assertNotIn("Extra 3", system)

    async def test_list_accounts_returns_all_of_them_including_dead_and_offline(self):
        zoya = await self.add("Zoya")
        del self.h.manager.clients[zoya["id"]]
        self.h.manager._dead_accounts[self.b["id"]], self.h.manager._dead_kinds[self.b["id"]] = "x", "UserDeactivatedBanError"
        fake = self.gemini(call("list_accounts"), call("list_accounts", status="block"), say("ok"))
        await self.h.say(OWNER, "sab account")
        everything = response_of(fake, 1)
        self.assertEqual((everything["total"], everything["matching"]), (3, 3))
        self.assertEqual({a["name"]: a["status"] for a in everything["accounts"]}, {"Rahul": "online", "Amit": "banned", "Zoya": "offline"})
        self.assertEqual(everything["counts"], {"banned": 1, "offline": 1, "online": 1})
        self.assertTrue(all("_digits" not in a and "phone" in a for a in everything["accounts"]))  # no private keys, phone only masked
        self.assertEqual([a["name"] for a in response_of(fake, 2)["accounts"]], ["Amit"])

    async def test_show_accounts_prints_the_list_itself(self):
        fake = self.gemini(call("show_accounts"), say("Ye lo."))
        u = await self.h.say(OWNER, "list dikhao")
        first = u.effective_message.replies[0][0]
        self.assertIn("📋 Accounts: 2", first)
        self.assertIn("1. Rahul", first)
        self.assertIn("2. Amit", first)
        self.assertEqual(u.effective_message.replies[-1][0], "Ye lo.")
        told = response_of(fake, 1)
        self.assertTrue(told["shown_to_admin"])
        self.assertIn("Do not retype", told["note"])

    async def test_sajao_naam_se_keeps_the_panel_numbers(self):
        await self.add("Aarav")
        self.gemini(call("show_accounts", sort_by="naam"), say(""))
        u = await self.h.say(OWNER, "line mein naam se sajao")
        lines = [l for l in self.shown(u).splitlines() if l[:1].isdigit()]
        self.assertEqual([l.split(" · ")[0] for l in lines], ["3. Aarav", "2. Amit", "1. Rahul"])
        self.assertIn("naam ke hisaab se", self.shown(u))
        self.assertEqual(u.effective_message.replies[-1][0].startswith("📋"), True)  # no stray "Theek hai" after a list

    async def test_reverse_order_and_problems_first(self):
        await self.h.manager._save_health(self.a["id"], "limited", "limited")
        self.gemini(call("show_accounts", sort_by="name", order="Z-A"), say(""), call("show_accounts", sort_by="status"), say(""))
        u = await self.h.say(OWNER, "z se a")
        self.assertLess(self.shown(u).index("Rahul"), self.shown(u).index("Amit"))
        u = await self.h.say(OWNER, "problem wale pehle")
        self.assertLess(self.shown(u).index("Rahul"), self.shown(u).index("Amit"))  # limited before online

    async def test_ban_and_block_accounts_are_found_and_grouped(self):
        self.h.manager._dead_accounts[self.b["id"]], self.h.manager._dead_kinds[self.b["id"]] = "The user has been deleted/deactivated", "UserDeactivatedBanError"
        await self.h.manager._save_health(self.a["id"], "limited", "limited until 5 Nov")
        fake = self.gemini(call("show_accounts", status="block"), say(""),
                           call("show_accounts", status="problem", group_by="status"), say("2 account mein dikkat hai."))
        u = await self.h.say(OWNER, "block ban account dikhao")
        text = self.shown(u)
        self.assertIn("(banned)", text)
        self.assertIn("2. Amit · ⛔ banned — The user has been deleted/deactivated", text)
        self.assertNotIn("Rahul", text)
        u = await self.h.say(OWNER, "sab kharab account")
        text = self.shown(u)
        self.assertIn("⛔ Banned (1)", text)
        self.assertIn("⚠️ Limited (spam) (1)", text)
        self.assertIn("limited until 5 Nov", text)
        self.assertEqual(response_of(fake, 3)["counts"], {"banned": 1, "limited": 1})

    async def test_nothing_matching_is_said_plainly(self):
        fake = self.gemini(call("show_accounts", status="banned"), say("Koi banned account nahi hai."))
        u = await self.h.say(OWNER, "banned dikhao")
        self.assertEqual(u.effective_message.replies[-1][0], "Koi banned account nahi hai.")
        self.assertEqual(response_of(fake, 1)["matching"], 0)

    async def test_a_wrong_filter_goes_back_to_the_model(self):
        fake = self.gemini(call("show_accounts", status="purple"), call("show_accounts", sort_by="size"), say("ok"))
        await self.h.say(OWNER, "kuch")
        self.assertIn("status must be one of", response_of(fake, 1)["error"])
        self.assertIn("sort_by must be one of", response_of(fake, 2)["error"])

    async def test_find_accounts_forgives_a_typo_and_gives_the_numbers_back(self):
        await self.add("Rohan", username="rohan_k")
        fake = self.gemini(call("find_accounts", query="rahull"), say("Rahul mila, number 1."))
        u = await self.h.say(OWNER, "rahull wala account dhundo")
        self.assertIn("milte-julte", self.shown(u))
        self.assertIn("1. Rahul", self.shown(u))
        found = response_of(fake, 1)
        self.assertTrue(found["fuzzy_match"])
        self.assertEqual([m["number"] for m in found["matches"]], [1])

    async def test_find_by_the_end_of_a_phone_number(self):
        await self.h.store.accounts.update(self.b["id"], {"phone": "+919800098765", "phone_masked": "+91••••••8765"})
        fake = self.gemini(call("find_accounts", query="98765"), say("ok"))
        u = await self.h.say(OWNER, "jo 98765 pe khatam hota hai")
        self.assertIn("2. Amit · +91••••••8765", self.shown(u))
        self.assertNotIn("9800098765", self.shown(u) + str(fake.requests[1]["contents"]))  # the full number never leaves the bot

    async def test_find_with_no_hit_and_an_empty_query(self):
        fake = self.gemini(call("find_accounts", query="qqqqq"), call("find_accounts", query=" "), say("nahi mila"))
        u = await self.h.say(OWNER, "qqqqq")
        self.assertEqual(response_of(fake, 1)["matches"], [])
        self.assertIn("What should I look for", response_of(fake, 2)["error"])
        self.assertEqual([t for t, _ in u.effective_message.replies], ["nahi mila"])

    async def test_a_loosely_named_account_is_understood_for_actions(self):
        await self.add("Rajesh")
        self.gemini(call("send_message", accounts=["rahu"], target="@mandal4482", text="hello"), say("Ijazat chahiye."))
        u = await self.h.say(OWNER, "rahu se hello bhejo")
        self.assertIn("Rahul", u.effective_message.replies[-1][0])
        fake = self.gemini(call("send_message", accounts=["ra"], target="@mandal4482", text="hello"), say("kaun sa?"))
        await self.h.say(OWNER, "ra se hello bhejo")
        error = response_of(fake, 1)["error"]
        self.assertIn("fits 2 accounts", error)
        self.assertIn("1 Rahul", error)
        self.assertIn("3 Rajesh", error)
        self.assertIn("ask_user", error)

    async def test_what_was_shown_is_remembered_for_the_next_message(self):
        fake = self.gemini(call("show_accounts", status="problem"), say(""), call("show_accounts", sort_by="name"), say(""))
        await self.h.manager._save_health(self.a["id"], "limited", "x")
        await self.h.say(OWNER, "kharab wale dikhao")
        await self.h.say(OWNER, "usko naam se sajao")
        remembered = fake.requests[2]["contents"][1]["parts"][0]["text"]
        self.assertIn("(notes:", remembered)
        self.assertIn("status=problem", remembered)
        self.assertIn("numbers 1", remembered)


class AICheckTests(AITestCase):
    def shown(self, update) -> str:
        return "\n".join(text for text, _ in update.effective_message.replies)

    async def setup_trouble(self):
        zoya = await self.h.add_account(OWNER, "Zoya")
        answers = {self.a["id"]: "Good news, no limits are currently applied to your account.",
                   self.b["id"]: "Unfortunately, some phone numbers may have been limited until 5 Nov."}

        async def spambot(client):
            for account_id, c in self.h.manager.clients.items():
                if c is client and account_id in answers:
                    return answers[account_id]
            raise AssertionError("asked a client that should not be asked")

        async def get_me():
            raise errors.UserDeactivatedBanError("banned")

        self.h.manager._ask_spambot = spambot
        self.h.manager.clients[zoya["id"]].get_me = get_me
        return zoya

    async def test_check_asks_first_then_finds_the_banned_and_limited(self):
        h = self.h
        await self.setup_trouble()
        fake = self.gemini(call("check_accounts"), say("Ijazat chahiye."))
        u = await h.say(OWNER, "ban ya limit wale account check karo")
        asked = u.effective_message.replies[-1]
        self.assertIn("@SpamBot", asked[0])
        self.assertIn("3 account", asked[0])
        self.assertEqual(await h.store.accounts.get(self.b["id"]) and (await h.store.accounts.get(self.b["id"])).get("health"), None)  # nothing yet
        done = await h.tap(OWNER, f"aia|ok|{_token(asked[1])}")
        text = self.shown(done)
        self.assertIn("⛔ Banned (1)", text)
        self.assertIn("3. Zoya", text)
        self.assertIn("⚠️ Limited (spam) (1)", text)
        self.assertIn("limited until 5 Nov", text)
        self.assertIn("🟢 Online (1)", text)
        self.assertIn("3 account check hue", texts(done))
        self.assertEqual((await h.store.accounts.get(self.b["id"]))["health"]["state"], "limited")
        self.assertEqual(len(fake.requests), 2)

    async def test_without_spam_check_it_runs_at_once_in_confirm_mode(self):
        h = self.h
        await self.setup_trouble()
        self.h.manager._ask_spambot = lambda client: (_ for _ in ()).throw(AssertionError("no @SpamBot"))
        fake = self.gemini(call("check_accounts", accounts=["all"], spam_check=False), say("2 theek, 1 banned."))
        u = await h.say(OWNER, "sirf ban check karo")
        self.assertIn("⛔ Banned (1)", self.shown(u))
        result = response_of(fake, 1)
        self.assertEqual(result["counts"], {"banned": 1, "online": 2})
        self.assertEqual([(p["number"], p["status"]) for p in result["problems"]], [(3, "banned")])

    async def test_one_account_can_be_checked_by_name(self):
        h = self.h
        await self.setup_trouble()
        await h.store.settings.set_ai(OWNER, mode="auto")
        fake = self.gemini(call("check_accounts", accounts=["Amit"]), say("Amit limited hai."))
        u = await h.say(OWNER, "Amit check karo")
        text = self.shown(u)
        self.assertIn("2. Amit", text)
        self.assertNotIn("Rahul", text)
        self.assertEqual(response_of(fake, 1)["counts"], {"limited": 1})


class GuideToolTests(AITestCase):
    async def test_the_ai_can_read_its_own_manual(self):
        fake = self.gemini(call("panel_guide", topic="health"), call("panel_guide", topic="Start Screen"), call("panel_guide", topic="nope"), say("ok"))
        await self.h.say(OWNER, "ban kya hota hai")
        self.assertIn("banned: Telegram banned or froze it", response_of(fake, 1)["guide"])
        self.assertIn("800", response_of(fake, 2)["guide"])
        self.assertIn("health", response_of(fake, 3)["topics"])

    async def test_the_system_text_carries_the_overview(self):
        fake = self.gemini(say("ok"))
        await self.h.say(OWNER, "hello")
        system = fake.requests[0]["system"]
        for needle in ("PANEL NUMBER", "show_accounts", "find_accounts", "check_accounts", "panel_guide", "Numbers never change when you sort"):
            self.assertIn(needle, system)


if __name__ == "__main__":
    unittest.main()
