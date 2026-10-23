"""The AI's own private chats, and the extra control tools."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.harness import OWNER, texts
from tests.test_ai import AITestCase, KEY1, FakeGemini, _token, call, say

from telegram_manager.ai.conversation import Conversations
from telegram_manager.ai.gemini import GeminiError

PEER = 555


class ChatCase(AITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        h = self.h
        self.sent: list[tuple[str, str]] = []  # (account id, text) the AI sent to the person

        async def resolve(client, target):
            return SimpleNamespace(id=PEER, first_name="Rahul K", username="rahulk1")

        async def known(account_id, client, peer_id):
            return SimpleNamespace(id=peer_id)

        h.manager._resolve_any_entity, h.manager._known_entity = resolve, known
        for account in (self.a, self.b):
            self._wire_client(account["id"])
        h.deps.chats.sleep = lambda seconds: asyncio.sleep(0)

    def _wire_client(self, account_id):
        client = self.h.manager.clients[account_id]

        async def send_message(entity, text, parse_mode=None, **kw):
            self.sent.append((account_id, text))
            return SimpleNamespace(id=900 + len(self.sent))

        client.send_message = send_message

    async def start(self, **extra):
        args = dict(account="Rahul", target="@rahulk1", goal="Unhe course ke baare mein batao", **extra)
        fake = self.gemini(call("start_chat", **args), say("Ijazat chahiye."))
        update = await self.h.say(OWNER, "Rahul se @rahulk1 se chat karo")
        token = _token(update.effective_message.replies[-1][1])
        await self.h.tap(OWNER, f"aia|ok|{token}")
        return fake

    async def incoming(self, text, sender=PEER, account=None, private=True):
        account = account or self.a
        row = await self.h.store.accounts.get(account["id"], owner_id=OWNER)
        await self.h.deps.chats.on_incoming(row, SimpleNamespace(sender_id=sender, raw_text=text, is_private=private))
        while self.h.manager._background:
            await asyncio.gather(*list(self.h.manager._background))


class StartAndReplyTests(ChatCase):
    async def test_start_needs_approval_then_sends_the_opener(self):
        h = self.h
        self.gemini(call("start_chat", account="Rahul", target="@rahulk1", goal="course batao", first_message="Namaste!", autonomy="auto"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "Rahul se @rahulk1 se chat karo, namaste bolke")
        self.assertEqual(await h.deps.chats.sessions(OWNER), [])  # nothing yet
        self.assertIn("replies by itself", update.effective_message.replies[-1][0])
        done = await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertIn("AI chat shuru", texts(done))
        ((session,),) = [await h.deps.chats.sessions(OWNER)]
        self.assertEqual((session["peer_id"], session["autonomy"], session["state"]), (PEER, "auto", "active"))
        self.assertEqual([c[3] for c in h.calls if c[0] == "send_any"], ["Namaste!"])
        self.assertEqual(session["history"], [{"who": "me", "text": "Namaste!"}])

    async def test_only_people_not_groups(self):
        h = self.h

        async def group(client, target):
            return SimpleNamespace(id=1, title="A group")

        h.manager._resolve_any_entity = group
        fake = self.gemini(call("start_chat", account="Rahul", target="@somegroup", goal="hello all"), say("Ijazat."))
        update = await h.say(OWNER, "group se chat karo")
        await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(await h.deps.chats.sessions(OWNER), [])

    async def test_auto_mode_answers_what_the_person_writes(self):
        h = self.h
        await self.start(autonomy="auto", first_message="Hello")
        fake = self.gemini(say("Course 3 mahine ka hai."))
        await self.incoming("Kitne din ka hai?")
        self.assertEqual(self.sent, [(self.a["id"], "Course 3 mahine ka hai.")])
        request = fake.requests[0]
        self.assertIn("Mandal Sir", request["system"])
        self.assertIn("course ke baare mein", request["system"])  # the goal
        roles = [c["role"] for c in request["contents"]]
        self.assertEqual(roles, ["user", "model", "user"])  # their words and ours alternate, they come first
        self.assertEqual(request["contents"][-1]["parts"][0]["text"], "Kitne din ka hai?")
        self.assertTrue(any("Course 3 mahine" in t for _, t in h.bot.sent))  # the admin is told
        self.assertEqual(h.manager.sent_count(OWNER, 5), 1)  # /unsend can take it back

    async def test_draft_mode_waits_for_the_admin(self):
        h = self.h
        await self.start()
        self.gemini(say("Haan, details bhejta hu."))
        await self.incoming("Details do")
        self.assertEqual(self.sent, [])
        ((token, (owner, sid, text)),) = h.deps.chats._drafts.items()
        self.assertEqual(text, "Haan, details bhejta hu.")
        done = await h.tap(OWNER, f"aic|send|{token}")
        self.assertIn("bhej diya", texts(done))
        self.assertEqual([t for _, t in self.sent], ["Haan, details bhejta hu."])
        again = await h.tap(OWNER, f"aic|send|{token}")
        self.assertIn("purana", texts(again))
        self.assertEqual(len(self.sent), 1)

    async def test_skip_a_draft(self):
        h = self.h
        await self.start()
        self.gemini(say("draft"))
        await self.incoming("hi")
        token = next(iter(h.deps.chats._drafts))
        await h.tap(OWNER, f"aic|skip|{token}")
        self.assertEqual((self.sent, h.deps.chats._drafts), ([], {}))

    async def test_other_people_and_other_accounts_and_groups_are_ignored(self):
        await self.start(autonomy="auto")
        fake = self.gemini(say("never"))
        await self.incoming("hi", sender=999)
        await self.incoming("hi", account=self.b)
        await self.incoming("hi", private=False)
        self.assertEqual((self.sent, fake.requests), ([], []))

    async def test_two_quick_messages_get_one_answer(self):
        await self.start(autonomy="auto")
        fake = self.gemini(say("Ek jawab."), say("duja jawab (nahi aana chahiye)"))
        row = await self.h.store.accounts.get(self.a["id"], owner_id=OWNER)
        for text in ("hello", "kya hua?"):
            await self.h.deps.chats.on_incoming(row, SimpleNamespace(sender_id=PEER, raw_text=text, is_private=True))
        while self.h.manager._background:
            await asyncio.gather(*list(self.h.manager._background))
        self.assertEqual([t for _, t in self.sent], ["Ek jawab."])
        self.assertIn("hello\nkya hua?", fake.requests[0]["contents"][-1]["parts"][0]["text"])


class EndingTests(ChatCase):
    async def test_the_person_says_stop(self):
        h = self.h
        await self.start(autonomy="auto")
        fake = self.gemini(say("no"))
        await self.incoming("please stop messaging me")
        self.assertEqual((self.sent, fake.requests), ([], []))
        self.assertEqual(await h.deps.chats.sessions(OWNER), [])  # ended chats are dropped
        self.assertTrue(any("rokne ko kaha" in t for _, t in h.bot.sent))

    async def test_rude_message_pauses_and_asks_the_admin(self):
        h = self.h
        await self.start(autonomy="auto")
        self.gemini(say("[[ADMIN]] unhone gaali di"))
        await self.incoming("tu kaun hai bc")
        self.assertEqual(self.sent, [])
        self.assertEqual((await h.deps.chats.sessions(OWNER))[0]["state"], "paused")
        self.assertTrue(any("faisla chahiye" in t and "unhone gaali di" in t for _, t in h.bot.sent))
        fake = self.gemini(say("not used"))
        await self.incoming("hello??")  # while paused: remembered, not answered
        self.assertEqual((self.sent, fake.requests), ([], []))
        self.gemini(call("resume_chat", target="@rahulk1"), say("Jaari."))
        await h.say(OWNER, "usse chat jaari rakho")
        self.assertEqual((await h.deps.chats.sessions(OWNER))[0]["state"], "active")

    async def test_the_end_tag_and_the_reply_limit(self):
        h = self.h
        await self.start(autonomy="auto", max_replies=2)
        self.gemini(say("Dhanyavaad! [[END]]"))
        await self.incoming("ok thik hai")
        self.assertEqual([t for _, t in self.sent], ["Dhanyavaad!"])
        self.assertEqual(await h.deps.chats.sessions(OWNER), [])
        self.sent.clear()
        await self.start(autonomy="auto", max_replies=1)
        self.gemini(say("Pehla"))
        await self.incoming("ek")
        self.assertEqual(await h.deps.chats.sessions(OWNER), [])  # the single reply was used up
        self.assertTrue(any("limit poora" in t for _, t in h.bot.sent))

    async def test_gemini_failure_tells_the_admin_and_keeps_the_chat(self):
        h = self.h
        await self.start(autonomy="auto")

        class Boom(FakeGemini):
            async def generate(self, *a, **k):
                raise GeminiError("keys are resting")

        h.deps.gemini = Boom()
        await self.incoming("hello")
        self.assertTrue(any("keys are resting" in t for _, t in h.bot.sent))
        self.assertEqual((await h.deps.chats.sessions(OWNER))[0]["state"], "active")

    async def test_chats_survive_a_restart(self):
        h = self.h
        await self.start(autonomy="auto")
        fresh = Conversations(h.deps)
        loaded = await fresh.sessions(OWNER)
        self.assertEqual((loaded[0]["peer_id"], loaded[0]["goal"]), (PEER, "Unhe course ke baare mein batao"))


class ListAndStopTests(ChatCase):
    async def test_aichats_lists_and_stops(self):
        h = self.h
        empty = await h.command(OWNER, "/aichats")
        self.assertIn("koi AI chat nahi", texts(empty))
        await self.start(autonomy="auto")
        shown = await h.command(OWNER, "/aichats")
        self.assertIn("@rahulk1", texts(shown))
        sid = (await h.deps.chats.sessions(OWNER))[0]["id"]
        done = await h.tap(OWNER, f"aic|stop|{sid}")
        self.assertIn("band", texts(done))
        self.assertEqual(await h.deps.chats.sessions(OWNER), [])

    async def test_the_ai_can_list_and_end_chats(self):
        h = self.h
        await self.start(autonomy="auto")
        fake = self.gemini(call("list_chats"), call("end_chat", target="rahulk1"), say("Band kar di."))
        await h.say(OWNER, "kaun si chat chal rahi hai? band karo")
        listed = fake.requests[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["chats"]
        self.assertEqual((listed[0]["with"], listed[0]["account"]), ("@rahulk1", 1))
        self.assertEqual(await h.deps.chats.sessions(OWNER), [])


class MoreControlTests(AITestCase):
    async def test_join_chat(self):
        h = self.h
        joined = []

        async def join(account_id, link, owner_id=None):
            joined.append((account_id, link))

        h.manager.join_chat_on_account = join
        self.gemini(call("join_chat", accounts=["all"], link="t.me/+abcDEF123"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "sab account is group mein jodo")
        self.assertEqual(joined, [])
        done = await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(sorted(a for a, _ in joined), sorted([self.a["id"], self.b["id"]]))
        self.assertIn("Joined on 2 of 2", texts(done))

    async def test_set_profile(self):
        h = self.h
        changed = []

        async def update_profile(account_id, owner_id=None, first_name=None, last_name=None, about=None):
            changed.append((account_id, first_name, about))

        h.manager.update_profile = update_profile
        await h.store.settings.set_ai(OWNER, mode="auto")
        self.gemini(call("set_profile", accounts=["Rahul"], first_name="Raj", about="Open for work"), say("Ho gaya."))
        await h.say(OWNER, "Rahul ka naam Raj kar do")
        self.assertEqual(changed, [(self.a["id"], "Raj", "Open for work")])

    async def test_logout_always_asks_even_in_auto_mode(self):
        h = self.h
        removed = []

        async def remove(account_id, owner_id=None):
            removed.append(account_id)
            return True

        h.manager.remove_account = remove
        await h.store.settings.set_ai(OWNER, mode="auto")
        self.gemini(call("logout_account", accounts=["Amit"]), say("Ijazat chahiye."))
        update = await h.say(OWNER, "Amit ko logout karo")
        self.assertEqual(removed, [])
        ask = update.effective_message.replies[-1]
        self.assertIn("LOG OUT", ask[0])
        await h.tap(OWNER, f"aia|ok|{_token(ask[1])}")
        self.assertEqual(removed, [self.b["id"]])

    async def test_send_later_shows_the_wait(self):
        h = self.h
        self.gemini(call("send_later", accounts=["Rahul"], target="@mandal4482", text="yaad dilana", minutes=30), say("Ijazat chahiye."))
        update = await h.say(OWNER, "30 min baad yaad dilao")
        self.assertIn("In 30 min", update.effective_message.replies[-1][0])
        self.assertEqual([c for c in h.calls if c[0] == "send_any"], [])

    async def test_every_six_hours_is_four_times_a_day(self):
        h = self.h
        self.gemini(call("create_daily_broadcast", target="@mandal4482", text="hi", time="06:00", times_per_day=4), say("Ijazat."))
        update = await h.say(OWNER, "har 6 ghante message karo")
        self.assertIn("4×/day", update.effective_message.replies[-1][0])
