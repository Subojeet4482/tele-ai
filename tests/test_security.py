"""2-step password, other devices, channels, privacy, blocked users: the core, the AI's tools and the button screens."""
from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace

from tests.harness import BOB, OWNER, FakeClient, Harness, texts
from tests.test_ai import AITestCase, _token, call, say
from tests.test_ai_mode import response_of

from telethon import errors  # noqa: E402

from telegram_manager.ai import knowledge, tools
from telegram_manager.ai.agent import POWERS
from telegram_manager.core import security


class Channel:  # the class NAME is what the code looks at, like Telethon's own
    def __init__(self, id, title, creator=False, broadcast=True, username=""):
        self.id, self.title, self.creator, self.broadcast, self.username = id, title, creator, broadcast, username
        self.participants_count = 5


class Chat:
    def __init__(self, id, title, creator=False):
        self.id, self.title, self.creator = id, title, creator


class FreshResetAuthorisationForbiddenError(Exception):
    pass


def auth(hash, app="Telegram Android", device="Pixel", current=False, country="India"):
    return SimpleNamespace(hash=hash, current=current, device_model=device, platform="Android", app_name=app, ip="1.2.3.4",
                           country=country, date_active=datetime(2026, 10, 1, 12, 30))


class SecClient(FakeClient):
    """A Telethon client that records the requests it gets and answers the ones the security code reads."""

    def __init__(self):
        super().__init__()
        self.requests: list = []
        self.has_password, self.hint = False, ""
        self.sessions = [auth(1, "Telegram bot", "Server", current=True), auth(2), auth(3, "Telegram Desktop", "Windows", country="USA")]
        self.chats = [Channel(10, "My News", creator=True, username="mynews"), Chat(11, "Family", creator=True),
                      Channel(12, "Others Channel"), Channel(13, "Club", creator=True, broadcast=False)]
        self.fail_reset = None
        self.edit_calls: list = []
        self.photos = ["p1", "p2"]

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetPasswordRequest":
            return SimpleNamespace(has_password=self.has_password, hint=self.hint)
        if name == "GetAuthorizationsRequest":
            return SimpleNamespace(authorizations=list(self.sessions))
        if name == "ResetAuthorizationRequest" and self.fail_reset:
            raise self.fail_reset
        if name == "CreateChannelRequest":
            return SimpleNamespace(chats=[Channel(99, request.kwargs["title"], creator=True)])
        if name == "GetBlockedRequest":
            return SimpleNamespace(users=[SimpleNamespace(id=7, first_name="Spammer", last_name="", username="spam7")])
        return None

    async def edit_2fa(self, **kwargs):
        self.edit_calls.append(kwargs)
        if kwargs.get("current_password") == "wrong":
            raise errors.PasswordHashInvalidError("bad")
        return True

    async def iter_dialogs(self, limit=None):
        for entity in self.chats:
            yield SimpleNamespace(entity=entity, id=-100 * 10**10 - entity.id if isinstance(entity, Channel) else -entity.id)

    async def get_profile_photos(self, who):
        return self.photos

    async def get_entity(self, target):
        return SimpleNamespace(id=7, username=str(target).lstrip("@"))

    def sent(self, name):
        return [r for r in self.requests if type(r).__name__ == name]


class CoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.row = await self.h.add_account(OWNER, "Rahul")
        self.client = SecClient()
        self.h.manager.clients[self.row["id"]] = self.client
        self.id = self.row["id"]
        self.m = self.h.manager


class TwoFactorTests(CoreCase):
    async def test_status_and_enable_change_disable(self):
        self.assertEqual(await self.m.two_fa_status(self.id, OWNER), {"enabled": False, "hint": ""})
        self.assertIn("ON", await self.m.set_two_fa(self.id, OWNER, "enable", password="secret", hint="pet"))
        self.assertEqual(self.client.edit_calls[-1], {"current_password": None, "new_password": "secret", "hint": "pet"})
        self.client.has_password = True
        await self.m.set_two_fa(self.id, OWNER, "change", password="newer", current="secret")
        self.assertEqual(self.client.edit_calls[-1]["current_password"], "secret")
        self.assertEqual(self.client.edit_calls[-1]["new_password"], "newer")
        self.assertIn("OFF", await self.m.set_two_fa(self.id, OWNER, "disable", current="secret"))
        self.assertIsNone(self.client.edit_calls[-1]["new_password"])

    async def test_guards_and_a_wrong_password(self):
        self.client.has_password = True
        with self.assertRaisesRegex(ValueError, "already ON"):
            await self.m.set_two_fa(self.id, OWNER, "enable", password="x")
        with self.assertRaisesRegex(ValueError, "current"):
            await self.m.set_two_fa(self.id, OWNER, "disable")
        with self.assertRaisesRegex(ValueError, "current 2-step password is wrong"):
            await self.m.set_two_fa(self.id, OWNER, "disable", current="wrong")
        self.client.has_password = False
        with self.assertRaisesRegex(ValueError, "already OFF"):
            await self.m.set_two_fa(self.id, OWNER, "disable", current="x")

    async def test_another_admins_account_is_refused(self):
        with self.assertRaises(ValueError):
            await self.m.two_fa_status(self.id, BOB)


