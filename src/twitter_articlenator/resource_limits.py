"""In-process fairness limits for heavy jobs."""

from __future__ import annotations

import threading
from collections.abc import Callable
from functools import wraps

from flask import Response, current_app, g, jsonify, make_response

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


class StreamResourceLease:
    """Keep a streamed response lease while its background workers are active."""

    def __init__(self, lease: ResourceLease) -> None:
        self._lease = lease
        self._response_closed = False
        self._active_workers = 0
        self._lock = threading.Lock()

    def start_thread(
        self,
        *,
        target: Callable[[], None],
        daemon: bool = False,
        name: str | None = None,
    ) -> threading.Thread:
        """Start a worker whose lifetime extends the underlying resource lease."""
        with self._lock:
            if self._response_closed:
                raise RuntimeError("Cannot start a resource worker after response close")
            self._active_workers += 1

        def tracked_target() -> None:
            try:
                target()
            finally:
                self._worker_finished()

        thread = threading.Thread(target=tracked_target, daemon=daemon, name=name)
        try:
            thread.start()
        except Exception:
            self._worker_finished()
            raise
        return thread

    def response_closed(self) -> None:
        should_release = False
        with self._lock:
            self._response_closed = True
            should_release = self._active_workers == 0
        if should_release:
            self._lease.release()

    def _worker_finished(self) -> None:
        should_release = False
        with self._lock:
            self._active_workers = max(0, self._active_workers - 1)
            should_release = self._response_closed and self._active_workers == 0
        if should_release:
            self._lease.release()


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


def current_stream_resource_lease(resource: str) -> StreamResourceLease:
    """Return the lease tracker installed by ``limit_resource`` for this request."""
    trackers = getattr(g, "_resource_lease_trackers", {})
    try:
        return trackers[resource]
    except KeyError as exc:
        raise RuntimeError(f"No active resource lease for {resource}") from exc


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
            tracker = StreamResourceLease(lease)
            trackers = getattr(g, "_resource_lease_trackers", None)
            if trackers is None:
                trackers = {}
                g._resource_lease_trackers = trackers
            trackers[resource] = tracker
            try:
                response: Response = make_response(view(*args, **kwargs))
            except Exception:
                tracker.response_closed()
                raise
            if response.is_streamed:
                response.call_on_close(tracker.response_closed)
            else:
                tracker.response_closed()
            return response

        return wrapped

    return decorate
