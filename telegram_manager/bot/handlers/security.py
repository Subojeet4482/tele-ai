"""⚙️ Settings (everything important in one place), 🔐 account security (2-step password, other devices,
spam-limit check, privacy) and 📢 channels / groups an account owns. The AI does the same through its tools."""
from __future__ import annotations

import asyncio
import logging

from telegram_manager.ai import account_tools
from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase
from telegram_manager.bot.keyboards import Btn, _markup
from telegram_manager.core import security
from telegram_manager.errors import describe_error

logger = logging.getLogger(__name__)

PRIVACY_PRESETS = (
    ("📞 Hide phone number", "phone", "nobody"),
    ("🕶 Hide last seen", "last_seen", "nobody"),
    ("👥 Only contacts can add to groups", "groups", "contacts"),
    ("🖼 Profile photo: contacts only", "photo", "contacts"),
)
MAX_CHAT_BUTTONS = 15
ROWS_KEY = "sch_rows"


def settings_keyboard(is_owner: bool):
    rows = [
        [Btn("🤖 AI settings", callback_data="ai|home"), Btn("🔑 AI keys", callback_data="ai|keys")],
        [Btn("📝 AI prompt", callback_data="ai|prompts"), Btn("🧩 Confirm / Auto", callback_data="ai|mode")],
        [Btn("📥 Forwarding", callback_data="fw|home"), Btn("🕒 Daily broadcasts", callback_data="sched|home")],
        [Btn("🔐 Account security", callback_data="sec|home"), Btn("📢 Channels / groups", callback_data="sch|home")],
        [Btn("🩺 Health check (all)", callback_data="sec|check"), Btn("🔄 Refresh names", callback_data="acc_refresh")],
        [Btn("🎨 Start screen", callback_data="sc|home"), Btn("🎁 Give accounts", callback_data="give|start")],
        [Btn("📤 Export session", callback_data="exp|pick"), Btn("🔗 Connect account", callback_data="connect")],
    ]
    if is_owner:
        rows.append([Btn("👑 Admins", callback_data="adm|home"), Btn("📜 Live logs", callback_data="logs|link")])
    rows.append([Btn("⬅️ Back", callback_data="mode|choose")])
    return _markup(rows)


