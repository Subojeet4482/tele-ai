"""Firestore storage, one small class per kind of data."""
from __future__ import annotations

from telegram_manager.store.accounts import AccountStore
from telegram_manager.store.admins import AdminStore
from telegram_manager.store.database import connect
from telegram_manager.store.jobs import JobStore
from telegram_manager.store.lease import LeaseStore
from telegram_manager.store.replies import ReplyStore
from telegram_manager.store.settings import SettingsStore


class Store:
    def __init__(self, credentials_b64: str, database_id: str | None, owner_id: int, db=None) -> None:
        db = db if db is not None else connect(credentials_b64, database_id)
        self.settings = SettingsStore(db, owner_id)
        self.accounts = AccountStore(db, owner_id, self.settings)
        self.admins = AdminStore(db)
        self.jobs = JobStore(db, owner_id)
        self.lease = LeaseStore(db)
        self.replies = ReplyStore(db, owner_id)


__all__ = ["Store"]
