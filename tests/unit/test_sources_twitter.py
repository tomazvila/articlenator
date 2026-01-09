"""Tests for sources/twitter_playwright.py - Twitter Playwright source implementation."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from twitter_articlenator.sources.base import Article


class TestTwitterPlaywrightSourceCanHandle:
    """Tests for TwitterPlaywrightSource.can_handle method."""

    def test_can_handle_x_url(self):
        """Test TwitterPlaywrightSource handles x.com URLs."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source.can_handle("https://x.com/user/status/123456789") is True

    def test_can_handle_x_url_with_www(self):
        """Test TwitterPlaywrightSource handles www.x.com URLs."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source.can_handle("https://www.x.com/user/status/123456789") is True

    def test_can_handle_twitter_url(self):
        """Test TwitterPlaywrightSource handles twitter.com URLs."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source.can_handle("https://twitter.com/user/status/123456789") is True

    def test_can_handle_twitter_url_with_www(self):
        """Test TwitterPlaywrightSource handles www.twitter.com URLs."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source.can_handle("https://www.twitter.com/user/status/123456789") is True

    def test_rejects_non_twitter_url(self):
        """Test TwitterPlaywrightSource rejects non-Twitter URLs."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source.can_handle("https://example.com/article") is False
        assert source.can_handle("https://substack.com/post/123") is False

    def test_rejects_twitter_profile_url(self):
        """Test TwitterPlaywrightSource rejects profile URLs (not status)."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source.can_handle("https://x.com/username") is False
        assert source.can_handle("https://twitter.com/username") is False

    def test_rejects_empty_url(self):
        """Test TwitterPlaywrightSource rejects empty URLs."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source.can_handle("") is False

    def test_rejects_invalid_url(self):
        """Test TwitterPlaywrightSource rejects invalid URLs."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source.can_handle("not-a-url") is False


class TestTwitterPlaywrightSourceInit:
    """Tests for TwitterPlaywrightSource initialization."""

    def test_init_without_cookies(self):
        """Test TwitterPlaywrightSource can be initialized without cookies."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source._cookies_str is None

    def test_init_with_cookies(self):
        """Test TwitterPlaywrightSource can be initialized with cookies."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        cookies = "auth_token=abc123; ct0=xyz789"
        source = TwitterPlaywrightSource(cookies=cookies)
        assert source._cookies_str == cookies


class TestParseCookies:
    """Tests for TwitterPlaywrightSource._parse_cookies method."""

    def test_parse_cookies_empty(self):
        """Test parsing empty cookies."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source._parse_cookies() == []

    def test_parse_cookies_single(self):
        """Test parsing single cookie."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource(cookies="auth_token=abc123")
        cookies = source._parse_cookies()
        # Should have 2 entries (one for x.com, one for twitter.com)
        assert len(cookies) == 2
        assert cookies[0]["name"] == "auth_token"
        assert cookies[0]["value"] == "abc123"
        assert cookies[0]["domain"] == ".x.com"
        assert cookies[1]["domain"] == ".twitter.com"

    def test_parse_cookies_multiple(self):
        """Test parsing multiple cookies."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource(cookies="auth_token=abc; ct0=xyz")
        cookies = source._parse_cookies()
        # Should have 4 entries (2 for each domain)
        assert len(cookies) == 4
        names = [c["name"] for c in cookies]
        assert names.count("auth_token") == 2
        assert names.count("ct0") == 2


