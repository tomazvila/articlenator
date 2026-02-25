"""Bookmark scraper using Playwright browser automation."""

import asyncio
import re
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

    async def scrape(self) -> list[BookmarkEntry]:
        """Scrape all bookmarks from x.com/i/bookmarks.

        Returns:
            List of BookmarkEntry objects.
        """
        log.info("bookmark_scrape_starting")

        pool = get_browser_pool()
        cookies = self._parse_cookies()

        async with pool.get_context(cookies=cookies) as context:
            page = await context.new_page()

            # Establish session by visiting home first (same as TwitterPlaywrightSource)
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Verify authentication
            try:
                await page.wait_for_selector('[data-testid="tweet"]', timeout=10000)
                log.info("bookmark_scrape_authenticated")
            except Exception:
                log.warning("bookmark_scrape_auth_unclear", hint="Home feed did not load")

            # Navigate to bookmarks
            await page.goto(
                "https://x.com/i/bookmarks",
                wait_until="domcontentloaded",
                timeout=30000,
                referer="https://x.com/home",
            )
            await asyncio.sleep(3)

            # Wait for bookmarks to load
            try:
                await page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
            except Exception:
                # Check if it's an empty bookmarks page
                empty = await page.query_selector(
                    'text="You haven\'t added any posts to your Bookmarks yet"'
                )
                if empty:
                    log.info("bookmark_scrape_empty")
                    return []
                raise ValueError("Bookmarks page failed to load. Check your cookies.")

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
            card_link = await tweet_el.query_selector('[data-testid="card.wrapper"] a[href]')
            if card_link:
                href = await card_link.get_attribute("href")
                if href and href.startswith("http") and not self.IGNORE_URL_PATTERNS.match(href):
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
                bookmarked_at=bookmarked_at,
            )

        except Exception as e:
            log.warning("bookmark_extract_failed", error=str(e))
            return None
