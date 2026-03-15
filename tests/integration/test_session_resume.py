"""Integration tests for session resume, listing, and PDF generation."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

VALID_COOKIES = "auth_token=test123456789012345678901234567890; ct0=test123456789012345678901234567890"


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Create Flask test application with temp directories."""
    monkeypatch.setenv("TWITTER_ARTICLENATOR_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")

    import twitter_articlenator.config as config_module

    config_module._config_instance = None

    from twitter_articlenator.app import create_app

    app = create_app(test_config={"TESTING": True})
    yield app


@pytest.fixture
def client(app):
    """Create Flask test client."""
    return app.test_client()


def _make_mock_article(url="https://example.com/1", title="Test Article"):
    """Create a mock Article for testing."""
    from twitter_articlenator.sources.base import Article

    return Article(
        title=title,
        author="testuser",
        content="<p>Test content for " + title + "</p>",
        published_at=datetime.now(),
        source_url=url,
        source_type="web",
    )


def _create_session_with_articles(tmp_path, session_id, urls, num_saved=None):
    """Helper to create a session directory with saved articles."""
    import hashlib

    from twitter_articlenator.routes.api import _save_session_meta

    session_dir = tmp_path / "output" / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    _save_session_meta(session_dir, urls, status="interrupted")

    if num_saved is None:
        num_saved = len(urls)

    for url in urls[:num_saved]:
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        data = {
            "url": url,
            "title": f"Article for {url}",
            "author": "testuser",
            "content": f"<p>Content for {url}</p>",
            "published_at": datetime.now().isoformat(),
            "source_url": url,
            "source_type": "web",
        }
        (session_dir / f"{url_hash}.json").write_text(json.dumps(data))

    return session_dir


class TestListSessions:
    """Tests for GET /api/sessions."""

    def test_returns_empty_list(self, client):
        """No sessions returns empty list."""
        response = client.get("/api/sessions")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["sessions"] == []

    def test_returns_sessions_with_meta(self, client, tmp_path):
        """Sessions with metadata are listed."""
        urls = ["https://example.com/1", "https://example.com/2"]
        _create_session_with_articles(tmp_path, "session-abc", urls, num_saved=1)

        response = client.get("/api/sessions")
        data = json.loads(response.data)

        assert len(data["sessions"]) == 1
        session = data["sessions"][0]
        assert session["id"] == "session-abc"
        assert session["total"] == 2
        assert session["saved"] == 1
        assert session["status"] == "interrupted"

    def test_returns_multiple_sessions(self, client, tmp_path):
        """Multiple sessions are all listed."""
        _create_session_with_articles(
            tmp_path, "session-1", ["https://example.com/1"]
        )
        _create_session_with_articles(
            tmp_path, "session-2", ["https://example.com/2", "https://example.com/3"]
        )

        response = client.get("/api/sessions")
        data = json.loads(response.data)
        assert len(data["sessions"]) == 2


class TestGetSession:
    """Tests for GET /api/sessions/<id>."""

    def test_returns_session_details(self, client, tmp_path):
        """Returns full session details."""
        urls = ["https://example.com/1", "https://example.com/2"]
        _create_session_with_articles(tmp_path, "session-abc", urls, num_saved=1)

        response = client.get("/api/sessions/session-abc")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["id"] == "session-abc"
        assert data["total"] == 2
        assert data["saved"] == 1
        assert len(data["saved_urls"]) == 1
        assert len(data["remaining_urls"]) == 1

    def test_not_found(self, client):
        """Non-existent session returns 404."""
        response = client.get("/api/sessions/nonexistent")
        assert response.status_code == 404


class TestSessionPdf:
    """Tests for POST /api/sessions/<id>/pdf."""

    def test_generates_pdf_from_session(self, client, tmp_path):
        """Generates PDF from saved session articles."""
        urls = ["https://example.com/1", "https://example.com/2"]
        _create_session_with_articles(tmp_path, "session-pdf", urls)

        with patch(
            "twitter_articlenator.routes.api.generate_combined_pdf"
        ) as mock_pdf:
            mock_pdf.return_value = tmp_path / "output" / "test.pdf"
            (tmp_path / "output" / "test.pdf").write_bytes(b"%PDF-1.4")

            response = client.post("/api/sessions/session-pdf/pdf")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert "filename" in data
        mock_pdf.assert_called_once()

    def test_not_found(self, client):
        """Non-existent session returns 404."""
        response = client.post("/api/sessions/nonexistent/pdf")
        assert response.status_code == 404

    def test_empty_session_returns_error(self, client, tmp_path):
        """Session with no saved articles returns error."""
        from twitter_articlenator.routes.api import _save_session_meta

        session_dir = tmp_path / "output" / "sessions" / "empty-session"
        session_dir.mkdir(parents=True)
        _save_session_meta(session_dir, ["https://example.com/1"], status="interrupted")

        response = client.post("/api/sessions/empty-session/pdf")
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data


