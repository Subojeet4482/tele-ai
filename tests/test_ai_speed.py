"""Speed settings of the Gemini client, the Menu-only AI keyboard and the prompt default."""
from __future__ import annotations

import unittest

from tests import stubs  # noqa: F401  (stand-ins for the telegram libraries)

from telegram_manager.ai.config import DEFAULT_PROMPT, LEGACY_PROMPTS, AISettings
from telegram_manager.ai.gemini import GeminiClient, thinking_config
from telegram_manager.bot import reply_menu

OK = {"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]}
USER = [{"role": "user", "parts": [{"text": "hi"}]}]


class Recorder:
    def __init__(self, *replies):
        self.replies, self.bodies = list(replies), []

    async def __call__(self, url, headers, body):
        self.bodies.append(body)
        return self.replies.pop(0)


class ThinkingTests(unittest.IsolatedAsyncioTestCase):
    def test_gemini_3_uses_a_level_and_gemini_2_a_budget(self):
        self.assertEqual(thinking_config("gemini-flash-latest"), {"thinkingLevel": "low"})
        self.assertEqual(thinking_config("gemini-3.1-flash-lite", "minimal"), {"thinkingLevel": "minimal"})
        self.assertEqual(thinking_config("gemini-2.5-flash", "minimal"), {"thinkingBudget": 0})

    async def test_thinking_is_kept_low_by_default(self):
        post = Recorder((200, OK))
        await GeminiClient(post).generate(["k"], "gemini-flash-latest", "sys", USER)
        self.assertEqual(post.bodies[0]["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})

    async def test_a_model_that_refuses_the_setting_is_asked_again_without_it_and_remembered(self):
        refusal = (400, {"error": {"message": "Thinking level is not supported for this model"}})
        post = Recorder(refusal, (200, OK), (200, OK))
        client = GeminiClient(post)
        await client.generate(["k"], "some-model", "sys", USER)
        self.assertIn("thinkingConfig", post.bodies[0]["generationConfig"])
        self.assertNotIn("thinkingConfig", post.bodies[1]["generationConfig"])
        await client.generate(["k"], "some-model", "sys", USER)
        self.assertNotIn("thinkingConfig", post.bodies[2]["generationConfig"])  # no second failed try

    async def test_other_400_errors_are_still_reported(self):
        from telegram_manager.ai.gemini import GeminiError

        post = Recorder((400, {"error": {"message": "Request payload is invalid"}}))
        with self.assertRaises(GeminiError):
            await GeminiClient(post).generate(["k"], "gemini-flash-latest", "sys", USER)


class KeyboardAndPromptTests(unittest.TestCase):
    def test_ai_mode_keyboard_is_the_menu_and_the_instant_switch_to_manual(self):
        self.assertEqual([[item.key for item in row] for row in reply_menu.AI_LAYOUT], [["menu", "to_manual"]])

    def test_taps_on_the_old_ai_keyboard_still_work(self):
        for label in ("⚙️ AI Settings", "🔑 AI Keys", "🧹 Nayi Baat", "🖐 Manual Mode"):
            self.assertIsNotNone(reply_menu.lookup(label), label)

    def test_an_unedited_old_default_follows_the_new_default(self):
        self.assertEqual(AISettings.from_dict({"prompt": LEGACY_PROMPTS[0]}).prompt, DEFAULT_PROMPT)
        self.assertEqual(AISettings.from_dict({}).prompt, DEFAULT_PROMPT)
        self.assertEqual(AISettings.from_dict({"prompt": "Mera apna prompt hai, bilkul alag."}).prompt, "Mera apna prompt hai, bilkul alag.")


if __name__ == "__main__":
    unittest.main()
