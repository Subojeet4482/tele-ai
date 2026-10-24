"""Wires everything together: which handler answers which command, button or message."""
from __future__ import annotations

from dataclasses import dataclass

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    MessageReactionHandler,
    TypeHandler,
    filters,
)

from telegram_manager.access import AccessControl
from telegram_manager.bot import keyboards, reply_menu
from telegram_manager.bot.bulk_runner import BulkRunner
from telegram_manager.bot.deps import Deps
from telegram_manager.bot.flows.admins import build_admin_specs
from telegram_manager.bot.flows.bulk import build_bulk_specs
from telegram_manager.bot.flows.connect import build_connect_specs
from telegram_manager.bot.flows.export import build_export_specs
from telegram_manager.bot.flows.give import build_give_spec
from telegram_manager.bot.flows.message import build_message_spec
from telegram_manager.bot.flows.multi import build_multi_spec
from telegram_manager.bot.flows.profile import build_profile_specs
from telegram_manager.bot.flows.schedule import build_schedule_spec
from telegram_manager.bot.flows.security import build_security_specs
from telegram_manager.bot.flows.startcfg import build_startcfg_specs
from telegram_manager.bot.flows.utilities import build_utility_specs
from telegram_manager.bot.guard import Guard
from telegram_manager.ai.conversation import Conversations
from telegram_manager.ai.gemini import GeminiClient
from telegram_manager.ai.openrouter import OpenRouterClient
from telegram_manager.bot.handlers.accounts import AccountHandlers
from telegram_manager.bot.handlers.ai import AIHandlers
from telegram_manager.bot.handlers.admins import AdminHandlers
from telegram_manager.bot.handlers.commands import CommandHandlers
from telegram_manager.bot.handlers.control import ControlHandlers
from telegram_manager.bot.handlers.extras import ExtraHandlers
from telegram_manager.bot.handlers.security import SecurityHandlers
from telegram_manager.bot.handlers.forwarding import ForwardingHandlers
from telegram_manager.bot.handlers.logs import LogsHandlers
from telegram_manager.bot.handlers.schedule import ScheduleHandlers
from telegram_manager.bot.handlers.start import StartHandlers
from telegram_manager.bot.handlers.text import TextHandlers
from telegram_manager.bot.handlers.utilities import UtilityHandlers
from telegram_manager.bot.lifecycle import post_init, post_shutdown
from telegram_manager.bot.middleware import Middleware
from telegram_manager.bot.wizard.engine import WizardEngine
from telegram_manager.core.manager import TelethonManager
from telegram_manager.crypto import SecretBox
from telegram_manager.store import Store


def _bulk(commands: CommandHandlers, kind: str):
    async def handler(update, context) -> None:
        await commands.bulk_command(update, context, kind)

    return handler


def _mode_command(utils: UtilityHandlers, mode: str):
    async def handler(update, context) -> None:
        await utils.switch_command(update, context, mode)

    return handler


def _switch(forwarding: ForwardingHandlers, key: str):
    async def handler(update, context) -> None:
        await forwarding.command(update, context, key)

    return handler


@dataclass
class Components:
    engine: WizardEngine
    start: StartHandlers
    accounts: AccountHandlers
    forwarding: ForwardingHandlers
    commands: CommandHandlers
    schedule: ScheduleHandlers
    admins: AdminHandlers
    control: ControlHandlers
    extras: ExtraHandlers
    ai: AIHandlers
    security: SecurityHandlers
    logs: LogsHandlers
    utils: UtilityHandlers
    text: TextHandlers
    middleware: Middleware
    menu: reply_menu.MenuButtons


