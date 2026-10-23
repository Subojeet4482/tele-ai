"""Every inline keyboard in one place. Callback data prefixes:

home / menu            main screens
connect, cx|otp|session  connecting an account
acc_list, acc_refresh, acc|<id>   accounts list, refresh names, one account's screen
logout, lo|<id>, loc|<id>   logout picker / confirm / do it
fw|...                 forwarding switches
bulk|<kind>, msg|start, msg_acc|<id>, pf|name|photo|bio|<id>   start a wizard from a button
sched|home|new|off|view|edit|del, adm|home|add|remove, logs|link
give|start|acc|<id>    hand accounts to the owner / an admin
un|set|rm|<id>         set or remove an account's @username
exp|pick|ask|go|<id>   export a session string of an own account
exp|multi              export several accounts' sessions at once, as a .txt file
expr|<token>           owner/admin taps Revoke on a "a session was exported" notification
sc|home|text|photo|rmphoto|reset|preview|pub   start screen settings
mode|ai|manual|choose  the two modes picked on /start (AI = just write, Manual = the buttons)
aiq|<token>|<n>        a button the AI asked as a question (its tap goes back to the AI)
ex|start               public session export (anybody); help = all commands as buttons
wz|<wizard>|<action>|<arg>   inside step-by-step flows (see bot/wizard)
"""
from __future__ import annotations

import inspect

from telegram import InlineKeyboardButton as _Button
from telegram import InlineKeyboardMarkup as Markup
from telegram import KeyboardButton as _KeyButton
from telegram import ReplyKeyboardMarkup as _ReplyMarkup

from telegram_manager.parsing import format_clock

# Telegram (Bot API 9.4+, Feb 2026) can colour buttons: primary = blue, success = green, danger = red,
# and show a custom emoji in front of the text (`icon_custom_emoji_id`, needs the bot owner to have
# Telegram Premium). This works on inline buttons AND on the bottom reply keyboard.
# python-telegram-bot 21.6 has no such arguments yet, so they go through api_kwargs; newer versions
# take them natively. Old Telegram apps just ignore both.
_DANGER = ("🗑", "🚪", "🛑", "❌", "➖", "🚫")
_SUCCESS = ("✅", "➕", "🔗")
_PRIMARY = ("⚙️", "📋", "🕒", "🎁", "📤", "🎨", "📖", "📨", "🖱", "📥", "👥", "👑", "📜")

# leading emoji -> custom emoji id, filled from BUTTON_ICONS at startup (empty = plain emoji everywhere)
_ICONS: dict[str, str] = {}


def _norm(token: str) -> str:
    return token.replace("\ufe0f", "")


def configure_icons(mapping: dict[str, str] | None) -> None:
    """Use a custom emoji in place of the leading emoji of every button that has one."""
    _ICONS.clear()
    for emoji, emoji_id in (mapping or {}).items():
        if emoji and emoji_id:
            _ICONS[_norm(str(emoji))] = str(emoji_id)


def strip_icon(text: str) -> str:
    """The button text as Telegram shows it: without the leading emoji when a custom icon replaces it."""
    head, _, rest = text.partition(" ")
    return rest if rest and _norm(head) in _ICONS else text


def _icon_for(text: str) -> str | None:
    head, _, rest = text.partition(" ")
    return _ICONS.get(_norm(head)) if rest else None


def _auto_style(text: str) -> str | None:
    if text.startswith("⬅️") or text.startswith("◀️") or text.startswith("Next"):
        return None
    for icons, style in ((_DANGER, "danger"), (_SUCCESS, "success"), (_PRIMARY, "primary")):
        if any(text.startswith(icon) for icon in icons):
            return style
    return None


def _extra(cls, fields: dict, kwargs: dict) -> dict:
    """Pass style / icon_custom_emoji_id as real arguments when the library knows them, else via api_kwargs."""
    fields = {key: value for key, value in fields.items() if value}
    if not fields:
        return kwargs
    known = inspect.signature(cls.__init__).parameters
    kwargs = {**kwargs, **{key: value for key, value in fields.items() if key in known}}
    rest = {key: value for key, value in fields.items() if key not in known}
    if rest:
        kwargs["api_kwargs"] = {**kwargs.get("api_kwargs", {}), **rest}
    return kwargs


def Btn(text: str, callback_data: str | None = None, style: str | None = None, **kwargs):  # noqa: N802
    """An inline button; the colour follows the leading emoji unless `style` is given."""
    style = style or _auto_style(text)
    icon = _icon_for(text)
    shown = strip_icon(text) if icon else text
    return _Button(shown, callback_data=callback_data, **_extra(_Button, {"style": style, "icon_custom_emoji_id": icon}, kwargs))


