"""/start, /menu, /cancel and the main screens. The start screen can carry an image and a custom
welcome text (set by the admins); somebody who is not an admin sees the public version of it."""
from __future__ import annotations

from contextlib import suppress

from telegram import ReplyKeyboardRemove

from telegram_manager.ai.config import UI_AI, UI_MANUAL, AISettings
from telegram_manager.bot import cards, keyboards, reply_menu, ui
from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.store.settings import SettingsStore

CAPTION_LIMIT = 1024

PUBLIC_DEFAULT = (
    "<b>👋 Welcome!</b>\n\n"
    "This bot is run by its admins.\n\n"
    "<blockquote>📤 You can still export the session string of your own Telegram account here: tap the button, "
    "log in with your number and code, and I send you the session. I keep nothing.</blockquote>"
)

COMMANDS = (
    "/menu /list /refresh /all /alll /multi /click /allblock /broadcast /message "
    "/usermessage /botmessage /channelmessage /send /unsend /delchat /whois /setapi /import /ai /aichats /aisettings /settings /security /channels /give /export /startscreen /proxy /getproxy /proxyurl /tempmail /aimode /manual /keyboard /help /cancel"
)


class StartHandlers(HandlerBase):
    ai = None  # the AI handlers, set by the registry (AI mode's home screen lives there)

    async def _mode(self, uid: int) -> str:
        """'ai', 'manual', or '' when this admin has not picked one yet."""
        try:
            return AISettings.from_dict(await self.store.settings.get_ai(uid)).ui_mode
        except Exception:
            return ""

    async def _card(self, uid: int) -> str:
        """The live status card: accounts online, forwarding switches, daily broadcasts. Never raises."""
        try:
            accounts = await self.store.accounts.list(uid)
            online = sum(1 for account in accounts if self.manager.is_online(str(account["id"])))
            forwarding = await self.store.settings.get_forwarding(uid)
            jobs = len(self.manager.daily_jobs_for(uid))
        except Exception:
            return ""
        tick = lambda on: "✅" if on else "❌"  # noqa: E731
        lines = [
            f"📱 Accounts: <b>{len(accounts)}</b>  ·  🟢 {online} online  ·  🔴 {len(accounts) - online} offline",
            f"📥 Forwarding: 👤 {tick(forwarding['forward_user_messages'])}  "
            f"🤖 {tick(forwarding['forward_bot_messages'])}  📢 {tick(forwarding['forward_channel_messages'])}",
            f"🕒 Daily broadcasts: <b>{jobs}</b>",
        ]
        return cards.quote(lines)

    async def _admin_text(self, uid: int, first: str, custom: str, has_media: bool) -> str:
        owner = self.deps.access.is_owner(uid)
        role = f"You are the {'👑 owner' if owner else '🛡 admin'}."
        card = await self._card(uid)
        if custom:
            body = cards.esc(custom).replace("{name}", cards.esc(first))
            return cards.fit([[body, role, card], [body, role]], has_media)
        title = cards.bold("📱 Telegram Account Manager")
        intro = f"{role} You only see and control the accounts you connect yourself."
        hint = "👇 Mode chuno — 🖐 Manual: saare buttons · ⚙️ Settings: sab important settings · 🤖 AI: bas normal message likho, main kar dunga"
        commands = cards.quote([cards.esc(COMMANDS + (" /logs" if owner else ""))], expandable=True)
        return cards.fit(
            [[title, intro, card, hint, commands], [title, intro, card, hint], [title, intro, card], [title, intro]],
            has_media,
        )

    async def _parts(self, update, as_public: bool = False, panel: str = "manual"):
        """(text in HTML, markup, media file id, media kind) of the start screen for this person.
        panel "chooser" puts the AI / Manual buttons under it, "manual" the full panel of buttons."""
        uid = self.uid(update)
        await self.deps.access.ensure_loaded()
        cfg = await self.store.settings.get_start_screen()
        export_on = bool(cfg.get("public_export", True))
        first = (getattr(update.effective_user, "first_name", "") or "").strip() or "there"
        custom = (cfg.get("text") or "").strip()
        media, kind = SettingsStore.start_media(cfg)
        if self.deps.access.is_admin(uid) and not as_public:
            owner = self.deps.access.is_owner(uid)
            text = await self._admin_text(uid, first, custom, bool(media))
            markup = keyboards.mode_keyboard(await self._mode(uid)) if panel == "chooser" else keyboards.start_keyboard(owner)
        else:
            if custom:
                text = cards.esc(custom).replace("{name}", cards.esc(first))
            else:
                text = PUBLIC_DEFAULT if export_on else "🚫 This bot is only for its admin."
            markup = keyboards.public_keyboard(export_on)
        return text, markup, media, kind

    async def _render(self, update, context, panel: str = "manual") -> None:
        text, markup, media, kind = await self._parts(update, panel=panel)
        if media:
            await ui.show_media(update, context, media, kind, text, markup, "HTML")
        else:
            await ui.show(update, text, markup, "HTML")

    async def _show_keyboard(self, update, context, mode: str | None = None, force: bool = False) -> None:
        """Put the bottom menu under the chat (once per session and mode; /keyboard brings it back any time).
        AI mode gets its small keyboard, Manual (and an admin who never chose) the full quick menu."""
        mode = mode or await self._mode(self.uid(update)) or UI_MANUAL
        if context.user_data.get("kb") == mode and not force:
            return
        context.user_data["kb"] = mode
        if mode == UI_AI:
            text, markup = "⌨️ Seedha kuch bhi likho — main kar dunga. Menu neeche hai 👇", reply_menu.ai_keyboard()
        else:
            text, markup = "⌨️ Quick menu is on — use the buttons below 👇", reply_menu.keyboard(self.is_owner(update))
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=markup)

    async def start(self, update, context) -> None:
        await self.deps.access.ensure_loaded()
        user = update.effective_user
        if self.deps.access.is_admin(user.id):
            await self.deps.access.remember_name(user.id, user.full_name)
            chat = update.effective_chat
            mode = await self._mode(user.id)
            if chat is not None and chat.type == "private" and mode:
                with suppress(Exception):
                    await self._show_keyboard(update, context, mode)  # a mode was picked before: its menu stays
            await self._render(update, context, "chooser")  # AI / Manual
            return
        chat = update.effective_chat
        if chat is not None and chat.type != "private":
            return  # never chat with strangers in groups
        await self._render(update, context)

    async def home_screen(self, update, context) -> None:
        """Back / Cancel lead here: AI mode's home for somebody in AI mode, else the full panel."""
        uid = self.uid(update)
        if self.ai is not None and self.deps.access.is_admin(uid) and await self._mode(uid) == UI_AI:
            await self.ai.show_panel(update)
            return
        await self._render(update, context)

    async def switch_mode(self, update, context, mode: str) -> None:
        """AI / Manual picked (from /start, the panels or the bottom keyboard)."""
        if mode == "choose":
            await self._render(update, context, "chooser")
            return
        if mode not in (UI_AI, UI_MANUAL):
            return
        await self.store.settings.set_ai(self.uid(update), ui_mode=mode)
        chat = update.effective_chat
        if chat is not None and chat.type == "private":
            with suppress(Exception):
                await self._show_keyboard(update, context, mode, force=True)
        if mode == UI_AI and self.ai is not None:
            await self.ai.show_panel(update)
        else:
            await self._render(update, context)

    async def mode_button(self, update, context) -> None:
        """mode|ai, mode|manual, mode|choose."""
        if await self.entry(update):
            await self.switch_mode(update, context, self.arg(update))

    async def preview(self, update, context) -> None:
        """👁 Preview: the start screen exactly as a stranger sees it, as a new message."""
        if not await self.entry(update):
            return
        text, markup, media, kind = await self._parts(update, as_public=True)
        chat_id = update.effective_chat.id
        if media:
            sender = {"animation": context.bot.send_animation, "video": context.bot.send_video}.get(
                kind, context.bot.send_photo
            )
            await sender(chat_id, media, caption=text, reply_markup=markup, parse_mode="HTML")
        else:
            await context.bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

    async def menu_screen(self, update, context) -> None:
        await ui.show(update, "⚙️ Menu — pick what you want to do.", keyboards.menu_keyboard(self.is_owner(update)))

    async def help_screen(self, update, context) -> None:
        text = (
            "📖 All commands\n\nEvery command is a button here. Typed commands still work, for example:\n"
            "/all @user hello [follow] (join) 3sec Y\n"
            "/message 1 @user hello [follow]\n"
            "/click @bot [follow] (join)\n\n"
            "[name] or (name) at the end of a message = an inline button to press after it is sent."
        )
        await ui.show(update, text, keyboards.help_keyboard(self.is_owner(update)))

    async def help(self, update, context) -> None:
        if await self.entry(update):
            await self.help_screen(update, context)

    async def home(self, update, context) -> None:
        if await self.entry(update):
            await self.home_screen(update, context)

    async def keyboard(self, update, context) -> None:
        """/keyboard: bring the bottom menu back."""
        if await self.guard.allow(update):
            await self._show_keyboard(update, context, force=True)

    async def hide_keyboard(self, update, context) -> None:
        """/hidekeyboard: take the bottom menu away."""
        if not await self.guard.allow(update):
            return
        context.user_data.pop("kb", None)
        await update.effective_message.reply_text("⌨️ Quick menu hidden. /keyboard brings it back.", reply_markup=ReplyKeyboardRemove())

    async def emoji_id(self, update, context) -> None:
        """/emojiid: how to find a custom emoji's ID."""
        if await self.guard.allow(update):
            await update.effective_message.reply_text(
                "🔎 Send me a message that contains custom emoji (Telegram Premium ones) and I reply with their IDs."
            )

    async def menu(self, update, context) -> None:
        if await self.entry(update):
            await self.menu_screen(update, context)

    async def cancel(self, update, context) -> None:
        await self.deps.access.ensure_loaded()
        if not self.deps.access.is_admin(self.uid(update)):
            # A stranger can only be inside the session export; the middleware already closed it.
            await ui.show(update, "❌ Cancelled.", keyboards.public_keyboard(True))
            return
        stopped = self.deps.bulk.cancel(self.uid(update))
        text = "❌ Cancelled." + (" The running bulk action is being stopped." if stopped else "")
        await ui.show(update, text, keyboards.back("menu", "⚙️ Menu"))

    async def moved(self, update, context) -> None:
        """/delay and /onalltime were replaced by step-by-step buttons."""
        if not await self.guard.allow(update):
            return
        command = update.effective_message.text.split()[0].split("@")[0]
        if command == "/onalltime":
            text = "🕒 The daily broadcast now lives in the menu, with one question at a time."
            markup = keyboards.back("sched|home", "🕒 Open daily broadcasts")
        else:
            text = "⏱ The delay is now asked every time you run /all, /alll or /allblock."
            markup = keyboards.back("menu", "⚙️ Menu")
        await ui.show(update, text, markup)
