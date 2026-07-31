"""In-process fairness limits for heavy jobs."""

from __future__ import annotations

import threading
from collections.abc import Callable
from functools import wraps

from flask import Response, current_app, jsonify, make_response

from .auth import current_user


class ResourceLease:
    """One idempotently releasable capacity reservation."""

    def __init__(self, release_callback: Callable[[], None]) -> None:
        self._release_callback = release_callback
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release_callback()


class ResourceLimiter:
    """Atomic per-user and global counters for named resources."""

    def __init__(self, limits: dict[str, tuple[int, int]]) -> None:
        for name, (per_user, global_limit) in limits.items():
            if per_user < 1 or global_limit < 1:
                raise ValueError(f"Resource limits for {name} must be positive")
        self._limits = dict(limits)
        self._global: dict[str, int] = {}
        self._users: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def try_acquire(self, resource: str, user_id: str) -> ResourceLease | None:
        """Reserve capacity without blocking, or return None when capped."""
        per_user_limit, global_limit = self._limits[resource]
        with self._lock:
            global_active = self._global.get(resource, 0)
            user_active = self._users.get(resource, {}).get(user_id, 0)
            if global_active >= global_limit or user_active >= per_user_limit:
                return None
            self._global[resource] = global_active + 1
            self._users.setdefault(resource, {})[user_id] = user_active + 1
        return ResourceLease(lambda: self._release(resource, user_id))

    def active(self, resource: str) -> dict[str, object]:
        """Return safe counter data for diagnostics and tests."""
        with self._lock:
            return {
                "global": self._global.get(resource, 0),
                "users": dict(self._users.get(resource, {})),
            }

    def _release(self, resource: str, user_id: str) -> None:
        with self._lock:
            self._global[resource] = max(0, self._global.get(resource, 0) - 1)
            users = self._users.setdefault(resource, {})
            users[user_id] = max(0, users.get(user_id, 0) - 1)
            if users[user_id] == 0:
                users.pop(user_id, None)


def get_resource_limiter() -> ResourceLimiter:
    return current_app.extensions["resource_limiter"]


def acquire_for_current_user(resource: str) -> ResourceLease | None:
    user = current_user()
    user_id = user.id if user is not None else "legacy-test-mode"
    return get_resource_limiter().try_acquire(resource, user_id)


def limit_resource(resource: str):
    """Hold a lease until a normal response or streamed response closes."""

    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            lease = acquire_for_current_user(resource)
            if lease is None:
                response = jsonify(
                    {
                        "error": f"{resource.capitalize()} job limit reached",
                        "resource": resource,
                    }
                )
                response.headers["Retry-After"] = "5"
                return response, 429
            try:
                response: Response = make_response(view(*args, **kwargs))
            except Exception:
                lease.release()
                raise
            if response.is_streamed:
                response.call_on_close(lease.release)
            else:
                lease.release()
            return response

        return wrapped

    return decorate