class TestSessionResume:
    """Tests for POST /api/sessions/<id>/resume."""

    def test_resume_returns_stream(self, client, tmp_path):
        """Resume endpoint returns SSE stream."""
        urls = ["https://example.com/1", "https://example.com/2"]
        _create_session_with_articles(tmp_path, "session-resume", urls, num_saved=1)

        mock_article = _make_mock_article(
            "https://example.com/2", "Second Article"
        )

        with patch(
            "twitter_articlenator.routes.api.get_source_for_url"
        ) as mock_get_source:
            mock_source = AsyncMock()
            mock_source.fetch = AsyncMock(return_value=mock_article)
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/sessions/session-resume/resume",
                json={"cookies": VALID_COOKIES},
            )

        assert "text/event-stream" in response.content_type
        data = response.data.decode()
        # Should report the already-saved article as resumed
        assert "resumed" in data

    def test_resume_not_found(self, client):
        """Non-existent session returns 404."""
        response = client.post(
            "/api/sessions/nonexistent/resume",
            json={"cookies": VALID_COOKIES},
        )
        assert response.status_code == 404

    def test_resume_no_meta(self, client, tmp_path):
        """Session without meta returns 404."""
        session_dir = tmp_path / "output" / "sessions" / "no-meta"
        session_dir.mkdir(parents=True)

        response = client.post(
            "/api/sessions/no-meta/resume",
            json={"cookies": VALID_COOKIES},
        )
        assert response.status_code == 404


class TestStreamSessionMeta:
    """Tests for session metadata being saved during streaming."""

    def test_convert_stream_saves_meta(self, client, tmp_path):
        """convert/stream saves session metadata on start."""
        mock_article = _make_mock_article()

        with patch(
            "twitter_articlenator.routes.api.get_source_for_url"
        ) as mock_get_source:
            mock_source = AsyncMock()
            mock_source.fetch = AsyncMock(return_value=mock_article)
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/convert/stream",
                json={
                    "links": ["https://example.com/1"],
                    "cookies": VALID_COOKIES,
                    "session_id": "meta-test-session",
                },
            )

        # Check that meta was saved
        meta_path = (
            tmp_path / "output" / "sessions" / "meta-test-session" / "_meta.json"
        )
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["urls"] == ["https://example.com/1"]
        assert meta["total"] == 1

    def test_bookmarks_convert_saves_meta(self, client, tmp_path):
        """bookmarks/convert saves session metadata on start."""
        mock_article = _make_mock_article()

        with patch(
            "twitter_articlenator.routes.api.get_source_for_url"
        ) as mock_get_source:
            mock_source = AsyncMock()
            mock_source.fetch = AsyncMock(return_value=mock_article)
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/bookmarks/convert",
                json={
                    "urls": ["https://example.com/1"],
                    "cookies": VALID_COOKIES,
                    "session_id": "bkmk-meta-session",
                },
            )

        meta_path = (
            tmp_path / "output" / "sessions" / "bkmk-meta-session" / "_meta.json"
        )
        assert meta_path.exists()


class TestFetchTimeout:
    """Tests for fetch timeout behavior."""

    def test_fetch_timeout_reports_failure(self, client, tmp_path):
        """A fetch that exceeds timeout is reported as failed."""
        import time as time_module

        def slow_fetch(url):
            time_module.sleep(5)
            return _make_mock_article()

        with patch(
            "twitter_articlenator.routes.api.get_source_for_url"
        ) as mock_get_source, patch(
            "twitter_articlenator.routes.api.FETCH_TIMEOUT", 1
        ):
            mock_source = AsyncMock()
            mock_source.fetch = AsyncMock(side_effect=slow_fetch)
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/convert/stream",
                json={
                    "links": ["https://example.com/slow"],
                    "cookies": VALID_COOKIES,
                },
            )

        data = response.data.decode()
        assert "failed" in data or "timed out" in data.lower() or "timeout" in data.lower()
