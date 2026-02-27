"""Bookmark scraper using Playwright browser automation."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog
from playwright.async_api._generated import SetCookieParam

from .browser_pool import get_browser_pool

log = structlog.get_logger()

# Delay between scrolls (seconds)
SCROLL_DELAY = 3.0

# Number of consecutive scrolls with no new bookmarks before stopping
MAX_EMPTY_SCROLLS = 3


@dataclass
class BookmarkEntry:
    """A single bookmarked tweet with extracted metadata."""

    tweet_id: str
    tweet_url: str
    author: str
    display_name: str
    text_preview: str
    article_urls: list[str] = field(default_factory=list)
    is_article: bool = False
    bookmarked_at: str | None = None

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "tweet_id": self.tweet_id,
            "tweet_url": self.tweet_url,
            "author": self.author,
            "display_name": self.display_name,
            "text_preview": self.text_preview,
            "article_urls": self.article_urls,
            "is_article": self.is_article,
            "bookmarked_at": self.bookmarked_at,
        }


class BookmarkScraper:
    """Scrape bookmarks from x.com/i/bookmarks using Playwright.

    Uses the same browser pool and cookie approach as TwitterPlaywrightSource.
    """

    # Max text preview length
    MAX_PREVIEW_LENGTH = 200

    # URLs to ignore when extracting article links
    IGNORE_URL_PATTERNS = re.compile(
        r"https?://(?:www\.)?(?:twitter\.com|x\.com|t\.co)/",
    )

    # Pattern matching X Article URLs (native long-form articles on X)
    X_ARTICLE_URL_PATTERN = re.compile(
        r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/article/",
    )

    def __init__(self, cookies: str) -> None:
        """Initialize with cookie string.

        Args:
            cookies: Twitter authentication cookies (auth_token=...; ct0=...).
        """
        self._cookies_str = cookies

    def _parse_cookies(self) -> list[SetCookieParam]:
        """Parse cookie string into Playwright cookie format."""
        cookies: list[SetCookieParam] = []
        for part in self._cookies_str.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                cookies.append(
                    SetCookieParam(
                        name=name.strip(),
                        value=value.strip(),
                        domain=".x.com",
                        path="/",
                    )
                )
                cookies.append(
                    SetCookieParam(
                        name=name.strip(),
                        value=value.strip(),
                        domain=".twitter.com",
                        path="/",
                    )
                )
        return cookies

    async def scrape(
        self,
        on_bookmark: "Callable[[BookmarkEntry, int], None] | None" = None,
    ) -> list[BookmarkEntry]:
        """Scrape all bookmarks from x.com/i/bookmarks.

        Args:
            on_bookmark: Optional callback invoked for each new bookmark found.
                         Receives (entry, running_total).

        Returns:
            List of BookmarkEntry objects.
        """
        log.info("bookmark_scrape_starting")

        pool = get_browser_pool()
        cookies = self._parse_cookies()

        async with pool.get_context(cookies=cookies) as context:
            page = await context.new_page()

            # Establish session by visiting home first (same as TwitterPlaywrightSource)
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            # Verify authentication — fail fast if cookies are expired
            try:
                await page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
                log.info("bookmark_scrape_authenticated")
            except Exception:
                page_url = page.url
                page_title = await page.title()
                log.error(
                    "bookmark_scrape_auth_failed",
                    page_url=page_url,
                    page_title=page_title,
                )
                # Detect login redirect — cookies are expired
                if "login" in page_url or "Log in" in page_title:
                    raise ValueError(
                        "Authentication failed — your Twitter cookies have expired. "
                        "Please go to Setup and enter fresh cookies."
                    )
                raise ValueError(
                    "Could not verify Twitter authentication. "
                    "Please check your cookies and try again."
                )

            # Navigate to bookmarks using React Router (same approach as
            # TwitterPlaywrightSource which is known to work reliably)
            await page.evaluate("""
                window.history.pushState({}, '', '/i/bookmarks');
                window.dispatchEvent(new PopStateEvent('popstate'));
            """)
            await asyncio.sleep(5)

            # If pushState didn't navigate, fall back to goto
            if "/i/bookmarks" not in page.url:
                log.info("bookmark_pushstate_fallback", page_url=page.url)
                await page.goto(
                    "https://x.com/i/bookmarks",
                    wait_until="domcontentloaded",
                    timeout=60000,
                    referer="https://x.com/home",
                )
                await asyncio.sleep(5)

            # Wait for bookmarks to load (generous timeout for slow environments)
            try:
                await page.wait_for_selector('[data-testid="tweet"]', timeout=30000)
            except Exception:
                # Check if it's an empty bookmarks page
                page_text = await page.inner_text("body")
                if "haven't added any" in page_text or "Save posts for later" in page_text:
                    log.info("bookmark_scrape_empty")
                    return []

                # Save debug info before raising
                try:
                    await page.screenshot(path="/tmp/bookmark_debug.png")
                    log.error(
                        "bookmark_page_load_failed",
                        page_url=page.url,
                        page_title=await page.title(),
                        screenshot="/tmp/bookmark_debug.png",
                    )
                except Exception:
                    pass

                raise ValueError(
                    f"Bookmarks page failed to load (url={page.url}). "
                    "Check your cookies or try again."
                )

            log.info("bookmark_scrape_page_loaded")

            # Scroll and collect
            bookmarks: list[BookmarkEntry] = []
            seen_ids: set[str] = set()
            empty_scroll_count = 0

            while empty_scroll_count < MAX_EMPTY_SCROLLS:
                # Extract all visible tweet elements
                tweet_elements = await page.query_selector_all('article[data-testid="tweet"]')

                new_count = 0
                for tweet_el in tweet_elements:
                    entry = await self._extract_bookmark(tweet_el)
                    if entry and entry.tweet_id not in seen_ids:
                        seen_ids.add(entry.tweet_id)
                        bookmarks.append(entry)
                        new_count += 1
                        if on_bookmark:
                            on_bookmark(entry, len(bookmarks))

                if new_count > 0:
                    empty_scroll_count = 0
                    log.info("bookmark_scrape_progress", total=len(bookmarks), new=new_count)
                else:
                    empty_scroll_count += 1
                    log.debug("bookmark_scrape_no_new", empty_scrolls=empty_scroll_count)

                # Scroll down
                await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(SCROLL_DELAY)

            log.info("bookmark_scrape_complete", total=len(bookmarks))
            return bookmarks

    async def _extract_bookmark(self, tweet_el) -> BookmarkEntry | None:
        """Extract a BookmarkEntry from a tweet article element.

        Args:
            tweet_el: Playwright element handle for an article[data-testid="tweet"].

        Returns:
            BookmarkEntry or None if extraction fails.
        """
        try:
            # Extract tweet URL (contains /status/ID)
            tweet_url = ""
            tweet_id = ""
            status_links = await tweet_el.query_selector_all('a[href*="/status/"]')
            for link in status_links:
                href = await link.get_attribute("href")
                if (
                    href
                    and "/status/" in href
                    and "/analytics" not in href
                    and "/photo/" not in href
                ):
                    # Normalize to full URL
                    if href.startswith("/"):
                        href = f"https://x.com{href}"
                    tweet_url = href
                    # Extract tweet ID
                    match = re.search(r"/status/(\d+)", href)
                    if match:
                        tweet_id = match.group(1)
                    break

            if not tweet_id:
                return None

            # Extract author
            author = ""
            display_name = ""
            username_el = await tweet_el.query_selector('[data-testid="User-Name"]')
            if username_el:
                # Display name is the first span
                name_span = await username_el.query_selector("span")
                if name_span:
                    display_name = await name_span.inner_text()

                # Username is in the link href
                author_link = await username_el.query_selector("a")
                if author_link:
                    href = await author_link.get_attribute("href")
                    if href:
                        author = href.strip("/").split("/")[0]

            # Extract tweet text
            text_preview = ""
            text_el = await tweet_el.query_selector('[data-testid="tweetText"]')
            if text_el:
                full_text = await text_el.inner_text()
                text_preview = full_text[: self.MAX_PREVIEW_LENGTH]
                if len(full_text) > self.MAX_PREVIEW_LENGTH:
                    text_preview += "..."

            # Extract external article URLs (non-Twitter links)
            article_urls = []
            if text_el:
                links = await text_el.query_selector_all("a")
                for link in links:
                    href = await link.get_attribute("href")
                    if (
                        href
                        and href.startswith("http")
                        and not self.IGNORE_URL_PATTERNS.match(href)
                    ):
                        article_urls.append(href)

            # Also check for card links (preview cards for articles)
            card_links = await tweet_el.query_selector_all('[data-testid="card.wrapper"] a[href]')
            is_article = False
            for card_link in card_links:
                href = await card_link.get_attribute("href")
                if not href or not href.startswith("http"):
                    continue
                # Check if this is a native X Article link
                if self.X_ARTICLE_URL_PATTERN.match(href):
                    is_article = True
                    if href not in article_urls:
                        article_urls.append(href)
                elif not self.IGNORE_URL_PATTERNS.match(href):
                    if href not in article_urls:
                        article_urls.append(href)

            # Also detect X Articles by looking for all links with /article/ pattern
            # (may appear outside card wrappers)
            all_links = await tweet_el.query_selector_all("a[href]")
            for link in all_links:
                href = await link.get_attribute("href")
                if href:
                    if href.startswith("/"):
                        href = f"https://x.com{href}"
                    if self.X_ARTICLE_URL_PATTERN.match(href):
                        is_article = True
                        if href not in article_urls:
                            article_urls.append(href)

            # Extract timestamp
            bookmarked_at = None
            time_el = await tweet_el.query_selector("time")
            if time_el:
                bookmarked_at = await time_el.get_attribute("datetime")

            return BookmarkEntry(
                tweet_id=tweet_id,
                tweet_url=tweet_url,
                author=author,
                display_name=display_name,
                text_preview=text_preview,
                article_urls=article_urls,
                is_article=is_article,
                bookmarked_at=bookmarked_at,
            )

        except Exception as e:
            log.warning("bookmark_extract_failed", error=str(e))
            return None