class TestTruncateTitle:
    """Tests for TwitterPlaywrightSource._truncate_title method."""

    def test_short_title_unchanged(self):
        """Test short titles are not truncated."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        text = "Short title"
        assert source._truncate_title(text) == "Short title"

    def test_long_title_truncated(self):
        """Test long titles are truncated with ellipsis."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        text = "x" * 150
        result = source._truncate_title(text)
        assert len(result) == 100
        assert result.endswith("...")

    def test_newlines_removed(self):
        """Test newlines are removed from title."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        text = "Line 1\nLine 2\nLine 3"
        result = source._truncate_title(text)
        assert "\n" not in result
        assert result == "Line 1 Line 2 Line 3"


class TestCreateArticle:
    """Tests for TwitterPlaywrightSource._create_article method."""

    def test_create_article_basic(self):
        """Test creating article from tweet data."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        tweet_data = {
            "author": "testuser",
            "display_name": "Test User",
            "content": "Test tweet content",
            "timestamp": datetime(2025, 12, 29, 10, 0, 0, tzinfo=timezone.utc),
            "quoted_tweets": [],
        }

        article = source._create_article(tweet_data, "https://x.com/testuser/status/123")

        assert isinstance(article, Article)
        assert article.author == "testuser"
        assert article.source_url == "https://x.com/testuser/status/123"
        assert article.source_type == "twitter"
        assert "Test tweet content" in article.content

    def test_create_article_with_replies(self):
        """Test creating article with replies."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        tweet_data = {
            "author": "testuser",
            "display_name": "Test User",
            "content": "Main tweet",
            "timestamp": None,
            "images": [],
            "replies": [
                {
                    "author": "replier",
                    "display_name": "Reply User",
                    "content": "Reply content here",
                    "images": [],
                    "is_op": False,
                }
            ],
        }

        article = source._create_article(tweet_data, "https://x.com/testuser/status/123")

        assert "Main tweet" in article.content
        assert "Reply content here" in article.content
        assert "Reply User" in article.content

    def test_create_article_without_content(self):
        """Test creating article without content generates default title."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        tweet_data = {
            "author": "testuser",
            "display_name": "Test User",
            "content": "",
            "timestamp": None,
            "quoted_tweets": [],
        }

        article = source._create_article(tweet_data, "https://x.com/testuser/status/123")

        assert article.title == "Tweet by @testuser"


class TestFetch:
    """Tests for TwitterPlaywrightSource.fetch method."""

    @pytest.mark.asyncio
    async def test_fetch_requires_cookies(self):
        """Test fetch raises error when cookies not configured."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()  # No cookies

        with pytest.raises(ValueError, match="[Cc]ookies.*required"):
            await source.fetch("https://x.com/user/status/123")

    @pytest.mark.asyncio
    async def test_fetch_invalid_url_raises_error(self):
        """Test fetch raises error for invalid URL."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource(cookies="auth_token=test; ct0=test")

        with pytest.raises(ValueError, match="Invalid Twitter URL"):
            await source.fetch("https://example.com/not-twitter")

    @pytest.mark.asyncio
    async def test_fetch_with_mocked_browser_pool(self):
        """Test fetch with mocked browser pool."""
        from contextlib import asynccontextmanager
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource(cookies="auth_token=test; ct0=test")

        # Mock the _extract_tweet_data method
        mock_tweet_data = {
            "author": "testuser",
            "display_name": "Test User",
            "content": "Test tweet content",
            "timestamp": datetime(2025, 12, 29, 10, 0, 0, tzinfo=timezone.utc),
            "quoted_tweets": [],
        }

        # Create mock page and context
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.evaluate = AsyncMock()
        mock_page.screenshot = AsyncMock()
        mock_page.title = AsyncMock(return_value="Test Page")
        mock_page.url = "https://x.com/testuser/status/123456789"

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        # Create a mock browser pool
        mock_pool = AsyncMock()

        @asynccontextmanager
        async def mock_get_context(cookies=None):
            yield mock_context

        mock_pool.get_context = mock_get_context

        with patch(
            "twitter_articlenator.sources.twitter_playwright.get_browser_pool",
            return_value=mock_pool
        ):
            # Mock _extract_tweet_data
            with patch.object(
                source, "_extract_tweet_data", new_callable=AsyncMock
            ) as mock_extract:
                mock_extract.return_value = mock_tweet_data

                article = await source.fetch("https://x.com/testuser/status/123456789")

                assert isinstance(article, Article)
                assert article.author == "testuser"
                assert "Test tweet content" in article.content


class TestSourceRegistry:
    """Tests for source registry with TwitterPlaywrightSource."""

    def test_twitter_source_alias(self):
        """Test TwitterSource is aliased to TwitterPlaywrightSource."""
        from twitter_articlenator.sources import TwitterSource, TwitterPlaywrightSource

        assert TwitterSource is TwitterPlaywrightSource

    def test_get_source_for_twitter_url(self):
        """Test get_source_for_url returns TwitterPlaywrightSource for Twitter URLs."""
        from twitter_articlenator.sources import get_source_for_url, TwitterPlaywrightSource

        source = get_source_for_url("https://x.com/user/status/123")
        assert isinstance(source, TwitterPlaywrightSource)

    def test_get_source_for_twitter_url_with_cookies(self):
        """Test get_source_for_url passes cookies to source."""
        from twitter_articlenator.sources import get_source_for_url, TwitterPlaywrightSource

        source = get_source_for_url(
            "https://x.com/user/status/123",
            cookies="auth_token=test; ct0=test"
        )
        assert isinstance(source, TwitterPlaywrightSource)
        assert source._cookies_str == "auth_token=test; ct0=test"
