import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet  # noqa: E402

from telegram_manager.config import Settings  # noqa: E402
from telegram_manager.crypto import SecretBox, SecretDecryptError  # noqa: E402
from telegram_manager.log_setup import mask_id, redact  # noqa: E402

KEY = Fernet.generate_key().decode()
CLEAN = ("SELF_PING_INTERVAL", "SELF_PING_URL", "RENDER_EXTERNAL_URL", "PUBLIC_URL", "PORT", "OWNER_ID", "ADMIN_ID")


class ConfigTests(unittest.TestCase):
    ENV = {
        "BOT_TOKEN": "123456:ABCdef_ghiJKL-mno",
        "OWNER_ID": "42",
        "FIREBASE_CREDENTIALS_B64": base64.b64encode(b'{"a":1}').decode(),
        "SESSION_ENCRYPTION_KEY": KEY,
    }

    def load(self, **extra):
        for name in CLEAN:
            os.environ.pop(name, None)
        os.environ.update(self.ENV)
        os.environ.update(extra)
        import telegram_manager.config as config

        config.load_dotenv = lambda: None
        return Settings.from_env()

    def test_owner_id_and_legacy_admin_id(self):
        self.assertEqual(self.load().owner_id, 42)
        os.environ.pop("OWNER_ID", None)
        for name in CLEAN:
            os.environ.pop(name, None)
        env = {k: v for k, v in self.ENV.items() if k != "OWNER_ID"}
        os.environ.update(env)
        os.environ["ADMIN_ID"] = "77"
        import telegram_manager.config as config

        config.load_dotenv = lambda: None
        self.assertEqual(Settings.from_env().owner_id, 77)
        os.environ.pop("ADMIN_ID")
        with self.assertRaisesRegex(RuntimeError, "OWNER_ID"):
            Settings.from_env()
        with self.assertRaisesRegex(RuntimeError, "numeric"):
            self.load(OWNER_ID="abc")

    def test_ping_interval(self):
        with self.assertRaisesRegex(RuntimeError, "SELF_PING_INTERVAL"):
            self.load(SELF_PING_INTERVAL="5")
        s = self.load(SELF_PING_INTERVAL="0", RENDER_EXTERNAL_URL="https://a.onrender.com")
        self.assertIsNone(s.self_ping_url)
        self.assertEqual(s.public_url, "https://a.onrender.com")
        self.assertTrue(s.secure_cookies)

    def test_no_logs_token_any_more(self):
        s = self.load()
        self.assertFalse(hasattr(s, "logs_token"))
        self.assertFalse(s.secure_cookies)
        self.assertEqual(s.bot_user_id, 123456)
        with self.assertRaises(RuntimeError):
            self.load(BOT_TOKEN="notatoken")


class CryptoAndMaskTests(unittest.TestCase):
    def test_wrong_key_has_reason(self):
        enc = SecretBox(KEY).encrypt("hello")
        other = SecretBox(Fernet.generate_key().decode())
        with self.assertRaises(SecretDecryptError) as ctx:
            other.decrypt(enc)
        self.assertIn("SESSION_ENCRYPTION_KEY", str(ctx.exception))
        self.assertEqual(SecretBox(KEY).decrypt(enc), "hello")

    def test_mask_id(self):
        self.assertEqual(mask_id(123456789), mask_id(123456789))  # stable within a run
        self.assertNotEqual(mask_id(1), mask_id(2))
        self.assertNotIn("123456789", mask_id(123456789))
        self.assertEqual(mask_id(None), "-")

    def test_redact(self):
        self.assertEqual(redact("token=secret123&x=1"), "token=<redacted>&x=1")
        self.assertIn("+<phone>", redact("phone +919876543210"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
