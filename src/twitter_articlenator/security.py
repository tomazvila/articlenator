"""Small security helpers for browser-facing state changes."""

from __future__ import annotations

import hmac
import secrets

from flask import request, session

CSRF_SESSION_KEY = "_csrf_token"


def get_csrf_token() -> str:
    """Return the current session CSRF token, creating one when needed."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def is_valid_csrf_request() -> bool:
    """Validate a state-changing request's CSRF token."""
    expected = session.get(CSRF_SESSION_KEY)
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not expected or not supplied:
        return False
    return hmac.compare_digest(str(expected), str(supplied))
