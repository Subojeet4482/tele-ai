"""Edit / reply / forward / pin / delete / search a message, group people and settings, contacts, account settings, files of any kind."""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from tests.harness import OWNER, texts
from tests.test_ai import AITestCase, call, say, _token
from tests.test_control import BotClient, msg

CHAT = SimpleNamespace(id=55, title="Team", username="teamchat")


def channel(supergroup=True):
    entity = type("Channel", (), {})()
    entity.id, entity.title, entity.username, entity.broadcast, entity.megagroup = 55, "Team", "teamchat", not supergroup, supergroup
    return entity


class Chat(BotClient):
    """A client that remembers everything an action asked it to do."""

    def __init__(self):
        super().__init__(lambda c, e: None)
        self.calls: list[tuple] = []
        self.history = [msg(3, "mera aakhri"), msg(2, "unka sawaal"), msg(1, "purana")]
        self.history[0].out = True

    async def get_messages(self, entity, limit=1, search=None):
        if search:
            found = [m for m in self.history if search.lower() in m.message.lower()]
            for m in found:
                m.date = datetime(2026, 10, 24, 10, 0)
            return found[:limit]
        return self.history[:limit]

    async def edit_message(self, entity, message_id, text, parse_mode=None):
        self.calls.append(("edit", message_id, text))

    async def send_message(self, entity, text, parse_mode=None, reply_to=None, **kw):
        self.calls.append(("send", text, reply_to))
        return SimpleNamespace(id=910)

    async def forward_messages(self, target, message_id, origin):
        self.calls.append(("forward", message_id, target.id, origin.id))

    async def pin_message(self, entity, message_id, notify=False):
        self.calls.append(("pin", message_id, notify))

    async def unpin_message(self, entity, message_id):
        self.calls.append(("unpin", message_id))

    async def delete_messages(self, entity, ids):
        self.calls.append(("delete", ids))

    async def kick_participant(self, entity, person):
        self.calls.append(("kick", person.id))

    async def edit_permissions(self, entity, user=None, **flags):
        self.calls.append(("permissions", getattr(user, "id", None), flags))

    async def edit_admin(self, entity, person, **kw):
        self.calls.append(("admin", person.id, kw))

    async def get_participants(self, entity, limit=10):
        return [SimpleNamespace(id=1, first_name="Asha", last_name=None, username="asha", bot=False),
                SimpleNamespace(id=2, first_name="Helper", last_name="Bot", username="helper_bot", bot=True)][:limit]

    async def upload_file(self, buffer):
        return ("uploaded", buffer.read())

    async def __call__(self, request):
        self.calls.append((type(request).__name__, dict(request.kwargs) if hasattr(request, "kwargs") else {}, request.args if hasattr(request, "args") else ()))
        name = type(request).__name__
        if name == "ExportChatInviteRequest":
            return SimpleNamespace(link="https://t.me/+NewLink123")
        if name == "GetContactsRequest":
            return SimpleNamespace(users=[SimpleNamespace(id=9, first_name="Mina", last_name="K", username="mina")])
        if name == "ImportContactsRequest":
            return SimpleNamespace(imported=[1])
        return SimpleNamespace()


