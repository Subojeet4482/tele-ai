"""Startup and shutdown of the bot process."""
from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand

from telegram_manager.errors import describe_error
from telegram_manager.web.health import heartbeat_loop
from telegram_manager.web.selfping import self_ping_loop

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Open the account manager"),
    BotCommand("menu", "All actions as buttons"),
    BotCommand("list", "My connected accounts (in number order)"),
    BotCommand("refresh", "Re-read all account names from Telegram"),
    BotCommand("all", "Message @user from all my accounts (existing chats)"),
    BotCommand("alll", "Message @user from all my accounts (opens chat)"),
    BotCommand("multi", "Message @user from accounts I choose"),
    BotCommand("click", "Press a bot's buttons from all accounts: [follow] (join)"),
    BotCommand("broadcast", "Daily broadcasts (up to 10, all or chosen accounts)"),
    BotCommand("give", "Give accounts to the owner / an admin"),
    BotCommand("export", "Export a session string of an account"),
    BotCommand("startscreen", "Change the start photo / GIF / video and welcome text"),
    BotCommand("keyboard", "Show the quick menu under the chat"),
    BotCommand("hidekeyboard", "Hide the quick menu"),
    BotCommand("emojiid", "Find the ID of a custom emoji (button icons)"),
    BotCommand("help", "All commands as buttons"),
    BotCommand("allblock", "Block @user on all my accounts"),
    BotCommand("settings", "Settings: AI, forwarding, broadcasts, security, channels"),
    BotCommand("security", "2-step password, other devices, spam check, privacy"),
    BotCommand("channels", "Channels / groups an account owns (delete them)"),
    BotCommand("ai", "Talk to the AI manager: /ai <what to do>"),
    BotCommand("aichats", "AI's running chats with people"),
    BotCommand("aisettings", "AI name, prompt, model, keys, mode"),
    BotCommand("send", "Message from accounts: /send 1,2,5 @user hi  (or all)"),
    BotCommand("unsend", "Delete what was sent in the last N minutes"),
    BotCommand("delchat", "Delete a chat on chosen accounts"),
    BotCommand("whois", "Find the id / name of a @username"),
    BotCommand("setapi", "Save my API ID and hash"),
    BotCommand("import", "Add many accounts from a .txt of sessions"),
    BotCommand("message", "Message from one account"),
    BotCommand("usermessage", "User-message forwarding for an account"),
    BotCommand("botmessage", "Bot-message forwarding for an account"),
    BotCommand("channelmessage", "Channel-message forwarding for an account"),
    BotCommand("logs", "Live logs link (owner)"),
    BotCommand("cancel", "Cancel the current action"),
]


async def _startup(application) -> None:
    """Connect the saved accounts in the background, so the bot answers Telegram right away
    instead of waiting for every account (and any network trouble)."""
    manager = application.bot_data["manager"]
    settings = application.bot_data["settings"]
    try:
        # Claim the accounts first. If an older copy of the app is still running (a redeploy
        # overlap), this waits for it to disconnect so a session is never used by two copies.
        await manager.acquire_lease()
        await manager.start_saved_accounts()
    except Exception:
        logger.exception("Starting the saved accounts failed")
    try:
        status = manager.status_summary()
        text = (
            "✅ Bot started.\n"
            f"Accounts online: {status['accounts_online']}/{status['accounts_saved']}\n"
            "📜 Send /logs for a fresh logs link."
        )
        await application.bot.send_message(settings.owner_id, text)
    except Exception as exc:
        logger.warning("Could not send the startup message: %s", describe_error(exc))


async def post_init(application) -> None:
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:
        logger.warning("Could not register the bot command list: %s", describe_error(exc))
    await application.bot_data["access"].ensure_loaded()

    settings = application.bot_data["settings"]
    application.bot_data["heartbeat_task"] = asyncio.create_task(
        heartbeat_loop(application.bot, application.bot_data["health"])
    )
    if settings.self_ping_url:
        application.bot_data["self_ping_task"] = asyncio.create_task(
            self_ping_loop(settings.self_ping_url, settings.self_ping_interval)
        )
    else:
        logger.info(
            "Self-ping is off (SELF_PING_URL/RENDER_EXTERNAL_URL not set, or SELF_PING_INTERVAL=0) - "
            "relying on an external monitor hitting /live or /health to stay awake."
        )
    application.bot_data["startup_task"] = asyncio.create_task(_startup(application))


async def post_shutdown(application) -> None:
    logger.info("Shutting down")
    for key in ("startup_task", "self_ping_task", "heartbeat_task"):
        task = application.bot_data.get(key)
        if task is not None:
            task.cancel()
    application.bot_data["bulk"].cancel_all()
    await application.bot_data["manager"].stop_all()
    try:
        from telegram_manager.ai import gemini, openrouter

        await gemini.close_client()  # the pooled connections of the AI providers
        await openrouter.close_client()
    except Exception:
        logger.debug("Gemini client was not closed cleanly", exc_info=True)
