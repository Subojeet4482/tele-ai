from __future__ import annotations

import base64
import json


def connect(credentials_b64: str, database_id: str | None = None):
    """Return a Firestore client. firebase_admin is imported here so the rest of the
    store package can be imported (and tested) without it."""
    import firebase_admin
    from firebase_admin import credentials, firestore

    service_account = json.loads(base64.b64decode(credentials_b64, validate=True).decode("utf-8"))
    try:
        app = firebase_admin.get_app()
    except ValueError:
        app = firebase_admin.initialize_app(credentials.Certificate(service_account))
    if database_id:
        return firestore.client(app=app, database_id=database_id)
    return firestore.client(app=app)
