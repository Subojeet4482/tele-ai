"""Numbers and limits shared across the app, in one place."""

# --- bulk sends: /all, /alll, /allblock --------------------------------------
BULK_DELAY_MIN = 0
BULK_DELAY_MAX = 100
OPEN_CHAT_MIN_DELAY = 3  # /alll and the daily broadcast open new chats, so keep accounts this far apart
DELAY_PRESETS = (0, 1, 3, 5, 10, 30)
OPEN_CHAT_DELAY_PRESETS = (3, 5, 10, 30, 60)

# --- auto-click of inline buttons: /all @bot hello [follow] (join) --------------
AUTOCLICK_MAX_BUTTONS = 5  # button names per command
AUTOCLICK_WAIT = 25  # seconds to wait for a matching button to show up, per button name
AUTOCLICK_POLL = 1.5  # seconds between looks at the chat while waiting
AUTOCLICK_PAUSE = 1.5  # seconds after a click, so the bot can answer before the next look
BOT_START_WAIT = 20  # seconds to wait for a bot to answer /start
BOT_START_AFTER_CLICK_WAIT = 8  # seconds to wait for the bot's answer after 'Verify' / a second /start
BOT_START_SETTLE = 1.5  # a bot often sends two messages: look again a moment after the first one
BOT_START_ROUNDS = 3  # how often 'join first, then verify' is repeated before giving up
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # a file the admin sends to the AI (a bot cannot download more than 20 MB)
UPLOAD_TTL_SECONDS = 3600  # an uploaded photo is forgotten after an hour

# --- timed messages ("send in N minutes") -------------------------------------
WHEN_PRESETS = (0, 1, 5, 15, 30, 60)  # minutes; 0 = now
WHEN_MAX_MINUTES = 24 * 60

# --- daily broadcast ----------------------------------------------------------
MAX_TIMES_PER_DAY = 1440
MAX_DAILY_JOBS = 10  # daily broadcasts one owner/admin can have at the same time
COUNT_PRESETS = (1, 2, 3, 4, 6, 12)
TIME_PRESETS = ("09:00", "12:00", "18:00", "21:00")
DRIFT_MAX = 9_999_999_999_999  # seconds the start time may move later every day (0 = same time daily)
DRIFT_PRESETS = (0, 10, 30, 60, 300)

# --- bot UI ---------------------------------------------------------------------
PAGE_SIZE = 8  # buttons per page in long lists (chats, accounts)
DIALOG_FETCH_LIMIT = 100
WIZARD_TTL = 900  # seconds a half-finished step-by-step flow stays alive
DENY_REPLY_INTERVAL = 30  # a stranger gets the "admins only" reply at most this often

# --- Telegram limits ------------------------------------------------------------
MAX_BOT_UPLOAD_BYTES = 49 * 1024 * 1024  # Bot API upload limit is 50 MB
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

# --- connection tuning ----------------------------------------------------------
CONNECT_TIMEOUT = 45  # seconds to wait for a (re)connect before giving up on that attempt
REQUEST_TIMEOUT = 60  # seconds to wait for a single Telegram request during login/registration
WATCHDOG_INTERVAL = 60  # seconds between connection health checks
LIVENESS_EVERY_TICKS = 5  # every Nth watchdog tick also makes a real request to prove the session is alive
DIALOG_CACHE_TTL = 120  # seconds an account's chat list is reused before it is read again
DIALOG_CACHE_MIN_REFRESH = 20  # a missing chat forces a re-read, but not more often than this
DOWNLOAD_TIMEOUT = 300  # seconds allowed for downloading one incoming media file
UPLOAD_READ_TIMEOUT = 60
UPLOAD_WRITE_TIMEOUT = 180

# --- instance lease (two copies must never use the same sessions) ----------------
LEASE_INTERVAL = 10
LEASE_STALE_SECONDS = 35
TAKEOVER_WAIT = 25

# --- web server -------------------------------------------------------------------
LOGIN_KEY_TTL = 600  # a /logs login link works once, for this many seconds
SESSION_TTL = 12 * 3600  # browser session after using the link
SELF_PING_MIN_INTERVAL = 30

# --- start screen + public session export ------------------------------------------
START_TEXT_MAX = 800  # welcome text; a photo caption may hold 1024, the rest is for the role line
EXPORT_COOLDOWN = 30  # seconds between two login-code requests of one (non-admin) user
EXPORT_AUTODELETE = 300  # the exported session message deletes itself after this many seconds
USERNAME_MIN = 5  # Telegram's rule for public usernames
USERNAME_MAX = 32

# --- export notifications + multi-session export ------------------------------------
EXPORT_NOTIFY_TTL = 600  # seconds the owner/admin can still tap Revoke after a public export
EXPORT_FILE_AUTODELETE = 60  # a .txt session export deletes itself after this many seconds

# --- proxies handed out + temp mail ---------------------------------------------------------------------------------
PROXY_INLINE_MAX = 20  # up to this many proxies are sent as a chat message; more than this comes as a .txt file
PROXY_GIVE_DEFAULT = 10  # /getproxy with no number
TEMPMAIL_POLL_ROUNDS = 3  # times the inbox is looked at when 📥 Get is tapped and nothing has arrived yet
TEMPMAIL_POLL_GAP = 4  # seconds between those looks