class SecurityHandlers(HandlerBase):
    # --- ⚙️ Settings -----------------------------------------------------------------------
    async def settings_command(self, update, context) -> None:
        if await self.entry(update):
            await self.settings_screen(update)

    async def settings_screen(self, update) -> None:
        text = (
            "⚙️ Settings\n\n"
            "Everything important is here: the AI (settings, keys, prompt, confirm / auto), forwarding, daily broadcasts, "
            "account security (2-step password, devices, privacy), channels, health check, start screen and more."
        )
        await ui.show(update, text, settings_keyboard(self.is_owner(update)))

    # --- 🔐 account security -----------------------------------------------------------------
    async def security_command(self, update, context) -> None:
        if await self.entry(update):
            await self.security_picker(update)

    async def security_picker(self, update) -> None:
        accounts = await self.accounts_or_notice(update)
        if accounts is None:
            return
        await ui.show(
            update,
            "🔐 Account security\n\nPick an account: 2-step password, other devices, spam-limit check and privacy.",
            keyboards.accounts_picker(accounts, "sec|acc", self.manager.is_online, "set|home", "🔐"),
        )

    async def sec_button(self, update, context) -> None:
        """sec|home · sec|check · sec|acc|<id> · sec|tfa|<action>|<id> · sec|termq|<id> · sec|term|<id> · sec|spam|<id> · sec|priv|<id> · sec|pv|<n>|<id>"""
        if not await self.entry(update):
            return
        parts = self.arg(update).split("|")
        action, rest = parts[0], parts[1:]
        if action == "home":
            await self.security_picker(update)
        elif action == "check":
            await self._check_all(update)
        elif action == "tfa" and len(rest) == 2 and rest[0] in ("enable", "change", "disable"):
            if await self._online_account(update, rest[1]) is not None:
                await self.engine.start(update, context, "tfa", {"accounts": [rest[1]], "action": rest[0]})
        elif action in ("acc", "termq", "term", "spam", "priv") and rest:
            account = await self._online_account(update, rest[-1])
            if account is not None:
                await getattr(self, f"_{action}")(update, account)
        elif action == "pv" and len(rest) == 2:
            account = await self._online_account(update, rest[1])
            if account is not None:
                await self._apply_privacy(update, account, rest[0])

    async def _online_account(self, update, account_id: str) -> dict | None:
        account = await self.own_account(update, account_id, "sec|home")
        if account is None:
            return None
        if not self.manager.is_online(str(account["id"])):
            await ui.show(update, "🔴 This account is offline right now.", keyboards.back("sec|home"))
            return None
        return account

    def _name(self, account: dict) -> str:
        return f"{account.get('number')}. {account.get('display_name') or 'Unnamed'}"

    async def _acc(self, update, account: dict, note: str = "") -> None:
        account_id, uid = str(account["id"]), self.uid(update)
        try:
            two = await self.manager.two_fa_status(account_id, uid)
            sessions = await self.manager.list_sessions(account_id, uid)
        except Exception as exc:
            await ui.show(update, f"❌ Could not read the security of {self._name(account)}: {describe_error(exc)}", keyboards.back("sec|home"))
            return
        others = [s for s in sessions if not s["current"]]
        lines = [
            f"🔐 {self._name(account)}",
            "",
            "2-step verification: " + ("ON ✅" + (f" (hint: {two['hint']})" if two["hint"] else "") if two["enabled"] else "OFF ⚠️"),
            f"Devices: {len(others)} other device(s) + this bot",
        ]
        lines += [security.session_line(s) for s in others[:8]]
        if note:
            lines += ["", note]
        rows = [
            [Btn("🔁 Change password", callback_data=f"sec|tfa|change|{account_id}"), Btn("🔓 Turn OFF", callback_data=f"sec|tfa|disable|{account_id}")]
            if two["enabled"]
            else [Btn("🔐 Turn 2-step ON", callback_data=f"sec|tfa|enable|{account_id}", style="success")],
        ]
        if others:
            rows.append([Btn(f"🚫 End other devices ({len(others)})", callback_data=f"sec|termq|{account_id}", style="danger")])
        rows += [
            [Btn("🩺 Spam-limit check", callback_data=f"sec|spam|{account_id}"), Btn("🔒 Privacy", callback_data=f"sec|priv|{account_id}")],
            [Btn("📢 Channels / groups", callback_data=f"sch|acc|{account_id}"), Btn("🔄 Refresh", callback_data=f"sec|acc|{account_id}")],
            [Btn("⬅️ Back", callback_data="sec|home")],
        ]
        await ui.show(update, "\n".join(lines), _markup(rows))

    async def _termq(self, update, account: dict) -> None:
        account_id = str(account["id"])
        await ui.show(
            update,
            f"🚫 End ALL other devices of {self._name(account)}?\n\nEvery phone / computer except this bot is logged out. "
            "The owner of the account has to log in again there.",
            keyboards.confirm_pair(f"sec|term|{account_id}", f"sec|acc|{account_id}", "✅ End them"),
        )

    async def _term(self, update, account: dict) -> None:
        try:
            result = await self.manager.terminate_sessions(str(account["id"]), self.uid(update))
        except Exception as exc:
            await self._acc(update, account, f"❌ {describe_error(exc)}")
            return
        note = f"✅ {result['ended']} device(s) logged out."
        if result["errors"]:
            note += "\n⚠️ " + "; ".join(result["errors"][:3])
        await self._acc(update, account, note)

    async def _spam(self, update, account: dict) -> None:
        await ui.show(update, f"🩺 Checking {self._name(account)} (asks @SpamBot)…")
        try:
            await self.manager.check_account(self.uid(update), str(account["id"]), True)
        except Exception as exc:
            await self._acc(update, account, f"❌ Check failed: {describe_error(exc)}")
            return
        row = await self.store.accounts.get(str(account["id"]), owner_id=self.uid(update)) or account
        view = account_tools.view_of(row, self.manager.account_health(row))
        await self._acc(update, account, "🩺 " + account_tools.line(view))

    async def _priv(self, update, account: dict) -> None:
        account_id = str(account["id"])
        rows = [[Btn(label, callback_data=f"sec|pv|{i}|{account_id}")] for i, (label, _k, _w) in enumerate(PRIVACY_PRESETS)]
        rows.append([Btn("⬅️ Back", callback_data=f"sec|acc|{account_id}")])
        await ui.show(update, f"🔒 Privacy of {self._name(account)}\n\nTap to apply:", _markup(rows))

    async def _apply_privacy(self, update, account: dict, index: str) -> None:
        if not index.isdigit() or int(index) >= len(PRIVACY_PRESETS):
            return
        label, key, who = PRIVACY_PRESETS[int(index)]
        try:
            await self.manager.set_privacy(str(account["id"]), self.uid(update), key, who)
        except Exception as exc:
            await self._acc(update, account, f"❌ {describe_error(exc)}")
            return
        await self._acc(update, account, f"✅ {label}")

    async def _check_all(self, update) -> None:
        accounts = await self.accounts_or_notice(update)
        if accounts is None:
            return
        await ui.show(update, f"🩺 Checking {len(accounts)} account(s)… this can take a while.")
        gate, uid = asyncio.Semaphore(5), self.uid(update)

        async def one(row: dict) -> None:
            async with gate:
                try:
                    await self.manager.check_account(uid, str(row["id"]), True)
                except Exception as exc:
                    logger.warning("Health check failed: %s", describe_error(exc))

        await asyncio.gather(*(one(row) for row in accounts))
        rows = await self.store.accounts.list(uid)
        views = account_tools.sort_views([account_tools.view_of(r, self.manager.account_health(r)) for r in rows], "status")
        blocks = account_tools.render(views, title="Health check", sort_by="status", group_by="status")
        await ui.show(update, blocks[0] if blocks else "No accounts.", keyboards.back("set|home", "⚙️ Settings"))
        for extra in blocks[1:]:
            await update.effective_chat.send_message(extra)

    # --- 📢 channels / groups ------------------------------------------------------------------
    async def channels_command(self, update, context) -> None:
        if not await self.entry(update):
            return
        await self._channel_picker(update)

    async def _channel_picker(self, update) -> None:
        accounts = await self.accounts_or_notice(update)
        if accounts is None:
            return
        await ui.show(
            update,
            "📢 Channels / groups\n\nPick an account to see the channels and groups it OWNS (you can delete them).",
            keyboards.accounts_picker(accounts, "sch|acc", self.manager.is_online, "set|home", "📢"),
        )

    async def sch_button(self, update, context) -> None:
        """sch|home · sch|acc|<id> · sch|dq|<id>|<n> · sch|d|<id>|<n>"""
        if not await self.entry(update):
            return
        parts = self.arg(update).split("|")
        action = parts[0]
        if action == "home":
            await self._channel_picker(update)
            return
        account = await self._online_account(update, parts[1] if len(parts) > 1 else "")
        if account is None:
            return
        account_id = str(account["id"])
        if action == "acc":
            await self._owned(update, context, account)
            return
        rows = context.user_data.get(ROWS_KEY, {}).get(account_id, [])
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        if not 0 <= index < len(rows):
            await self._owned(update, context, account, "⚠️ That list is old, here is the fresh one.")
            return
        row = rows[index]
        if action == "dq":
            await ui.show(
                update,
                f"🗑 DELETE the {row['kind']} “{row['title']}” for EVERYONE?\n\nThis cannot be undone.",
                keyboards.confirm_pair(f"sch|d|{account_id}|{index}", f"sch|acc|{account_id}", "🗑 Delete it"),
            )
        elif action == "d":
            try:
                await self.manager.delete_owned_chat(account_id, self.uid(update), str(row["id"]))
            except Exception as exc:
                await self._owned(update, context, account, f"❌ {describe_error(exc)}")
                return
            await self._owned(update, context, account, f"🗑 “{row['title']}” deleted.")

    async def _owned(self, update, context, account: dict, note: str = "") -> None:
        account_id = str(account["id"])
        try:
            rows = await self.manager.list_chats_of(account_id, self.uid(update), "all", owned_only=True)
        except Exception as exc:
            await ui.show(update, f"❌ {describe_error(exc)}", keyboards.back("sch|home"))
            return
        context.user_data.setdefault(ROWS_KEY, {})[account_id] = rows[:MAX_CHAT_BUTTONS]
        lines = [f"📢 {self._name(account)} owns {len(rows)} channel(s)/group(s)" + (f" (first {MAX_CHAT_BUTTONS} shown)" if len(rows) > MAX_CHAT_BUTTONS else "")]
        if note:
            lines += ["", note]
        buttons = [[Btn(f"🗑 {r['title'][:30]} · {r['kind']}", callback_data=f"sch|dq|{account_id}|{i}")] for i, r in enumerate(rows[:MAX_CHAT_BUTTONS])]
        buttons.append([Btn("⬅️ Back", callback_data="sch|home")])
        await ui.show(update, "\n".join(lines), _markup(buttons))