def wire(deps: Deps) -> Components:
    """Create the flows and handler objects. Needs no Telegram connection (tests use it directly)."""
    engine = WizardEngine(deps)
    engine.register(
        *build_bulk_specs(deps),
        build_message_spec(deps),
        build_multi_spec(deps),
        *build_profile_specs(deps),
        *build_connect_specs(deps),
        build_schedule_spec(deps),
        *build_admin_specs(deps),
        build_give_spec(deps),
        *build_export_specs(deps),
        *build_startcfg_specs(deps),
        *build_security_specs(deps),
        *build_utility_specs(deps),
    )
    if deps.chats is None:  # the AI's private chats listen to every account's incoming messages
        deps.chats = Conversations(deps)
        deps.manager.incoming_hook = deps.chats.on_incoming
    parts = Components(
        engine=engine,
        start=StartHandlers(deps, engine),
        accounts=AccountHandlers(deps, engine),
        forwarding=ForwardingHandlers(deps, engine),
        commands=CommandHandlers(deps, engine),
        schedule=ScheduleHandlers(deps, engine),
        admins=AdminHandlers(deps, engine),
        control=ControlHandlers(deps, engine),
        extras=ExtraHandlers(deps, engine),
        ai=AIHandlers(deps, engine),
        security=SecurityHandlers(deps, engine),
        logs=LogsHandlers(deps, engine),
        utils=UtilityHandlers(deps, engine),
        text=TextHandlers(deps, engine),
        middleware=Middleware(deps, engine),
        menu=None,
    )
    parts.menu = reply_menu.MenuButtons(deps, engine, parts)
    parts.extras.ai = parts.ai  # a file sent outside any flow is kept for the AI
    parts.text.ai = parts.ai  # plain messages go to the AI (unless the admin picked Manual mode)
    parts.start.ai = parts.ai  # AI mode's home screen
    parts.utils.start = parts.start  # /aimode and /manual
    # Where Back / Cancel lead from inside a flow.
    engine.screens = {
        "home": parts.start.home_screen,
        "menu": parts.start.menu_screen,
        "adm|home": parts.admins.home_screen,
        "acc_list": parts.accounts.list_screen,
        "sched|home": parts.schedule.home_screen,
        "sc|home": parts.control.startcfg_screen,
        "ai|home": lambda update, context=None: parts.ai.show_settings(update),
        "set|home": lambda update, context=None: parts.security.settings_screen(update),
        "sec|home": lambda update, context=None: parts.security.security_picker(update),
        "sch|home": lambda update, context=None: parts.security._channel_picker(update),
        "px|home": lambda update, context=None: parts.utils.hub_screen(update, context),
    }
    return parts


