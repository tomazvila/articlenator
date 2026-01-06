"""Tests for async event loop issues and cookie endpoints.

These tests verify that multiple sequential API calls work correctly
with the Playwright-based Twitter source.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAsyncEventLoopIssues:
    """Tests for event loop isolation between requests."""

    def test_multiple_cookie_status_checks(self, client):
        """Test that multiple cookie status checks don't cause event loop issues."""
        # First call
        response1 = client.get("/api/cookies/status")
        assert response1.status_code == 200

        # Second call should not fail with event loop errors
        response2 = client.get("/api/cookies/status")
        assert response2.status_code == 200

        # Third call
        response3 = client.get("/api/cookies/status")
        assert response3.status_code == 200

    def test_cookie_status_then_test(self, client):
        """Test checking status then testing cookies doesn't cause event loop issues."""
        # Save some cookies first (long enough to pass validation)
        client.post("/api/cookies", json={"cookies": "auth_token=abcdefghijklmnopqrstuvwxyz; ct0=abcdefghijklmnopqrstuvwxyz"})

        # Check status (creates one async call)
        response1 = client.get("/api/cookies/status")
        assert response1.status_code == 200

        # Test cookies (creates another async call) - should not fail with lock errors
        response2 = client.get("/api/cookies/status?test=true")
        assert response2.status_code == 200
        data = json.loads(response2.data)
        # Should succeed, not fail with event loop errors
        assert "event loop" not in data.get("message", "").lower()
        assert "Lock object" not in data.get("message", "")

    def test_convert_after_status_check(self, client):
        """Test conversion after status check doesn't cause event loop issues."""
        from contextlib import asynccontextmanager

        # Save cookies (long enough to pass validation)
        client.post("/api/cookies", json={"cookies": "auth_token=abcdefghijklmnopqrstuvwxyz; ct0=abcdefghijklmnopqrstuvwxyz"})

        # Check status first
        r1 = client.get("/api/cookies/status")
        assert r1.status_code == 200

        mock_tweet_data = {
            "author": "testuser",
            "display_name": "Test User",
            "content": "Test tweet content",
            "timestamp": None,
            "quoted_tweets": [],
        }

        # Create mock page and context
        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        # Create a mock browser pool with get_context
        mock_pool = MagicMock()

        @asynccontextmanager
        async def mock_get_context(cookies=None):
            yield mock_context

        mock_pool.get_context = mock_get_context

        # Mock the browser pool
        with patch(
            "twitter_articlenator.sources.twitter_playwright.get_browser_pool",
            return_value=mock_pool
        ):
            # Mock the _extract_tweet_data method
            with patch(
                "twitter_articlenator.sources.twitter_playwright.TwitterPlaywrightSource._extract_tweet_data",
                new_callable=AsyncMock,
                return_value=mock_tweet_data
            ):
                # Now try to convert
                r2 = client.post("/api/convert", json={"links": ["https://x.com/user/status/123"]})
                data = json.loads(r2.data)
                error_msg = data.get("error", "")
                # Should not have event loop errors
                assert "event loop" not in error_msg.lower()
                assert "Lock object" not in error_msg

    def test_multiple_conversions(self, client):
        """Test multiple conversions don't cause event loop issues."""
        from contextlib import asynccontextmanager

        # Save cookies (long enough to pass validation)
        client.post("/api/cookies", json={"cookies": "auth_token=abcdefghijklmnopqrstuvwxyz; ct0=abcdefghijklmnopqrstuvwxyz"})

        mock_tweet_data = {
            "author": "testuser",
            "display_name": "Test User",
            "content": "Test tweet content",
            "timestamp": None,
            "quoted_tweets": [],
        }

        # Create mock page and context
        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        # Create a mock browser pool with get_context
        mock_pool = MagicMock()

        @asynccontextmanager
        async def mock_get_context(cookies=None):
            yield mock_context

        mock_pool.get_context = mock_get_context

        # Mock the browser pool
        with patch(
            "twitter_articlenator.sources.twitter_playwright.get_browser_pool",
            return_value=mock_pool
        ):
            with patch(
                "twitter_articlenator.sources.twitter_playwright.TwitterPlaywrightSource._extract_tweet_data",
                new_callable=AsyncMock,
                return_value=mock_tweet_data
            ):
                # First conversion attempt
                r1 = client.post("/api/convert", json={"links": ["https://x.com/user/status/123"]})
                data1 = json.loads(r1.data)
                error1 = data1.get("error", "")
                assert "Lock object" not in error1

                # Second conversion attempt
                r2 = client.post("/api/convert", json={"links": ["https://x.com/user/status/456"]})
                data2 = json.loads(r2.data)
                error2 = data2.get("error", "")
                assert "Lock object" not in error2

    def test_interleaved_operations(self, client):
        """Test interleaved async operations don't conflict."""
        client.post("/api/cookies", json={"cookies": "auth_token=abcdefghijklmnopqrstuvwxyz; ct0=abcdefghijklmnopqrstuvwxyz"})

        # Interleave different async operations
        for _ in range(3):
            r1 = client.get("/api/cookies/status")
            assert r1.status_code == 200

            r2 = client.get("/api/cookies/status?test=true")
            assert r2.status_code == 200


