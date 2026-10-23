"""AI mode: typing at once, a loading message only when slow, one request at a time per admin (the rest wait in line)."""
from __future__ import annotations

import asyncio
from unittest import mock

from tests.harness import OWNER
from tests.test_ai import AITestCase, FakeGemini, say

from telegram_manager.ai.gemini import GeminiError
from telegram_manager.bot.handlers import ai as ai_handler


class GatedGemini(FakeGemini):
    """Answers only after the test opens the gate, like a slow Gemini."""

    def __init__(self, *script):
        super().__init__(*script)
        self.gate = asyncio.Event()

    async def generate(self, *args, **kwargs):
        await self.gate.wait()
        return await super().generate(*args, **kwargs)


class LoadingTests(AITestCase):
    def slow(self, *script) -> GatedGemini:
        self.fake = GatedGemini(*script)
        self.h.deps.gemini = self.fake
        return self.fake

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    def loading_messages(self) -> list[str]:
        return [text for _chat, text in self.h.bot.sent if "Kaam chal raha hai" in text]

    async def test_a_quick_answer_shows_only_the_typing_sign(self):
        self.gemini(say("Ji Mandal Sir."))
        update = await self.h.say(OWNER, "Hello")
        self.assertIn("Ji Mandal Sir.", update.effective_message.replies[-1][0])
        self.assertEqual(self.loading_messages(), [])  # no extra message, so no extra delay
        self.assertEqual(self.h.bot.deleted, [])
        self.assertIn("typing", [action for _chat, action in self.h.bot.actions])

    async def test_typing_shows_at_once_while_the_ai_works(self):
        fake = self.slow(say("Theek hai."))
        job = asyncio.create_task(self.h.say(OWNER, "Hello"))
        await self.settle()
        self.assertIn("typing", [action for _chat, action in self.h.bot.actions])  # before Gemini answered
        self.assertEqual(self.loading_messages(), [])  # not yet: it is not slow so far
        fake.gate.set()
        await job

    async def test_a_slow_answer_gets_a_loading_message_that_counts_and_then_goes(self):
        fake = self.slow(say("Der se aaya."))
        with mock.patch.object(ai_handler, "TYPING_TICK_SECONDS", 0.01), mock.patch.object(ai_handler, "SLOW_AFTER_SECONDS", 0.03):
            job = asyncio.create_task(self.h.say(OWNER, "Hello"))
            await asyncio.sleep(0.15)
            self.assertGreaterEqual(len(self.loading_messages()), 1)
            fake.gate.set()
            update = await job
        self.assertTrue(self.h.bot.edits)  # the seconds were counted on the same message
        self.assertTrue(all("Kaam chal raha hai" in text and "sec" in text for _c, _m, text in self.h.bot.edits))
        self.assertEqual(len(self.h.bot.deleted), 1)  # the loading message is gone again
        self.assertEqual(self.h.bot.deleted[0][1], self.h.bot.edits[0][1])  # the one that was counting
        self.assertIn("Der se aaya.", update.effective_message.replies[-1][0])

    async def test_messages_sent_meanwhile_wait_in_line_and_are_all_answered_in_order(self):
        fake = self.slow(say("Pehla jawab."), say("Doosra jawab."), say("Teesra jawab."))
        first = asyncio.create_task(self.h.say(OWNER, "konsa account login hai"))
        await self.settle()
        second = asyncio.create_task(self.h.say(OWNER, "naam dekho"))
        await self.settle()
        third = asyncio.create_task(self.h.say(OWNER, "oooi"))
        await self.settle()
        self.assertEqual(len(fake.requests), 0)
        fake.gate.set()
        answers = [await first, await second, await third]
        self.assertEqual(len(fake.requests), 3)  # nothing was thrown away
        self.assertEqual([a.effective_message.replies[-1][0] for a in answers], ["Pehla jawab.", "Doosra jawab.", "Teesra jawab."])
        self.assertIn("uske baad", " ".join(r[0] for r in answers[1].effective_message.replies))

    async def test_too_many_waiting_messages_are_refused_politely(self):
        fake = self.slow(say("Ek."), say("Do."))
        with mock.patch.object(ai_handler, "MAX_WAITING", 1):
            first = asyncio.create_task(self.h.say(OWNER, "ek"))
            await self.settle()
            second = asyncio.create_task(self.h.say(OWNER, "do"))
            await self.settle()
            third = await self.h.say(OWNER, "teen")
            self.assertIn("Bahut saare kaam", third.effective_message.replies[-1][0])
            fake.gate.set()
            await first
            await second
        self.assertEqual(len(fake.requests), 2)

    async def test_the_next_message_works_after_the_first_is_done(self):
        fake = self.gemini(say("Pehla."), say("Doosra."))
        await self.h.say(OWNER, "ek")
        update = await self.h.say(OWNER, "do")
        self.assertEqual(len(fake.requests), 2)
        self.assertIn("Doosra.", update.effective_message.replies[-1][0])

    async def test_a_failed_request_clears_the_loading_message_and_frees_the_limit(self):
        class Broken(FakeGemini):
            async def generate(self, *args, **kwargs):
                raise RuntimeError("boom")

        self.h.deps.gemini = Broken()
        failed = await self.h.say(OWNER, "kuch karo")
        self.assertIn("gadbad", failed.effective_message.replies[-1][0])
        self.gemini(say("Wapas chalu."))
        again = await self.h.say(OWNER, "dobara")
        self.assertIn("Wapas chalu.", again.effective_message.replies[-1][0])

    async def test_a_gemini_error_is_shown_and_does_not_leave_the_loading_message(self):
        class Overloaded(FakeGemini):
            async def generate(self, *args, **kwargs):
                raise GeminiError("Could not get an answer: Gemini is having trouble (HTTP 503).")

        self.h.deps.gemini = Overloaded()
        update = await self.h.say(OWNER, "Hi")
        self.assertIn("HTTP 503", update.effective_message.replies[-1][0])
        self.assertEqual(self.loading_messages(), [])

    async def test_the_limit_is_per_admin(self):
        fake = self.slow(say("Owner ka jawab."))
        job = asyncio.create_task(self.h.say(OWNER, "hello"))
        await self.settle()
        self.assertIn(OWNER, self.h.parts.ai._busy)
        fake.gate.set()
        await job
        self.assertEqual(self.h.parts.ai._busy, set())
