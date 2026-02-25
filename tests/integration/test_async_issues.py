"""Tests for async event loop issues and cookie validation.

These tests verify that multiple sequential API calls work correctly
with the stateless cookie validation approach.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch


VALID_COOKIES = "auth_token=abcdefghijklmnopqrstuvwxyz; ct0=abcdefghijklmnopqrstuvwxyz"


class TestAsyncEventLoopIssues:
    """Tests for event loop isolation between requests."""

    def test_multiple_validate_calls(self, client):
        """Test that multiple cookie validation calls don't cause event loop issues."""
        for _ in range(3):
            response = client.post("/api/cookies/validate", json={"cookies": VALID_COOKIES})
            assert response.status_code == 200

    def test_validate_then_convert(self, client):
        """Test validation then conversion doesn't cause event loop issues."""
        from contextlib import asynccontextmanager

        # Validate first
        r1 = client.post("/api/cookies/validate", json={"cookies": VALID_COOKIES})
        assert r1.status_code == 200

        mock_tweet_data = {
            "author": "testuser",
            "display_name": "Test User",
            "content": "Test tweet content",
            "timestamp": None,
            "quoted_tweets": [],
        }

        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        mock_pool = MagicMock()

        @asynccontextmanager
        async def mock_get_context(cookies=None):
            yield mock_context

        mock_pool.get_context = mock_get_context

        with patch(
            "twitter_articlenator.sources.twitter_playwright.get_browser_pool",
            return_value=mock_pool,
        ):
            with patch(
                "twitter_articlenator.sources.twitter_playwright.TwitterPlaywrightSource._extract_tweet_data",
                new_callable=AsyncMock,
                return_value=mock_tweet_data,
            ):
                r2 = client.post(
                    "/api/convert",
                    json={"links": ["https://x.com/user/status/123"], "cookies": VALID_COOKIES},
                )
                data = json.loads(r2.data)
                error_msg = data.get("error", "")
                assert "event loop" not in error_msg.lower()
                assert "Lock object" not in error_msg

    def test_multiple_conversions(self, client):
        """Test multiple conversions don't cause event loop issues."""
        from contextlib import asynccontextmanager

        mock_tweet_data = {
            "author": "testuser",
            "display_name": "Test User",
            "content": "Test tweet content",
            "timestamp": None,
            "quoted_tweets": [],
        }

        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        mock_pool = MagicMock()

        @asynccontextmanager
        async def mock_get_context(cookies=None):
            yield mock_context

        mock_pool.get_context = mock_get_context

        with patch(
            "twitter_articlenator.sources.twitter_playwright.get_browser_pool",
            return_value=mock_pool,
        ):
            with patch(
                "twitter_articlenator.sources.twitter_playwright.TwitterPlaywrightSource._extract_tweet_data",
                new_callable=AsyncMock,
                return_value=mock_tweet_data,
            ):
                # First conversion
                r1 = client.post(
                    "/api/convert",
                    json={"links": ["https://x.com/user/status/123"], "cookies": VALID_COOKIES},
                )
                data1 = json.loads(r1.data)
                assert "Lock object" not in data1.get("error", "")

                # Second conversion
                r2 = client.post(
                    "/api/convert",
                    json={"links": ["https://x.com/user/status/456"], "cookies": VALID_COOKIES},
                )
                data2 = json.loads(r2.data)
                assert "Lock object" not in data2.get("error", "")


class TestCookieValidation:
    """Tests for POST /api/cookies/validate endpoint."""

    def test_validate_with_valid_cookies(self, client):
        """Test validation with properly formatted cookies."""
        response = client.post("/api/cookies/validate", json={"cookies": VALID_COOKIES})
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["valid"]
        assert data["status"] == "valid"

    def test_validate_with_short_auth_token(self, client):
        """Test validation with too short auth_token."""
        response = client.post(
            "/api/cookies/validate",
            json={"cookies": "auth_token=short; ct0=abcdefghijklmnopqrstuvwxyz"},
        )
        assert response.status_code == 200

        data = json.loads(response.data)
        assert not data["valid"]
        assert data["status"] == "invalid"

    def test_validate_with_missing_ct0(self, client):
        """Test validation with missing ct0."""
        response = client.post(
            "/api/cookies/validate",
            json={"cookies": "auth_token=abcdefghijklmnopqrstuvwxyz"},
        )
        assert response.status_code == 200

        data = json.loads(response.data)
        assert not data["valid"]
        assert data["status"] == "invalid"

    def test_validate_with_no_cookies(self, client):
        """Test validation with no cookies."""
        response = client.post("/api/cookies/validate", json={})
        assert response.status_code == 200

        data = json.loads(response.data)
        assert not data["valid"]
        assert data["status"] == "not_configured"

    def test_validate_with_empty_cookies(self, client):
        """Test validation with empty string."""
        response = client.post("/api/cookies/validate", json={"cookies": ""})
        assert response.status_code == 200

        data = json.loads(response.data)
        assert not data["valid"]