class TestCookiesCurrentEndpoint:
    """Tests for GET /api/cookies/current endpoint."""

    def test_no_cookies_configured(self, client, tmp_path, monkeypatch):
        """Test response when no cookies are configured."""
        # Use a fresh config dir to ensure no cookies
        import twitter_articlenator.config as config_module
        config_module._config_instance = None
        monkeypatch.setenv("TWITTER_ARTICLENATOR_CONFIG_DIR", str(tmp_path / "fresh_config"))

        response = client.get("/api/cookies/current")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["configured"] is False
        assert data["cookies"] == []

    def test_cookies_are_masked(self, client):
        """Test that cookie values are properly masked."""
        # Save cookies with known values
        client.post("/api/cookies", json={
            "cookies": "auth_token=abcdefghijklmnop; ct0=1234567890abcdef"
        })

        response = client.get("/api/cookies/current")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["configured"] is True
        assert len(data["cookies"]) == 2

        # Check masking
        for cookie in data["cookies"]:
            assert "..." in cookie["value_masked"]
            # Full value should not be present
            assert cookie["value_masked"] != "abcdefghijklmnop"
            assert cookie["value_masked"] != "1234567890abcdef"

    def test_cookies_have_required_fields(self, client):
        """Test that cookie info has all required fields."""
        client.post("/api/cookies", json={"cookies": "auth_token=test123; ct0=csrf456"})

        response = client.get("/api/cookies/current")
        data = json.loads(response.data)

        for cookie in data["cookies"]:
            assert "name" in cookie
            assert "value_masked" in cookie
            assert "length" in cookie

    def test_short_cookie_values_masked(self, client):
        """Test that short cookie values are also masked."""
        client.post("/api/cookies", json={"cookies": "short=abc"})

        response = client.get("/api/cookies/current")
        data = json.loads(response.data)

        assert data["configured"] is True
        cookie = data["cookies"][0]
        assert cookie["name"] == "short"
        assert cookie["value_masked"] != "abc"  # Should be masked


class TestCookieStatusValidation:
    """Tests for cookie status validation (format checking)."""

    def test_status_with_valid_cookies(self, client):
        """Test status shows working with properly formatted cookies."""
        # Long enough cookies to pass validation
        client.post("/api/cookies", json={
            "cookies": "auth_token=abcdefghijklmnopqrstuvwxyz; ct0=abcdefghijklmnopqrstuvwxyz"
        })

        response = client.get("/api/cookies/status?test=true")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["status"] == "working"

    def test_status_with_short_auth_token(self, client):
        """Test status shows invalid with too short auth_token."""
        client.post("/api/cookies", json={
            "cookies": "auth_token=short; ct0=abcdefghijklmnopqrstuvwxyz"
        })

        response = client.get("/api/cookies/status?test=true")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["status"] == "invalid"
        assert "auth_token" in data["message"]

    def test_status_with_missing_ct0(self, client):
        """Test status shows invalid with missing ct0."""
        client.post("/api/cookies", json={
            "cookies": "auth_token=abcdefghijklmnopqrstuvwxyz"
        })

        response = client.get("/api/cookies/status?test=true")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["status"] == "invalid"
        assert "ct0" in data["message"]

    def test_status_without_test_parameter(self, client):
        """Test status without test parameter just checks if configured."""
        client.post("/api/cookies", json={
            "cookies": "auth_token=abcdefghijklmnopqrstuvwxyz; ct0=abcdefghijklmnopqrstuvwxyz"
        })

        response = client.get("/api/cookies/status")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["status"] == "configured"
        assert "not tested" in data["message"].lower()