class SessionTests(CoreCase):
    async def test_list_marks_the_bot_session(self):
        rows = await self.m.list_sessions(self.id, OWNER)
        self.assertEqual([r["current"] for r in rows], [True, False, False])
        self.assertIn("this bot", security.session_line(rows[0]))

    async def test_terminate_keeps_the_bots_own_session(self):
        result = await self.m.terminate_sessions(self.id, OWNER)
        self.assertEqual((result["ended"], result["kept"]), (2, 1))
        self.assertEqual([r.kwargs["hash"] for r in self.client.sent("ResetAuthorizationRequest")], [2, 3])

    async def test_terminate_only_matching_devices(self):
        result = await self.m.terminate_sessions(self.id, OWNER, match="windows")
        self.assertEqual(result["ended"], 1)
        self.assertEqual(self.client.sent("ResetAuthorizationRequest")[0].kwargs["hash"], 3)

    async def test_a_too_new_session_is_explained(self):
        self.client.fail_reset = FreshResetAuthorisationForbiddenError()
        result = await self.m.terminate_sessions(self.id, OWNER)
        self.assertEqual(result["ended"], 0)
        self.assertIn("few hours", result["errors"][0])


class ChatTests(CoreCase):
    async def test_list_owned_and_by_kind(self):
        owned = await self.m.list_chats_of(self.id, OWNER, owned_only=True)
        self.assertEqual([r["title"] for r in owned], ["My News", "Family", "Club"])
        self.assertEqual([r["kind"] for r in owned], ["channel", "group", "supergroup"])
        channels = await self.m.list_chats_of(self.id, OWNER, kind="channel")
        self.assertEqual([r["title"] for r in channels], ["My News", "Others Channel"])

    async def test_delete_a_channel_a_group_and_not_somebody_elses(self):
        self.assertEqual(await self.m.delete_owned_chat(self.id, OWNER, "@mynews"), "My News")
        self.assertEqual(len(self.client.sent("DeleteChannelRequest")), 1)
        self.assertEqual(await self.m.delete_owned_chat(self.id, OWNER, "family"), "Family")
        self.assertEqual(self.client.sent("DeleteChatRequest")[0].kwargs["chat_id"], 11)
        with self.assertRaisesRegex(ValueError, "not the owner"):
            await self.m.delete_owned_chat(self.id, OWNER, "Others Channel")
        with self.assertRaisesRegex(ValueError, "No channel or group"):
            await self.m.delete_owned_chat(self.id, OWNER, "nothing like this")

    async def test_an_unclear_name_is_not_guessed(self):
        self.client.chats.append(Channel(14, "My News 2", creator=True))
        with self.assertRaisesRegex(ValueError, "fits 2 chats"):
            await self.m.delete_owned_chat(self.id, OWNER, "news")

    async def test_create_channel_and_group(self):
        made = await self.m.create_chat(self.id, OWNER, "Offers", "daily deals", "channel", "offers_x")
        self.assertEqual(made["link"], "https://t.me/offers_x")
        request = self.client.sent("CreateChannelRequest")[0]
        self.assertTrue(request.kwargs["broadcast"])
        self.assertFalse(request.kwargs["megagroup"])
        await self.m.create_chat(self.id, OWNER, "Chat", kind="group")
        self.assertTrue(self.client.sent("CreateChannelRequest")[1].kwargs["megagroup"])
        with self.assertRaises(ValueError):
            await self.m.create_chat(self.id, OWNER, "  ")


