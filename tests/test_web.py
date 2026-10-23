import json
import logging
import os
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram_manager.log_setup import setup_logging  # noqa: E402
from telegram_manager.web.access import LogAccess  # noqa: E402
from telegram_manager.web.health import HealthMonitor  # noqa: E402
from telegram_manager.web.server import start_http_server  # noqa: E402


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def http(url, method="GET", data=None, cookie=None):
    headers = {"Cookie": cookie} if cookie else {}
    body = urllib.parse.urlencode(data).encode() if data else None
    request = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with OPENER.open(request, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


class HealthTests(unittest.TestCase):
    def make(self, summary, now):
        clock = {"t": 1000.0}
        health = HealthMonitor(startup_grace=100, bot_stale=200, watchdog_stale=300, clock=lambda: clock["t"])
        health.status_provider = lambda: summary
        clock["t"] += now
        return health, clock

    def test_starting_is_ok(self):
        health, _ = self.make({"accounts_saved": 3, "accounts_online": 0}, 10)
        self.assertEqual(health.check(), ("ok", "starting"))

    def test_bot_silent_is_down(self):
        health, _ = self.make({}, 500)
        self.assertEqual(health.check(), ("down", "bot_not_responding"))

    def test_all_accounts_offline_is_down(self):
        health, _ = self.make({"accounts_saved": 2, "accounts_online": 0}, 500)
        health.beat()
        self.assertEqual(health.check(), ("down", "no_accounts_online"))

    def test_some_offline_is_degraded(self):
        health, _ = self.make({"accounts_saved": 2, "accounts_online": 1, "accounts_offline": ["x"]}, 500)
        health.beat()
        self.assertEqual(health.check()[0], "degraded")

    def test_healthy_and_no_accounts(self):
        health, _ = self.make({"accounts_saved": 0, "accounts_online": 0}, 500)
        health.beat()
        self.assertEqual(health.check(), ("ok", "ok"))

    def test_stalled_watchdog_is_down(self):
        health, _ = self.make({"accounts_saved": 1, "accounts_online": 1, "watchdog_age_seconds": 900}, 500)
        health.beat()
        self.assertEqual(health.check(), ("down", "watchdog_stalled"))

    def test_superseded_copy_is_standby(self):
        health, _ = self.make({"instance_superseded": True, "accounts_saved": 2, "accounts_online": 0}, 500)
        self.assertEqual(health.check(), ("ok", "standby"))


class LogAccessTests(unittest.TestCase):
    def test_key_is_single_use(self):
        access = LogAccess()
        key = access.new_login_key()
        self.assertTrue(access.peek(key))
        self.assertTrue(access.peek(key))  # peeking never uses it up
        session = access.redeem(key)
        self.assertTrue(access.session_valid(session))
        self.assertIsNone(access.redeem(key))
        self.assertFalse(access.peek(key))

    def test_expiry(self):
        clock = {"t": 0.0}
        access = LogAccess(clock=lambda: clock["t"])
        key = access.new_login_key()
        clock["t"] = 601
        self.assertIsNone(access.redeem(key))
        key = access.new_login_key()
        session = access.redeem(key)
        clock["t"] += 13 * 3600
        self.assertFalse(access.session_valid(session))

    def test_unknown_values(self):
        access = LogAccess()
        self.assertFalse(access.session_valid("nope"))
        self.assertFalse(access.session_valid(None))
        self.assertIsNone(access.redeem("nope"))

    def test_logout(self):
        access = LogAccess()
        session = access.redeem(access.new_login_key())
        access.end_session(session)
        self.assertFalse(access.session_valid(session))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.buffer = setup_logging("INFO")
        self.access = LogAccess()
        self.health = HealthMonitor(startup_grace=0)
        self.summary = {"accounts_saved": 1, "accounts_online": 1, "accounts_offline": []}
        self.health.status_provider = lambda: self.summary
        self.health.beat()
        self.server = start_http_server(0, self.buffer, self.access, self.health, host="127.0.0.1")
        self.server.status_provider = lambda: {"accounts_online": 2}
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        logging.getLogger("x").info("hello-visible-line")
        logging.getLogger("x").warning("careful-warning")

    def tearDown(self):
        self.server.shutdown()

    def login(self):
        key = self.access.new_login_key()
        status, _, headers = http(self.base + "/logs/login", "POST", {"key": key})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/logs")
        return headers["Set-Cookie"].split(";")[0], headers["Set-Cookie"]

    def test_root_reports_real_health(self):
        status, body, _ = http(self.base + "/")
        self.assertEqual((status, json.loads(body)["status"]), (200, "ok"))
        self.summary["accounts_online"] = 0
        self.summary["accounts_offline"] = ["x"]
        status, body, _ = http(self.base + "/health")
        self.assertEqual((status, json.loads(body)["reason"]), (503, "no_accounts_online"))
        self.assertEqual(http(self.base + "/", "HEAD")[0], 503)
        self.assertEqual(http(self.base + "/live")[0], 200)  # process-alive probe stays green

    def test_health_body_has_no_account_details(self):
        self.summary["accounts_offline"] = ["Secret Name"]
        _, body, _ = http(self.base + "/")
        self.assertNotIn("Secret", body)

    def test_unknown_path_is_404(self):
        self.assertEqual(http(self.base + "/anything")[0], 404)

    def test_old_token_urls_no_longer_work(self):
        self.assertEqual(http(self.base + "/logs?token=" + "t" * 32)[0], 403)
        self.assertEqual(http(self.base + "/status")[0], 403)

    def test_login_flow_and_no_secret_in_url(self):
        key = self.access.new_login_key()
        status, body, headers = http(f"{self.base}/logs/login?key={key}")
        self.assertEqual(status, 200)
        self.assertIn("<form", body)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertTrue(self.access.peek(key))  # a preview/scanner GET does not burn the key
        status, _, headers = http(self.base + "/logs/login", "POST", {"key": key})
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(http(self.base + "/logs/login", "POST", {"key": key})[0], 403)  # replay
        self.assertEqual(http(f"{self.base}/logs/login?key={key}")[0], 403)
        status, page, _ = http(self.base + "/logs", cookie=cookie.split(";")[0])
        self.assertEqual(status, 200)
        self.assertIn("hello-visible-line", page)
        self.assertNotIn("token=", page)
        self.assertNotIn("key=", page)

    def test_secure_flag_behind_https_proxy(self):
        key = self.access.new_login_key()
        request = urllib.request.Request(
            self.base + "/logs/login", method="POST", data=urllib.parse.urlencode({"key": key}).encode(),
            headers={"X-Forwarded-Proto": "https"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            OPENER.open(request, timeout=5)
        self.assertIn("Secure", ctx.exception.headers["Set-Cookie"])

    def test_pages_need_a_session_and_logout_ends_it(self):
        cookie, _ = self.login()
        self.assertEqual(http(self.base + "/logs?format=text&level=warning", cookie=cookie)[1].count("careful-warning"), 1)
        status, body, _ = http(self.base + "/status", cookie=cookie)
        data = json.loads(body)
        self.assertEqual((status, data["accounts_online"], data["status"]), (200, 2, "ok"))
        self.assertEqual(http(self.base + "/logs", cookie="lsid=forged")[0], 403)
        http(self.base + "/logs/logout", cookie=cookie)
        self.assertEqual(http(self.base + "/logs", cookie=cookie)[0], 403)

    def test_filters_escaping_and_masking(self):
        cookie, _ = self.login()
        logging.getLogger("x").info("<script>alert(1)</script>")
        logging.getLogger("x").info("POST https://api.telegram.org/bot123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/x +919876543210")
        _, body, _ = http(self.base + "/logs", cookie=cookie)
        self.assertNotIn("<script>alert(1)</script>", body)
        _, text, _ = http(self.base + "/logs?format=text", cookie=cookie)
        self.assertNotIn("AAAAAAAAAAAAAAAAAAAA", text)
        self.assertNotIn("919876543210", text)
        _, newest, _ = http(self.base + "/logs?format=text&order=new", cookie=cookie)
        _, oldest, _ = http(self.base + "/logs?format=text&order=old", cookie=cookie)
        self.assertEqual(newest.strip().splitlines()[::-1], oldest.strip().splitlines())
        self.assertEqual(http(self.base + "/logs?lines=abc&level=zzz&refresh=-4", cookie=cookie)[0], 200)

    def test_logs_disabled_without_access(self):
        server = start_http_server(0, self.buffer, None, None, host="127.0.0.1")
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            self.assertEqual(http(base + "/logs")[0], 404)
            self.assertEqual(http(base + "/")[0], 200)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
