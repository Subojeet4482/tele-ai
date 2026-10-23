"""Builds the whole bot (real handlers, wizards, store over a fake Firestore, real manager
with fake Telethon clients) and lets tests act as different Telegram users."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import stubs  # noqa: E402,F401  (must come before the telegram_manager imports)
from tests.fakes import FakeBot, FakeContext, FakeDB, FakeUpdate  # noqa: E402

from telegram_manager.access import AccessControl  # noqa: E402
from telegram_manager.bot.bulk_runner import BulkRunner  # noqa: E402
from telegram_manager.bot.deps import Deps  # noqa: E402
from telegram_manager.bot.guard import Guard  # noqa: E402
from telegram_manager.bot.registry import wire  # noqa: E402
from telegram_manager.core.manager import TelethonManager  # noqa: E402
from telegram_manager.store import Store  # noqa: E402
from telegram_manager.web.access import LogAccess  # noqa: E402

OWNER, BOB, STRANGER = 100, 200, 999


def patch_sleep(case, on_sleep) -> None:
    """Make the bulk sender's waits instant; on_sleep(seconds) is told how long it would have waited."""
    import telegram_manager.core.messaging as messaging

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *args):
        on_sleep(seconds)
        await real_sleep(0)

    patcher = mock.patch.object(messaging, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": fake_sleep}))
    patcher.start()
    case.addCleanup(patcher.stop)


def _fake_get_file(data: bytes):
    async def get_file():
        return SimpleNamespace(download_as_bytearray=_async_value(bytearray(data)))
    return get_file


def _async_value(value):
    async def inner():
        return value
    return inner


class FakeClient:
    def __init__(self):
        self.connected = True
        self.me = None  # what get_me() returns (tests set it)

    def is_connected(self):
        return self.connected

    async def get_me(self):
        return self.me


class Harness:
    def __init__(self):
        self.db = FakeDB()
        self.store = Store("", None, OWNER, db=self.db)
        self.bot = FakeBot()
        self.manager = TelethonManager(self.store, None, self.bot, OWNER, "UTC")
        self.access = AccessControl(self.store, OWNER)
        self.settings = SimpleNamespace(owner_id=OWNER, public_url="https://bot.example")
        self.log_access = LogAccess()
        self.deps = Deps(
            settings=self.settings, store=self.store, manager=self.manager, access=self.access,
            guard=Guard(self.access), bulk=BulkRunner(self.manager, self.store),
            log_buffer=object(), log_access=self.log_access,
        )
        self.parts = wire(self.deps)
        self.contexts: dict[int, FakeContext] = {}
        self.calls: list[tuple] = []  # what the fake Telethon layer was asked to do
        self.sleeps: list[float] = []

    def ctx(self, user_id: int) -> FakeContext:
        return self.contexts.setdefault(user_id, FakeContext(self.bot))

    async def add_account(self, owner: int, name: str, online: bool = True) -> dict:
        row = await self.store.accounts.create(owner, {"display_name": name, "telegram_user_id": abs(hash(name)) % 10**9,
                                                        "enabled": True})
        self.manager.accounts[row["id"]] = row
        client = FakeClient()
        client.connected = online
        self.manager.clients[row["id"]] = client
        return row

    def record_telethon(self, case) -> None:
        """Replace the Telethon-touching methods with recorders."""
        manager = self.manager

        async def send_existing(account_id, target, text, owner_id=None, buttons=None):
            self.calls.append(("send_existing", account_id, target, text, owner_id) + ((buttons,) if buttons else ()))

        async def send_any(account_id, target, text, owner_id=None, buttons=None):
            self.calls.append(("send_any", account_id, target, text, owner_id) + ((buttons,) if buttons else ()))

        async def block(account_id, target, owner_id=None):
            self.calls.append(("block", account_id, target, owner_id))

        manager.send_to_existing_dialog = send_existing
        manager.send_to_any = send_any
        manager.block_on_account = block
        patch_sleep(case, self.sleeps.append)

    # --- acting as a user ----------------------------------------------------------------
    async def _run(self, user_id: int, update, handler) -> FakeUpdate:
        context = self.ctx(user_id)
        await self.parts.middleware.track_update(update, context)
        await handler(update, context)
        return update

    async def command(self, user_id: int, text: str) -> FakeUpdate:
        name = text.split()[0].lstrip("/")
        p = self.parts
        handlers = {
            "start": p.start.start, "menu": p.start.menu, "cancel": p.start.cancel, "list": p.accounts.list_accounts,
            "message": p.commands.message_command, "multi": p.commands.multi_command, "logs": p.logs.logs, "delay": p.start.moved,
            "onalltime": p.start.moved, "refresh": p.accounts.refresh, "broadcast": p.schedule.home,
            "give": p.control.give_start, "export": p.control.export_command, "startscreen": p.control.startcfg_command,
            "help": p.control.help_command, "keyboard": p.start.keyboard, "hidekeyboard": p.start.hide_keyboard,
            "emojiid": p.start.emoji_id,
            "setapi": p.extras.setapi, "send": p.extras.send_command, "unsend": p.extras.unsend,
            "delchat": p.extras.delchat, "whois": p.extras.whois, "import": p.extras.import_command,
            "ai": p.ai.ai_command, "aisettings": p.ai.settings_command, "aichats": p.ai.chats_command, "aiclear": p.ai.clear_command,
            "settings": p.security.settings_command, "security": p.security.security_command,
            "channels": p.security.channels_command,
        }
        for kind in ("all", "alll", "allblock", "click"):
            handlers[kind] = self._bulk_handler("block" if kind == "allblock" else kind)
        for command, key in (("usermessage", "u"), ("botmessage", "b"), ("channelmessage", "c")):
            handlers[command] = self._switch_handler(key)
        return await self._run(user_id, FakeUpdate(user_id, text=text), handlers[name])

    def _bulk_handler(self, kind):
        async def handler(update, context):
            await self.parts.commands.bulk_command(update, context, kind)

        return handler

    def _switch_handler(self, key):
        async def handler(update, context):
            await self.parts.forwarding.command(update, context, key)

        return handler

    async def tap(self, user_id: int, data: str) -> FakeUpdate:
        p = self.parts
        head = data.split("|")[0]
        routes = {
            "home": p.start.home, "menu": p.start.menu, "connect": p.accounts.connect_menu, "cx": p.accounts.connect_start,
            "acc_list": p.accounts.list_accounts, "acc_refresh": p.accounts.refresh, "logout": p.accounts.logout_menu, "lo": p.accounts.logout_select,
            "loc": p.accounts.logout_confirm, "acc": p.forwarding.account_screen, "bulk": p.commands.bulk_button,
            "msg_acc": p.commands.message_for_account, "stop_bulk": p.commands.stop_bulk, "logs": p.logs.logs,
            "wz": p.engine.on_callback, "pf": p.accounts.profile_button,
            "un": p.control.username_button, "exp": p.control.export_button, "help": p.start.help,
            "expr": p.control.revoke_export_button, "unsend": p.extras.unsend_button, "delchat": p.extras.delchat_button,
            "mode": p.start.mode_button, "aiq": p.ai.question_button,
            "ai": p.ai.settings_button, "aia": p.ai.approval_button, "aib": p.ai.broadcast_button, "aic": p.ai.chat_button,
            "sec": p.security.sec_button, "sch": p.security.sch_button,
        }
        special = {
            "fw|home": p.forwarding.home, "fw|accs": p.forwarding.account_picker, "msg|start": p.commands.message_button,
            "sched|home": p.schedule.home, "sched|new": p.schedule.new, "sched|off": p.schedule.off,
            "adm|home": p.admins.home, "adm|add": p.admins.add, "adm|remove": p.admins.remove,
            "give|start": p.control.give_start, "ex|start": p.control.public_export, "sc|preview": p.start.preview,
            "set|home": p.security.settings_command,
        }
        if data in special:
            handler = special[data]
        elif data.startswith("fw|g|"):
            handler = p.forwarding.toggle_global
        elif data.startswith("fw|set|"):
            handler = p.forwarding.cycle
        elif data.startswith("sched|view|"):
            handler = p.schedule.view
        elif data.startswith("sched|edit|"):
            handler = p.schedule.edit
        elif data.startswith("sched|del|"):
            handler = p.schedule.delete
        elif data.startswith("give|acc|"):
            handler = p.control.give_account
        elif data.startswith("sc|"):
            handler = p.control.startcfg_button
        else:
            handler = routes[head]
        return await self._run(user_id, FakeUpdate(user_id, data=data), handler)

    async def say(self, user_id: int, text: str) -> FakeUpdate:
        return await self._run(user_id, FakeUpdate(user_id, text=text), self.parts.text.text_input)

    async def send_photo(self, user_id: int, data: bytes) -> FakeUpdate:
        return await self._run(user_id, FakeUpdate(user_id, text=None).with_photo(data), self.parts.text.photo_input)

    async def send_media(self, user_id: int, kind: str, file_id: str) -> FakeUpdate:
        return await self._run(user_id, FakeUpdate(user_id, text=None).with_media(kind, file_id), self.parts.text.media_input)

    async def send_txt(self, user_id: int, content: str) -> FakeUpdate:
        """An admin sends a .txt file."""
        update = FakeUpdate(user_id, text=None)
        data = content.encode("utf-8")
        update.effective_message.document = SimpleNamespace(
            file_size=len(data), get_file=_fake_get_file(data), file_name="sessions.txt")
        return await self._run(user_id, update, self.parts.extras.txt_input)

    async def press(self, user_id: int, label: str) -> FakeUpdate:
        """Tap a button of the bottom menu (it arrives as a text message)."""
        return await self._run(user_id, FakeUpdate(user_id, text=label), self.parts.menu.press)

    async def finish_bulk(self, user_id: int) -> None:
        task = self.deps.bulk._tasks.get(user_id)
        if task is not None:
            await task


def texts(update) -> str:
    return update.last()[0]