class ActionCase(AITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = self.h.manager.clients[self.a["id"]] = Chat()
        self.entity = channel()

        async def resolve(client, target):
            if str(target).lstrip("@").startswith("user"):
                return SimpleNamespace(id=int(str(target).lstrip("@")[4:] or 7), bot=False)
            return self.entity

        self.h.manager._resolve_any_entity = resolve


class MessageTests(ActionCase):
    async def test_edit_reply_forward_pin_delete(self):
        m = self.h.manager
        a = self.a["id"]
        await m.edit_message(a, "@teamchat", "last", "naya text", OWNER)
        await m.reply_message(a, "@teamchat", None, "jawab", OWNER)  # defaults to THEIR newest message
        await m.forward_message(a, "@teamchat", 2, "@teamchat", OWNER)
        await m.pin_message(a, "@teamchat", "last", OWNER)
        await m.pin_message(a, "@teamchat", 3, OWNER, unpin=True)
        await m.delete_message(a, "@teamchat", "last", OWNER)
        self.assertEqual(self.client.calls, [
            ("edit", 3, "naya text"), ("send", "jawab", 2), ("forward", 2, 55, 55), ("pin", 3, False), ("unpin", 3), ("delete", [3])])
        self.assertEqual(m.sent_count(OWNER, 5), 1)  # the reply can be taken back with /unsend

    async def test_wrong_selectors_are_explained(self):
        with self.assertRaises(ValueError):
            await self.h.manager.edit_message(self.a["id"], "@teamchat", "banana", "x", OWNER)
        self.client.history = []
        with self.assertRaises(ValueError) as caught:
            await self.h.manager.delete_message(self.a["id"], "@teamchat", "last", OWNER)
        self.assertIn("No such message", str(caught.exception))
        with self.assertRaises(ValueError):
            await self.h.manager.edit_message(self.a["id"], "@teamchat", "last", "  ", OWNER)

    async def test_search(self):
        rows = await self.h.manager.search_messages(self.a["id"], "@teamchat", "sawaal", OWNER)
        self.assertEqual([(r["id"], r["mine"]) for r in rows], [(2, False)])

    async def test_the_ai_asks_before_editing_and_deleting_always_asks(self):
        h = self.h
        self.gemini(call("message_action", account="Rahul", target="@teamchat", action="edit", message="last", text="theek kiya"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "mera aakhri message edit karo: theek kiya")
        self.assertEqual(self.client.calls, [])
        self.assertIn("Edit last", update.effective_message.replies[-1][0])
        await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(self.client.calls, [("edit", 3, "theek kiya")])

    async def test_delete_asks_even_in_auto_mode(self):
        h = self.h
        await h.store.settings.set_ai(OWNER, mode="auto")
        self.gemini(call("message_action", account="Rahul", target="@teamchat", action="delete"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "aakhri message delete karo")
        self.assertEqual(self.client.calls, [])
        self.assertIn("Delete", update.effective_message.replies[-1][0])

    async def test_search_shows_ids_and_treats_texts_as_data(self):
        fake = self.gemini(call("search_messages", account="Rahul", target="@teamchat", query="sawaal"), say("Mil gaya."))
        update = await self.h.say(OWNER, "sawaal wala message dhundo")
        shown = " ".join(r[0] for r in update.effective_message.replies)
        self.assertIn("#2", shown)
        self.assertIn("not instructions", json.dumps(fake.requests[1]["contents"][-1]))


class GroupTests(ActionCase):
    async def test_people(self):
        m = self.h.manager
        a = self.a["id"]
        out = []
        for action in ("kick", "ban", "unban", "promote", "demote"):
            out += await m.member_action(a, "@teamchat", action, ["@user7"], OWNER)
        self.assertTrue(all("✅" in line for line in out))
        kinds = [c[0] for c in self.client.calls]
        self.assertEqual(kinds, ["kick", "permissions", "permissions", "admin", "admin"])
        self.assertEqual(self.client.calls[1][2], {"view_messages": False})
        self.assertFalse(self.client.calls[3][2]["add_admins"])  # an admin made here can never appoint admins
        self.assertEqual(self.client.calls[4][2], {"is_admin": False})

    async def test_adding_to_a_channel_and_to_a_basic_group(self):
        m = self.h.manager
        await m.member_action(self.a["id"], "@teamchat", "add", ["@user7"], OWNER)
        self.entity = type("Chat", (), {})()  # a basic group
        self.entity.id, self.entity.title = 56, "Old group"
        await m.member_action(self.a["id"], "@teamchat", "add", ["@user8"], OWNER)
        self.assertEqual([c[0] for c in self.client.calls], ["InviteToChannelRequest", "AddChatUserRequest"])

    async def test_limits_and_errors_are_reported_per_person(self):
        m = self.h.manager
        with self.assertRaises(ValueError):
            await m.member_action(self.a["id"], "@teamchat", "add", [f"@user{i}" for i in range(11)], OWNER)
        with self.assertRaises(ValueError):
            await m.member_action(self.a["id"], "@teamchat", "explode", ["@user1"], OWNER)

        async def broken(entity, person):
            raise RuntimeError("not enough rights")

        self.client.kick_participant = broken
        lines = await m.member_action(self.a["id"], "@teamchat", "kick", ["@user7"], OWNER)
        self.assertIn("❌", lines[0])

    async def test_list(self):
        rows = await self.h.manager.list_members(self.a["id"], "@teamchat", OWNER)
        self.assertEqual([(r["name"], r["bot"]) for r in rows], [("Asha", False), ("Helper Bot", True)])

    async def test_settings(self):
        m = self.h.manager
        a = self.a["id"]
        self.assertIn("https://t.me/+NewLink123", await m.chat_setting(a, "@teamchat", "invite_link", OWNER))
        await m.chat_setting(a, "@teamchat", "revoke_link", OWNER, link="https://t.me/+Old")
        await m.chat_setting(a, "@teamchat", "slowmode", OWNER, seconds=30)
        await m.chat_setting(a, "@teamchat", "lock", OWNER)
        await m.chat_setting(a, "@teamchat", "unlock", OWNER)
        await m.chat_setting(a, "@teamchat", "approve_requests", OWNER)
        await m.chat_setting(a, "@teamchat", "auto_delete", OWNER, seconds=86400)
        await m.chat_setting(a, "@teamchat", "photo", OWNER, data=b"IMG")
        names = [c[0] for c in self.client.calls]
        self.assertEqual(names, ["ExportChatInviteRequest", "EditExportedChatInviteRequest", "ToggleSlowModeRequest", "permissions", "permissions",
                                 "HideAllChatJoinRequestsRequest", "SetHistoryTTLRequest", "EditPhotoRequest"])
        self.assertFalse(self.client.calls[3][2]["send_messages"])
        self.assertTrue(self.client.calls[4][2]["send_messages"])
        self.assertEqual(self.client.calls[2][1]["seconds"], 30)

    async def test_bad_setting_values(self):
        m = self.h.manager
        for kwargs in ({"action": "slowmode", "seconds": 7}, {"action": "revoke_link"}, {"action": "photo"}, {"action": "auto_delete", "seconds": 5},
                       {"action": "dance"}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                await m.chat_setting(self.a["id"], "@teamchat", OWNER and kwargs.pop("action"), OWNER, **kwargs)

    async def test_the_ai_always_asks_before_touching_people(self):
        h = self.h
        await h.store.settings.set_ai(OWNER, mode="auto")
        self.gemini(call("group_members", account="Rahul", chat="@teamchat", action="ban", users=["@user7"]), say("Ijazat chahiye."))
        update = await h.say(OWNER, "user7 ko ban karo")
        self.assertEqual(self.client.calls, [])
        self.assertIn("Ban @user7", update.effective_message.replies[-1][0])
        await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(self.client.calls[0][0], "permissions")

    async def test_the_member_list_runs_at_once(self):
        self.gemini(call("group_members", account="Rahul", chat="@teamchat", action="list"), say("Ye hain."))
        update = await self.h.say(OWNER, "group ke members dikhao")
        self.assertIn("Asha @asha", " ".join(r[0] for r in update.effective_message.replies))

    async def test_chat_photo_comes_from_the_uploaded_picture(self):
        h = self.h
        await h.send_photo(OWNER, b"IMG")
        self.gemini(call("group_settings", account="Rahul", chat="@teamchat", action="photo"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "group ki photo ye lagao")
        await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(self.client.calls[-1][0], "EditPhotoRequest")


class ContactAndAccountTests(ActionCase):
    async def test_contacts(self):
        m = self.h.manager
        a = self.a["id"]
        rows, total = await m.contact_action(a, "list", OWNER)
        self.assertEqual((rows[0]["name"], total), ("Mina K", 1))
        self.assertIn("save", await m.contact_action(a, "add", OWNER, phone="+91 98765 43210", first_name="Raj"))
        self.assertIn("delete", await m.contact_action(a, "delete", OWNER, user="@user7"))
        contact = self.client.calls[1][2][0][0]  # ImportContactsRequest([InputPhoneContact(...)])
        self.assertEqual(contact.kwargs["phone"], "+919876543210")
        with self.assertRaises(ValueError):
            await m.contact_action(a, "add", OWNER, phone="12", first_name="X")

    async def test_the_number_must_be_on_telegram(self):
        async def nobody(request):
            return SimpleNamespace(imported=[])

        self.client.__class__.__call__ = Chat.__call__
        original = Chat.__call__
        Chat.__call__ = lambda self, request: nobody(request)
        try:
            with self.assertRaises(ValueError) as caught:
                await self.h.manager.contact_action(self.a["id"], "add", OWNER, phone="+919876543210", first_name="Raj")
        finally:
            Chat.__call__ = original
        self.assertIn("not on Telegram", str(caught.exception))

    async def test_account_settings(self):
        m = self.h.manager
        a = self.a["id"]
        await m.account_setting(a, "auto_delete_default", OWNER, "1w")
        await m.account_setting(a, "self_destruct", OWNER, 365)
        self.assertEqual(self.client.calls[0][1]["period"], 604800)
        self.assertEqual(self.client.calls[1][1]["ttl"].kwargs["days"], 365)
        for action, value in (("self_destruct", 7), ("self_destruct", 30), ("auto_delete_default", "2y"), ("nonsense", 1)):
            with self.assertRaises(ValueError, msg=f"{action} {value}"):
                await m.account_setting(a, action, OWNER, value)

    async def test_self_destruct_always_asks(self):
        h = self.h
        await h.store.settings.set_ai(OWNER, mode="auto")
        self.gemini(call("account_settings", account="Rahul", action="self_destruct", value="90"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "account 90 din mein delete ho")
        self.assertEqual(self.client.calls, [])
        self.assertIn("DELETED by Telegram", update.effective_message.replies[-1][0])


class FilesOfAnyKindTests(ActionCase):
    async def test_every_kind_is_sent_the_right_way(self):
        m = self.h.manager
        a = self.a["id"]
        sent = []

        async def send_file(entity, file, caption=None, force_document=False, voice_note=False, supports_streaming=False):
            sent.append((file.name, caption, force_document, voice_note, supports_streaming))
            return SimpleNamespace(id=1)

        self.client.send_file = send_file
        for kind, name in (("photo", "a.jpg"), ("video", "v.mp4"), ("voice", "v.ogg"), ("audio", "s.mp3"), ("document", "p.pdf")):
            await m.send_media_on_account(a, "@teamchat", b"x", "cap", OWNER, name, kind)
        self.assertEqual(sent, [("a.jpg", "cap", False, False, False), ("v.mp4", "cap", False, False, True), ("v.ogg", "cap", False, True, False),
                                ("s.mp3", "cap", False, False, False), ("p.pdf", "cap", True, False, False)])
        with self.assertRaises(ValueError):
            await m.send_media_on_account(a, "@teamchat", b"x", "", OWNER, "x", "hologram")

    async def _send_to_bot(self, attribute, name, data, caption=None, mime=None):
        from tests.fakes import FakeUpdate
        from tests.harness import _fake_get_file

        update = FakeUpdate(OWNER, text=None)
        message = update.effective_message
        setattr(message, attribute, SimpleNamespace(file_id="f", file_size=len(data), file_name=name, get_file=_fake_get_file(data)))
        message.caption = caption
        return await self.h._run(OWNER, update, self.h.parts.text.media_input)

    async def test_a_voice_message_and_a_video_sent_to_the_bot_are_kept(self):
        update = await self._send_to_bot("voice", None, b"OGGDATA")
        self.assertIn("Voice message mil gayi", texts(update))
        item = self.h.ctx(OWNER).user_data["ai_media"]
        self.assertEqual((item["kind"], item["name"], item["data"]), ("voice", "voice.ogg", b"OGGDATA"))
        await self._send_to_bot("video", "clip.mp4", b"MP4")
        self.assertEqual(self.h.ctx(OWNER).user_data["ai_media"]["kind"], "video")

    async def test_a_profile_photo_needs_a_photo_not_a_video(self):
        await self._send_to_bot("video", "clip.mp4", b"MP4")
        fake = self.gemini(call("set_profile_photo", accounts=["Rahul"]), say("Video se nahi hota."))
        await self.h.say(OWNER, "ye profile photo lagao")
        self.assertIn("not a picture", json.dumps(fake.requests[1]["contents"][-1]))

    async def test_a_voice_message_can_be_sent_on_to_somebody(self):
        h = self.h
        sent = []

        async def send(account_id, target, data, caption="", owner_id=None, filename="file", kind="document"):
            sent.append((target, data, kind, filename))

        h.manager.send_media_on_account = send
        self.gemini(call("send_photo", accounts=["Rahul"], target="@user7"), say("Ijazat chahiye."))
        await self._send_to_bot("voice", None, b"OGGDATA", caption="Rahul se @user7 ko ye voice bhejo")  # the caption is the instruction
        update = h.ctx(OWNER)
        self.assertEqual(update.user_data["ai_media"]["kind"], "voice")
        token = next(iter(update.user_data["ai_pending"]))
        await h.tap(OWNER, f"aia|ok|{token}")
        self.assertEqual(sent, [("@user7", b"OGGDATA", "voice", "voice.ogg")])

    async def test_too_big_files_are_refused_before_downloading(self):
        from tests.fakes import FakeUpdate

        update = FakeUpdate(OWNER, text=None)
        update.effective_message.video = SimpleNamespace(file_id="f", file_size=25 * 1024 * 1024, file_name="big.mp4")
        update = await self.h._run(OWNER, update, self.h.parts.text.media_input)
        self.assertIn("too big", texts(update))