def build_application(settings, log_buffer=None, log_access=None, health=None) -> Application:
    store = Store(settings.firebase_credentials_b64, settings.firebase_database_id, settings.owner_id)
    manager = TelethonManager(
        store,
        SecretBox(settings.session_encryption_key),
        None,
        settings.owner_id,
        settings.timezone,
        bot_user_id=settings.bot_user_id,
        use_lease=settings.instance_lease,
    )
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    manager.bot = application.bot
    keyboards.configure_icons(getattr(settings, "button_icons", None))

    access = AccessControl(store, settings.owner_id)
    deps = Deps(
        settings=settings,
        store=store,
        manager=manager,
        access=access,
        guard=Guard(access),
        bulk=BulkRunner(manager, store),
        log_buffer=log_buffer,
        log_access=log_access,
        health=health,
        gemini=GeminiClient(),
        openrouter=OpenRouterClient(),
    )
    parts = wire(deps)
    application.bot_data.update(
        manager=manager, settings=settings, access=access, bulk=deps.bulk, health=health, deps=deps
    )

    # Group -1 runs before every other handler: logging + abandoning half-finished flows.
    application.add_handler(TypeHandler(Update, parts.middleware.track_update), group=-1)
    application.add_error_handler(parts.middleware.on_error)
    start, accounts, forwarding = parts.start, parts.accounts, parts.forwarding
    commands, schedule, admins, logs, text = parts.commands, parts.schedule, parts.admins, parts.logs, parts.text
    control, extras, ai, security = parts.control, parts.extras, parts.ai, parts.security
    utils = parts.utils
    engine = parts.engine

    for name, callback in (
        ("start", start.start),
        ("menu", start.menu),
        ("cancel", start.cancel),
        ("list", accounts.list_accounts),
        ("all", _bulk(commands, "all")),
        ("alll", _bulk(commands, "alll")),
        ("allblock", _bulk(commands, "block")),
        ("click", _bulk(commands, "click")),
        ("refresh", accounts.refresh),
        ("broadcast", schedule.home),
        ("message", commands.message_command),
        ("multi", commands.multi_command),
        ("usermessage", _switch(forwarding, "u")),
        ("botmessage", _switch(forwarding, "b")),
        ("channelmessage", _switch(forwarding, "c")),
        ("chanelmessage", _switch(forwarding, "c")),  # common misspelling
        ("delay", start.moved),
        ("onalltime", start.moved),
        ("give", control.give_start),
        ("export", control.export_command),
        ("startscreen", control.startcfg_command),
        ("help", control.help_command),
        ("keyboard", start.keyboard),
        ("hidekeyboard", start.hide_keyboard),
        ("emojiid", start.emoji_id),
        ("logs", logs.logs),
        ("setapi", extras.setapi),
        ("send", extras.send_command),
        ("unsend", extras.unsend),
        ("delchat", extras.delchat),
        ("whois", extras.whois),
        ("import", extras.import_command),
        ("ai", ai.ai_command),
        ("aisettings", ai.settings_command),
        ("aiclear", ai.clear_command),
        ("aichats", ai.chats_command),
        ("settings", security.settings_command),
        ("security", security.security_command),
        ("channels", security.channels_command),
        ("proxy", utils.proxy_command),
        ("getproxy", utils.getproxy_command),
        ("proxyurl", utils.proxyurl_command),
        ("tempmail", utils.tempmail_command),
        ("aimode", _mode_command(utils, "ai")),
        ("manual", _mode_command(utils, "manual")),
    ):
        application.add_handler(CommandHandler(name, callback))

    for pattern, callback in (
        (r"^home$", start.home),
        (r"^menu$", start.menu),
        (r"^connect$", accounts.connect_menu),
        (r"^cx\|", accounts.connect_start),
        (r"^acc_list$", accounts.list_accounts),
        (r"^acc_refresh$", accounts.refresh),
        (r"^logout$", accounts.logout_menu),
        (r"^lo\|", accounts.logout_select),
        (r"^loc\|", accounts.logout_confirm),
        (r"^acc\|", forwarding.account_screen),
        (r"^pf\|", accounts.profile_button),
        (r"^fw\|home$", forwarding.home),
        (r"^fw\|g\|", forwarding.toggle_global),
        (r"^fw\|accs$", forwarding.account_picker),
        (r"^fw\|set\|", forwarding.cycle),
        (r"^bulk\|", commands.bulk_button),
        (r"^msg\|start$", commands.message_button),
        (r"^msg_acc\|", commands.message_for_account),
        (r"^stop_bulk$", commands.stop_bulk),
        (r"^sched\|home$", schedule.home),
        (r"^sched\|new$", schedule.new),
        (r"^sched\|off$", schedule.off),
        (r"^sched\|view\|", schedule.view),
        (r"^sched\|edit\|", schedule.edit),
        (r"^sched\|del\|", schedule.delete),
        (r"^adm\|home$", admins.home),
        (r"^adm\|add$", admins.add),
        (r"^adm\|remove$", admins.remove),
        (r"^logs\|link$", logs.logs),
        (r"^help$", start.help),
        (r"^un\|", control.username_button),
        (r"^exp\|", control.export_button),
        (r"^expr\|", control.revoke_export_button),
        (r"^give\|start$", control.give_start),
        (r"^give\|acc\|", control.give_account),
        (r"^sc\|preview$", start.preview),
        (r"^sc\|", control.startcfg_button),
        (r"^ex\|start$", control.public_export),
        (r"^mode\|", start.mode_button),
        (r"^set\|home$", security.settings_command),
        (r"^sec\|", security.sec_button),
        (r"^sch\|", security.sch_button),
        (r"^px\|", utils.proxy_button),
        (r"^tm\|", utils.tm_button),
        (r"^aiq\|", ai.question_button),
        (r"^ai\|", ai.settings_button),
        (r"^aia\|", ai.approval_button),
        (r"^aib\|", ai.broadcast_button),
        (r"^aic\|", ai.chat_button),
        (r"^unsend\|", extras.unsend_button),
        (r"^delchat\|", extras.delchat_button),
        (r"^wz\|", engine.on_callback),
    ):
        application.add_handler(CallbackQueryHandler(callback, pattern=pattern))

    # The bottom menu's buttons arrive as plain text: catch them before the generic text handler.
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(reply_menu.pattern()), parts.menu.press))
    application.add_handler(MessageHandler(filters.TEXT & filters.Entity("custom_emoji"), text.custom_emoji_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text.text_input))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, text.photo_input))
    application.add_handler(MessageHandler(filters.Document.ALL, extras.txt_input))
    application.add_handler(MessageHandler(filters.ANIMATION | filters.VIDEO | filters.VIDEO_NOTE | filters.VOICE | filters.AUDIO, text.media_input))
    application.add_handler(MessageReactionHandler(text.on_reaction))
    return application



