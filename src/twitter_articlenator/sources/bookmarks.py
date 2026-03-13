"""Bookmark scraper using Playwright with GraphQL API interception.

Instead of parsing DOM elements (which misses article URLs due to t.co
shortening), this intercepts Twitter's GraphQL API responses to get
expanded URLs and structured tweet data directly.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from playwright.async_api._generated import SetCookieParam

from .browser_pool import get_browser_pool

log = structlog.get_logger()

# Delay between scrolls (seconds) — fast enough to paginate 500+ bookmarks
SCROLL_DELAY = 1.5

# Consecutive scrolls with no new bookmarks before stopping.
# Generous to handle Twitter's lazy-loading gaps.
MAX_EMPTY_SCROLLS = 15


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

    Intercepts GraphQL API responses for reliable data extraction.
    This captures expanded URLs (not t.co) and structured tweet data
    directly from Twitter's API responses.
    """

    MAX_PREVIEW_LENGTH = 200

    # Internal Twitter/X URLs to ignore (not external articles)
    IGNORE_URL_PATTERNS = re.compile(
        r"https?://(?:www\.)?(?:twitter\.com|x\.com)/",
    )

    # Pattern matching X Article URLs (native long-form articles on X)
    X_ARTICLE_URL_PATTERN = re.compile(
        r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/article/",
    )

    def __init__(self, cookies: str) -> None:
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
        on_bookmark: Callable[[BookmarkEntry, int], None] | None = None,
    ) -> list[BookmarkEntry]:
        """Scrape all bookmarks from x.com/i/bookmarks.

        Intercepts GraphQL API responses while scrolling the page to
        trigger pagination. Returns BookmarkEntry objects with expanded
        (real) URLs extracted from the API data.
        """
        log.info("bookmark_scrape_starting")

        pool = get_browser_pool()
        cookies = self._parse_cookies()

        async with pool.get_context(cookies=cookies) as context:
            page = await context.new_page()

            # Collect entries intercepted from GraphQL API responses
            intercepted: list[BookmarkEntry] = []

            async def on_response(response):
                try:
                    url = response.url
                    if "/graphql/" not in url or "Bookmark" not in url:
                        return
                    if response.status != 200:
                        return
                    body = await response.json()
                    entries = self._parse_graphql_response(body)
                    if entries:
                        intercepted.extend(entries)
                        log.info(
                            "bookmark_api_intercepted",
                            count=len(entries),
                            total=len(intercepted),
                        )
                except Exception as e:
                    log.debug("bookmark_response_handler_error", error=str(e))

            page.on("response", on_response)

            # Establish session by visiting home first
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            await self._dismiss_consent_banner(page)

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
                if "login" in page_url or "Log in" in page_title:
                    raise ValueError(
                        "Authentication failed — your Twitter cookies have expired. "
                        "Please go to Setup and enter fresh cookies."
                    )
                raise ValueError(
                    "Could not verify Twitter authentication. "
                    "Please check your cookies and try again."
                )

            # Navigate to bookmarks (triggers first GraphQL Bookmarks call)
            await page.goto(
                "https://x.com/i/bookmarks",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await asyncio.sleep(5)

            await self._dismiss_consent_banner(page)

            # Wait for bookmarks page to load
            try:
                await page.wait_for_selector('[data-testid="tweet"]', timeout=30000)
            except Exception:
                page_text = await page.inner_text("body")
                if "haven't added any" in page_text or "Save posts for later" in page_text:
                    log.info("bookmark_scrape_empty")
                    return []

                try:
                    await page.screenshot(path="/tmp/bookmark_debug.png")
                    log.error(
                        "bookmark_page_load_failed",
                        page_url=page.url,
                        page_title=await page.title(),
                    )
                except Exception:
                    pass

                raise ValueError(
                    f"Bookmarks page failed to load (url={page.url}). "
                    "Check your cookies or try again."
                )

            log.info("bookmark_scrape_page_loaded")

            # Give time for the initial API response handler to complete
            await asyncio.sleep(2)

            # Scroll and collect from intercepted API responses
            bookmarks: list[BookmarkEntry] = []
            seen_ids: set[str] = set()
            empty_scroll_count = 0
            last_intercepted_len = 0

            while empty_scroll_count < MAX_EMPTY_SCROLLS:
                # Process new entries from API interception
                current_len = len(intercepted)
                new_entries = intercepted[last_intercepted_len:current_len]
                last_intercepted_len = current_len

                new_count = 0
                for entry in new_entries:
                    if entry.tweet_id not in seen_ids:
                        seen_ids.add(entry.tweet_id)
                        bookmarks.append(entry)
                        new_count += 1
                        if on_bookmark:
                            on_bookmark(entry, len(bookmarks))

                if new_count > 0:
                    empty_scroll_count = 0
                    log.info(
                        "bookmark_scrape_progress",
                        total=len(bookmarks),
                        new=new_count,
                    )
                else:
                    empty_scroll_count += 1
                    log.debug(
                        "bookmark_scrape_no_new",
                        empty_scrolls=empty_scroll_count,
                    )

                # Scroll 2x viewport height for faster pagination
                await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                await asyncio.sleep(SCROLL_DELAY)

            log.info("bookmark_scrape_complete", total=len(bookmarks))
            return bookmarks

    @staticmethod
    async def _dismiss_consent_banner(page) -> None:
        """Dismiss the X/Twitter cookie-consent banner if present."""
        try:
            buttons = await page.query_selector_all("button")
            for btn in buttons:
                try:
                    text = await btn.inner_text()
                    if "Accept all" in text or "Accept All" in text:
                        await btn.click()
                        log.info("bookmark_consent_banner_dismissed")
                        await asyncio.sleep(1)
                        return
                except Exception:
                    continue
        except Exception as exc:
            log.debug("bookmark_consent_banner_check_failed", error=str(exc))

    def _extract_urls_from_entities(self, entities: dict, article_urls: list[str]) -> bool:
        """Extract article URLs from tweet entities.

        Returns True if an X Article URL was found.
        """
        is_article = False
        for url_entity in entities.get("urls", []):
            expanded = url_entity.get("expanded_url", "")
            if not expanded:
                continue
            if self.X_ARTICLE_URL_PATTERN.match(expanded):
                is_article = True
                if expanded not in article_urls:
                    article_urls.append(expanded)
            elif not self.IGNORE_URL_PATTERNS.match(expanded):
                if expanded not in article_urls:
                    article_urls.append(expanded)
        return is_article

    def _parse_graphql_response(self, data: dict) -> list[BookmarkEntry]:
        """Parse bookmark entries from a Twitter GraphQL API response."""
        entries: list[BookmarkEntry] = []
        try:
            data_root = data.get("data", {})
            timeline = None
            for key in ("bookmark_timeline_v2", "bookmark_timeline"):
                if key in data_root:
                    timeline = data_root[key].get("timeline", {})
                    break

            if not timeline:
                return entries

            for instruction in timeline.get("instructions", []):
                inst_type = instruction.get("type", "")
                if inst_type == "TimelineAddEntries":
                    for entry in instruction.get("entries", []):
                        bookmark = self._parse_timeline_entry(entry)
                        if bookmark:
                            entries.append(bookmark)
                elif inst_type == "TimelineAddToModule":
                    for item in instruction.get("moduleItems", []):
                        entry_item = item.get("item", {})
                        bookmark = self._parse_item_content(entry_item.get("itemContent", {}))
                        if bookmark:
                            entries.append(bookmark)
        except Exception as e:
            log.warning("bookmark_graphql_parse_error", error=str(e))

        return entries

    def _parse_timeline_entry(self, entry: dict) -> BookmarkEntry | None:
        """Parse a single timeline entry from the GraphQL response."""
        try:
            content = entry.get("content", {})
            entry_type = content.get("entryType", "") or content.get("__typename", "")

            if entry_type == "TimelineTimelineItem":
                return self._parse_item_content(content.get("itemContent", {}))
            elif entry_type == "TimelineTimelineModule":
                for item in content.get("items", []):
                    result = self._parse_item_content(item.get("item", {}).get("itemContent", {}))
                    if result:
                        return result
        except Exception as e:
            log.debug("bookmark_entry_parse_failed", error=str(e))
        return None

    def _parse_item_content(self, item_content: dict) -> BookmarkEntry | None:
        """Parse tweet data from an itemContent object."""
        if not item_content:
            return None

        item_type = item_content.get("itemType", "") or item_content.get("__typename", "")
        if item_type != "TimelineTweet":
            return None

        result = item_content.get("tweet_results", {}).get("result", {})
        return self._parse_tweet_result(result)

    def _parse_tweet_result(self, tweet: dict) -> BookmarkEntry | None:
        """Parse a tweet result object into a BookmarkEntry."""
        try:
            typename = tweet.get("__typename", "")
            if typename == "TweetWithVisibilityResults":
                tweet = tweet.get("tweet", {})
            elif typename == "TweetTombstone":
                return None

            tweet_id = tweet.get("rest_id", "")
            if not tweet_id:
                return None

            # User info
            user_legacy = (
                tweet.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})
            )
            author = user_legacy.get("screen_name", "")
            display_name = user_legacy.get("name", "")

            # Tweet content
            legacy = tweet.get("legacy", {})
            full_text = legacy.get("full_text", "")

            # Check for extended text (Twitter Blue long-form tweets)
            note_tweet = tweet.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
            if note_tweet:
                note_text = note_tweet.get("text", "")
                if note_text and len(note_text) > len(full_text):
                    full_text = note_text

            text_preview = full_text[: self.MAX_PREVIEW_LENGTH]
            if len(full_text) > self.MAX_PREVIEW_LENGTH:
                text_preview += "..."

            tweet_url = f"https://x.com/{author}/status/{tweet_id}"

            # Extract expanded URLs from entities (real URLs, not t.co)
            article_urls: list[str] = []
            is_article = self._extract_urls_from_entities(legacy.get("entities", {}), article_urls)

            # Also check note_tweet entities
            if note_tweet:
                if self._extract_urls_from_entities(note_tweet.get("entity_set", {}), article_urls):
                    is_article = True

            # Check quoted tweet for additional article URLs
            quoted = tweet.get("quoted_status_result", {}).get("result", {})
            if quoted:
                q_typename = quoted.get("__typename", "")
                if q_typename == "TweetWithVisibilityResults":
                    quoted = quoted.get("tweet", {})
                if q_typename != "TweetTombstone":
                    q_legacy = quoted.get("legacy", {})
                    if self._extract_urls_from_entities(q_legacy.get("entities", {}), article_urls):
                        is_article = True

            # Timestamp
            bookmarked_at = legacy.get("created_at")
            if bookmarked_at:
                try:
                    dt = datetime.strptime(bookmarked_at, "%a %b %d %H:%M:%S %z %Y")
                    bookmarked_at = dt.isoformat()
                except (ValueError, TypeError):
                    pass

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
            log.warning("bookmark_tweet_parse_failed", error=str(e))
            return None