def KBtn(text: str, style: str | None = None, **kwargs):  # noqa: N802
    """A button of the bottom (reply) keyboard. Tapping it sends its text as a message."""
    style = style or _auto_style(text)
    icon = _icon_for(text)
    shown = strip_icon(text) if icon else text
    return _KeyButton(shown, **_extra(_KeyButton, {"style": style, "icon_custom_emoji_id": icon}, kwargs))


def reply_keyboard(rows: list[list], placeholder: str | None = None):
    """A bottom keyboard that stays on screen until it is replaced or removed."""
    return _ReplyMarkup(rows, resize_keyboard=True, is_persistent=True, input_field_placeholder=placeholder)


BULK_TITLES = {
    "all": "📨 Message all accounts",
    "alll": "📤 Message all (opens chat)",
    "block": "🚫 Block on all accounts",
    "multi": "👥 Message chosen accounts",
    "click": "🖱 Click buttons",
}


def _markup(rows: list[list[Btn]]) -> Markup:
    return Markup(rows)


def back(to: str = "home", label: str = "⬅️ Back") -> Markup:
    return _markup([[Btn(label, callback_data=to)]])


def mode_keyboard(current: str = "") -> Markup:
    """The three big buttons under /start: Manual (the buttons panel), Settings, AI (just write normal messages)."""
    def mark(name: str, mode: str) -> tuple[str, str]:
        return (name + (" ✅" if current == mode else ""), "success" if current == mode else "primary")

    ai_text, ai_style = mark("🤖 AI Mode", "ai")
    manual_text, manual_style = mark("🖐 Manual Mode", "manual")
    return _markup([
        [Btn(manual_text, callback_data="mode|manual", style=manual_style)],
        [Btn("⚙️ Settings", callback_data="set|home")],
        [Btn(ai_text, callback_data="mode|ai", style=ai_style)],
    ])


def ai_panel_keyboard() -> Markup:
    """The home of AI mode: settings, keys, prompt, its chats, and the way back to Manual."""
    return _markup([
        [Btn("⚙️ AI Settings", callback_data="ai|home"), Btn("🔑 Keys", callback_data="ai|keys")],
        [Btn("📝 Prompt", callback_data="ai|prompts"), Btn("💬 AI chats", callback_data="ai|chats")],
        [Btn("🧹 Nayi baat", callback_data="ai|newchat"), Btn("🖐 Manual Mode", callback_data="mode|manual")],
        [Btn("⚙️ Settings", callback_data="set|home"), Btn("🔀 Mode chuno", callback_data="mode|choose")],
    ])


def start_keyboard(is_owner: bool) -> Markup:
    rows = [
        [Btn("🔗 Connect account", callback_data="connect", style="success"), Btn("📋 My accounts", callback_data="acc_list")],
        [Btn("🎁 Give accounts", callback_data="give|start"), Btn("📤 Export session", callback_data="exp|pick")],
        [Btn("📦 Export multiple (.txt)", callback_data="exp|multi")],
        [Btn("🕒 Daily broadcasts", callback_data="sched|home"), Btn("🎨 Start screen", callback_data="sc|home")],
        [Btn("🚪 Logout account", callback_data="logout")],
    ]
    if is_owner:
        rows.append([Btn("👑 Manage admins", callback_data="adm|home")])
    rows.append([Btn("🤖 AI Mode", callback_data="mode|ai"), Btn("🔀 Mode chuno", callback_data="mode|choose")])
    rows.append([Btn("⚙️ Settings", callback_data="set|home"), Btn("🔐 Account security", callback_data="sec|home")])
    rows.append([Btn("⚙️ Menu", callback_data="menu"), Btn("📖 All commands", callback_data="help")])
    return _markup(rows)


def public_keyboard(export_on: bool) -> Markup:
    """What somebody who is not an admin sees under /start."""
    rows = []
    if export_on:
        rows.append([Btn("📤 Export my session", callback_data="ex|start", style="primary")])
    return _markup(rows) if rows else None


