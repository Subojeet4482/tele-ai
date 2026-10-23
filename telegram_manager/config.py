from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from dotenv import load_dotenv

MIN_SELF_PING_INTERVAL = 30


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: int
    firebase_credentials_b64: str
    session_encryption_key: str
    firebase_database_id: str | None
    log_level: str
    port: int
    self_ping_url: str | None
    self_ping_interval: int
    timezone: str = "Asia/Kolkata"
    bot_user_id: int = 0
    public_url: str | None = None
    instance_lease: bool = True
    button_icons: dict = field(default_factory=dict)  # leading emoji -> custom emoji id (see BUTTON_ICONS)

    @property
    def secure_cookies(self) -> bool:
        """Browser cookies get the Secure flag when the public address is https."""
        return bool(self.public_url and self.public_url.lower().startswith("https://"))

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        required = {
            "BOT_TOKEN": os.getenv("BOT_TOKEN", "").strip(),
            "OWNER_ID": (os.getenv("OWNER_ID", "") or os.getenv("ADMIN_ID", "")).strip(),
            "FIREBASE_CREDENTIALS_B64": os.getenv("FIREBASE_CREDENTIALS_B64", "").strip(),
            "SESSION_ENCRYPTION_KEY": os.getenv("SESSION_ENCRYPTION_KEY", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment values: {', '.join(missing)}")

        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", required["BOT_TOKEN"]):
            raise RuntimeError("BOT_TOKEN does not look like a BotFather token (expected 123456:ABC...)")
        bot_user_id = int(required["BOT_TOKEN"].split(":", 1)[0])

        try:
            owner_id = int(required["OWNER_ID"])
        except ValueError as exc:
            raise RuntimeError("OWNER_ID must be a numeric Telegram user ID") from exc
        if owner_id <= 0:
            raise RuntimeError("OWNER_ID must be greater than zero")

        try:
            decoded = base64.b64decode(required["FIREBASE_CREDENTIALS_B64"], validate=True)
            if not decoded.strip().startswith(b"{"):
                raise ValueError
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("FIREBASE_CREDENTIALS_B64 is not valid base64 JSON") from exc

        try:
            Fernet(required["SESSION_ENCRYPTION_KEY"].encode())
        except Exception as exc:  # cryptography exposes several validation exceptions
            raise RuntimeError("SESSION_ENCRYPTION_KEY is not a valid Fernet key") from exc

        try:
            port = int(os.getenv("PORT", "8080"))
        except ValueError as exc:
            raise RuntimeError("PORT must be a number") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("PORT must be between 1 and 65535")

        self_ping_url = os.getenv("SELF_PING_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip() or None
        if self_ping_url and not re.match(r"^https?://", self_ping_url, flags=re.I):
            raise RuntimeError("SELF_PING_URL / RENDER_EXTERNAL_URL must start with http:// or https://")

        try:
            self_ping_interval = int(os.getenv("SELF_PING_INTERVAL", "600"))
        except ValueError as exc:
            raise RuntimeError("SELF_PING_INTERVAL must be a number of seconds") from exc
        # 0 switches the self-ping off. Anything else must be a sane interval - a
        # zero/negative/tiny value would turn the loop into a busy request storm.
        if self_ping_interval != 0 and self_ping_interval < MIN_SELF_PING_INTERVAL:
            raise RuntimeError(
                f"SELF_PING_INTERVAL must be 0 (disabled) or at least {MIN_SELF_PING_INTERVAL} seconds, "
                f"got {self_ping_interval}"
            )

        public_url = os.getenv("PUBLIC_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip() or None
        if not public_url and self_ping_url:
            parsed = urlparse(self_ping_url)
            public_url = f"{parsed.scheme}://{parsed.netloc}"
        if public_url and not re.match(r"^https?://", public_url, flags=re.I):
            raise RuntimeError("PUBLIC_URL must start with http:// or https://")
        if self_ping_interval == 0:
            self_ping_url = None  # self-ping disabled

        lease_value = os.getenv("INSTANCE_LEASE", "on").strip().lower() or "on"
        if lease_value not in ("on", "off", "1", "0", "true", "false", "yes", "no"):
            raise RuntimeError("INSTANCE_LEASE must be on or off")
        instance_lease = lease_value in ("on", "1", "true", "yes")

        timezone_name = os.getenv("TIMEZONE", "").strip() or "Asia/Kolkata"
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise RuntimeError(f"TIMEZONE '{timezone_name}' is not a valid IANA timezone (e.g. Asia/Kolkata)") from exc

        button_icons: dict[str, str] = {}
        raw_icons = os.getenv("BUTTON_ICONS", "").strip()
        if raw_icons:
            try:
                parsed = json.loads(raw_icons)
                if not isinstance(parsed, dict) or not all(
                    isinstance(k, str) and re.fullmatch(r"\d{5,25}", str(v)) for k, v in parsed.items()
                ):
                    raise ValueError
                button_icons = {k: str(v) for k, v in parsed.items()}
            except ValueError as exc:  # json.JSONDecodeError is a ValueError
                raise RuntimeError(
                    'BUTTON_ICONS must be a JSON object like {"🔗": "5368324170671202286"} (emoji -> custom emoji id)'
                ) from exc

        return cls(
            bot_token=required["BOT_TOKEN"],
            owner_id=owner_id,
            firebase_credentials_b64=required["FIREBASE_CREDENTIALS_B64"],
            session_encryption_key=required["SESSION_ENCRYPTION_KEY"],
            firebase_database_id=os.getenv("FIREBASE_DATABASE_ID", "").strip() or None,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            port=port,
            self_ping_url=self_ping_url,
            self_ping_interval=self_ping_interval,
            timezone=timezone_name,
            bot_user_id=bot_user_id,
            public_url=public_url,
            instance_lease=instance_lease,
            button_icons=button_icons,
        )
