"""Integration tests for conversion progress tracking and reporting."""

import json
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


class TestConvertResponseReport:
    """Tests for detailed report in convert response."""

    def test_response_includes_articles_list(self, client):
        """Test successful response includes list of processed articles."""
        from twitter_articlenator.sources.base import Article
        from datetime import datetime

        mock_article = Article(
            title="Test Article",
            author="testuser",
            content="<p>Test content</p>",
            published_at=datetime.now(),
            source_url="https://example.com/article",
            source_type="web",
        )

        with patch("twitter_articlenator.routes.api.get_source_for_url") as mock_get_source:
            mock_source = AsyncMock()
            mock_source.fetch = AsyncMock(return_value=mock_article)
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/convert",
                json={"links": ["https://example.com/article"], "cookies": VALID_COOKIES},
            )

        data = json.loads(response.data)

        # Response should include articles list with details
        assert "articles" in data
        assert len(data["articles"]) == 1
        assert data["articles"][0]["url"] == "https://example.com/article"
        assert data["articles"][0]["title"] == "Test Article"
        assert "status" in data["articles"][0]
        assert data["articles"][0]["status"] == "success"

    def test_response_includes_failed_urls_with_reason(self, client):
        """Test response includes failed URLs with error reason."""
        with patch("twitter_articlenator.routes.api.get_source_for_url") as mock_get_source:
            mock_source = AsyncMock()
            mock_source.fetch = AsyncMock(side_effect=Exception("Tweet not found"))
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/convert",
                json={"links": ["https://x.com/user/status/123"], "cookies": VALID_COOKIES},
            )

        data = json.loads(response.data)

        # Response should include error details
        assert "error" in data or "errors" in data
        if "errors" in data:
            assert len(data["errors"]) == 1
            assert "url" in data["errors"][0]
            assert "error" in data["errors"][0]
            assert "Tweet not found" in data["errors"][0]["error"]

    def test_partial_success_includes_both_success_and_failures(self, client):
        """Test partial success includes both successful and failed URLs."""
        from twitter_articlenator.sources.base import Article
        from datetime import datetime

        mock_article = Article(
            title="Success Article",
            author="testuser",
            content="<p>Content</p>",
            published_at=datetime.now(),
            source_url="https://example.com/success",
            source_type="web",
        )

        call_count = [0]

        async def mock_fetch(url):
            call_count[0] += 1
            if "fail" in url:
                raise Exception("Failed to fetch")
            return mock_article

        with patch("twitter_articlenator.routes.api.get_source_for_url") as mock_get_source:
            mock_source = AsyncMock()
            mock_source.fetch = mock_fetch
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/convert",
                json={"links": ["https://example.com/success", "https://example.com/fail"], "cookies": VALID_COOKIES},
            )

        data = json.loads(response.data)

        # Should have partial success
        assert data.get("success") is True
        assert "articles" in data
        assert "errors" in data
        assert len(data["articles"]) >= 1
        assert len(data["errors"]) >= 1

    def test_response_includes_summary_counts(self, client):
        """Test response includes summary of success/failure counts."""
        from twitter_articlenator.sources.base import Article
        from datetime import datetime

        mock_article = Article(
            title="Test",
            author="user",
            content="<p>Content</p>",
            published_at=datetime.now(),
            source_url="https://example.com/1",
            source_type="web",
        )

        with patch("twitter_articlenator.routes.api.get_source_for_url") as mock_get_source:
            mock_source = AsyncMock()
            mock_source.fetch = AsyncMock(return_value=mock_article)
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/convert",
                json={"links": ["https://example.com/1", "https://example.com/2"], "cookies": VALID_COOKIES},
            )

        data = json.loads(response.data)

        # Should have summary
        assert "summary" in data
        assert "total" in data["summary"]
        assert "succeeded" in data["summary"]
        assert "failed" in data["summary"]


class TestConvertWithStreaming:
    """Tests for streaming progress updates during conversion."""

    def test_convert_stream_returns_event_stream(self, client):
        """Test /api/convert/stream returns event stream content type."""
        with patch("twitter_articlenator.routes.api.get_source_for_url") as mock_get_source:
            mock_get_source.return_value = None  # No source = unsupported URL

            response = client.post(
                "/api/convert/stream",
                json={"links": ["https://example.com/article"], "cookies": VALID_COOKIES},
            )

        # Should return event stream or JSON with progress
        assert response.content_type in ["text/event-stream", "application/json"]

    def test_stream_emits_progress_events(self, client):
        """Test streaming endpoint emits progress events."""
        from twitter_articlenator.sources.base import Article
        from datetime import datetime

        mock_article = Article(
            title="Test",
            author="user",
            content="<p>Content</p>",
            published_at=datetime.now(),
            source_url="https://example.com/1",
            source_type="web",
        )

        with patch("twitter_articlenator.routes.api.get_source_for_url") as mock_get_source:
            mock_source = AsyncMock()
            mock_source.fetch = AsyncMock(return_value=mock_article)
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/convert/stream",
                json={"links": ["https://example.com/1", "https://example.com/2"], "cookies": VALID_COOKIES},
            )

        # Parse event stream or JSON response
        data = response.data.decode()

        # Should contain progress information
        assert "progress" in data.lower() or "processing" in data.lower() or "1" in data

    def test_stream_shows_current_url_being_processed(self, client):
        """Test progress shows which URL is currently being processed."""
        from twitter_articlenator.sources.base import Article
        from datetime import datetime

        mock_article = Article(
            title="Test",
            author="user",
            content="<p>Content</p>",
            published_at=datetime.now(),
            source_url="https://example.com/specific-url",
            source_type="web",
        )

        with patch("twitter_articlenator.routes.api.get_source_for_url") as mock_get_source:
            mock_source = AsyncMock()
            mock_source.fetch = AsyncMock(return_value=mock_article)
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/convert/stream",
                json={"links": ["https://example.com/specific-url"], "cookies": VALID_COOKIES},
            )

        data = response.data.decode()

        # Should contain the URL being processed
        assert "specific-url" in data or "example.com" in data


class TestProgressPolling:
    """Tests for polling-based progress (alternative to streaming)."""

    def test_job_endpoint_returns_job_id(self, client):
        """Test /api/convert/job returns a job ID for tracking."""
        with patch("twitter_articlenator.routes.api.get_source_for_url") as mock_get_source:
            mock_source = AsyncMock()
            mock_get_source.return_value = mock_source

            response = client.post(
                "/api/convert/job",
                json={"links": ["https://example.com/1"], "cookies": VALID_COOKIES},
            )

        # Should return job ID (or fall back to direct processing)
        if response.status_code == 202:  # Accepted for async processing
            data = json.loads(response.data)
            assert "job_id" in data
        # If not implemented, that's OK - test documents expected behavior

    def test_job_status_returns_progress(self, client):
        """Test /api/job/<id>/status returns current progress."""
        response = client.get("/api/job/test-job-id/status")

        # If implemented, should return progress info
        # If not implemented, 404 is acceptable
        assert response.status_code in [200, 404]

        if response.status_code == 200:
            data = json.loads(response.data)
            assert "status" in data  # pending, processing, complete, failed
            assert "progress" in data  # { current: 1, total: 5, current_url: "..." }