def help_keyboard(is_owner: bool) -> Markup:
    """Every command as a button, so nothing has to be typed."""
    rows = [
        [Btn(BULK_TITLES["all"], callback_data="bulk|all"), Btn(BULK_TITLES["alll"], callback_data="bulk|alll")],
        [Btn(BULK_TITLES["multi"], callback_data="bulk|multi"), Btn(BULK_TITLES["click"], callback_data="bulk|click")],
        [Btn(BULK_TITLES["block"], callback_data="bulk|block"), Btn("💬 Message (one account)", callback_data="msg|start")],
        [Btn("📋 /list", callback_data="acc_list"), Btn("🔄 /refresh", callback_data="acc_refresh")],
        [Btn("📥 Forwarding switches", callback_data="fw|home"), Btn("🕒 /broadcast", callback_data="sched|home")],
        [Btn("🎁 /give", callback_data="give|start"), Btn("📤 /export", callback_data="exp|pick")],
        [Btn("📦 Export multiple", callback_data="exp|multi")],
        [Btn("🎨 /startscreen", callback_data="sc|home"), Btn("🔗 Connect", callback_data="connect")],
        [Btn("🤖 /aisettings", callback_data="ai|home")],
    ]
    if is_owner:
        rows.append([Btn("👑 Admins", callback_data="adm|home"), Btn("📜 /logs", callback_data="logs|link")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return _markup(rows)


def menu_keyboard(is_owner: bool) -> Markup:
    rows = [
        # short labels: two buttons share a row on a phone, so long ones were cut off
        [Btn("📨 Msg all accounts", callback_data="bulk|all"), Btn("📤 Msg all (new chat)", callback_data="bulk|alll")],
        [Btn("👥 Msg chosen accs", callback_data="bulk|multi"), Btn(BULK_TITLES["click"], callback_data="bulk|click")],
        [
            Btn("🚫 Block on all", callback_data="bulk|block"),
            Btn("💬 Msg from 1 acc", callback_data="msg|start"),
        ],
        [Btn("📥 Forwarding", callback_data="fw|home"), Btn("🕒 Daily broadcast", callback_data="sched|home")],
        [Btn("📋 My accounts", callback_data="acc_list"), Btn("🔄 Refresh names", callback_data="acc_refresh")],
        [Btn("🎁 Give accounts", callback_data="give|start"), Btn("📤 Export session", callback_data="exp|pick")],
        [Btn("📦 Export multiple (.txt)", callback_data="exp|multi")],
        [Btn("🎨 Start screen", callback_data="sc|home"), Btn("📖 All commands", callback_data="help")],
        [Btn("🤖 AI manager", callback_data="ai|home")],
    ]
    if is_owner:
        rows.append([Btn("📜 Live logs", callback_data="logs|link")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return _markup(rows)


def connect_keyboard() -> Markup:
    return _markup([
        [Btn("📱 Phone number + code", callback_data="cx|otp")],
        [Btn("🔑 Any session string (auto-detect)", callback_data="cx|session")],
        [Btn("⬅️ Back", callback_data="home")],
    ])


def after_connect() -> Markup:
    return _markup([[Btn("📋 My accounts", callback_data="acc_list"), Btn("⚙️ Menu", callback_data="menu")]])


def accounts_picker(accounts: list[dict], prefix: str, online, back_to: str, icon: str = "📱") -> Markup:
    """One button per account; `prefix|<id>` is sent back. online(id) -> bool."""
    rows = []
    for account in accounts:
        dot = "🟢" if online(str(account["id"])) else "🔴"
        name = str(account.get("display_name") or "Unnamed")[:28]
        rows.append([Btn(f"{icon} {account.get('number')}. {name} {dot}", callback_data=f"{prefix}|{account['id']}")])
    rows.append([Btn("⬅️ Back", callback_data=back_to)])
    return _markup(rows)


def accounts_list(accounts: list[dict], online) -> Markup:
    rows = [
        [Btn(f"📱 {a.get('number')}. {str(a.get('display_name') or 'Unnamed')[:28]} "
             f"{'🟢' if online(str(a['id'])) else '🔴'}", callback_data=f"acc|{a['id']}")]
        for a in accounts
    ]
    rows.append([Btn("🔄 Refresh", callback_data="acc_refresh"), Btn("🔗 Connect account", callback_data="connect")])
    rows.append([Btn("🎁 Give accounts", callback_data="give|start"), Btn("📦 Export multiple", callback_data="exp|multi")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return _markup(rows)


def account_screen(account_id: str, labels: dict[str, str], back_to: str = "acc_list", has_username: bool = False) -> Markup:
    """labels: {"u": "...", "b": "...", "c": "..."} button texts for the three forwarding switches."""
    username_row = [Btn("🔤 Change username" if has_username else "🔤 Add username", callback_data=f"un|set|{account_id}")]
    if has_username:
        username_row.append(Btn("🗑 Remove username", callback_data=f"un|rm|{account_id}"))
    return _markup([
        [Btn(labels["u"], callback_data=f"fw|set|{account_id}|u")],
        [Btn(labels["b"], callback_data=f"fw|set|{account_id}|b")],
        [Btn(labels["c"], callback_data=f"fw|set|{account_id}|c")],
        [Btn("💬 Message from this account", callback_data=f"msg_acc|{account_id}")],
        [
            Btn("✏️ Name", callback_data=f"pf|name|{account_id}"),
            Btn("🖼 Photo", callback_data=f"pf|photo|{account_id}"),
            Btn("📝 Bio", callback_data=f"pf|bio|{account_id}"),
        ],
        username_row,
        [
            Btn("📤 Export session", callback_data=f"exp|ask|{account_id}"),
            Btn("🎁 Give account", callback_data=f"give|acc|{account_id}"),
        ],
        [Btn("🚪 Logout this account", callback_data=f"lo|{account_id}")],
        [Btn("⬅️ Back", callback_data=back_to)],
    ])


def logout_confirm(account_id: str) -> Markup:
    return _markup([
        [Btn("✅ Yes, log it out", callback_data=f"loc|{account_id}")],
        [Btn("⬅️ Back", callback_data="logout")],
    ])


def forwarding_home(settings: dict[str, bool]) -> Markup:
    def label(title: str, on: bool) -> str:
        return f"{title}: {'🟢 ON' if on else '🔴 OFF'}"

    return _markup([
        [Btn(label("👤 User messages", settings["forward_user_messages"]), callback_data="fw|g|u")],
        [Btn(label("🤖 Bot messages", settings["forward_bot_messages"]), callback_data="fw|g|b")],
        [Btn(label("📢 Channel & group messages", settings["forward_channel_messages"]), callback_data="fw|g|c")],
        [Btn("📱 Per-account settings", callback_data="fw|accs")],
        [Btn("⬅️ Back", callback_data="menu")],
    ])


def schedule_home(jobs=None, can_add: bool = True) -> Markup:
    """jobs: [(slot, short label)]. One button per broadcast, then New / Turn all off."""
    if isinstance(jobs, bool):  # older callers passed "has a job"
        jobs = [(1, "Broadcast #1")] if jobs else []
    rows = [[Btn(f"⏰ #{slot} · {label}"[:60], callback_data=f"sched|view|{slot}")] for slot, label in (jobs or [])]
    if can_add:
        rows.append([Btn("➕ New broadcast", callback_data="sched|new")])
    if jobs:
        rows.append([Btn("🗑 Turn all off", callback_data="sched|off")])
    rows.append([Btn("⬅️ Back", callback_data="menu")])
    return _markup(rows)


def schedule_job(slot: int) -> Markup:
    return _markup([
        [Btn("✏️ Replace this broadcast", callback_data=f"sched|edit|{slot}")],
        [Btn("🗑 Delete this broadcast", callback_data=f"sched|del|{slot}")],
        [Btn("⬅️ All broadcasts", callback_data="sched|home")],
    ])


def startcfg_home(public_export: bool, has_photo: bool) -> Markup:
    rows = [
        [Btn("✏️ Welcome text", callback_data="sc|text"), Btn("🖼 Photo / GIF / Video", callback_data="sc|photo")],
    ]
    if has_photo:
        rows.append([Btn("🗑 Remove media", callback_data="sc|rmphoto")])
    rows.append([Btn(f"📤 Public session export: {'🟢 ON' if public_export else '🔴 OFF'}", callback_data="sc|pub")])
    rows.append([Btn("👁 Preview", callback_data="sc|preview"), Btn("♻️ Reset to default", callback_data="sc|reset")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return _markup(rows)


def export_confirm(account_id: str) -> Markup:
    return _markup([
        [Btn("✅ Yes, show the session", callback_data=f"exp|go|{account_id}")],
        [Btn("⬅️ Back", callback_data=f"acc|{account_id}")],
    ])


def revoke_confirm(token: str) -> Markup:
    """Attached to the 'a session was exported' notification sent to the owner/admin."""
    return _markup([[Btn("🗑 Revoke this session", callback_data=f"expr|{token}", style="danger")]])


def admins_home(can_remove: bool) -> Markup:
    rows = [[Btn("➕ Add admin", callback_data="adm|add")]]
    if can_remove:
        rows.append([Btn("➖ Remove admin", callback_data="adm|remove")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return _markup(rows)


def after_action(again_label: str, again_data: str) -> Markup:
    return _markup([[Btn(again_label, callback_data=again_data), Btn("⚙️ Menu", callback_data="menu")]])


def confirm_pair(yes_data: str, no_data: str, yes_label: str = "✅ Confirm") -> Markup:
    return _markup([[Btn(yes_label, callback_data=yes_data, style="success"), Btn("❌ Cancel", callback_data=no_data, style="danger")]])


def stop_bulk() -> Markup:
    return _markup([[Btn("🛑 Stop", callback_data="stop_bulk")]])


def no_accounts() -> Markup:
    return _markup([[Btn("🔗 Connect account", callback_data="connect")], [Btn("⬅️ Back", callback_data="home")]])


def schedule_overview(manager, owner_id: int) -> Markup:
    """The daily-broadcast screen's buttons for this admin's current broadcasts."""
    jobs = [
        (slot, f"{job.target} · {format_clock(job.start_time)}")
        for slot, job in manager.daily_jobs_for(owner_id)
    ]
    return schedule_home(jobs, can_add=manager.free_daily_slot(owner_id) is not None)
