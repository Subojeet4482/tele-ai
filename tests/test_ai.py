"""The AI manager: Gemini key rotation, tools, approvals, settings. Gemini itself is faked."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from cryptography.fernet import Fernet

from tests.harness import BOB, OWNER, STRANGER, Harness, texts

from telegram_manager.ai.config import DEFAULT_PROMPT, AISettings
from telegram_manager.ai.gemini import GeminiClient, GeminiError
from telegram_manager.crypto import SecretBox

KEY1 = "AIza" + "a" * 35
KEY2 = "AIza" + "b" * 35


def call(name, **args):
    return {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}, "thoughtSignature": "sig"}]}


def say(text):
    return {"role": "model", "parts": [{"text": text}]}


class FakeGemini:
    """Plays back scripted answers and remembers what it was asked."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.forgotten: list[str] = []

    async def generate(self, keys, model, system, contents, tools=None, temperature=0.4):
        self.requests.append({"keys": list(keys), "model": model, "system": system, "contents": [dict(c) for c in contents]})
        return self.script.pop(0)

    def forget(self, key):
        self.forgotten.append(key)

    async def check_key(self, key, model):
        return None if key == KEY1 else "rejected"


class GeminiClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *statuses):
        calls = []
        queue = list(statuses)

        async def post(url, headers, body):
            calls.append((url, headers["x-goog-api-key"], body))
            status = queue.pop(0)
            if status == 200:
                return 200, {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
            return status, {"error": {"message": "API key not valid" if status == 400 else "quota"}}

        return GeminiClient(post), calls

    async def test_keys_take_turns(self):
        client, calls = self.client(200, 200, 200)
        for _ in range(3):
            await client.generate(["k1", "k2"], "gemini-flash-latest", "s", [{"role": "user", "parts": [{"text": "x"}]}])
        self.assertEqual([c[1] for c in calls], ["k1", "k2", "k1"])
        self.assertIn("models/gemini-flash-latest:generateContent", calls[0][0])

    async def test_a_limited_key_rests_and_the_next_one_answers(self):
        client, calls = self.client(429, 200, 200)
        reply = await client.generate(["k1", "k2"], "m", "s", [])
        self.assertEqual(reply["parts"][0]["text"], "hi")
        await client.generate(["k1", "k2"], "m", "s", [])
        self.assertEqual([c[1] for c in calls], ["k1", "k2", "k2"])  # k1 is resting

    async def test_a_rejected_key_is_set_aside(self):
        client, calls = self.client(400, 200, 200)
        await client.generate(["bad", "good"], "m", "s", [])
        await client.generate(["bad", "good"], "m", "s", [])
        self.assertEqual([c[1] for c in calls], ["bad", "good", "good"])

    async def test_all_keys_failing_says_so_without_leaking_a_key(self):
        client, _ = self.client(429, 429)
        with self.assertRaises(GeminiError) as caught:
            await client.generate(["secret-key-1", "secret-key-2"], "m", "s", [])
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertIn("limit", str(caught.exception))

    async def test_other_errors_are_not_retried_on_every_key(self):
        client, calls = self.client(404)
        with self.assertRaises(GeminiError):
            await client.generate(["k1", "k2"], "nope", "s", [])
        self.assertEqual(len(calls), 1)


class AITestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.h.manager.secret_box = SecretBox(Fernet.generate_key().decode())
        self.h.record_telethon(self)
        self.a = await self.h.add_account(OWNER, "Rahul")
        self.b = await self.h.add_account(OWNER, "Amit")
        await self.h.store.settings.set_ai(OWNER, keys_enc=[self.h.manager.secret_box.encrypt(KEY1)])

    def gemini(self, *script):
        self.fake = FakeGemini(*script)
        self.h.deps.gemini = self.fake
        return self.fake


class SettingsTests(AITestCase):
    async def test_defaults_call_the_admin_mandal_sir(self):
        s = AISettings.from_dict({})
        self.assertEqual(s.address, "Mandal Sir")
        self.assertIn("Mandal Sir", s.system_prompt())
        self.assertNotIn("{address}", s.system_prompt())

    async def test_settings_screen_shows_everything(self):
        update = await self.h.command(OWNER, "/aisettings")
        shown = texts(update)
        for part in ("Nova", "Mandal Sir", "gemini-flash-latest", "Confirm", "Gemini keys: 1", "Prompt"):
            self.assertIn(part, shown)

    async def test_change_name_address_prompt_and_model(self):
        h = self.h
        for button, answer in (("ai|name", "Jarvis"), ("ai|address", "Boss"), ("ai|model_custom", "gemini-3.1-flash-lite"),
                               ("ai|prompt", "Tum {name} ho aur {address} ki madad karte ho. Chhota jawab do.")):
            await h.tap(OWNER, button)
            await h.say(OWNER, answer)
        s = AISettings.from_dict(await h.store.settings.get_ai(OWNER))
        self.assertEqual((s.name, s.address, s.model), ("Jarvis", "Boss", "gemini-3.1-flash-lite"))
        self.assertEqual(s.system_prompt(), "Tum Jarvis ho aur Boss ki madad karte ho. Chhota jawab do.")
        await h.tap(OWNER, "ai|promptreset")
        self.assertEqual(AISettings.from_dict(await h.store.settings.get_ai(OWNER)).prompt, DEFAULT_PROMPT)

    async def test_bad_inputs_are_refused(self):
        h = self.h
        await h.tap(OWNER, "ai|prompt")
        update = await h.say(OWNER, "short")
        self.assertIn("20", texts(update))
        await h.tap(OWNER, "ai|model_custom")
        update = await h.say(OWNER, "bad model name!")
        self.assertIn("Model", texts(update))

    async def test_mode_and_switch(self):
        h = self.h
        await h.tap(OWNER, "ai|mode")
        self.assertEqual(AISettings.from_dict(await h.store.settings.get_ai(OWNER)).mode, "auto")
        await h.tap(OWNER, "ai|toggle")
        self.assertFalse(AISettings.from_dict(await h.store.settings.get_ai(OWNER)).enabled)

    async def test_preset_model_button(self):
        await self.h.tap(OWNER, "ai|model_set|1")
        self.assertEqual(AISettings.from_dict(await self.h.store.settings.get_ai(OWNER)).model, "gemini-flash-lite-latest")

    async def test_keys_add_many_encrypted_delete_message_remove_and_test(self):
        h = self.h
        self.gemini()
        await h.tap(OWNER, "ai|key_add")
        update = await h.say(OWNER, f"{KEY2}\n{KEY1}\n")
        self.assertTrue(update.effective_message.deleted)  # keys never stay in the chat
        keys = (await h.parts.ai.agent.load(OWNER))[1]
        self.assertEqual(keys, [KEY1, KEY2])  # the duplicate was not added twice
        stored = await h.store.settings.get_ai(OWNER)
        self.assertNotIn(KEY1, str(stored))
        tested = await h.tap(OWNER, "ai|key_test")
        self.assertIn("✅", texts(tested))
        self.assertIn("❌", texts(tested))
        await h.tap(OWNER, "ai|key_del|0")
        self.assertEqual((await h.parts.ai.agent.load(OWNER))[1], [KEY2])

    async def test_a_gemini_key_pasted_in_chat_is_saved_not_sent_to_the_ai(self):
        fake = self.gemini()
        update = await self.h.say(OWNER, f"ye lo key {KEY2}")
        self.assertTrue(update.effective_message.deleted)
        self.assertEqual(fake.requests, [])
        self.assertEqual((await self.h.parts.ai.agent.load(OWNER))[1], [KEY1, KEY2])

    async def test_api_id_and_hash_in_chat_are_saved_locally(self):
        fake = self.gemini()
        update = await self.h.say(OWNER, "api id 1234567 api hash 0123456789abcdef0123456789abcdef save rakho")
        self.assertTrue(update.effective_message.deleted)
        self.assertEqual(fake.requests, [])
        self.assertEqual(await self.h.manager.saved_api(OWNER), (1234567, "0123456789abcdef0123456789abcdef"))

    async def test_strangers_cannot_open_the_settings(self):
        update = await self.h.command(STRANGER, "/aisettings")
        self.assertIn("only for its admin", texts(update))


class ConversationTests(AITestCase):
    async def test_reads_accounts_with_a_tool_then_answers(self):
        fake = self.gemini(call("list_accounts"), say("Mandal Sir, 2 account hain: Rahul aur Amit."))
        update = await self.h.say(OWNER, "kitne account hain?")
        self.assertIn("Mandal Sir, 2 account", texts(update))
        second = fake.requests[1]["contents"]
        self.assertIn("functionResponse", second[-1]["parts"][0])
        self.assertEqual(second[-2]["parts"][0].get("thoughtSignature"), "sig")  # sent back as received
        names = [a["name"] for a in second[-1]["parts"][0]["functionResponse"]["response"]["accounts"]]
        self.assertEqual(names, ["Rahul", "Amit"])
        self.assertIn("Mandal Sir", fake.requests[0]["system"])
        self.assertIn("IST", fake.requests[0]["system"])
        self.assertEqual(fake.requests[0]["keys"], [KEY1])

    async def test_the_ai_remembers_the_last_talk(self):
        fake = self.gemini(say("Account 1 se karun?"), say("Ho jayega."))
        await self.h.say(OWNER, "hello bhejna hai")
        await self.h.say(OWNER, "haan wahi")
        history = fake.requests[1]["contents"]
        self.assertEqual([c["role"] for c in history], ["user", "model", "user"])
        await self.h.command(OWNER, "/aiclear")
        fake.script.append(say("ok"))
        await self.h.say(OWNER, "phir")
        self.assertEqual(len(fake.requests[2]["contents"]), 1)

    async def test_secrets_never_reach_gemini(self):
        fake = self.gemini(say("ok"))
        session = "1" + "Q" * 200
        await self.h.say(OWNER, f"is session ko dekho {session} aur hash 0123456789abcdef0123456789abcdef")
        sent = str(fake.requests[0]["contents"])
        self.assertNotIn(session, sent)
        self.assertNotIn("0123456789abcdef", sent)

    async def test_replying_to_a_message_gives_the_ai_that_message(self):
        fake = self.gemini(say("Samajh gaya."))
        update = SimpleNamespace()
        from tests.fakes import FakeUpdate

        update = FakeUpdate(OWNER, text="ai isko kya bolu")
        update.effective_message.reply_to_message = SimpleNamespace(text="📱 Account 1 — Rahul: tum kaun ho?", caption=None, message_id=5)
        await self.h._run(OWNER, update, self.h.parts.text.text_input)
        prompt = fake.requests[0]["contents"][0]["parts"][0]["text"]
        self.assertIn("tum kaun ho?", prompt)
        self.assertIn("isko kya bolu", prompt)
        self.assertNotIn("ai isko", prompt)  # the "ai" prefix is dropped

    async def test_without_a_key_the_old_hint_is_shown(self):
        await self.h.store.settings.set_ai(OWNER, keys_enc=[])
        fake = self.gemini()
        update = await self.h.say(OWNER, "kuch bhi")
        self.assertIn("Use /start", texts(update))
        self.assertIn("/aisettings", texts(update))
        self.assertEqual(fake.requests, [])

    async def test_a_gemini_failure_is_explained(self):
        class Boom(FakeGemini):
            async def generate(self, *a, **k):
                raise GeminiError("All keys are resting")

        self.h.deps.gemini = Boom()
        update = await self.h.say(OWNER, "hello")
        self.assertIn("resting", texts(update))

    async def test_a_runaway_tool_loop_stops(self):
        self.gemini(*[call("list_accounts")] * 10)
        update = await self.h.say(OWNER, "loop")
        self.assertIn("lamba", texts(update))


class ApprovalTests(AITestCase):
    async def test_sending_waits_for_the_buttons_and_then_goes_out_from_the_named_accounts(self):
        h = self.h
        self.gemini(call("send_message", accounts=["Rahul"], target="@mandal4482", text="hello"), say("Ijazat ka intezaar hai."))
        update = await h.say(OWNER, "Rahul se @mandal4482 ko hello bhejo")
        self.assertEqual([c for c in h.calls if c[0] == "send_any"], [])  # nothing sent yet
        ask = update.effective_message.replies[-1]
        self.assertIn("ijazat", ask[0])
        self.assertIn("Rahul", ask[0])
        token = _token(ask[1])
        await h.tap(OWNER, f"aia|ok|{token}")
        await h.finish_bulk(OWNER)
        self.assertEqual([(c[1], c[2], c[3]) for c in h.calls if c[0] == "send_any"], [(self.a["id"], "@mandal4482", "hello")])

    async def test_no_button_cancels_and_a_token_works_once(self):
        h = self.h
        self.gemini(call("send_message", accounts=["all"], target="@mandal4482", text="hi"), say("ok"))
        update = await h.say(OWNER, "sabse hi bhejo")
        token = _token(update.effective_message.replies[-1][1])
        done = await h.tap(OWNER, f"aia|no|{token}")
        self.assertIn("nahi kiya", texts(done))
        again = await h.tap(OWNER, f"aia|ok|{token}")
        self.assertIn("purani", texts(again))
        self.assertEqual([c for c in h.calls if c[0] == "send_any"], [])

    async def test_auto_mode_runs_straight_away(self):
        h = self.h
        await h.store.settings.set_ai(OWNER, mode="auto")
        self.gemini(call("send_message", accounts=["1", "2"], target="@mandal4482", text="hey"), say("Bhej raha hu."))
        await h.say(OWNER, "1 aur 2 se hey bhejo")
        await h.finish_bulk(OWNER)
        self.assertEqual(sorted(c[1] for c in h.calls if c[0] == "send_any"), sorted([self.a["id"], self.b["id"]]))

    async def test_unknown_account_or_empty_text_is_sent_back_to_the_model(self):
        fake = self.gemini(call("send_message", accounts=["Nobody"], target="@mandal4482", text="hi"),
                           call("send_message", accounts=["Rahul"], target="@mandal4482", text="  "), say("Kya bhejna hai?"))
        update = await self.h.say(OWNER, "bhejo")
        self.assertIn("Kya bhejna hai", texts(update))
        errors = [r["parts"][0]["functionResponse"]["response"]["error"] for r in (fake.requests[1]["contents"][-1], fake.requests[2]["contents"][-1])]
        self.assertIn("No account is called", errors[0])
        self.assertIn("empty", errors[1])
        self.assertEqual(self.h.calls, [])

    async def test_ai_can_only_use_the_admins_own_accounts(self):
        h = self.h
        await h.access.add(BOB, OWNER, "Bob")
        bob = await h.add_account(BOB, "BobAcc")
        await h.store.settings.set_ai(BOB, keys_enc=[h.manager.secret_box.encrypt(KEY1)], mode="auto")
        fake = self.gemini(call("send_message", accounts=["Rahul"], target="@mandal4482", text="hi"), say("nahi mila"))
        await h.say(BOB, "Rahul se bhejo")
        err = fake.requests[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["error"]
        self.assertIn("No account is called", err)
        self.assertEqual([c for c in h.calls if c[0] == "send_any"], [])

    async def test_block_and_delete_chat_need_approval(self):
        h = self.h
        self.gemini(call("block_user", accounts=["Rahul"], target="@spammer1"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "isko Rahul se block karo")
        self.assertEqual([c for c in h.calls if c[0] == "block"], [])
        await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual([(c[1], c[2]) for c in h.calls if c[0] == "block"], [(self.a["id"], "@spammer1")])

    async def test_unsend_through_the_ai(self):
        h = self.h
        h.manager._remember_sent(self.a["id"], "@userx", 5, OWNER)
        deleted = []

        async def get_entity(target):
            return target

        async def delete_messages(entity, ids, revoke=True):
            deleted.append((entity, list(ids)))

        client = h.manager.clients[self.a["id"]]
        client.get_entity, client.delete_messages = get_entity, delete_messages
        self.gemini(call("unsend_recent", minutes=30), say("Ijazat chahiye."))
        update = await h.say(OWNER, "pichhle 30 min ka sab delete karo")
        self.assertIn("Delete 1 message", update.effective_message.replies[-1][0])
        done = await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(deleted, [("@userx", [5])])
        self.assertIn("1 message", texts(done))


class BroadcastToolTests(AITestCase):
    async def asyncTearDown(self):
        for slot, _ in self.h.manager.daily_jobs_for(OWNER):
            await self.h.manager.stop_daily_job(OWNER, slot)

    async def test_create_in_india_time_with_random_shift_then_delete_with_buttons(self):
        h = self.h
        self.gemini(
            call("create_daily_broadcast", target="@mandal4482", text="Good morning", time="18:30", shift_min_seconds=10, shift_max_seconds=60),
            say("Ijazat chahiye."),
            call("delete_broadcast"), say("Kaunsa delete karu?"),
        )
        update = await h.say(OWNER, "roz shaam 6:30 ko Good morning bhejo, har din 10 se 60 sec late")
        ask = update.effective_message.replies[-1]
        self.assertIn("6:30 PM IST", ask[0])
        self.assertIn("random +10-60s", ask[0])
        done = await h.tap(OWNER, f"aia|ok|{_token(ask[1])}")
        self.assertIn("Daily broadcast #1 is set", texts(done))
        ((slot, job),) = h.manager.daily_jobs_for(OWNER)
        self.assertEqual((job.start_time.hour, job.start_time.minute, job.drift_seconds, job.drift_max_seconds), (18, 30, 10, 60))
        self.assertIsNone(job.account_ids)  # every account
        pick = await h.say(OWNER, "broadcast delete karo")
        buttons = [b.callback_data for row in pick.effective_message.replies[-1][1].inline_keyboard for b in row]
        self.assertEqual(buttons, [f"aib|del|{slot}"])
        gone = await h.tap(OWNER, buttons[0])
        self.assertIn("delete ho gaya", texts(gone))
        self.assertEqual(h.manager.daily_jobs_for(OWNER), [])

    async def test_bad_time_is_reported(self):
        fake = self.gemini(call("create_daily_broadcast", target="@mandal4482", text="x", time="shaam"), say("Time batao."))
        await self.h.say(OWNER, "roz bhejo")
        self.assertIn("error", fake.requests[1]["contents"][-1]["parts"][0]["functionResponse"]["response"])


class MiscToolTests(AITestCase):
    async def test_forwarding_switch_global_and_per_account(self):
        h = self.h
        self.gemini(call("set_forwarding", kind="user", enabled=True), call("set_forwarding", kind="bot", enabled=False, account="Rahul"), say("Ho gaya."))
        await h.say(OWNER, "user ke message on karo, Rahul pe bot off")
        self.assertTrue((await h.store.settings.get_forwarding(OWNER))["forward_user_messages"])

    async def test_read_chat_and_unread(self):
        h = self.h
        async def read_chat(owner, account_id, target, limit=10):
            return [{"from": "them", "text": "hello", "time": "now"}]

        async def unread(owner, account_id, limit=15):
            return [{"name": "Rahul K", "chat": "@rk", "unread": 2, "last": "yo"}]

        h.manager.read_chat, h.manager.unread_chats = read_chat, unread
        fake = self.gemini(call("read_chat", account="Rahul", chat="@userxx"), call("unread_chats"), say("Ek message aaya hai."))
        await h.say(OWNER, "dekho kya aaya")
        first = fake.requests[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]
        self.assertEqual(first["messages"][0]["text"], "hello")
        second = fake.requests[2]["contents"][-1]["parts"][0]["functionResponse"]["response"]
        self.assertEqual(second["accounts_with_unread"][0]["chats"][0]["unread"], 2)

    async def test_add_account_starts_the_login_flow_with_the_saved_api(self):
        h = self.h
        await h.manager.save_api(OWNER, 1234567, "0123456789abcdef0123456789abcdef")
        self.gemini(call("add_account"), say("Number bhejo."))
        update = await h.say(OWNER, "naya account jodo")
        self.assertIn("phone", texts(update).lower())

    async def test_whois_tool(self):
        h = self.h

        async def lookup(owner, target):
            return {"id": 9, "username": "mandal4482", "name": "Mandal", "is_bot": False, "kind": "User", "via": 1}

        h.manager.lookup_user = lookup
        fake = self.gemini(call("whois", target="@mandal4482"), say("ID 9 hai."))
        await h.say(OWNER, "@mandal4482 kaun hai")
        self.assertEqual(fake.requests[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["id"], 9)


def _token(markup) -> str:
    return markup.inline_keyboard[0][0].callback_data.split("|")[2]


if __name__ == "__main__":
    unittest.main()
