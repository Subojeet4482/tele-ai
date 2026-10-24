"""Bottom menu, coloured buttons, custom emoji icons, GIF / video start screen and the HTML cards."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import FakeUpdate, buttons  # noqa: E402
from tests.harness import BOB, OWNER, STRANGER, Harness, texts  # noqa: E402

from telegram_manager.bot import keyboards, reply_menu  # noqa: E402


def labels(markup):
    return [[button.text for button in row] for row in markup.keyboard]


class ReplyKeyboardTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        keyboards.configure_icons({})

    def test_layout_is_a_wide_green_button_then_a_grid(self):
        rows = reply_menu.keyboard(is_owner=False).keyboard
        self.assertEqual(len(rows[0]), 1)
        self.assertEqual(rows[0][0].text, "🔗 Connect Account")
        self.assertEqual(rows[0][0].api_kwargs, {"style": "success"})
        self.assertTrue(all(len(row) == 2 for row in rows[1:-1]))
        self.assertEqual([b.text for b in rows[-1]], ["🤖 AI Mode"])  # the instant switch: one wide button at the very bottom
        self.assertEqual(rows[-1][0].api_kwargs, {"style": "success"})
        logout = next(b for row in rows for b in row if "Logout" in b.text)
        self.assertEqual(logout.api_kwargs, {"style": "danger"})

    def test_owner_gets_the_admin_row_and_others_do_not(self):
        owner = [text for row in labels(reply_menu.keyboard(True)) for text in row]
        admin = [text for row in labels(reply_menu.keyboard(False)) for text in row]
        self.assertIn("👑 Admins", owner)
        self.assertIn("📜 Live Logs", owner)
        self.assertNotIn("👑 Admins", admin)
        self.assertNotIn("📜 Live Logs", admin)

    def test_keyboard_is_persistent_and_resized(self):
        kwargs = reply_menu.keyboard(True).kwargs
        self.assertTrue(kwargs["is_persistent"])
        self.assertTrue(kwargs["resize_keyboard"])

    def test_every_label_is_unique_and_found_again(self):
        found = [item.label for item in reply_menu.ITEMS.values()]
        self.assertEqual(len(found), len(set(found)))
        for item in reply_menu.ITEMS.values():
            self.assertIs(reply_menu.lookup(item.label), item)
        self.assertIsNone(reply_menu.lookup("hello"))

    def test_custom_icons_replace_the_leading_emoji_and_taps_still_match(self):
        keyboards.configure_icons({"🔗": "5368324170671202286"})
        button = keyboards.KBtn("🔗 Connect Account", style="success")
        self.assertEqual(button.text, "Connect Account")
        self.assertEqual(button.api_kwargs, {"style": "success", "icon_custom_emoji_id": "5368324170671202286"})
        inline = keyboards.Btn("🔗 Connect account", callback_data="connect")
        self.assertEqual(inline.text, "Connect account")
        self.assertEqual(inline.api_kwargs["icon_custom_emoji_id"], "5368324170671202286")
        # Telegram now sends the text without the emoji: it must still open the same screen
        self.assertIs(reply_menu.lookup("Connect Account"), reply_menu.ITEMS["connect"])
        self.assertIs(reply_menu.lookup("🔗 Connect Account"), reply_menu.ITEMS["connect"])

    def test_without_icons_text_is_untouched(self):
        self.assertEqual(keyboards.KBtn("🔗 Connect Account").text, "🔗 Connect Account")

    async def test_start_puts_the_menu_under_the_chat_once(self):
        h = Harness()
        await h.command(OWNER, "/start")
        self.assertEqual(len(h.bot.keyboards), 0)  # nothing under the chat until a mode is picked
        await h.tap(OWNER, "mode|manual")
        await h.command(OWNER, "/start")
        await h.command(OWNER, "/start")
        self.assertEqual(len(h.bot.keyboards), 1)
        chat, markup = h.bot.keyboards[0]
        self.assertEqual(chat, OWNER)
        self.assertIn("👑 Admins", [t for row in labels(markup) for t in row])

    async def test_keyboard_command_brings_it_back_and_stranger_gets_none(self):
        h = Harness()
        await h.command(OWNER, "/start")
        await h.tap(OWNER, "mode|manual")
        await h.command(OWNER, "/keyboard")
        self.assertEqual(len(h.bot.keyboards), 2)
        await h.command(STRANGER, "/start")
        self.assertEqual([c for c, _ in h.bot.keyboards], [OWNER, OWNER])
        u = await h.command(STRANGER, "/keyboard")
        self.assertIn("only for its admin", texts(u))

    async def test_hide_keyboard(self):
        h = Harness()
        await h.command(OWNER, "/start")
        u = await h.command(OWNER, "/hidekeyboard")
        self.assertIn("hidden", texts(u))
        self.assertNotIn("kb", h.ctx(OWNER).user_data)


class MenuPressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        await self.h.access.add(BOB, OWNER, "Bob")
        await self.h.add_account(OWNER, "Owner A")

    async def test_each_button_opens_its_screen(self):
        h = self.h
        expect = {
            "🔗 Connect Account": "How do you want to connect",
            "📋 My Accounts": "Your accounts",
            "🕒 Daily Broadcast": "",
            "📥 Forwarding": "Forwarding",
            "⚙️ Menu": "Menu",
            "📖 Help": "All commands",
            "🎨 Start Screen": "Start screen",
            "🚪 Logout": "Which account do you want to log out",
            "📤 Export Session": "Which account's session",
            "👑 Admins": "Admins",
        }
        for label, needle in expect.items():
            u = await h.press(OWNER, label)
            self.assertIn(needle, texts(u), label)

    async def test_loading_message_is_removed_afterwards(self):
        h = self.h
        u = await h.press(OWNER, "📋 My Accounts")
        replies = [text for text, _ in u.effective_message.replies]
        self.assertEqual(replies[0], "⏳ Loading My Accounts…")
        self.assertEqual(len(h.bot.deleted), 1)

    async def test_message_all_starts_the_bulk_flow(self):
        h = self.h
        await h.press(OWNER, "📨 Message All")
        self.assertEqual(h.ctx(OWNER).user_data["wz"].name, "bulk_all")
        await h.press(OWNER, "🖱 Click Buttons")
        self.assertEqual(h.ctx(OWNER).user_data["wz"].name, "bulk_click")
        await h.press(OWNER, "💬 Message One")
        self.assertEqual(h.ctx(OWNER).user_data["wz"].name, "message")

    async def test_pressing_a_button_abandons_a_half_finished_flow(self):
        h = self.h
        await h.tap(OWNER, "connect")
        await h.tap(OWNER, "cx|otp")
        self.assertIsNotNone(h.ctx(OWNER).user_data.get("wz"))
        await h.press(OWNER, "📋 My Accounts")
        self.assertIsNone(h.ctx(OWNER).user_data.get("wz"))

    async def test_a_stranger_cannot_use_the_menu(self):
        h = self.h
        u = await h.press(STRANGER, "📋 My Accounts")
        self.assertIn("only for its admin", texts(u))
        self.assertEqual(h.bot.deleted, [])

    async def test_admin_cannot_press_owner_buttons(self):
        h = self.h
        u = await h.press(BOB, "👑 Admins")
        self.assertIn("Only the owner", texts(u))
        u = await h.press(BOB, "📜 Live Logs")
        self.assertIn("Only the owner", texts(u))

    async def test_no_accounts_notice_for_bulk_buttons(self):
        h = Harness()
        u = await h.press(OWNER, "📨 Message All")
        self.assertIn("no connected accounts", texts(u))


class StartCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_start_is_an_html_card_with_live_numbers(self):
        h = Harness()
        await h.add_account(OWNER, "On", online=True)
        await h.add_account(OWNER, "Off", online=False)
        u = await h.command(OWNER, "/start")
        text = texts(u)
        self.assertIn("<b>📱 Telegram Account Manager</b>", text)
        self.assertIn("<blockquote>", text)
        self.assertIn("Accounts: <b>2</b>", text)
        self.assertIn("🟢 1 online", text)
        self.assertIn("<blockquote expandable>", text)  # the command list is collapsed

    async def test_custom_text_is_escaped(self):
        h = Harness()
        await h.store.settings.set_start_screen(text="Hi {name} <b>x</b> & more")
        u = await h.command(OWNER, "/start")
        self.assertIn("Hi there &lt;b&gt;x&lt;/b&gt; &amp; more", texts(u))
        self.assertNotIn("<b>x</b>", texts(u))

    async def test_long_text_with_media_stays_inside_the_caption_limit(self):
        from telegram_manager.bot.cards import visible_length

        h = Harness()
        await h.store.settings.set_start_screen(text="x" * 800, media="gif1", media_type="animation")
        await h.command(OWNER, "/start")
        caption = [text for chat, text in h.bot.sent if chat == OWNER and text.startswith("xxx")][0]
        self.assertLessEqual(visible_length(caption), 1024)
        self.assertIn("You are the 👑 owner", caption)


class StartMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_gif_becomes_the_start_screen(self):
        h = Harness()
        await h.tap(OWNER, "sc|photo")
        u = await h.say(OWNER, "just text")
        self.assertIn("Send a photo, a GIF or a video", texts(u))
        u = await h.send_media(OWNER, "animation", "GIFID1")
        self.assertIn("Media: GIF", texts(u))
        cfg = await h.store.settings.get_start_screen()
        self.assertEqual((cfg["media"], cfg["media_type"]), ("GIFID1", "animation"))
        await h.command(OWNER, "/start")
        self.assertIn((OWNER, "animation", "GIFID1"), h.bot.media)
        await h.command(STRANGER, "/start")
        self.assertIn((STRANGER, "animation", "GIFID1"), h.bot.media)  # everybody sees it

    async def test_video_then_remove(self):
        h = Harness()
        await h.tap(OWNER, "sc|photo")
        u = await h.send_media(OWNER, "video", "VID9")
        self.assertIn("Media: video", texts(u))
        await h.command(OWNER, "/start")
        self.assertIn((OWNER, "video", "VID9"), h.bot.media)
        u = await h.tap(OWNER, "sc|rmphoto")
        self.assertIn("Media: none", texts(u))
        h.bot.media.clear()
        await h.command(OWNER, "/start")
        self.assertEqual(h.bot.media, [])

    async def test_preview_uses_the_media(self):
        h = Harness()
        await h.store.settings.set_start_screen(media="GIF2", media_type="animation", text="Hello")
        await h.tap(OWNER, "sc|preview")
        self.assertIn((OWNER, "animation", "GIF2"), h.bot.media)

    async def test_old_photo_setting_still_works(self):
        h = Harness()
        await h.store.settings.set_start_screen(photo="photoOLD")
        u = await h.tap(OWNER, "sc|home")
        self.assertIn("Media: photo", texts(u))

    async def test_a_gif_is_ignored_by_steps_that_only_take_photos(self):
        h = Harness()
        acc = await h.add_account(OWNER, "A")
        h.manager.clients[acc["id"]].connected = True
        await h.tap(OWNER, f"pf|photo|{acc['id']}")
        before = h.ctx(OWNER).user_data["wz"].step
        await h.send_media(OWNER, "animation", "GIF3")
        self.assertEqual(h.ctx(OWNER).user_data["wz"].step, before)

    async def test_stranger_gif_is_refused(self):
        h = Harness()
        u = await h.send_media(STRANGER, "animation", "X")
        self.assertIn("only for its admin", u.effective_message.replies[-1][0])


class EmojiIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_ids_with_utf16_offsets(self):
        from types import SimpleNamespace

        h = Harness()
        update = FakeUpdate(OWNER, text="a😀b🔥")
        # "a" = 1 unit, 😀 = 2 units (offset 1), "b" = 1 unit, 🔥 = 2 units (offset 4)
        update.effective_message.entities = [
            SimpleNamespace(type="custom_emoji", offset=1, length=2, custom_emoji_id="111111"),
            SimpleNamespace(type="custom_emoji", offset=4, length=2, custom_emoji_id="222222"),
        ]
        await h.parts.middleware.track_update(update, h.ctx(OWNER))
        await h.parts.text.custom_emoji_input(update, h.ctx(OWNER))
        reply = update.effective_message.replies[-1][0]
        self.assertIn("😀 → 111111", reply)
        self.assertIn("🔥 → 222222", reply)
        self.assertIn('"😀": "111111"', reply)


class ConfigIconTests(unittest.TestCase):
    def load(self, value):
        import base64

        from cryptography.fernet import Fernet

        from telegram_manager.config import Settings

        env = {
            "BOT_TOKEN": "123456:ABCdef_ghiJKL-mno", "OWNER_ID": "42",
            "FIREBASE_CREDENTIALS_B64": base64.b64encode(b'{"a":1}').decode(),
            "SESSION_ENCRYPTION_KEY": Fernet.generate_key().decode(), "BUTTON_ICONS": value,
        }
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return Settings.from_env()
        finally:
            for key, val in old.items():
                os.environ.pop(key, None) if val is None else os.environ.__setitem__(key, val)

    def test_valid_and_empty(self):
        self.assertEqual(self.load("").button_icons, {})
        self.assertEqual(self.load('{"🔗": "5368324170671202286"}').button_icons, {"🔗": "5368324170671202286"})

    def test_rejects_garbage(self):
        for bad in ("not json", '["a"]', '{"🔗": "abc"}', '{"🔗": 5}'):
            with self.assertRaises(RuntimeError):
                self.load(bad)


if __name__ == "__main__":
    unittest.main()
