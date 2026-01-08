"""Flask application factory and routes."""

import asyncio
import os
import threading
from collections.abc import Coroutine
from typing import Any

import structlog
from flask import Flask

from .config import get_config
from .logging import configure_logging
from .routes import api_bp, pages_bp

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

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on the persistent event loop.

        Args:
            coro: Coroutine to run.

        Returns:
            Result of the coroutine.
        """
        self._ensure_loop()
        assert self._loop is not None  # Guaranteed by _ensure_loop
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=120)  # 2 minute timeout


# Global async runner instance
_async_runner = AsyncRunner()


def run_async(coro):
    """Run an async coroutine safely from sync Flask code.

    Uses a persistent background event loop to avoid event loop conflicts
    with libraries like twscrape that have internal locks.

    Args:
        coro: Coroutine to run.

    Returns:
        Result of the coroutine.
    """
    return _async_runner.run(coro)


def create_app(test_config: dict | None = None) -> Flask:
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

    if test_config is None:
        # Load production config
        app.config.from_mapping(
            SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key"),
        )
    else:
        # Load test config
        app.config.update(test_config)

    log.info("app_created", testing=app.config.get("TESTING", False))

    # Register security headers
    @app.after_request
    def add_security_headers(response):
        """Add security headers to all responses."""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )
        return response

    # Store run_async in app config for blueprints to access
    app.config["RUN_ASYNC"] = run_async

    # Register blueprints
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)

    return app


def main() -> None:
    """Run the Flask development server."""
    app = create_app()
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)


if __name__ == "__main__":
    main()
