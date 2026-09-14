"""User accounts and request authentication."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from flask import Flask, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .security import is_valid_csrf_request

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
SESSION_USER_KEY = "user_id"
SESSION_AUTH_VERSION_KEY = "auth_version"

# Checking a real hash for unknown users reduces username-enumeration timing differences.
_DUMMY_PASSWORD_HASH = generate_password_hash("not a real account password", method="scrypt")


@dataclass(frozen=True, slots=True)
class User:
    """Authenticated local account."""

    id: str
    username: str
    password_hash: str
    is_admin: bool
    is_active: bool
    auth_version: int
    created_at: str


class UserStore:
    """SQLite-backed user account repository."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        """Create the database schema and protect the local database file."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.database_path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_normalized TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    auth_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "auth_version" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 1"
                )
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    def create_user(self, username: str, password: str, *, is_admin: bool = False) -> User:
        """Create an active account with a strong password hash."""
        display_name, normalized = self._validate_username(username)
        self._validate_password(password)
        user = User(
            id=str(uuid.uuid4()),
            username=display_name,
            password_hash=generate_password_hash(password, method="scrypt"),
            is_admin=is_admin,
            is_active=True,
            auth_version=1,
            created_at=datetime.now(UTC).isoformat(),
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO users (
                        id, username, username_normalized, password_hash,
                        is_admin, is_active, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user.id,
                        user.username,
                        normalized,
                        user.password_hash,
                        int(user.is_admin),
                        int(user.is_active),
                        user.created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Username already exists") from exc
        return user

    def authenticate(self, username: str, password: str) -> User | None:
        """Return an active user when the supplied credentials are valid."""
        if len(username) > 32 or len(password) > MAX_PASSWORD_LENGTH:
            check_password_hash(_DUMMY_PASSWORD_HASH, password[:MAX_PASSWORD_LENGTH])
            return None
        normalized = username.strip().casefold()
        user = self.get_by_username(normalized)
        password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        password_matches = check_password_hash(password_hash, password)
        if user is None or not user.is_active or not password_matches:
            return None
        return user

    def get(self, user_id: str) -> User | None:
        """Load a user by immutable identifier."""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._from_row(row)

    def get_by_username(self, username: str) -> User | None:
        """Load a user by case-insensitive username."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username_normalized = ?",
                (username.strip().casefold(),),
            ).fetchone()
        return self._from_row(row)

    def list_users(self) -> list[User]:
        """Return all accounts in creation order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY created_at, username"
            ).fetchall()
        return [user for row in rows if (user := self._from_row(row)) is not None]

    def set_password(self, username: str, password: str) -> User:
        """Replace an account password with a fresh scrypt hash."""
        self._validate_password(password)
        user = self.get_by_username(username)
        if user is None:
            raise ValueError("User not found")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE users
                SET password_hash = ?, auth_version = auth_version + 1
                WHERE id = ?
                """,
                (generate_password_hash(password, method="scrypt"), user.id),
            )
        updated = self.get(user.id)
        assert updated is not None
        return updated

    def set_active(self, username: str, *, active: bool) -> User:
        """Enable or disable an existing account."""
        user = self.get_by_username(username)
        if user is None:
            raise ValueError("User not found")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE users
                SET is_active = ?, auth_version = auth_version + 1
                WHERE id = ?
                """,
                (int(active), user.id),
            )
        updated = self.get(user.id)
        assert updated is not None
        return updated

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _validate_username(username: str) -> tuple[str, str]:
        display_name = username.strip()
        if not USERNAME_PATTERN.fullmatch(display_name):
            raise ValueError(
                "Username must be 3-32 characters using letters, numbers, '.', '_', or '-'"
            )
        return display_name, display_name.casefold()

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters")

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        return User(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            is_admin=bool(row["is_admin"]),
            is_active=bool(row["is_active"]),
            auth_version=int(row["auth_version"]),
            created_at=row["created_at"],
        )


class LoginRateLimiter:
    """Small in-process sliding-window limiter for public login attempts."""

    def __init__(
        self,
        *,
        max_failures: int,
        window_seconds: int,
        max_tracked_addresses: int = 10000,
    ) -> None:
        if max_failures < 1 or window_seconds < 1 or max_tracked_addresses < 1:
            raise ValueError("Login rate-limit settings must be positive")
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_tracked_addresses = max_tracked_addresses
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        with self._lock:
            failures = self._recent_failures(key)
            return len(failures) < self.max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            failures = self._recent_failures(key)
            if key not in self._failures and len(self._failures) >= self.max_tracked_addresses:
                self._failures.pop(next(iter(self._failures)))
            failures.append(time.monotonic())
            self._failures[key] = failures

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _recent_failures(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window_seconds
        failures = [timestamp for timestamp in self._failures.get(key, []) if timestamp >= cutoff]
        if failures:
            self._failures[key] = failures
        else:
            self._failures.pop(key, None)
        return failures


def current_user() -> User | None:
    """Return the account loaded for the current request."""
    return getattr(g, "current_user", None)


def get_user_store() -> UserStore:
    """Return the application user store."""
    from flask import current_app

    return current_app.extensions["user_store"]


def init_auth(app: Flask, store: UserStore) -> None:
    """Install centralized user loading and route protection."""
    app.extensions["user_store"] = store

    @app.before_request
    def load_and_require_user():
        user_id = session.get(SESSION_USER_KEY)
        user = store.get(str(user_id)) if user_id else None
        session_auth_version = session.get(SESSION_AUTH_VERSION_KEY)
        if user is not None and (not user.is_active or session_auth_version != user.auth_version):
            user = None
        g.current_user = user
        if user_id and user is None:
            session.clear()

        if not app.config.get("AUTH_REQUIRED", True):
            return None
        if (
            user is not None
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and not is_valid_csrf_request()
        ):
            if request.path.startswith("/api/"):
                return jsonify({"error": "CSRF token missing or invalid"}), 403
            return "CSRF token missing or invalid", 400
        if user is not None or _is_public_endpoint(request.endpoint):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("auth.login", next=_safe_request_target()))


def _is_public_endpoint(endpoint: str | None) -> bool:
    # Public surface: login flow, health probe, static assets, and the setup
    # guide (static documentation; its account-specific sections gate
    # themselves in the template and their APIs remain auth-only).
    return endpoint in {"auth.login", "pages.setup", "api.health", "favicon", "static"}


def _safe_request_target() -> str:
    target = request.full_path.rstrip("?")
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target
