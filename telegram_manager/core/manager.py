"""The Telethon manager: every feature lives in its own mixin module in this package."""
from __future__ import annotations

from telegram_manager.core.accountctl import AccountControlMixin
from telegram_manager.core.accounts import AccountsMixin
from telegram_manager.core.autoclick import AutoClickMixin
from telegram_manager.core.base import ManagerBase
from telegram_manager.core.control import ControlMixin
from telegram_manager.core.dialogs import DialogsMixin
from telegram_manager.core.extras import ExtrasMixin
from telegram_manager.core.forwarding import ForwardingMixin
from telegram_manager.core.health import HealthMixin
from telegram_manager.core.joiner import JoinerMixin
from telegram_manager.core.lease import LeaseMixin
from telegram_manager.core.lifecycle import LifecycleMixin
from telegram_manager.core.login import ExportLoginMixin, LoginMixin
from telegram_manager.core.messaging import MessagingMixin
from telegram_manager.core.profile import ProfileMixin
from telegram_manager.core.replies import RepliesMixin
from telegram_manager.core.scheduler import SchedulerMixin
from telegram_manager.core.security import SecurityMixin
from telegram_manager.core.status import StatusMixin
from telegram_manager.core.watchdog import WatchdogMixin


class TelethonManager(
    StatusMixin,
    LifecycleMixin,
    LeaseMixin,
    WatchdogMixin,
    LoginMixin,
    ExportLoginMixin,
    AccountsMixin,
    AccountControlMixin,
    DialogsMixin,
    MessagingMixin,
    ExtrasMixin,
    AutoClickMixin,
    JoinerMixin,
    ControlMixin,
    ProfileMixin,
    SecurityMixin,
    ForwardingMixin,
    HealthMixin,
    RepliesMixin,
    SchedulerMixin,
    ManagerBase,
):
    """Runs every admin's Telegram accounts. Each account belongs to one admin and
    everything (forwarding, bulk sends, jobs, notifications) stays inside that admin."""