class PrivacyAndBlockedTests(CoreCase):
    async def test_privacy_words_in_hinglish_and_english(self):
        self.assertEqual(security.resolve_privacy("Last seen", "nobody"), ("last_seen", "nobody"))
        self.assertEqual(security.resolve_privacy("number", "koi nahi"), ("phone", "nobody"))
        self.assertEqual(security.resolve_privacy("groups", "contacts"), ("groups", "contacts"))
        with self.assertRaises(ValueError):
            security.resolve_privacy("shoe size", "everyone")
        with self.assertRaises(ValueError):
            security.resolve_privacy("phone", "maybe")

    async def test_set_privacy_sends_the_right_rule(self):
        text = await self.m.set_privacy(self.id, OWNER, "phone", "nobody")
        request = self.client.sent("SetPrivacyRequest")[0]
        self.assertEqual(type(request.kwargs["key"]).__name__, "InputPrivacyKeyPhoneNumber")
        self.assertEqual(type(request.kwargs["rules"][0]).__name__, "InputPrivacyValueDisallowAll")
        self.assertIn("nobody", text)

    async def test_blocked_unblock_and_photos(self):
        self.assertEqual((await self.m.blocked_users(self.id, OWNER))[0]["username"], "spam7")
        await self.m.unblock_on_account(self.id, "@spam7", OWNER)
        self.assertEqual(len(self.client.sent("UnblockRequest")), 1)
        self.assertEqual(await self.m.delete_profile_photos(self.id, OWNER), 2)
        self.assertEqual(len(self.client.sent("DeletePhotosRequest")), 1)


