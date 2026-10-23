"""Tiny stand-ins for python-telegram-bot / Telethon, used only when the real libraries are not installed.

They let the tests import and drive the bot code (wizards, handlers, manager) without network access.
With the real libraries installed nothing is replaced."""
from __future__ import annotations

import sys
import types


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Btn:
    def __init__(self, text, callback_data=None, **kwargs):
        self.text = text
        self.callback_data = callback_data
        self.api_kwargs = kwargs.get("api_kwargs", {})


class _KBtn:
    def __init__(self, text, **kwargs):
        self.text = text
        self.api_kwargs = kwargs.get("api_kwargs", {})


class _ReplyMarkup:
    def __init__(self, keyboard, **kwargs):
        self.keyboard = keyboard
        self.kwargs = kwargs


class _Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class _Any:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


def install() -> None:
    try:
        import telegram  # noqa: F401
        import telethon  # noqa: F401

        return
    except ImportError:
        pass

    class BadRequest(Exception):
        pass

    class NetworkError(Exception):
        pass

    class Conflict(Exception):
        pass

    class _InputFile:
        def __init__(self, obj, filename=None, **kwargs):
            self.obj = obj
            self.filename = filename

    _module("telegram", Update=_Any, InlineKeyboardButton=_Btn, InlineKeyboardMarkup=_Markup, BotCommand=_Any,
            KeyboardButton=_KBtn, ReplyKeyboardMarkup=_ReplyMarkup, ReplyKeyboardRemove=_Any,
            LinkPreviewOptions=_Any, Bot=_Any, InputFile=_InputFile)
    _module("telegram.error", BadRequest=BadRequest, NetworkError=NetworkError, Conflict=Conflict)
    _module("telegram.ext", Application=_Any, ApplicationBuilder=_Any, CallbackQueryHandler=_Any,
            CommandHandler=_Any, MessageHandler=_Any, MessageReactionHandler=_Any, TypeHandler=_Any,
            filters=types.SimpleNamespace(TEXT=1, COMMAND=2, PHOTO=3, Document=types.SimpleNamespace(IMAGE=4),
                                          ANIMATION=5, VIDEO=6, Regex=lambda pattern: 7, Entity=lambda kind: 8))

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

    class _NewMessage:
        Event = object

        def __init__(self, *args, **kwargs):
            pass

    error_names = (
        "AuthKeyUnregisteredError AuthKeyDuplicatedError SessionRevokedError SessionExpiredError "
        "UserDeactivatedError UserDeactivatedBanError PasswordHashInvalidError PhoneCodeExpiredError "
        "PhoneCodeInvalidError SessionPasswordNeededError"
    ).split()
    errors = _module("telethon.errors", **{name: type(name, (Exception,), {}) for name in error_names})
    _module("telethon", TelegramClient=_Client, errors=errors, events=types.SimpleNamespace(NewMessage=_NewMessage))
    _module("telethon.sessions", StringSession=lambda value="": value)
    _module("telethon.tl")
    _module("telethon.tl.functions")
    def _requests(module: str, *names: str) -> None:
        _module(module, **{name: type(name, (_Any,), {}) for name in names})

    _requests("telethon.tl.functions.contacts", "BlockRequest", "UnblockRequest", "GetBlockedRequest", "SearchRequest")
    _requests("telethon.tl.functions.users", "GetFullUserRequest")
    _requests("telethon.tl.functions.messages", "SendReactionRequest", "ImportChatInviteRequest", "DeleteChatRequest",
              "EditChatAboutRequest", "EditChatTitleRequest", "StartBotRequest", "MarkDialogUnreadRequest")
    _requests("telethon.tl.functions.channels", "JoinChannelRequest", "DeleteChannelRequest", "CreateChannelRequest",
              "EditTitleRequest", "UpdateUsernameRequest", "GetParticipantRequest")
    _requests("telethon.tl.functions.account", "UpdateProfileRequest", "UpdateUsernameRequest", "GetPasswordRequest",
              "GetAuthorizationsRequest", "ResetAuthorizationRequest", "SetPrivacyRequest", "UpdateNotifySettingsRequest")
    _requests("telethon.tl.functions.photos", "UploadProfilePhotoRequest", "DeletePhotosRequest")
    privacy = ("InputPrivacyKeyPhoneNumber InputPrivacyKeyStatusTimestamp InputPrivacyKeyProfilePhoto InputPrivacyKeyAbout "
               "InputPrivacyKeyChatInvite InputPrivacyKeyPhoneCall InputPrivacyKeyForwards InputPrivacyValueAllowAll "
               "InputPrivacyValueAllowContacts InputPrivacyValueDisallowAll").split()
    _module("telethon.tl.types", ReactionEmoji=_Any, Channel=type("Channel", (), {}), InputDialogPeer=_Any, InputNotifyPeer=_Any,
            InputPeerNotifySettings=_Any,
            **{name: type(name, (_Any,), {}) for name in privacy})
    _module("telethon.utils", get_input_photo=lambda photo: photo)

install()
