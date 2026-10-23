from __future__ import annotations


def describe_error(exc: BaseException) -> str:
    """Readable reason for an exception (many have an empty str(), e.g. TimeoutError)."""
    return str(exc).strip() or exc.__class__.__name__


_SEND_HINTS = {
    "ChatGuestSendForbiddenError": (
        "That message is a channel post, so replying to it is a comment. The account must first join "
        "the channel's discussion group (open the channel from that account and join the group), then reply again."
    ),
    "ChatWriteForbiddenError": "This account is not allowed to write in that chat.",
    "UserBannedInChannelError": "This account is banned from sending messages in that chat.",
    "SlowModeWaitError": "That chat has slow mode on. Wait a little and reply again.",
    "FloodWaitError": "Telegram asked this account to slow down. Wait a while and try again.",
}


def explain_send_error(exc: BaseException) -> str:
    """describe_error plus a plain-language hint for the common reasons a send is refused."""
    reason = describe_error(exc)
    hint = _SEND_HINTS.get(exc.__class__.__name__)
    if hint is None and "join the discussion group" in reason:
        hint = _SEND_HINTS["ChatGuestSendForbiddenError"]
    return f"{hint}\n\n({reason})" if hint else reason


class AccountAlreadyConnectedError(ValueError):
    """The Telegram account being added is already connected."""
