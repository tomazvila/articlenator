"""Tests for browser pool module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestBrowserPoolInit:
    """Tests for BrowserPool initialization."""

    def test_browser_pool_default_max_browsers(self):
        """Test BrowserPool has default max_browsers of 2."""
        from twitter_articlenator.sources.browser_pool import BrowserPool

        pool = BrowserPool()
        assert pool._max_browsers == 2

    def test_browser_pool_custom_max_browsers(self):
        """Test BrowserPool accepts custom max_browsers."""
        from twitter_articlenator.sources.browser_pool import BrowserPool

        pool = BrowserPool(max_browsers=5)
        assert pool._max_browsers == 5

    def test_browser_pool_starts_uninitialized(self):
        """Test BrowserPool starts without playwright initialized."""
        from twitter_articlenator.sources.browser_pool import BrowserPool

        pool = BrowserPool()
        assert pool._playwright is None
        assert pool._initialized is False
        assert pool._browser_count == 0


class TestBrowserPoolGetBrowserPool:
    """Tests for get_browser_pool function."""

    def test_get_browser_pool_returns_pool(self):
        """Test get_browser_pool returns a BrowserPool instance."""
        from twitter_articlenator.sources.browser_pool import get_browser_pool, BrowserPool

        pool = get_browser_pool()
        assert isinstance(pool, BrowserPool)

    def test_get_browser_pool_returns_singleton(self):
        """Test get_browser_pool returns the same instance."""
        from twitter_articlenator.sources.browser_pool import get_browser_pool

        pool1 = get_browser_pool()
        pool2 = get_browser_pool()
        assert pool1 is pool2


class TestBrowserPoolAcquireRelease:
    """Tests for browser acquire and release."""

    @pytest.mark.asyncio
    async def test_acquire_creates_browser(self):
        """Test acquire creates a browser when pool is empty."""
        from twitter_articlenator.sources.browser_pool import BrowserPool

        pool = BrowserPool(max_browsers=1)

        # Mock playwright
        mock_browser = AsyncMock()
        mock_browser.is_connected.return_value = True

        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        with patch.object(pool, '_playwright', mock_playwright):
            pool._initialized = True

            browser = await pool.acquire()
            assert browser is mock_browser
            assert pool._browser_count == 1

    @pytest.mark.asyncio
    async def test_release_returns_browser_to_pool(self):
        """Test release returns browser to pool."""
        from twitter_articlenator.sources.browser_pool import BrowserPool

        pool = BrowserPool(max_browsers=2)

        # is_connected is a sync method, so use MagicMock for it
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True

        await pool.release(mock_browser)

        # Browser should be in the pool
        assert pool._browsers.qsize() == 1

    @pytest.mark.asyncio
    async def test_release_closes_disconnected_browser(self):
        """Test release handles disconnected browser."""
        from twitter_articlenator.sources.browser_pool import BrowserPool

        pool = BrowserPool(max_browsers=2)
        pool._browser_count = 1

        # is_connected is a sync method, so use MagicMock for it
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = False

        await pool.release(mock_browser)

        # Browser count should decrease
        assert pool._browser_count == 0
        # Browser should not be in pool
        assert pool._browsers.empty()


class TestBrowserPoolGetContext:
    """Tests for get_context context manager."""

    @pytest.mark.asyncio
    async def test_get_context_yields_context(self):
        """Test get_context yields a browser context."""
        from twitter_articlenator.sources.browser_pool import BrowserPool

        pool = BrowserPool(max_browsers=1)

        # Create mocks
        mock_context = AsyncMock()
        mock_context.add_init_script = AsyncMock()
        mock_context.add_cookies = AsyncMock()
        mock_context.close = AsyncMock()

        mock_browser = AsyncMock()
        mock_browser.is_connected.return_value = True
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        with patch.object(pool, '_playwright', mock_playwright):
            pool._initialized = True

            async with pool.get_context() as context:
                assert context is mock_context

            # Context should be closed after exiting
            mock_context.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_context_adds_cookies(self):
        """Test get_context adds cookies when provided."""
        from twitter_articlenator.sources.browser_pool import BrowserPool

        pool = BrowserPool(max_browsers=1)

        mock_context = AsyncMock()
        mock_context.add_init_script = AsyncMock()
        mock_context.add_cookies = AsyncMock()
        mock_context.close = AsyncMock()

        mock_browser = AsyncMock()
        mock_browser.is_connected.return_value = True
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        cookies = [{"name": "test", "value": "value", "domain": ".x.com", "path": "/"}]

        with patch.object(pool, '_playwright', mock_playwright):
            pool._initialized = True

            async with pool.get_context(cookies=cookies):
                pass

            mock_context.add_cookies.assert_called_once_with(cookies)


class TestBrowserPoolClose:
    """Tests for pool close method."""

    @pytest.mark.asyncio
    async def test_close_stops_playwright(self):
        """Test close stops playwright."""
        from twitter_articlenator.sources.browser_pool import BrowserPool

        pool = BrowserPool()

        mock_playwright = AsyncMock()
        mock_playwright.stop = AsyncMock()

        pool._playwright = mock_playwright
        pool._initialized = True

        await pool.close()

        mock_playwright.stop.assert_called_once()
        assert pool._playwright is None
        assert pool._initialized is False


class TestStealthScript:
    """Tests for stealth script content."""

    def test_stealth_script_exists(self):
        """Test STEALTH_SCRIPT constant exists."""
        from twitter_articlenator.sources.browser_pool import STEALTH_SCRIPT

        assert STEALTH_SCRIPT is not None
        assert len(STEALTH_SCRIPT) > 100

    def test_stealth_script_removes_webdriver(self):
        """Test stealth script removes webdriver property."""
        from twitter_articlenator.sources.browser_pool import STEALTH_SCRIPT

        assert "webdriver" in STEALTH_SCRIPT

    def test_stealth_script_fakes_plugins(self):
        """Test stealth script fakes plugins array."""
        from twitter_articlenator.sources.browser_pool import STEALTH_SCRIPT

        assert "plugins" in STEALTH_SCRIPT

    def test_stealth_script_fakes_languages(self):
        """Test stealth script fakes languages."""
        from twitter_articlenator.sources.browser_pool import STEALTH_SCRIPT

        assert "languages" in STEALTH_SCRIPT

    def test_stealth_script_adds_chrome_runtime(self):
        """Test stealth script adds chrome runtime."""
        from twitter_articlenator.sources.browser_pool import STEALTH_SCRIPT

        assert "chrome" in STEALTH_SCRIPT
        assert "runtime" in STEALTH_SCRIPT

    def test_stealth_script_spoofs_webgl(self):
        """Test stealth script spoofs WebGL."""
        from twitter_articlenator.sources.browser_pool import STEALTH_SCRIPT

        assert "WebGL" in STEALTH_SCRIPT
        assert "Intel" in STEALTH_SCRIPT
