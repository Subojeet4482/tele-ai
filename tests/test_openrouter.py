"""OpenRouter as a second AI provider: the client, the key rotation, the settings and the switch in /aisettings."""
from __future__ import annotations

import json
import unittest

from tests.harness import OWNER, texts
from tests.test_ai import AITestCase, FakeGemini, call, say
from tests.test_ai_mode import Base, data_of, key

from telegram_manager.ai.config import DEFAULT_OR_MODEL, AISettings
from telegram_manager.ai.gemini import GeminiError, NoKeysError
from telegram_manager.ai.openrouter import OpenRouterClient, to_content, to_messages, to_tools

OR1 = "sk-or-v1-" + "a" * 40
OR2 = "sk-or-v1-" + "b" * 40
GOOD = {"choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}]}
USER = [{"role": "user", "parts": [{"text": "hi"}]}]


def client(*replies):
    calls = []
    queue = list(replies)

    async def post(url, headers, body):
        calls.append((url, headers["Authorization"], body))
        return queue.pop(0)

    return OpenRouterClient(post=post), calls


class ConversionTests(unittest.TestCase):
    def test_a_tool_round_trip_becomes_openai_messages(self):
        contents = [
            {"role": "user", "parts": [{"text": "list"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "list_accounts", "args": {"status": "all"}, "id": "c1"}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "list_accounts", "response": {"total": 2}}}]},
            {"role": "model", "parts": [{"text": "Do hain."}]},
        ]
        m = to_messages("SYS", contents)
        self.assertEqual([x["role"] for x in m], ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(m[2]["tool_calls"][0]["function"], {"name": "list_accounts", "arguments": json.dumps({"status": "all"})})
        self.assertEqual(m[3]["tool_call_id"], m[2]["tool_calls"][0]["id"])  # the answer is tied to its call
        self.assertEqual(json.loads(m[3]["content"]), {"total": 2})

    def test_two_calls_of_the_same_tool_are_matched_in_order(self):
        contents = [
            {"role": "model", "parts": [{"functionCall": {"name": "t", "args": {"n": 1}}}, {"functionCall": {"name": "t", "args": {"n": 2}}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "t", "response": {"r": 1}}}, {"functionResponse": {"name": "t", "response": {"r": 2}}}]},
        ]
        m = to_messages("S", contents)
        ids = [c["id"] for c in m[1]["tool_calls"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual([x["tool_call_id"] for x in m[2:]], ids)

    def test_thought_parts_are_not_sent_back_as_text(self):
        m = to_messages("S", [{"role": "model", "parts": [{"text": "secret thinking", "thought": True}, {"text": "answer"}]}])
        self.assertEqual(m[1]["content"], "answer")

    def test_tools_become_function_tools(self):
        tools = to_tools([{"name": "a", "description": "d", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}}])
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["function"]["parameters"]["properties"]["x"]["type"], "string")

    def test_an_answer_with_a_tool_call_becomes_gemini_parts_and_keeps_the_id(self):
        content = to_content({"choices": [{"message": {"content": "ok", "tool_calls": [
            {"id": "abc", "type": "function", "function": {"name": "send_message", "arguments": "{\"text\": \"hi\"}"}}]}}]})
        self.assertEqual(content["parts"][0], {"text": "ok"})
        self.assertEqual(content["parts"][1]["functionCall"], {"name": "send_message", "args": {"text": "hi"}, "id": "abc"})

    def test_broken_arguments_do_not_crash(self):
        content = to_content({"choices": [{"message": {"tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "{oops"}}]}}]})
        self.assertEqual(content["parts"][0]["functionCall"]["args"], {})

    def test_an_empty_answer_is_an_error(self):
        with self.assertRaises(GeminiError):
            to_content({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_request_carries_the_key_the_model_and_low_reasoning(self):
        c, calls = client((200, GOOD))
        reply = await c.generate([OR1], "openrouter/free", "sys", USER, [{"name": "t", "description": "d", "parameters": {"type": "object"}}])
        self.assertEqual(reply["parts"], [{"text": "hello"}])
        url, auth, body = calls[0]
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(auth, f"Bearer {OR1}")
        self.assertEqual((body["model"], body["reasoning"]), ("openrouter/free", {"effort": "low"}))
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["tools"][0]["function"]["name"], "t")

    async def test_a_limited_key_rests_and_the_next_key_answers(self):
        c, calls = client((429, {"error": {"message": "rate"}}), (200, GOOD))
        await c.generate([OR1, OR2], "m/x", "s", USER)
        first, second = calls[0][1], calls[1][1]
        self.assertNotEqual(first, second)
        self.assertEqual(c.status(first.removeprefix("Bearer ")), "resting")
        c.forget(first.removeprefix("Bearer "))
        self.assertEqual(c.status(first.removeprefix("Bearer ")), "ok")

    async def test_rejected_and_unpaid_keys_are_set_aside(self):
        c, _ = client((401, {"error": {"message": "bad"}}), (402, {"error": {"message": "credits"}}))
        with self.assertRaises(GeminiError) as ctx:
            await c.generate([OR1, OR2], "m/x", "s", USER)
        self.assertEqual({c.status(OR1), c.status(OR2)}, {"rejected"})
        self.assertIn("no credit", str(ctx.exception))
        with self.assertRaises(GeminiError) as again:
            await c.generate([OR1, OR2], "m/x", "s", USER)
        self.assertIn("resting or rejected", str(again.exception))

    async def test_a_model_without_tool_support_gets_a_helpful_message(self):
        c, _ = client((404, {"error": {"message": "No endpoints found that support tool use"}}))
        with self.assertRaises(GeminiError) as ctx:
            await c.generate([OR1], "some/model:free", "s", USER)
        self.assertIn("support tool use", str(ctx.exception))
        self.assertIn("openrouter/free", str(ctx.exception))

    async def test_a_model_that_refuses_reasoning_is_asked_again_without_it_and_remembered(self):
        refusal = (400, {"error": {"message": "Reasoning is not supported for this model"}})
        c, calls = client(refusal, (200, GOOD), (200, GOOD))
        await c.generate([OR1], "m/x", "s", USER)
        self.assertIn("reasoning", calls[0][2])
        self.assertNotIn("reasoning", calls[1][2])
        await c.generate([OR1], "m/x", "s", USER)
        self.assertNotIn("reasoning", calls[2][2])

    async def test_other_400_errors_are_reported(self):
        c, _ = client((400, {"error": {"message": "messages: invalid"}}))
        with self.assertRaises(GeminiError) as ctx:
            await c.generate([OR1], "m/x", "s", USER)
        self.assertIn("messages: invalid", str(ctx.exception))

    async def test_a_failure_hidden_in_a_200_is_treated_as_a_provider_error(self):
        c, _ = client((200, {"error": {"code": 502, "message": "upstream"}}), (200, GOOD))
        reply = await c.generate([OR1, OR2], "m/x", "s", USER)  # the second key gets its turn
        self.assertEqual(reply["parts"][0]["text"], "hello")

    async def test_no_keys(self):
        c, _ = client()
        with self.assertRaises(NoKeysError):
            await c.generate([], "m/x", "s", USER)

    async def test_the_key_test_asks_openrouter_about_the_key_and_spends_nothing(self):
        seen = []

        async def get(url, headers):
            seen.append(url)
            return (200, {"data": {}}) if headers["Authorization"].endswith(OR1) else (401, {"error": {"message": "nope"}})

        c = OpenRouterClient(post=None, get=get)
        self.assertIsNone(await c.check_key(OR1, "openrouter/free"))
        self.assertIn("reject", await c.check_key(OR2, "openrouter/free"))
        self.assertTrue(seen[0].endswith("/key"))


class SettingsDefaultTests(unittest.TestCase):
    def test_a_new_admin_starts_on_openrouter_with_the_free_router(self):
        s = AISettings.from_dict({})
        self.assertEqual((s.provider, s.active_model, s.keys_field), ("openrouter", DEFAULT_OR_MODEL, "or_keys_enc"))
        self.assertEqual(DEFAULT_OR_MODEL, "openrouter/free")

    def test_an_admin_with_gemini_keys_stays_on_google(self):
        s = AISettings.from_dict({"keys_enc": ["x"], "model": "gemini-flash-latest"})
        self.assertEqual((s.provider, s.active_model, s.keys_field), ("google", "gemini-flash-latest", "keys_enc"))

    def test_each_provider_keeps_its_own_model_and_keys(self):
        s = AISettings.from_dict({"provider": "openrouter", "or_model": "qwen/qwen3-next-80b-a3b-instruct:free", "model": "gemini-flash-lite-latest", "keys_enc": ["g"], "or_keys_enc": ["o"]})
        self.assertEqual((s.active_model, s.active_keys_enc), ("qwen/qwen3-next-80b-a3b-instruct:free", ("o",)))
        g = AISettings.from_dict({"provider": "google", "model": "gemini-flash-lite-latest", "keys_enc": ["g"], "or_keys_enc": ["o"]})
        self.assertEqual((g.active_model, g.active_keys_enc), ("gemini-flash-lite-latest", ("g",)))


class OpenRouterFlowTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # an admin on OpenRouter with one key (the Gemini key from the base setup stays saved too)
        await self.h.store.settings.set_ai(OWNER, provider="openrouter", or_keys_enc=[self.h.manager.secret_box.encrypt(OR1)])

    async def test_the_turn_goes_through_openrouter_with_its_key_and_model(self):
        fake = FakeGemini(call("list_accounts"), say("Do accounts hain."))
        self.h.deps.openrouter = fake
        self.h.deps.gemini = FakeGemini()  # must stay unused
        u = await self.h.say(OWNER, "accounts kitne hain")
        self.assertIn("Do accounts hain.", u.effective_message.replies[-1][0])
        self.assertEqual((fake.requests[0]["keys"], fake.requests[0]["model"]), ([OR1], "openrouter/free"))
        self.assertEqual(self.h.deps.gemini.requests, [])

    async def test_the_settings_screen_shows_the_provider_and_its_keys(self):
        shown = texts(await self.h.command(OWNER, "/aisettings"))
        for part in ("OpenRouter", "openrouter/free", "OpenRouter keys: 1"):
            self.assertIn(part, shown)

    async def test_switching_provider_keeps_both_sets_of_keys_and_models(self):
        u = await self.h.tap(OWNER, "ai|provider")
        self.assertIn("ai|provider_set|1", data_of(u.last()[1]))
        await self.h.tap(OWNER, "ai|provider_set|1")  # Google Gemini
        s = await self.ai()
        self.assertEqual((s.provider, s.active_model), ("google", "gemini-flash-latest"))
        self.assertEqual(len(s.or_keys_enc), 1)
        self.assertEqual(len(s.keys_enc), 1)
        await self.h.tap(OWNER, "ai|provider_set|0")
        self.assertEqual((await self.ai()).provider, "openrouter")

    async def test_model_buttons_and_any_custom_model_are_saved_for_openrouter_only(self):
        u = await self.h.tap(OWNER, "ai|model")
        self.assertIn("ai|model_set|1", data_of(u.last()[1]))
        await self.h.tap(OWNER, "ai|model_set|1")
        s = await self.ai()
        self.assertEqual(s.or_model, "nvidia/nemotron-3-super-120b-a12b:free")
        self.assertEqual(s.model, "gemini-flash-latest")  # Gemini's own model is untouched
        await self.h.tap(OWNER, "ai|model_custom")
        await self.h.say(OWNER, "anthropic/claude-sonnet-5")
        self.assertEqual((await self.ai()).or_model, "anthropic/claude-sonnet-5")
        await self.h.tap(OWNER, "ai|model_custom")
        u = await self.h.say(OWNER, "bad model name!")
        self.assertIn("❌", texts(u))

    async def test_pasting_openrouter_keys_in_the_chat_adds_them_to_openrouter(self):
        u = await self.h.say(OWNER, f"{OR2}\nsk-or-v1-{'c' * 40}")
        self.assertIn("OpenRouter: 2 nayi key", texts(u))
        s = await self.ai()
        self.assertEqual((len(s.or_keys_enc), len(s.keys_enc)), (3, 1))
        self.assertEqual(s.provider, "openrouter")

    async def test_a_gemini_key_pasted_while_openrouter_has_no_key_switches_to_google(self):
        await self.h.store.settings.set_ai(OWNER, or_keys_enc=[], keys_enc=[])
        await self.h.say(OWNER, key(5))
        s = await self.ai()
        self.assertEqual((s.provider, len(s.keys_enc)), ("google", 1))

    async def test_a_gemini_key_pasted_while_openrouter_works_does_not_switch(self):
        await self.h.say(OWNER, key(5))
        s = await self.ai()
        self.assertEqual((s.provider, len(s.keys_enc)), ("openrouter", 2))

    async def test_keys_added_with_the_button_sort_themselves_by_kind(self):
        await self.h.tap(OWNER, "ai|key_add")
        await self.h.say(OWNER, f"{OR2}, {key(7)}")
        s = await self.ai()
        self.assertEqual((len(s.or_keys_enc), len(s.keys_enc)), (2, 2))

    async def test_the_key_screen_and_test_use_openrouter(self):
        class Probe(FakeGemini):
            async def check_key(self, key, model):
                self.checked = (key, model)
                return None

        probe = Probe()
        self.h.deps.openrouter = probe
        shown = texts(await self.h.tap(OWNER, "ai|keys"))
        self.assertIn("OpenRouter keys: 1/30", shown)
        await self.h.tap(OWNER, "ai|key_test")
        self.assertEqual(probe.checked, (OR1, "openrouter/free"))

    async def test_deleting_a_key_removes_it_from_openrouter_not_from_google(self):
        await self.h.tap(OWNER, "ai|key_del|0")
        s = await self.ai()
        self.assertEqual((len(s.or_keys_enc), len(s.keys_enc)), (0, 1))

    async def test_the_ai_can_change_provider_and_model_through_the_tool(self):
        fake = FakeGemini(call("change_ai_settings", provider="google", model="gemini-flash-lite-latest"), say("ok"))
        self.h.deps.openrouter = fake
        u = await self.h.say(OWNER, "gemini par switch karo")
        await self.approve(u)
        s = await self.ai()
        self.assertEqual((s.provider, s.model), ("google", "gemini-flash-lite-latest"))
        self.assertEqual(s.or_model, "openrouter/free")


if __name__ == "__main__":
    unittest.main()
