"""Flask application factory and routes."""

import asyncio
import os
import secrets
import sys
import threading
from collections.abc import Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
from cryptography.fernet import Fernet
from flask import Flask, g, request
from flask.cli import FlaskGroup
from werkzeug.middleware.proxy_fix import ProxyFix

from .auth import LoginRateLimiter, UserStore, current_user, init_auth
from .config import get_config
from .logging import configure_logging
from .resource_limits import ResourceLimiter
from .routes import api_bp, auth_bp, channel_bp, pages_bp, transcription_bp
from .routes.auth import register_cli
from .security import get_csrf_token
from .version import get_git_commit, get_version_string

log = structlog.get_logger()


class AsyncRunner:
    """Manages a persistent event loop in a background thread.

    This ensures all async operations (especially Playwright which has
    internal locks bound to event loops) run on the same event loop.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> None:
        """Ensure the background event loop is running."""
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._run_loop, daemon=True, name="async-runner"
                )
                self._thread.start()
                # Wait for loop to start
                while not self._loop.is_running():
                    pass

    def _run_loop(self) -> None:
        """Run the event loop forever in background thread."""
        assert self._loop is not None  # Set by _ensure_loop before thread starts
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run[T](self, coro: Coroutine[Any, Any, T], *, timeout: float = 120) -> T:
        """Run a coroutine on the persistent event loop.

        Args:
            coro: Coroutine to run.
            timeout: Maximum seconds to wait for the result (default 120).

        Returns:
            Result of the coroutine.
        """
        self._ensure_loop()
        assert self._loop is not None  # Guaranteed by _ensure_loop
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()  # Cancel the underlying asyncio task to free resources
            raise


# Global async runner instance
_async_runner = AsyncRunner()


def run_async(coro, *, timeout: float = 120):
    """Run an async coroutine safely from sync Flask code.

    Uses a persistent background event loop to avoid event loop conflicts
    with libraries like twscrape that have internal locks.

    Args:
        coro: Coroutine to run.
        timeout: Maximum seconds to wait for the result (default 120).

    Returns:
        Result of the coroutine.
    """
    return _async_runner.run(coro, timeout=timeout)


def _validate_production_security_config(app: Flask) -> None:
    """Fail closed when authentication secrets are unsafe in production."""
    if app.config.get("TESTING") or not app.config.get("AUTH_REQUIRED", True):
        return

    secret_key = app.config.get("SECRET_KEY")
    if not isinstance(secret_key, (str, bytes)) or len(secret_key) < 32:
        raise RuntimeError(
            "TWITTER_ARTICLENATOR_SECRET_KEY must be set to at least 32 random characters"
        )
    if secret_key == "dev-secret-key" or secret_key == b"dev-secret-key":
        raise RuntimeError("TWITTER_ARTICLENATOR_SECRET_KEY must not use the development default")

    encryption_key = app.config.get("COOKIE_ENCRYPTION_KEY")
    if not encryption_key:
        raise RuntimeError("TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY is required")
    try:
        Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc
    app.config["REQUIRE_COOKIE_ENCRYPTION"] = True


def create_app(
    test_config: dict | None = None,
    *,
    run_startup_tasks: bool = True,
) -> Flask:
    """Create and configure the Flask application.

    Args:
        test_config: Optional test configuration dict.

    Returns:
        Configured Flask application.
    """
    # Configure logging before creating app
    json_output = test_config is None
    config = get_config()
    if not config.json_logging:
        json_output = False
    configure_logging(json_output=json_output)

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    app.config.from_mapping(
        SECRET_KEY=os.environ.get(
            "TWITTER_ARTICLENATOR_SECRET_KEY",
            os.environ.get("SECRET_KEY", "dev-secret-key"),
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=os.environ.get(
            "TWITTER_ARTICLENATOR_SESSION_COOKIE_SAMESITE", "Lax"
        ),
        SESSION_COOKIE_SECURE=os.environ.get(
            "TWITTER_ARTICLENATOR_SESSION_COOKIE_SECURE", "false"
        ).lower()
        in ("true", "1", "yes"),
        PERMANENT_SESSION_LIFETIME=timedelta(
            hours=int(os.environ.get("TWITTER_ARTICLENATOR_SESSION_HOURS", "24"))
        ),
        MAX_CONTENT_LENGTH=int(
            os.environ.get("TWITTER_ARTICLENATOR_MAX_REQUEST_BYTES", str(1024 * 1024))
        ),
        AUTH_REQUIRED=True,
        USER_DATABASE_PATH=Path(
            os.environ.get(
                "TWITTER_ARTICLENATOR_USER_DATABASE",
                config.config_dir / "articlenator.sqlite3",
            )
        ),
        OUTPUT_ROOT=config.output_dir,
        CONFIG_ROOT=config.config_dir,
        COOKIE_ENCRYPTION_KEY=config.youtube_cookie_encryption_key,
        REQUIRE_COOKIE_ENCRYPTION=config.require_youtube_cookie_encryption,
        PLAYWRIGHT_PER_USER_LIMIT=int(
            os.environ.get("TWITTER_ARTICLENATOR_PLAYWRIGHT_PER_USER_LIMIT", "1")
        ),
        PLAYWRIGHT_GLOBAL_LIMIT=int(
            os.environ.get("TWITTER_ARTICLENATOR_PLAYWRIGHT_GLOBAL_LIMIT", "2")
        ),
        TRANSCRIPTION_PER_USER_LIMIT=int(
            os.environ.get("TWITTER_ARTICLENATOR_TRANSCRIPTION_PER_USER_LIMIT", "1")
        ),
        TRANSCRIPTION_GLOBAL_LIMIT=int(
            os.environ.get("TWITTER_ARTICLENATOR_TRANSCRIPTION_GLOBAL_LIMIT", "1")
        ),
        DOWNLOAD_PER_USER_LIMIT=int(
            os.environ.get("TWITTER_ARTICLENATOR_DOWNLOAD_PER_USER_LIMIT", "1")
        ),
        DOWNLOAD_GLOBAL_LIMIT=int(
            os.environ.get("TWITTER_ARTICLENATOR_DOWNLOAD_GLOBAL_LIMIT", "2")
        ),
        LOGIN_MAX_FAILURES=int(os.environ.get("TWITTER_ARTICLENATOR_LOGIN_MAX_FAILURES", "5")),
        LOGIN_FAILURE_WINDOW_SECONDS=int(
            os.environ.get("TWITTER_ARTICLENATOR_LOGIN_FAILURE_WINDOW_SECONDS", "900")
        ),
        TRUST_PROXY_HEADERS=os.environ.get(
            "TWITTER_ARTICLENATOR_TRUST_PROXY_HEADERS", "false"
        ).lower()
        in ("true", "1", "yes"),
        TRUSTED_HOSTS=[
            host.strip()
            for host in os.environ.get("TWITTER_ARTICLENATOR_TRUSTED_HOSTS", "").split(",")
            if host.strip()
        ]
        or None,
    )
    if test_config is not None:
        app.config.update(test_config)
        if "AUTH_REQUIRED" not in test_config:
            app.config["AUTH_REQUIRED"] = False

    _validate_production_security_config(app)
    if app.config["TRUST_PROXY_HEADERS"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    user_store = UserStore(app.config["USER_DATABASE_PATH"])
    user_store.initialize()
    init_auth(app, user_store)
    app.extensions["login_rate_limiter"] = LoginRateLimiter(
        max_failures=app.config["LOGIN_MAX_FAILURES"],
        window_seconds=app.config["LOGIN_FAILURE_WINDOW_SECONDS"],
    )
    app.extensions["resource_limiter"] = ResourceLimiter(
        {
            "playwright": (
                app.config["PLAYWRIGHT_PER_USER_LIMIT"],
                app.config["PLAYWRIGHT_GLOBAL_LIMIT"],
            ),
            "transcription": (
                app.config["TRANSCRIPTION_PER_USER_LIMIT"],
                app.config["TRANSCRIPTION_GLOBAL_LIMIT"],
            ),
            "download": (
                app.config["DOWNLOAD_PER_USER_LIMIT"],
                app.config["DOWNLOAD_GLOBAL_LIMIT"],
            ),
        }
    )

    log.info("app_created", testing=app.config.get("TESTING", False))

    # Inject version into all templates
    @app.context_processor
    def inject_version():
        """Make version info available to all templates."""
        return {
            "app_version": get_version_string(),
            "git_commit": get_git_commit(),
            "csrf_token": get_csrf_token,
            "csp_nonce": getattr(g, "csp_nonce", ""),
            "current_user": current_user(),
        }

    @app.before_request
    def create_csp_nonce():
        """Create a per-request nonce for inline scripts."""
        g.csp_nonce = secrets.token_urlsafe(16)

    # Register security headers
    @app.after_request
    def add_security_headers(response):
        """Add security headers to all responses."""
        nonce = getattr(g, "csp_nonce", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        if app.config.get("AUTH_REQUIRED", True) and request.endpoint not in {
            "api.health",
            "favicon",
            "static",
        }:
            response.headers["Cache-Control"] = "private, no-store"
        return response

    # Serve favicon.ico from root for Safari compatibility
    @app.route("/favicon.ico")
    def favicon():
        return app.send_static_file("favicon.ico")

    # Store run_async in app config for blueprints to access
    app.config["RUN_ASYNC"] = run_async

    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(transcription_bp)
    app.register_blueprint(channel_bp)
    register_cli(app)

    # Clean up stale sessions on startup
    if test_config is None and run_startup_tasks:
        user_paths = []
        try:
            from .routes.api import _cleanup_stale_sessions
            from .user_data import paths_for_user

            user_paths = [
                paths_for_user(user.id, app.config["OUTPUT_ROOT"], app.config["CONFIG_ROOT"])
                for user in user_store.list_users()
            ]
            _cleanup_stale_sessions([paths.sessions_dir for paths in user_paths])
        except Exception as e:
            log.warning("stale_session_cleanup_failed", error=str(e))

        # Resume any channel jobs interrupted by a crash/restart/sleep so the
        # pipeline survives the process dying, independent of any client.
        try:
            from .routes.channel import resume_inflight_channel_jobs
            from .sources.youtube_cookies import YouTubeCookieStore

            resume_inflight_channel_jobs(
                [
                    (
                        paths.channel_dir,
                        YouTubeCookieStore(
                            cookie_path=paths.youtube_cookie_path,
                            encryption_key=app.config.get("COOKIE_ENCRYPTION_KEY"),
                            require_encryption=app.config.get("REQUIRE_COOKIE_ENCRYPTION", True),
                            max_bytes=config.youtube_cookie_max_bytes,
                        ),
                        paths.user_id,
                    )
                    for paths in user_paths
                ],
                limiter=app.extensions["resource_limiter"],
            )
        except Exception as e:
            log.warning("channel_job_resume_on_startup_failed", error=str(e))

    return app


def main() -> None:
    """Run the Flask server."""
    cli_mode = len(sys.argv) > 1
    app = create_app(run_startup_tasks=not cli_mode)
    if cli_mode:
        FlaskGroup(create_app=lambda: app).main(
            args=sys.argv[1:],
            prog_name="twitter-articlenator",
            standalone_mode=True,
        )
        return
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)


if __name__ == "__main__":
    main()
