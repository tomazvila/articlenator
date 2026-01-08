"""Browser pool for efficient Playwright browser management."""

import asyncio
import random
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api._generated import SetCookieParam

log = structlog.get_logger()

# Comprehensive stealth script to avoid bot detection
STEALTH_SCRIPT = """
// Remove webdriver property
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});

// Fake plugins array (Chrome typically has 3-5 plugins)
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' }
        ];
        plugins.length = 3;
        return plugins;
    }
});

// Fake languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en']
});

// Chrome runtime (indicates real Chrome)
if (!window.chrome) {
    window.chrome = {};
}
if (!window.chrome.runtime) {
    window.chrome.runtime = {};
}

// Spoof permissions API
const originalQuery = window.navigator.permissions?.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
    );
}

// Override iframe contentWindow access detection
const originalAttachShadow = Element.prototype.attachShadow;
Element.prototype.attachShadow = function(init) {
    if (init && init.mode === 'closed') {
        init.mode = 'open';
    }
    return originalAttachShadow.call(this, init);
};

// Spoof WebGL vendor/renderer
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) {
        return 'Intel Inc.';
    }
    if (parameter === 37446) {
        return 'Intel Iris OpenGL Engine';
    }
    return getParameter.apply(this, arguments);
};

// Fix for WebGL2 as well
const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
WebGL2RenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) {
        return 'Intel Inc.';
    }
    if (parameter === 37446) {
        return 'Intel Iris OpenGL Engine';
    }
    return getParameter2.apply(this, arguments);
};

// Hide automation-related properties
delete navigator.__proto__.webdriver;

// Spoof screen properties to match viewport
Object.defineProperty(screen, 'availWidth', { get: () => window.innerWidth });
Object.defineProperty(screen, 'availHeight', { get: () => window.innerHeight });
"""


class BrowserPool:
    """Manages a pool of reusable browser instances.

    This avoids the overhead of launching a new browser for each request,
    which can take 500ms-2s per launch.
    """

    def __init__(self, max_browsers: int = 2):
        """Initialize the browser pool.

        Args:
            max_browsers: Maximum number of browsers to keep in the pool.
        """
        self._max_browsers = max_browsers
        self._playwright: Playwright | None = None
        self._browsers: asyncio.Queue[Browser] = asyncio.Queue(maxsize=max_browsers)
        self._browser_count = 0
        self._lock = asyncio.Lock()
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure Playwright is started."""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            self._playwright = await async_playwright().start()
            self._initialized = True
            log.info("browser_pool_initialized", max_browsers=self._max_browsers)

    async def _create_browser(self) -> Browser:
        """Create a new browser instance with stealth settings."""
        await self._ensure_initialized()
        assert self._playwright is not None

        browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        self._browser_count += 1
        log.info("browser_created", total_browsers=self._browser_count)
        return browser

    async def acquire(self) -> Browser:
        """Acquire a browser from the pool.

        Returns a browser from the pool if available, otherwise creates a new one.

        Returns:
            A Browser instance.
        """
        await self._ensure_initialized()

        # Try to get from pool first
        try:
            browser = self._browsers.get_nowait()
            # Check if browser is still connected
            if browser.is_connected():
                log.debug("browser_acquired_from_pool")
                return browser
            else:
                log.warning("browser_disconnected_creating_new")
                self._browser_count -= 1
        except asyncio.QueueEmpty:
            pass

        # Create new browser if pool is empty and under limit
        async with self._lock:
            if self._browser_count < self._max_browsers:
                return await self._create_browser()

        # Pool is at capacity, wait for one to be released
        log.debug("waiting_for_browser")
        browser = await self._browsers.get()
        if browser.is_connected():
            return browser

        # Browser disconnected, create new one
        self._browser_count -= 1
        return await self._create_browser()

    async def release(self, browser: Browser) -> None:
        """Release a browser back to the pool.

        Args:
            browser: The browser to release.
        """
        if not browser.is_connected():
            log.warning("releasing_disconnected_browser")
            self._browser_count -= 1
            return

        try:
            self._browsers.put_nowait(browser)
            log.debug("browser_released_to_pool")
        except asyncio.QueueFull:
            # Pool is full, close this browser
            await browser.close()
            self._browser_count -= 1
            log.debug("browser_closed_pool_full")

    @asynccontextmanager
    async def get_context(
        self, cookies: list[SetCookieParam] | None = None
    ) -> AsyncIterator[BrowserContext]:
        """Get a browser context from the pool.

        This is a context manager that acquires a browser, creates a context,
        and properly releases the browser when done.

        Args:
            cookies: Optional list of cookies to add to the context.

        Yields:
            A BrowserContext with stealth settings applied.
        """
        browser = await self.acquire()
        context = None

        try:
            # Randomize viewport slightly to avoid fingerprinting
            viewport_width = 1920 + random.randint(-100, 100)
            viewport_height = 1080 + random.randint(-50, 50)

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": viewport_width, "height": viewport_height},
                screen={"width": viewport_width, "height": viewport_height},
                locale="en-US",
                timezone_id="America/Los_Angeles",
                color_scheme="light",
                has_touch=False,
                is_mobile=False,
                device_scale_factor=1,
            )

            # Add comprehensive stealth script
            await context.add_init_script(STEALTH_SCRIPT)

            # Add cookies if provided
            if cookies:
                await context.add_cookies(cookies)

            yield context

        finally:
            if context:
                await context.close()
            await self.release(browser)

    async def close(self) -> None:
        """Close all browsers and shutdown Playwright."""
        # Close all browsers in the pool
        while not self._browsers.empty():
            try:
                browser = self._browsers.get_nowait()
                if browser.is_connected():
                    await browser.close()
            except asyncio.QueueEmpty:
                break

        # Stop Playwright
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        self._initialized = False
        self._browser_count = 0
        log.info("browser_pool_closed")


# Global browser pool instance
_browser_pool: BrowserPool | None = None


def get_browser_pool() -> BrowserPool:
    """Get the global browser pool instance.

    Returns:
        The global BrowserPool instance.
    """
    global _browser_pool
    if _browser_pool is None:
        _browser_pool = BrowserPool(max_browsers=2)
    return _browser_pool
