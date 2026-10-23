import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import buttons, find  # noqa: E402
from tests.harness import BOB, OWNER, STRANGER, Harness, texts  # noqa: E402


class BulkFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.h.record_telethon(self)
        await self.h.access.add(BOB, OWNER, "Bob")
        self.a1 = await self.h.add_account(OWNER, "Owner A")
        self.a2 = await self.h.add_account(OWNER, "Owner B")
        self.b1 = await self.h.add_account(BOB, "Bob A")

    async def test_step_by_step_all(self):
        h = self.h
        u = await h.command(OWNER, "/all")
        self.assertIn("Enter the user name", texts(u))
        u = await h.say(OWNER, "@mandal4482")
        self.assertIn("Confirm the user", texts(u))
        self.assertIn("@mandal4482", texts(u))
        labels = [b.text for b in buttons(u.last()[1])]
        self.assertTrue(any("Confirm" in x for x in labels) and any("Back" in x for x in labels))
        self.assertTrue(any("Cancel" in x for x in labels))
        u = await h.tap(OWNER, "wz|bulk_all|pick|yes")
        self.assertIn("Delay between accounts", texts(u))
        self.assertIn("0 = instant", texts(u))
        self.assertEqual([b.callback_data for b in buttons(u.last()[1]) if "pick" in b.callback_data][0], "wz|bulk_all|pick|0")
        u = await h.tap(OWNER, "wz|bulk_all|pick|3")
        self.assertIn("Send the message text", texts(u))
        u = await h.say(OWNER, "hello")
        self.assertIn("Which inline buttons", texts(u))
        u = await h.tap(OWNER, "wz|bulk_all|pick|-")  # Skip: no buttons
        self.assertIn("When should it be sent", texts(u))
        u = await h.tap(OWNER, "wz|bulk_all|pick|0")
        self.assertIn("Working on 2 account(s)", texts(u))
        await h.finish_bulk(OWNER)
        sent = [c for c in h.calls if c[0] == "send_existing"]
        self.assertEqual({c[1] for c in sent}, {self.a1["id"], self.a2["id"]})  # only the owner's accounts
        self.assertTrue(all(c[2] == "@mandal4482" and c[3] == "hello" for c in sent))
        self.assertEqual(h.sleeps, [3])  # one gap between two accounts
        self.assertIn("2/2 accounts", h.bot.sent[-1][1])
        self.assertEqual(h.bot.sent[-1][0], OWNER)

    async def test_instant_delay_zero_uses_no_sleep(self):
        h = self.h
        await h.command(OWNER, "/all @mandal4482 hi 0sec Y")
        await h.finish_bulk(OWNER)
        self.assertEqual(len([c for c in h.calls if c[0] == "send_existing"]), 2)
        self.assertEqual(h.sleeps, [])

    async def test_one_line_command_runs_directly(self):
        h = self.h
        u = await h.command(OWNER, "/all @mandal4482 hello 3sec Y")
        self.assertIn("Working on", texts(u))
        await h.finish_bulk(OWNER)
        self.assertEqual(len(h.calls), 2)

    async def test_command_with_y_asks_only_the_delay(self):
        u = await self.h.command(OWNER, "/all @mandal4482 hello Y")
        self.assertIn("Delay between accounts", texts(u))
        u = await self.h.tap(OWNER, "wz|bulk_all|pick|1")
        self.assertIn("Working on", texts(u))
        await self.h.finish_bulk(OWNER)
        self.assertEqual(self.h.sleeps, [1])

    async def test_command_without_confirmation_asks_to_confirm(self):
        u = await self.h.command(OWNER, "/all @mandal4482 hello")
        self.assertIn("Confirm the user", texts(u))
        self.assertIn("hello", texts(u))
        self.assertEqual(self.h.calls, [])

    async def test_no_cancels(self):
        u = await self.h.command(OWNER, "/all @mandal4482 hello 3sec N")
        self.assertIn("Cancelled", texts(u))
        self.assertEqual(self.h.calls, [])

    async def test_alll_has_minimum_three_seconds(self):
        h = self.h
        u = await h.command(OWNER, "/alll @mandal4482 hello 2sec Y")
        self.assertIn("between 3 and 100", texts(u))
        u = await h.command(OWNER, "/alll @mandal4482 hello Y")
        self.assertIn("at least 3", texts(u))
        labels = [b.text for b in buttons(u.last()[1])]
        self.assertNotIn("⚡ 0 · instant", labels)
        u = await h.say(OWNER, "1")
        self.assertIn("between 3 and 100", texts(u))  # typed value too low: asked again
        u = await h.tap(OWNER, "wz|bulk_alll|pick|5")
        await h.finish_bulk(OWNER)
        self.assertEqual({c[0] for c in h.calls}, {"send_any"})
        self.assertEqual(h.sleeps, [5])

    async def test_alll_manager_enforces_minimum_even_if_asked_for_less(self):
        result = await self.h.manager.send_all_any(OWNER, "@x_user1", "hi", 0)
        self.assertEqual(result.ok, 2)
        self.assertEqual(self.h.sleeps, [3])

    async def test_block_same_steps_without_message(self):
        h = self.h
        await h.command(OWNER, "/allblock")
        await h.say(OWNER, "@spammer1")
        await h.tap(OWNER, "wz|bulk_block|pick|yes")
        u = await h.tap(OWNER, "wz|bulk_block|pick|0")
        self.assertIn("Working on", texts(u))  # no message question for block
        await h.finish_bulk(OWNER)
        self.assertEqual({c[0] for c in h.calls}, {"block"})
        self.assertIn("Blocked: 2/2", h.bot.sent[-1][1])

    async def test_back_goes_one_step_at_a_time(self):
        h = self.h
        await h.command(OWNER, "/all")
        await h.say(OWNER, "@mandal4482")
        await h.tap(OWNER, "wz|bulk_all|pick|yes")
        u = await h.tap(OWNER, "wz|bulk_all|back")
        self.assertIn("Confirm the user", texts(u))
        u = await h.tap(OWNER, "wz|bulk_all|back")
        self.assertIn("Enter the user name", texts(u))
        u = await h.tap(OWNER, "wz|bulk_all|back")
        self.assertIn("Menu", texts(u))  # left the flow, back on the menu screen
        self.assertIsNone(h.ctx(OWNER).user_data.get("wz"))

    async def test_cancel_returns_to_menu(self):
        h = self.h
        await h.command(OWNER, "/all")
        u = await h.tap(OWNER, "wz|bulk_all|cancel")
        self.assertIn("Menu", texts(u))

    async def test_invalid_username_is_asked_again(self):
        await self.h.command(OWNER, "/all")
        u = await self.h.say(OWNER, "not a user!!")
        self.assertIn("❌", texts(u))
        self.assertIn("Enter the user name", texts(u))

    async def test_menu_button_starts_flow(self):
        u = await self.h.tap(OWNER, "bulk|alll")
        self.assertIn("Enter the user name", texts(u))

    async def test_every_admin_only_reaches_own_accounts(self):
        h = self.h
        await h.command(BOB, "/allblock @spammer1 0sec Y")
        await h.finish_bulk(BOB)
        self.assertEqual({c[1] for c in h.calls}, {self.b1["id"]})
        self.assertEqual(h.bot.sent[-1][0], BOB)

    async def test_offline_account_is_reported_not_waited_for(self):
        h = self.h
        h.manager.clients[self.a2["id"]].connected = False
        await h.command(OWNER, "/all @mandal4482 hi 5sec Y")
        await h.finish_bulk(OWNER)
        self.assertEqual(h.sleeps, [])
        self.assertIn("1/2", h.bot.sent[-1][1])
        self.assertIn("not connected", h.bot.sent[-1][1])

    async def test_stop_button_cancels_running_job(self):
        import asyncio

        h = self.h
        gate = asyncio.Event()

        async def slow(account_id, target, text, owner_id=None):
            await gate.wait()

        h.manager.send_to_existing_dialog = slow
        await h.command(OWNER, "/all @mandal4482 hi 0sec Y")
        self.assertTrue(h.deps.bulk.running(OWNER))
        await asyncio.sleep(0.01)  # let the job actually start, as it would in real life
        u = await h.tap(OWNER, "stop_bulk")
        self.assertEqual(u.callback_query.answers[-1][0], "Stopping…")
        task = h.deps.bulk._tasks.get(OWNER)
        if task:
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn("Stopped", h.bot.sent[-1][1])

    async def test_no_accounts_message(self):
        h = Harness()
        u = await h.command(OWNER, "/all @mandal4482 hi 0sec Y")
        self.assertIn("no connected accounts", texts(u))


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        await self.h.access.add(BOB, OWNER, "Bob")
        self.owner_acc = await self.h.add_account(OWNER, "Owner A")
        self.bob_acc = await self.h.add_account(BOB, "Bob A")

    async def test_stranger_start_shows_the_public_screen(self):
        u = await self.h.command(STRANGER, "/start")
        self.assertIn("export the session string of your own", texts(u))
        self.assertEqual([b.callback_data for b in buttons(u.last()[1])], ["ex|start"])
        self.assertNotIn("Account Manager", texts(u))

    async def test_stranger_is_told_admins_only_once_in_a_while(self):
        u = await self.h.command(STRANGER, "/menu")
        self.assertEqual(texts(u), "🚫 This bot is only for its admin.")
        u2 = await self.h.command(STRANGER, "/menu")
        self.assertEqual(u2.effective_message.replies, [])  # rate limited
        u3 = await self.h.tap(STRANGER, "menu")
        self.assertEqual(u3.callback_query.answers[-1], ("🚫 This bot is only for its admin.", True))
        u4 = await self.h.say(STRANGER, "hello")
        self.assertEqual(u4.effective_message.replies, [])

    async def test_stranger_in_a_group_gets_no_reply(self):
        from tests.fakes import FakeUpdate

        update = FakeUpdate(STRANGER, text="/start", chat_type="supergroup")
        await self.h.parts.start.start(update, self.h.ctx(STRANGER))
        self.assertEqual(update.effective_message.replies, [])

    async def test_stranger_cannot_use_any_action(self):
        h = self.h
        for command in ("/all @mandal4482 hi 0sec Y", "/list", "/message 1 @a_user1 hi", "/logs"):
            u = await h.command(STRANGER, command)
            self.assertIn("only for its admin", (u.effective_message.replies or [("only for its admin", None)])[-1][0])
        self.assertEqual(h.deps.bulk._tasks, {})
        self.assertEqual(h.bot.sent, [])

    async def test_start_screen_owner_vs_admin(self):
        # /start offers Manual, Settings and AI; the full panel of buttons is what "Manual" opens
        for who in (OWNER, BOB):
            started = await self.h.command(who, "/start")
            self.assertEqual([b.callback_data for b in buttons(started.last()[1])][:3], ["mode|manual", "set|home", "mode|ai"])
        owner = await self.h.tap(OWNER, "mode|manual")
        bob = await self.h.tap(BOB, "mode|manual")
        owner_labels = [b.text for b in buttons(owner.last()[1])]
        bob_labels = [b.text for b in buttons(bob.last()[1])]
        self.assertIn("👑 Manage admins", owner_labels)
        self.assertNotIn("👑 Manage admins", bob_labels)
        for labels in (owner_labels, bob_labels):
            self.assertIn("🔗 Connect account", labels)
            self.assertEqual(labels[-2:], ["⚙️ Menu", "📖 All commands"])  # Menu sits under the account buttons

    async def test_menu_lists_everything_as_buttons(self):
        u = await self.h.command(OWNER, "/menu")
        data = {b.callback_data for b in buttons(u.last()[1])}
        for expected in ("bulk|all", "bulk|alll", "bulk|block", "msg|start", "fw|home", "sched|home", "logs|link", "home"):
            self.assertIn(expected, data)
        bob_data = {b.callback_data for b in buttons((await self.h.command(BOB, "/menu")).last()[1])}
        self.assertNotIn("logs|link", bob_data)

    async def test_admin_cannot_open_owner_features(self):
        h = self.h
        u = await h.tap(BOB, "adm|home")
        self.assertEqual(u.callback_query.answers[-1], ("🔒 Only the owner can do this.", True))
        u = await h.command(BOB, "/logs")
        self.assertIn("Only the owner", texts(u))
        u = await h.tap(BOB, "adm|add")
        self.assertEqual(h.ctx(BOB).user_data.get("wz"), None)

    async def test_list_shows_only_own_accounts(self):
        u = await self.h.command(BOB, "/list")
        self.assertIn("Bob A", texts(u))
        self.assertNotIn("Owner A", texts(u))
        u = await self.h.command(OWNER, "/list")
        self.assertIn("Owner A", texts(u))
        self.assertNotIn("Bob A", texts(u))

    async def test_admin_cannot_touch_another_admins_account_by_id(self):
        h = self.h
        for data in (f"acc|{self.owner_acc['id']}", f"lo|{self.owner_acc['id']}", f"loc|{self.owner_acc['id']}",
                     f"msg_acc|{self.owner_acc['id']}", f"fw|set|{self.owner_acc['id']}|u"):
            u = await h.tap(BOB, data)
            self.assertIn("Account not found", texts(u), data)
        self.assertIn(self.owner_acc["id"], self.h.manager.clients)  # still connected
        self.assertIsNotNone(await h.store.accounts.get(self.owner_acc["id"]))

    async def test_manager_refuses_cross_admin_actions(self):
        with self.assertRaises(ValueError):
            await self.h.manager.remove_account(self.owner_acc["id"], BOB)
        with self.assertRaises(ValueError):
            await self.h.manager.list_dialogs(self.owner_acc["id"], 10, BOB)

    async def test_reply_routing_only_for_the_owner_of_the_account(self):
        h = self.h
        h.manager._remember_reply_target(OWNER, 500, self.owner_acc["id"], 7, 3)
        h.manager._remember_reply_target(BOB, 500, self.bob_acc["id"], 8, 4)
        sent = []

        class C(type(h.manager.clients[self.owner_acc["id"]])):
            async def send_message(self, entity, text, **kw):
                sent.append((entity, text, kw))

        for acc in (self.owner_acc, self.bob_acc):
            h.manager.clients[acc["id"]].__class__ = C

        async def known(account_id, client, peer_id):
            return f"entity-{peer_id}"

        h.manager._known_entity = known
        self.assertTrue(await h.manager.reply_via_forward(OWNER, OWNER, 500, "hi"))
        self.assertTrue(await h.manager.reply_via_forward(BOB, BOB, 500, "yo"))
        self.assertEqual([(s[0], s[1], s[2]["reply_to"]) for s in sent], [("entity-7", "hi", 3), ("entity-8", "yo", 4)])
        # Bob replying to a message that belongs to the owner's account is refused
        h.manager._reply_map[(BOB, 501)] = (self.owner_acc["id"], 7, 3)
        self.assertFalse(await h.manager.reply_via_forward(BOB, BOB, 501, "sneaky"))
        self.assertEqual(len(sent), 2)

    async def test_logs_link_is_one_time_and_owner_only(self):
        u = await self.h.command(OWNER, "/logs")
        text = texts(u)
        self.assertIn("https://bot.example/logs/login?key=", text)
        self.assertNotIn("token=", text)
        key = text.split("key=")[1].split()[0]
        self.assertTrue(self.h.log_access.peek(key))
        self.assertIsNotNone(self.h.log_access.redeem(key))
        self.assertFalse(self.h.log_access.peek(key))

    async def test_legacy_commands_point_to_the_new_places(self):
        u = await self.h.command(OWNER, "/onalltime @x 30 10:30am 1")
        self.assertIn("menu", texts(u))
        u = await self.h.command(OWNER, "/delay 5")
        self.assertIn("asked every time", texts(u))

    async def test_middleware_drops_flow_when_a_menu_button_is_tapped_but_not_wz_buttons(self):
        h = self.h
        await h.command(OWNER, "/all")
        self.assertIsNotNone(h.ctx(OWNER).user_data.get("wz"))
        await h.tap(OWNER, "menu")
        self.assertIsNone(h.ctx(OWNER).user_data.get("wz"))

    async def test_expired_flow(self):
        h = self.h
        await h.command(OWNER, "/all")
        h.ctx(OWNER).user_data["wz"].touched -= 10_000
        u = await h.tap(OWNER, "wz|bulk_all|back")
        self.assertIn("expired", texts(u))
        u = await h.say(OWNER, "@mandal4482")
        self.assertIn("Use /start", texts(u))


class ForwardingUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.acc = await self.h.add_account(OWNER, "Owner A")

    async def test_account_switch_cycles_default_on_off(self):
        h = self.h
        u = await h.tap(OWNER, f"acc|{self.acc['id']}")
        self.assertIn("(default)", find(u.last()[1], "User messages").text)
        cb = find(u.last()[1], "User messages").callback_data
        u = await h.tap(OWNER, cb)
        self.assertIn("🟢 ON (this account)", find(u.last()[1], "User messages").text)
        self.assertTrue(h.manager.accounts[self.acc["id"]]["forward_user_messages"])
        u = await h.tap(OWNER, cb)
        self.assertIn("🔴 OFF (this account)", find(u.last()[1], "User messages").text)
        u = await h.tap(OWNER, cb)
        self.assertIn("(default)", find(u.last()[1], "User messages").text)
        self.assertIsNone(h.manager.accounts[self.acc["id"]]["forward_user_messages"])

    async def test_global_switch_is_per_admin(self):
        h = self.h
        await h.access.add(BOB, OWNER, "Bob")
        u = await h.tap(OWNER, "fw|g|u")
        self.assertIn("User messages: 🟢 ON", [b.text for b in buttons(u.last()[1])][0])
        u = await h.tap(BOB, "fw|home")
        self.assertIn("User messages: 🔴 OFF", [b.text for b in buttons(u.last()[1])][0])

    async def test_command_forms(self):
        h = self.h
        u = await h.command(OWNER, "/usermessage")
        self.assertIn("Which account", texts(u))
        u = await h.command(OWNER, "/usermessage 1")
        self.assertIn("Tap a switch", texts(u))
        u = await h.command(OWNER, "/usermessage 1 on")
        self.assertIn("🟢 ON (this account)", find(u.last()[1], "User messages").text)
        u = await h.command(OWNER, "/usermessage owner a off")
        self.assertIn("🔴 OFF (this account)", find(u.last()[1], "User messages").text)
        u = await h.command(OWNER, "/botmessage 1 global")
        self.assertIn("(default)", find(u.last()[1], "Bot messages").text)
        u = await h.command(OWNER, "/usermessage 9 on")
        self.assertIn("Account not found", texts(u))

    async def test_incoming_message_goes_to_the_owner_of_the_account_only(self):
        h = self.h
        await h.access.add(BOB, OWNER, "Bob")
        bob_acc = await h.add_account(BOB, "Bob A")
        await h.store.settings.set_forwarding(BOB, "forward_user_messages", True)
        from types import SimpleNamespace

        class Event:
            is_channel = False
            is_group = False
            is_private = True
            chat_id = 5
            sender_id = 6
            message = SimpleNamespace(message="hey", media=None, forward=None, is_reply=False, id=1)

            async def get_sender(self):
                return SimpleNamespace(first_name="Ann", last_name=None, username=None, bot=False)

        await h.manager._handle_incoming(bob_acc["id"], Event())
        await h.manager._handle_incoming(self.acc["id"], Event())  # the owner did not enable forwarding
        self.assertEqual([chat for chat, _ in h.bot.sent], [BOB])
        self.assertIn("hey", h.bot.sent[0][1])
        self.assertIn("Bob A", h.bot.sent[0][1])

    async def test_forward_header_names_the_group_but_not_private_chats(self):
        h = self.h
        await h.store.settings.set_forwarding(OWNER, "forward_user_messages", True)
        await h.store.settings.set_forwarding(OWNER, "forward_channel_messages", True)  # group messages count as channel
        from types import SimpleNamespace

        def make_event(private):
            class Event:
                is_channel = False
                is_group = not private
                is_private = private
                chat_id = 5
                sender_id = 6
                message = SimpleNamespace(message="hi", media=None, forward=None, is_reply=False, id=1)

                async def get_sender(self):
                    return SimpleNamespace(first_name="Ann", last_name=None, username=None, bot=False)

                async def get_chat(self):
                    return SimpleNamespace(title="Study Group", username=None)

            return Event()

        await h.manager._handle_incoming(self.acc["id"], make_event(private=False))
        await h.manager._handle_incoming(self.acc["id"], make_event(private=True))
        self.assertIn("💭 Chat: Study Group", h.bot.sent[0][1])
        self.assertNotIn("Chat:", h.bot.sent[1][1])

    async def test_sticker_is_forwarded_as_a_sticker_with_header_after_it(self):
        import os
        import tempfile

        h = self.h
        sent_texts = []

        async def fake_forward(chat, path, header, body, remember, sticker=False):
            from telegram_manager.core.forwarding import ForwardingMixin

            return await ForwardingMixin._forward_with_media(h.manager, chat, path, header, body, remember, sticker)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sticker.webm")
            open(path, "wb").write(b"x")
            remembered = []
            await fake_forward(5, path, "📱 Account 1 — A\n👤 From: Ann", "", remembered.append, sticker=True)
        sent_texts = [text for _, text in h.bot.sent]
        self.assertEqual(sent_texts, ["<sticker>", "📱 Account 1 — A\n👤 From: Ann"])
        self.assertEqual(len(remembered), 2)  # replying to either one reaches the right account

    async def test_sender_kinds_follow_the_three_rules(self):
        from types import SimpleNamespace

        h = self.h

        def ev(sender, *, private=False, group=False, channel=False):
            class Event:
                is_channel = channel
                is_group = group
                is_private = private

                async def get_sender(self):
                    if sender == "ERR":
                        raise RuntimeError
                    return sender

            return Event()

        def person(username=None, bot=False):
            return SimpleNamespace(first_name="P", last_name=None, username=username, bot=bot)

        kind = h.manager._classify_sender
        self.assertEqual(await kind(ev(person(), private=True)), "user")  # DM with a person
        self.assertEqual(await kind(ev(person("rahul"), group=True)), "channel")  # person in a group
        self.assertEqual(await kind(ev(person(bot=True), private=True)), "bot")  # Telegram bot flag
        self.assertEqual(await kind(ev(person(bot=True), group=True)), "bot")  # bots count anywhere
        self.assertEqual(await kind(ev(person("Weather_BOT"), private=True)), "bot")  # username ends in bot
        self.assertEqual(await kind(ev(person("cannot_stop"), private=True)), "user")  # "bot" must be the ending
        self.assertEqual(await kind(ev(SimpleNamespace(title="News"), group=True)), "channel")  # sent as a channel
        self.assertEqual(await kind(ev(SimpleNamespace(title="News"), channel=True)), "channel")  # channel post
        self.assertEqual(await kind(ev("ERR", private=True)), "user")  # unknown sender, DM
        self.assertEqual(await kind(ev("ERR", group=True)), "channel")  # unknown sender elsewhere


if __name__ == "__main__":
    unittest.main(verbosity=2)