# --- the AI's tools ----------------------------------------------------------------------------------------
class AIToolCase(AITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = SecClient()
        self.h.manager.clients[self.a["id"]] = self.client


class AIToolTests(AIToolCase):
    async def test_the_new_tools_are_registered_and_the_ai_knows_its_role(self):
        for name in ("two_factor", "terminate_sessions", "delete_channel", "create_channel", "list_my_chats", "set_privacy",
                     "account_security", "unblock_user", "list_blocked", "list_sessions", "edit_chat", "delete_profile_photos"):
            self.assertIn(name, tools.TOOLS)
        declared = tools.declarations()
        self.assertEqual(len({d["name"] for d in declared}), len(declared))
        for word in ("2-step", "devices", "channels", "ROLE"):
            self.assertIn(word, POWERS + knowledge.OVERVIEW)
        self.assertIn("security", knowledge.topic_names())
        self.assertIsNotNone(knowledge.guide("channels"))

    async def test_security_overview_is_shown(self):
        self.gemini(call("account_security", accounts=["Rahul"]), say("Dekh lo."))
        update = await self.h.say(OWNER, "Rahul ki security dikhao")
        shown = " ".join(r[0] for r in update.effective_message.replies)
        self.assertIn("2FA OFF", shown)
        self.assertIn("2 other device", shown)

    async def test_two_factor_opens_a_form_and_the_password_never_reaches_the_ai(self):
        h = self.h
        fake = self.gemini(call("two_factor", accounts=["Rahul"], action="enable"), say("Form khul gaya."))
        update = await h.say(OWNER, "Rahul pe 2FA on karo")
        self.assertIn("NEW 2-step password", " ".join(r[0] for r in update.effective_message.replies))
        self.assertEqual(response_of(fake, 1)["status"], "password_form_opened")
        await h.say(OWNER, "Sup3rS3cret!")
        await h.say(OWNER, "my pet")
        done = await h.tap(OWNER, "wz|tfa|pick|yes")
        self.assertIn("2-step verification is ON", " ".join(r[0] for r in done.effective_message.replies + [done.last()]))
        self.assertEqual(self.client.edit_calls[-1], {"current_password": None, "new_password": "Sup3rS3cret!", "hint": "my pet"})
        self.assertNotIn("Sup3rS3cret!", str(fake.requests))

    async def test_two_factor_status_runs_at_once(self):
        self.client.has_password, self.client.hint = True, "pet"
        self.gemini(call("two_factor", accounts=["Rahul"], action="status"), say("Dekh lo."))
        update = await self.h.say(OWNER, "Rahul ka 2fa on hai?")
        shown = " ".join(r[0] for r in update.effective_message.replies)
        self.assertIn("ON", shown)
        self.assertIn("pet", shown)

    async def test_ending_devices_always_asks_even_in_auto_mode(self):
        h = self.h
        await h.store.settings.set_ai(OWNER, mode="auto")
        self.gemini(call("terminate_sessions", accounts=["Rahul"]), say("Ijazat chahiye."))
        update = await h.say(OWNER, "Rahul ke extra device hatao")
        ask = update.effective_message.replies[-1]
        self.assertIn("ALL other devices", ask[0])
        self.assertIn("2 other device", ask[0])
        self.assertEqual(self.client.sent("ResetAuthorizationRequest"), [])
        done = await h.tap(OWNER, f"aia|ok|{_token(ask[1])}")
        self.assertEqual(len(self.client.sent("ResetAuthorizationRequest")), 2)
        self.assertIn("2 device(s) ended", texts(done))

    async def test_delete_channel_lists_what_dies_and_asks_even_in_auto_mode(self):
        h = self.h
        await h.store.settings.set_ai(OWNER, mode="auto")
        self.gemini(call("delete_channel", account="Rahul", all_owned=True), say("Ijazat chahiye."))
        update = await h.say(OWNER, "Rahul ke saare channel delete karo")
        ask = update.effective_message.replies[-1]
        for title in ("My News", "Family", "Club"):
            self.assertIn(title, ask[0])
        self.assertNotIn("Others Channel", ask[0])
        self.assertEqual(self.client.sent("DeleteChannelRequest"), [])
        await h.tap(OWNER, f"aia|ok|{_token(ask[1])}")
        self.assertEqual(len(self.client.sent("DeleteChannelRequest")), 2)
        self.assertEqual(len(self.client.sent("DeleteChatRequest")), 1)

    async def test_deleting_somebody_elses_channel_is_refused_before_asking(self):
        fake = self.gemini(call("delete_channel", account="Rahul", chat="Others Channel"), say("Woh aapka nahi hai."))
        update = await self.h.say(OWNER, "Others Channel delete karo")
        self.assertIn("not the owner", response_of(fake, 1)["error"])
        self.assertEqual(self.client.sent("DeleteChannelRequest"), [])

    async def test_create_channel_and_privacy_ask_in_confirm_mode(self):
        h = self.h
        self.gemini(call("create_channel", accounts=["Rahul"], title="Offers", kind="channel"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "Offers naam ka channel banao")
        ask = update.effective_message.replies[-1]
        self.assertEqual(self.client.sent("CreateChannelRequest"), [])
        await h.tap(OWNER, f"aia|ok|{_token(ask[1])}")
        self.assertEqual(len(self.client.sent("CreateChannelRequest")), 1)
        self.gemini(call("set_privacy", accounts=["Rahul"], setting="last seen", who="nobody"), say("Ijazat chahiye."))
        update = await h.say(OWNER, "Rahul ka last seen chhupao")
        await h.tap(OWNER, f"aia|ok|{_token(update.effective_message.replies[-1][1])}")
        self.assertEqual(len(self.client.sent("SetPrivacyRequest")), 1)

    async def test_an_offline_account_is_refused_clearly(self):
        self.client.connected = False
        self.h.manager.clients[self.b["id"]].connected = False
        fake = self.gemini(call("account_security", accounts=["Rahul"]), say("Offline hai."))
        await self.h.say(OWNER, "Rahul ki security")
        self.assertIn("offline", response_of(fake, 1)["error"])

    async def test_lists_of_channels_and_blocked_users(self):
        self.gemini(call("list_my_chats", account="Rahul", owned_only=True), call("list_blocked", account="Rahul"), say("Ye lo."))
        update = await self.h.say(OWNER, "Rahul ke channel aur block list dikhao")
        shown = " ".join(r[0] for r in update.effective_message.replies)
        self.assertIn("My News", shown)
        self.assertNotIn("Others Channel", shown)
        self.assertIn("@spam7", shown)


# --- the buttons --------------------------------------------------------------------------------------------
def data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class ButtonTests(CoreCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.h.record_telethon(self)
        await self.h.store.settings.set_ai(OWNER, ui_mode="manual")

    async def test_settings_holds_the_important_screens(self):
        owner = data((await self.h.tap(OWNER, "set|home")).last()[1])
        for target in ("ai|home", "ai|keys", "ai|prompts", "ai|mode", "fw|home", "sched|home", "sec|home", "sch|home", "sec|check",
                       "sc|home", "give|start", "exp|pick", "connect", "adm|home", "logs|link", "mode|choose"):
            self.assertIn(target, owner)
        await self.h.access.add(BOB, OWNER, "Bob")
        admin = data((await self.h.tap(BOB, "set|home")).last()[1])
        self.assertIn("sec|home", admin)
        self.assertNotIn("adm|home", admin)  # owner only
        self.assertNotIn("logs|link", admin)

    async def test_settings_command_and_bottom_menu_open_it(self):
        self.assertIn("Settings", texts(await self.h.command(OWNER, "/settings")))
        self.assertIn("Settings", texts(await self.h.press(OWNER, "⚙️ Settings")))

    async def test_account_security_screen_and_enabling_2fa_from_buttons(self):
        h = self.h
        picker = await h.tap(OWNER, "sec|home")
        self.assertIn(f"sec|acc|{self.id}", data(picker.last()[1]))
        screen = await h.tap(OWNER, f"sec|acc|{self.id}")
        self.assertIn("OFF", texts(screen))
        self.assertIn("2 other device", texts(screen))
        self.assertIn(f"sec|tfa|enable|{self.id}", data(screen.last()[1]))
        await h.tap(OWNER, f"sec|tfa|enable|{self.id}")
        await h.say(OWNER, "pass123")
        await h.say(OWNER, "-")
        done = await h.tap(OWNER, "wz|tfa|pick|yes")
        self.assertIn("2-step verification is ON", texts(done))
        self.assertEqual(self.client.edit_calls[-1], {"current_password": None, "new_password": "pass123", "hint": ""})

    async def test_with_2fa_on_the_screen_offers_change_and_off_and_off_needs_the_old_password(self):
        h = self.h
        self.client.has_password = True
        screen = await h.tap(OWNER, f"sec|acc|{self.id}")
        self.assertIn(f"sec|tfa|change|{self.id}", data(screen.last()[1]))
        await h.tap(OWNER, f"sec|tfa|disable|{self.id}")
        await h.say(OWNER, "oldpass")
        done = await h.tap(OWNER, "wz|tfa|pick|yes")
        self.assertIn("OFF", texts(done))
        self.assertEqual(self.client.edit_calls[-1]["current_password"], "oldpass")
        self.assertIsNone(self.client.edit_calls[-1]["new_password"])

    async def test_ending_devices_asks_first(self):
        h = self.h
        ask = await h.tap(OWNER, f"sec|termq|{self.id}")
        self.assertEqual(self.client.sent("ResetAuthorizationRequest"), [])
        self.assertIn(f"sec|term|{self.id}", data(ask.last()[1]))
        done = await h.tap(OWNER, f"sec|term|{self.id}")
        self.assertEqual(len(self.client.sent("ResetAuthorizationRequest")), 2)
        self.assertIn("2 device(s) logged out", texts(done))

    async def test_privacy_preset(self):
        done = await self.h.tap(OWNER, f"sec|pv|0|{self.id}")
        self.assertEqual(len(self.client.sent("SetPrivacyRequest")), 1)
        self.assertIn("Hide phone number", texts(done))

    async def test_channels_screen_deletes_only_after_confirming(self):
        h = self.h
        listing = await h.tap(OWNER, f"sch|acc|{self.id}")
        self.assertIn("owns 3", texts(listing))
        self.assertNotIn("Others Channel", str([b.text for row in listing.last()[1].inline_keyboard for b in row]))
        ask = await h.tap(OWNER, f"sch|dq|{self.id}|0")
        self.assertIn("My News", texts(ask))
        self.assertEqual(self.client.sent("DeleteChannelRequest"), [])
        done = await h.tap(OWNER, f"sch|d|{self.id}|0")
        self.assertEqual(len(self.client.sent("DeleteChannelRequest")), 1)
        self.assertIn("deleted", texts(done))

    async def test_an_offline_account_and_another_admins_account_are_refused(self):
        self.client.connected = False
        self.assertIn("offline", texts(await self.h.tap(OWNER, f"sec|acc|{self.id}")))
        self.client.connected = True
        await self.h.access.add(BOB, OWNER, "Bob")
        self.assertIn("not found", texts(await self.h.tap(BOB, f"sec|acc|{self.id}")).lower())
        self.assertEqual(self.client.sent("GetAuthorizationsRequest"), [])

    async def test_health_check_of_all_accounts(self):
        checked = []

        async def check(owner_id, account_id, spam=True):
            checked.append((account_id, spam))
            return {}

        self.h.manager.check_account = check
        done = await self.h.tap(OWNER, "sec|check")
        self.assertEqual(checked, [(self.id, True)])
        self.assertIn("Health check", " ".join(m[0] for m in self.h.bot.sent) + texts(done))


if __name__ == "__main__":
    unittest.main()
