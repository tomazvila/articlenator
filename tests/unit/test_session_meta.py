"""Unit tests for session metadata management."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest


VALID_COOKIES = "auth_token=test123456789012345678901234567890; ct0=test123456789012345678901234567890"


@pytest.fixture
def sessions_dir(tmp_path):
    """Create a temporary sessions directory."""
    d = tmp_path / "output" / "sessions"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def session_dir(sessions_dir):
    """Create a single session directory."""
    d = sessions_dir / "test-session-001"
    d.mkdir()
    return d


class TestSaveSessionMeta:
    """Tests for _save_session_meta."""

    def test_creates_meta_file(self, session_dir):
        from twitter_articlenator.routes.api import _save_session_meta

        urls = ["https://example.com/1", "https://example.com/2"]
        _save_session_meta(session_dir, urls)

        meta_path = session_dir / "_meta.json"
        assert meta_path.exists()

    def test_meta_contains_urls(self, session_dir):
        from twitter_articlenator.routes.api import _save_session_meta

        urls = ["https://example.com/1", "https://example.com/2"]
        _save_session_meta(session_dir, urls)

        meta = json.loads((session_dir / "_meta.json").read_text())
        assert meta["urls"] == urls
        assert meta["total"] == 2

    def test_meta_contains_status(self, session_dir):
        from twitter_articlenator.routes.api import _save_session_meta

        _save_session_meta(session_dir, ["https://example.com/1"], status="running")

        meta = json.loads((session_dir / "_meta.json").read_text())
        assert meta["status"] == "running"

    def test_meta_contains_timestamps(self, session_dir):
        from twitter_articlenator.routes.api import _save_session_meta

        _save_session_meta(session_dir, ["https://example.com/1"])

        meta = json.loads((session_dir / "_meta.json").read_text())
        assert "created_at" in meta
        assert "updated_at" in meta
        # Should be valid ISO format
        datetime.fromisoformat(meta["created_at"])
        datetime.fromisoformat(meta["updated_at"])


class TestLoadSessionMeta:
    """Tests for _load_session_meta."""

    def test_returns_saved_data(self, session_dir):
        from twitter_articlenator.routes.api import _save_session_meta, _load_session_meta

        urls = ["https://example.com/1"]
        _save_session_meta(session_dir, urls, status="running")

        meta = _load_session_meta(session_dir)
        assert meta is not None
        assert meta["urls"] == urls
        assert meta["status"] == "running"

    def test_returns_none_for_missing_file(self, session_dir):
        from twitter_articlenator.routes.api import _load_session_meta

        meta = _load_session_meta(session_dir)
        assert meta is None

    def test_returns_none_for_invalid_json(self, session_dir):
        from twitter_articlenator.routes.api import _load_session_meta

        (session_dir / "_meta.json").write_text("not valid json")
        meta = _load_session_meta(session_dir)
        assert meta is None


class TestUpdateSessionStatus:
    """Tests for _update_session_status."""

    def test_updates_status(self, session_dir):
        from twitter_articlenator.routes.api import (
            _save_session_meta,
            _load_session_meta,
            _update_session_status,
        )

        _save_session_meta(session_dir, ["https://example.com/1"], status="running")
        _update_session_status(session_dir, "completed")

        meta = _load_session_meta(session_dir)
        assert meta["status"] == "completed"

    def test_updates_timestamp(self, session_dir):
        from twitter_articlenator.routes.api import (
            _save_session_meta,
            _load_session_meta,
            _update_session_status,
        )

        _save_session_meta(session_dir, ["https://example.com/1"])
        original_meta = _load_session_meta(session_dir)

        time.sleep(0.01)  # Ensure different timestamp
        _update_session_status(session_dir, "completed")

        updated_meta = _load_session_meta(session_dir)
        assert updated_meta["updated_at"] >= original_meta["updated_at"]

    def test_preserves_urls(self, session_dir):
        from twitter_articlenator.routes.api import (
            _save_session_meta,
            _load_session_meta,
            _update_session_status,
        )

        urls = ["https://example.com/1", "https://example.com/2"]
        _save_session_meta(session_dir, urls)
        _update_session_status(session_dir, "completed")

        meta = _load_session_meta(session_dir)
        assert meta["urls"] == urls

    def test_adds_extra_fields(self, session_dir):
        from twitter_articlenator.routes.api import (
            _save_session_meta,
            _load_session_meta,
            _update_session_status,
        )

        _save_session_meta(session_dir, ["https://example.com/1"])
        _update_session_status(session_dir, "interrupted", processed=5, errors=2)

        meta = _load_session_meta(session_dir)
        assert meta["processed"] == 5
        assert meta["errors"] == 2

    def test_works_without_existing_meta(self, session_dir):
        from twitter_articlenator.routes.api import _load_session_meta, _update_session_status

        _update_session_status(session_dir, "error", error="something broke")

        meta = _load_session_meta(session_dir)
        assert meta["status"] == "error"
        assert meta["error"] == "something broke"


class TestCleanupStaleSessions:
    """Tests for _cleanup_stale_sessions."""

    def test_removes_old_sessions(self, sessions_dir, monkeypatch):
        from twitter_articlenator.routes.api import _cleanup_stale_sessions

        import twitter_articlenator.config as config_module

        # Setup config to use our temp dir
        monkeypatch.setenv(
            "TWITTER_ARTICLENATOR_OUTPUT_DIR", str(sessions_dir.parent)
        )
        config_module._config_instance = None

        old_session = sessions_dir / "old-session"
        old_session.mkdir()

        # Write meta with old timestamp
        meta = {
            "urls": [],
            "total": 0,
            "status": "interrupted",
            "created_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
            "updated_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        }
        (old_session / "_meta.json").write_text(json.dumps(meta))

        _cleanup_stale_sessions()

        assert not old_session.exists()
        config_module._config_instance = None

    def test_keeps_recent_sessions(self, sessions_dir, monkeypatch):
        from twitter_articlenator.routes.api import (
            _save_session_meta,
            _cleanup_stale_sessions,
        )
        import twitter_articlenator.config as config_module

        monkeypatch.setenv(
            "TWITTER_ARTICLENATOR_OUTPUT_DIR", str(sessions_dir.parent)
        )
        config_module._config_instance = None

        recent_session = sessions_dir / "recent-session"
        recent_session.mkdir()
        _save_session_meta(recent_session, ["https://example.com/1"])

        _cleanup_stale_sessions()

        assert recent_session.exists()
        config_module._config_instance = None

    def test_handles_missing_sessions_dir(self, tmp_path, monkeypatch):
        import twitter_articlenator.config as config_module

        monkeypatch.setenv(
            "TWITTER_ARTICLENATOR_OUTPUT_DIR", str(tmp_path / "nonexistent")
        )
        config_module._config_instance = None

        from twitter_articlenator.routes.api import _cleanup_stale_sessions

        # Should not raise
        _cleanup_stale_sessions()
        config_module._config_instance = None

    def test_removes_old_sessions_without_meta_by_mtime(self, sessions_dir, monkeypatch):
        import twitter_articlenator.config as config_module

        monkeypatch.setenv(
            "TWITTER_ARTICLENATOR_OUTPUT_DIR", str(sessions_dir.parent)
        )
        config_module._config_instance = None

        from twitter_articlenator.routes.api import _cleanup_stale_sessions

        old_session = sessions_dir / "no-meta-old"
        old_session.mkdir()
        # Set mtime to 10 days ago
        import os
        old_time = time.time() - (10 * 86400)
        os.utime(old_session, (old_time, old_time))

        _cleanup_stale_sessions()

        assert not old_session.exists()
        config_module._config_instance = None
