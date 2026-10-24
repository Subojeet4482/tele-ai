"""Switching bots on (links, force-join), pictures, chat control, seen-before-reply, the person-like AI chat prompt,
duplicate approvals and the full phone number."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from tests.harness import OWNER, texts
from tests.test_ai import AITestCase, call, say, _token
from tests.test_ai_chats import PEER, ChatCase

import telegram_manager.core.control as control
from telegram_manager.core.joinparse import looks_like_force_join, message_links, parse_bot_ref


# --- pure helpers ----------------------------------------------------------------------------------------
class ParseTests(unittest.TestCase):
    def test_bot_references(self):
        self.assertEqual(parse_bot_ref("@MyBot"), ("MyBot", ""))
        self.assertEqual(parse_bot_ref("https://t.me/Some_bot?start=ref-123"), ("Some_bot", "ref-123"))
        self.assertEqual(parse_bot_ref("t.me/Some_bot?start=abc"), ("Some_bot", "abc"))
        self.assertEqual(parse_bot_ref("tg://resolve?domain=xbotx&start=p1"), ("xbotx", "p1"))
        self.assertEqual(parse_bot_ref("@abcde_bot /start code9"), ("abcde_bot", "code9"))

    def test_bad_references(self):
        for bad in ("", "hi", "@ab"):
            with self.assertRaises(ValueError):
                parse_bot_ref(bad)
        with self.assertRaises(ValueError):
            parse_bot_ref("@abcde_bot /start bad payload!")

    def test_force_join_detection(self):
        self.assertTrue(looks_like_force_join(["Please join our channel first"], []))
        self.assertTrue(looks_like_force_join(["x"], ["✅ I joined"]))
        self.assertFalse(looks_like_force_join(["Welcome! Pick an option"], ["Menu", "Help"]))

    def test_links_hidden_behind_words_and_buttons(self):
        message = SimpleNamespace(
            buttons=[[SimpleNamespace(text="Channel 1", url="https://t.me/chan_one"), SimpleNamespace(text="Verify", url=None)]],
            entities=[SimpleNamespace(url="https://t.me/+AbCdEfGh12345"), SimpleNamespace(offset=0)])
        self.assertEqual(message_links(message), ["https://t.me/chan_one", "https://t.me/+AbCdEfGh12345"])


# --- a fake bot on the other side ---------------------------------------------------------------------------
def msg(i, text, buttons=(), out=False):
    return SimpleNamespace(id=i, message=text, out=out, entities=[],
                           buttons=[[SimpleNamespace(text=b, url=None) for b in buttons]] if buttons else None)


class BotClient:
    """A Telethon client whose chat partner is a scripted bot. `reply(client, event)` plays the bot."""

    def __init__(self, reply, blocked=False):
        self.reply, self.blocked = reply, blocked
        self.messages: list = []  # newest first, like get_messages
        self.sent: list[str] = []
        self.requests: list = []
        self.files: list = []
        self.acks: list = []
        self.folders: list = []

    def is_connected(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=1)

    def add(self, message):
        self.messages.insert(0, message)

    async def get_messages(self, entity, limit=1):
        return self.messages[:limit]

    async def send_message(self, entity, text, parse_mode=None, **kw):
        if self.blocked:
            raise type("YouBlockedUserError", (Exception,), {})()
        self.sent.append(text)
        self.reply(self, ("send", text))
        return SimpleNamespace(id=900 + len(self.sent))

    async def __call__(self, request):
        name = type(request).__name__
        if name == "UnblockRequest":
            self.blocked = False
        self.requests.append(request)
        if name == "StartBotRequest":
            self.reply(self, ("start", request.kwargs.get("start_param", "")))
        return SimpleNamespace()

    async def send_file(self, entity, file, caption=None, force_document=False):
        self.files.append((file.read(), getattr(file, "name", None), caption, force_document))
        return SimpleNamespace(id=777)

    async def send_read_acknowledge(self, entity):
        self.acks.append(entity)

    async def edit_folder(self, entity, folder):
        self.folders.append(folder)

    async def get_input_entity(self, entity):
        return SimpleNamespace(peer=entity)


class FlowCase(AITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.h.manager._resolve_any_entity = self._resolve
        self.entity = SimpleNamespace(id=7, bot=True, first_name="Bot")
        for name in ("BOT_START_SETTLE", "AUTOCLICK_POLL"):
            old = getattr(control, name)
            setattr(control, name, 0)
            self.addCleanup(setattr, control, name, old)
        for name in ("BOT_START_WAIT", "BOT_START_AFTER_CLICK_WAIT"):
            old = getattr(control, name)
            setattr(control, name, 0.05)  # a bot that stays silent is given up on at once
            self.addCleanup(setattr, control, name, old)

    async def _resolve(self, client, target):
        return self.entity

    def use(self, client) -> BotClient:
        self.h.manager.clients[self.a["id"]] = client
        return client


class StartBotTests(FlowCase):
    async def test_a_plain_bot_is_started_and_its_answer_returned(self):
        client = self.use(BotClient(lambda c, e: c.add(msg(11, "Welcome! Use the menu below."))))
        report = await self.h.manager.start_bot_flow(self.a["id"], "@some_bot", OWNER)
        self.assertEqual(client.sent, ["/start"])
        self.assertEqual(report["reply"], "Welcome! Use the menu below.")
        self.assertFalse(report["still_asks_to_join"])
        self.assertEqual(report["steps"], ["/start bheja"])

    async def test_a_link_payload_starts_the_bot_with_it(self):
        client = self.use(BotClient(lambda c, e: c.add(msg(11, "Referral accepted"))))
        report = await self.h.manager.start_bot_flow(self.a["id"], "https://t.me/some_bot?start=ref-42", OWNER)
        (request,) = client.requests
        self.assertEqual((type(request).__name__, request.kwargs["start_param"]), ("StartBotRequest", "ref-42"))
        self.assertIn("payload ref-42", report["steps"][0])

    async def test_a_blocked_bot_is_unblocked_first(self):
        client = self.use(BotClient(lambda c, e: c.add(msg(11, "Hi again")), blocked=True))
        report = await self.h.manager.start_bot_flow(self.a["id"], "@some_bot", OWNER)
        self.assertEqual([type(r).__name__ for r in client.requests], ["UnblockRequest"])
        self.assertEqual(client.sent, ["/start"])
        self.assertIn("unblock", report["steps"][0])

    async def test_only_bots_can_be_started(self):
        self.use(BotClient(lambda c, e: None))
        self.entity = SimpleNamespace(id=8, first_name="Rahul")
        with self.assertRaises(ValueError) as caught:
            await self.h.manager.start_bot_flow(self.a["id"], "@rahul_x", OWNER)
        self.assertIn("bot nahi", str(caught.exception))

    async def test_a_silent_bot_is_reported(self):
        self.use(BotClient(lambda c, e: None))
        report = await self.h.manager.start_bot_flow(self.a["id"], "@some_bot", OWNER)
        self.assertIn("jawab nahi diya", report["steps"][-1])

    async def test_force_join_joins_every_channel_and_presses_verify(self):
        h = self.h
        joined, pressed = [], []

        def bot(client, event):
            if event[0] == "send" and len(client.sent) == 1:
                client.add(msg(11, "Pehle join karo: @newschan1 aur https://t.me/+AbCdEfGh12345", buttons=["✅ Verify"]))

        client = self.use(BotClient(bot))

        async def join(account_id, link, owner_id=None):
            joined.append(link)
            return "joined"

        async def click(c, entity, names, after_id=0):
            pressed.append(names)
            client.add(msg(12, "✅ Verified, welcome!"))
            return ["pressed"]

        h.manager.join_with_status, h.manager.click_buttons = join, click
        report = await h.manager.start_bot_flow(self.a["id"], "@some_bot", OWNER)
        self.assertEqual(sorted(joined), ["@newschan1", "https://t.me/+AbCdEfGh12345"])
        self.assertEqual(pressed, [["✅ Verify"]])
        self.assertEqual(report["reply"], "✅ Verified, welcome!")
        self.assertFalse(report["still_asks_to_join"])
        self.assertTrue(any("Verify" in step for step in report["steps"]))

    async def test_without_a_verify_button_start_is_sent_again(self):
        h = self.h

        def bot(client, event):
            if len(client.sent) == 1:
                client.add(msg(11, "You must join @needchan first"))
            else:
                client.add(msg(12, "Thanks! You are in."))

        client = self.use(BotClient(bot))

        async def join(account_id, link, owner_id=None):
            return "joined"

        h.manager.join_with_status = join
        report = await h.manager.start_bot_flow(self.a["id"], "@some_bot", OWNER)
        self.assertEqual(client.sent, ["/start", "/start"])
        self.assertEqual(report["reply"], "Thanks! You are in.")

    async def test_join_false_never_joins_anything(self):
        h = self.h
        client = self.use(BotClient(lambda c, e: c.add(msg(11, "Join our channel @needchan first", buttons=["✅ Verify"]))))

        async def join(*args, **kwargs):
            raise AssertionError("must not join")

        h.manager.join_with_status = join
        report = await h.manager.start_bot_flow(self.a["id"], "@some_bot", OWNER, join=False)
        self.assertEqual(client.sent, ["/start"])
        self.assertIn("Join our channel", report["reply"])

    async def test_a_bot_that_keeps_asking_is_given_up_on(self):
        h = self.h
        counter = []

        def bot(client, event):
            counter.append(1)
            client.add(msg(10 + len(counter), "Join @needchan to continue"))

        self.use(BotClient(bot))

        async def join(account_id, link, owner_id=None):
            return "request sent (admin must approve)"

        h.manager.join_with_status = join
        report = await h.manager.start_bot_flow(self.a["id"], "@some_bot", OWNER)
        self.assertTrue(report["still_asks_to_join"])
        self.assertEqual(sum("needchan" in s for s in report["steps"]), 1)  # joined once, not again and again


class AIStartBotTests(FlowCase):
    async def test_start_bot_asks_then_runs_with_the_link_payload(self):
        h = self.h
        runs = []

        async def flow(account_id, ref, owner_id=None, payload=None, join=True):
            runs.append((account_id, ref, payload, join))
            return {"steps": ["/start bheja (payload ref1)", "@chan: joined", "'Verify' dabaya"], "reply": "Welcome!", "still_asks_to_join": False}

        h.manager.start_bot_flow = flow
        self.gemini(call("start_bot", accounts=["Rahul"], bot="https://t.me/Some_bot?start=ref1"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "is link se bot on karo https://t.me/Some_bot?start=ref1")
        self.assertEqual(runs, [])
        self.assertIn("Start @Some_bot", update.effective_message.replies[-1][0])
        done = await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(runs, [(self.a["id"], "Some_bot", "ref1", True)])
        self.assertIn("Welcome!", texts(done))
        self.assertIn("@chan: joined", texts(done))


# --- pictures --------------------------------------------------------------------------------------------------
class PictureTests(FlowCase):
    async def test_send_photo_on_account(self):
        client = self.use(BotClient(lambda c, e: None))
        await self.h.manager.send_photo_on_account(self.a["id"], "@rahulk1", b"\xff\xd8jpeg", "Dekho", OWNER)
        self.assertEqual(client.files, [(b"\xff\xd8jpeg", "photo.jpg", "Dekho", False)])
        self.assertEqual(self.h.manager.sent_count(OWNER, 5), 1)  # /unsend can take it back

    async def test_a_picture_sent_to_the_bot_is_kept_and_asks_what_to_do(self):
        h = self.h
        update = await h.send_photo(OWNER, b"JPEGDATA")
        self.assertIn("Photo mil gayi", texts(update))
        self.assertEqual(h.ctx(OWNER).user_data["ai_media"]["data"], b"JPEGDATA")

    async def test_set_profile_photo_from_the_uploaded_picture(self):
        h = self.h
        changes = []

        async def set_photo(account_id, owner_id, data):
            changes.append((account_id, owner_id, data))

        h.manager.set_profile_photo = set_photo
        await h.send_photo(OWNER, b"JPEGDATA")
        fake = self.gemini(call("set_profile_photo", accounts=["Rahul"]), say("Ijazat chahiye."))
        update = await h.say(OWNER, "Rahul ka profile photo ye lagao")
        self.assertIn("system", fake.requests[0])
        self.assertIn("UPLOADED FILE", fake.requests[0]["system"])
        self.assertEqual(changes, [])
        done = await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(changes, [(self.a["id"], OWNER, b"JPEGDATA")])
        self.assertIn("Profile photo changed", texts(done))

    async def test_no_picture_no_tool(self):
        h = self.h
        fake = self.gemini(call("set_profile_photo", accounts=["Rahul"]), say("Pehle photo bhejo."))
        await h.say(OWNER, "profile photo lagao")
        answer = json.dumps(fake.requests[1]["contents"][-1])
        self.assertIn("nahi mili", answer)

    async def test_send_photo_to_a_person(self):
        h = self.h
        sent = []

        async def send(account_id, target, data, caption="", owner_id=None, filename="file", kind="document"):
            sent.append((account_id, target, data, caption, kind))

        h.manager.send_media_on_account = send
        await h.send_photo(OWNER, b"IMG")
        self.gemini(call("send_photo", accounts=["Amit"], target="@rahulk1", caption="Ye dekho"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "Amit se @rahulk1 ko ye photo bhejo")
        self.assertIn("Send the uploaded photo", update.effective_message.replies[-1][0])
        await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(sent, [(self.b["id"], "@rahulk1", b"IMG", "Ye dekho", "photo")])


# --- chats: seen, mute, archive --------------------------------------------------------------------------------------
class ChatControlTests(FlowCase):
    async def test_actions_on_the_account(self):
        client = self.use(BotClient(lambda c, e: None))
        manager = self.h.manager
        self.assertIn("seen", await manager.chat_action(self.a["id"], "@rahulk1", "read", OWNER))
        await manager.chat_action(self.a["id"], "@rahulk1", "archive", OWNER)
        await manager.chat_action(self.a["id"], "@rahulk1", "unarchive", OWNER)
        await manager.chat_action(self.a["id"], "@rahulk1", "mute", OWNER)
        await manager.chat_action(self.a["id"], "@rahulk1", "unmute", OWNER)
        await manager.chat_action(self.a["id"], "@rahulk1", "unread", OWNER)
        self.assertEqual((len(client.acks), client.folders), (1, [1, 0]))
        self.assertEqual([type(r).__name__ for r in client.requests],
                         ["UpdateNotifySettingsRequest", "UpdateNotifySettingsRequest", "MarkDialogUnreadRequest"])
        mute, unmute, _ = client.requests
        self.assertEqual((mute.kwargs["settings"].kwargs["mute_until"] > 0, unmute.kwargs["settings"].kwargs["mute_until"]), (True, 0))
        with self.assertRaises(ValueError):
            await manager.chat_action(self.a["id"], "@rahulk1", "explode", OWNER)

    async def test_seen_karo_runs_at_once_without_asking(self):
        h = self.h
        done = []

        async def action(account_id, target, what, owner_id=None):
            done.append((account_id, target, what))
            return "ok"

        h.manager.chat_action = action
        self.gemini(call("chat_control", accounts=["Rahul"], target="@rahulk1", action="seen"), say("Seen kar diya."))
        update = await h.say(OWNER, "message karna sa phala seen v karo")
        self.assertEqual(done, [(self.a["id"], "@rahulk1", "read")])  # no approval card, nothing was sent
        self.assertIn("Seen kar diya", texts(update))


class SearchAndInfoTests(FlowCase):
    async def test_search_public(self):
        class Client(BotClient):
            async def __call__(self, request):
                chat = type("Channel", (), {})()
                chat.id, chat.title, chat.username, chat.broadcast, chat.megagroup, chat.creator = 5, "Python India", "pyindia", False, True, False
                bot = SimpleNamespace(id=9, first_name="Py", last_name=None, username="py_bot", bot=True)
                return SimpleNamespace(chats=[chat], users=[bot])

        self.use(Client(lambda c, e: None))
        rows = await self.h.manager.search_public(self.a["id"], "python", OWNER)
        self.assertEqual([(r["kind"], r["username"]) for r in rows], [("supergroup", "pyindia"), ("bot", "py_bot")])

    async def test_full_phone_is_shown_to_the_admin_but_never_to_the_model(self):
        h = self.h

        async def info(account_id, owner_id=None):
            return {"id": 123456, "first_name": "Aarav", "last_name": "Sharma", "username": "aarav_x", "phone": "+919876543218",
                    "premium": True, "about": "Hello"}

        h.manager.account_info = info
        fake = self.gemini(call("account_info", accounts=["Rahul"]), say("Number upar hai."))
        update = await h.say(OWNER, "pura number batao")
        shown = " ".join(r[0] for r in update.effective_message.replies)
        self.assertIn("+919876543218", shown)
        self.assertIn("Premium: haan", shown)
        for request in fake.requests:
            self.assertNotIn("9876543218", json.dumps(request["contents"]))  # the model only ever sees the masked one
        self.assertIn("masked", json.dumps(fake.requests[1]["contents"][-1]))


# --- the AI's private chats ---------------------------------------------------------------------------------------------
class PrivateChatTests(ChatCase):
    async def test_the_person_is_marked_seen_before_the_answer_goes_out(self):
        order = []
        client = self.h.manager.clients[self.a["id"]]
        send = client.send_message

        async def ack(entity):
            order.append("seen")

        async def logged_send(entity, text, parse_mode=None, **kw):
            order.append("sent")
            return await send(entity, text, parse_mode=parse_mode, **kw)

        client.send_read_acknowledge, client.send_message = ack, logged_send
        await self.start(autonomy="auto")
        self.gemini(say("Haan bolo."))
        await self.incoming("Hello")
        self.assertEqual(order[-2:], ["seen", "sent"])

    async def test_the_chat_prompt_is_a_person_not_the_panel_manager(self):
        await self.start(autonomy="auto")
        fake = self.gemini(say("ok"))
        await self.incoming("Tum kaun ho?")
        system = fake.requests[0]["system"]
        self.assertNotIn("personal AI manager", system)  # the panel prompt stays out of private chats
        self.assertNotIn("account_info", system)
        self.assertIn("aam insaan", system)
        self.assertIn("kabhi mat batao", system)  # never reveals who owns it
        self.assertIn("jhooth mat bolo", system)  # but never denies being an AI when asked sincerely
        self.assertIn("Rahul", system)  # the account's own name

    async def test_a_chat_that_is_already_running_is_not_started_again(self):
        h = self.h
        await self.start(autonomy="auto")
        fake = self.gemini(call("start_chat", account="Rahul", target="@rahulk1", goal="phir se chat karo"), say("Wo pehle se chal rahi hai."))
        update = await h.say(OWNER, "chat shuru karo")
        self.assertEqual(len(await h.deps.chats.sessions(OWNER)), 1)
        self.assertIn("already", json.dumps(fake.requests[1]["contents"][-1]))
        self.assertNotIn("💬 AI chat with", " ".join(r[0] for r in update.effective_message.replies))


# --- the same approval is never shown twice ----------------------------------------------------------------------------------
class DuplicateApprovalTests(AITestCase):
    async def test_the_same_request_twice_shows_one_card(self):
        h = self.h
        self.gemini(call("join_chat", accounts=["all"], link="t.me/+abcDEF123"), say("Ijazat chahiye."),
                    call("join_chat", accounts=["all"], link="t.me/+abcDEF123"), say("Wahi card upar hai."))
        first = await h.say(OWNER, "sab ko is group mein jodo")
        second = await h.say(OWNER, "aur is group mein bhi jodo")
        cards = [r for r in first.effective_message.replies + second.effective_message.replies if r[1] is not None]
        self.assertEqual(len(cards), 1)
        self.assertEqual(len(h.ctx(OWNER).user_data["ai_pending"]), 1)
        self.assertIn("Wahi card", texts(second))

    async def test_a_different_request_still_gets_its_own_card(self):
        h = self.h
        self.gemini(call("join_chat", accounts=["all"], link="t.me/+abcDEF123"), say("Ijazat."),
                    call("join_chat", accounts=["all"], link="t.me/+zzzYYY999"), say("Ijazat."))
        await h.say(OWNER, "ek group")
        await h.say(OWNER, "doosra group")
        self.assertEqual(len(h.ctx(OWNER).user_data["ai_pending"]), 2)


if __name__ == "__main__":
    unittest.main()
