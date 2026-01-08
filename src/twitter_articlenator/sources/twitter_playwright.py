"""Twitter/X content source using Playwright browser automation."""

import asyncio
import re
from datetime import datetime

import structlog
from playwright.async_api._generated import SetCookieParam

from .base import Article, ContentSource
from .browser_pool import get_browser_pool

log = structlog.get_logger()


class TwitterPlaywrightSource(ContentSource):
    """Fetch tweets using Playwright browser automation.

    This is more reliable than API-based scrapers because it uses
    the same path as a real browser user.
    """

    # URL patterns for Twitter/X
    TWITTER_URL_PATTERN = re.compile(
        r"https?://(?:www\.)?(?:twitter\.com|x\.com)/(\w+)/status/(\d+)"
    )

    # Maximum title length
    MAX_TITLE_LENGTH = 100

    def __init__(self, cookies: str | None = None) -> None:
        """Initialize Twitter source with cookies.

        Args:
            cookies: Twitter authentication cookies string (auth_token=...; ct0=...).
        """
        self._cookies_str = cookies

    def can_handle(self, url: str) -> bool:
        """Check if URL is a Twitter/X status URL."""
        if not url:
            return False
        return bool(self.TWITTER_URL_PATTERN.match(url))

    def _parse_cookies(self) -> list[SetCookieParam]:
        """Parse cookie string into Playwright cookie format.

        Returns:
            List of cookie dicts for Playwright.
        """
        if not self._cookies_str:
            return []

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
                # Also add for twitter.com domain
                cookies.append(
                    SetCookieParam(
                        name=name.strip(),
                        value=value.strip(),
                        domain=".twitter.com",
                        path="/",
                    )
                )
        return cookies

    async def fetch(self, url: str) -> Article:
        """Fetch a tweet using Playwright and convert to Article.

        Uses a browser pool for efficient resource management.

        Args:
            url: Twitter/X status URL.

        Returns:
            Article containing the tweet content.

        Raises:
            ValueError: If cookies not configured or URL invalid.
        """
        if not self._cookies_str:
            raise ValueError("Cookies are required to fetch tweets")

        match = self.TWITTER_URL_PATTERN.match(url)
        if not match:
            raise ValueError(f"Invalid Twitter URL: {url}")

        username = match.group(1)
        tweet_id = match.group(2)

        log.info("fetching_tweet_playwright", tweet_id=tweet_id, url=url)

        # Use browser pool for efficient browser reuse
        pool = get_browser_pool()
        cookies = self._parse_cookies()

        async with pool.get_context(cookies=cookies) as context:
            page = await context.new_page()

            # First go to home to establish session and let React app initialize
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            # Wait for home feed to load (proves we're logged in)
            try:
                await page.wait_for_selector('[data-testid="tweet"]', timeout=10000)
                log.info("home_feed_loaded")
            except Exception:
                log.warning("home_feed_not_loaded")

            # Now use React Router navigation by manipulating history
            # This simulates clicking a link rather than direct navigation
            await page.evaluate(f"""
                window.history.pushState({{}}, '', '{url}');
                window.dispatchEvent(new PopStateEvent('popstate'));
            """)
            await asyncio.sleep(3)

            # If that didn't work, try clicking the URL in address bar style
            # by using goto but with a referrer
            if page.url != url:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                    referer="https://x.com/home",
                )
                await asyncio.sleep(3)

            # Take screenshot for debugging
            await page.screenshot(path="/tmp/twitter_nav_test.png")
            log.info("navigation_complete", url=page.url, title=await page.title())

            # Wait for tweet content to load (regular tweet OR article)
            try:
                # Try to find either a regular tweet or an article
                await page.wait_for_selector(
                    '[data-testid="tweetText"], [data-testid="longformRichTextComponent"]',
                    timeout=30000,
                )
            except Exception as wait_error:
                # Save screenshot for debugging
                screenshot_path = "/tmp/twitter_debug.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                # Also save HTML for debugging
                html_path = "/tmp/twitter_debug.html"
                with open(html_path, "w") as f:
                    f.write(await page.content())
                log.error(
                    "tweet_not_found",
                    screenshot=screenshot_path,
                    html_path=html_path,
                    page_url=page.url,
                    page_title=await page.title(),
                )
                raise wait_error

            # Extract tweet data
            tweet_data = await self._extract_tweet_data(page, username)

            log.info(
                "tweet_extracted_playwright",
                author=tweet_data["author"],
                has_content=bool(tweet_data["content"]),
            )

            return self._create_article(tweet_data, url)

    async def _extract_tweet_data(self, page, expected_username: str) -> dict:
        """Extract tweet data from the page.

        Args:
            page: Playwright page object.
            expected_username: Expected author username.

        Returns:
            Dict with tweet data.
        """
        # Check if this is an article (long-form content)
        article_element = await page.query_selector('[data-testid="longformRichTextComponent"]')
        is_article = article_element is not None

        content = ""
        title = None

        if is_article:
            # Extract article content
            log.info("extracting_article_content")
            content = await article_element.inner_text()

            # Try to get article title from the article header or cover
            try:
                # Look for article title in the page - usually in a heading
                title_element = await page.query_selector(
                    'article h1, [data-testid="article-cover-image"] + div h1'
                )
                if title_element:
                    title = await title_element.inner_text()
            except Exception:
                pass

            # If no title found, try to extract from page title
            if not title:
                page_title = await page.title()
                if " / X" in page_title:
                    title = page_title.replace(" / X", "").strip()
        else:
            # Regular tweet - get tweet text
            tweet_text_elements = await page.query_selector_all('[data-testid="tweetText"]')

            content_parts = []
            for element in tweet_text_elements:
                text = await element.inner_text()
                if text:
                    content_parts.append(text)

            content = "\n\n".join(content_parts) if content_parts else ""

        # Get author display name
        display_name = expected_username
        try:
            # The display name is usually in a span inside the user name link
            name_element = await page.query_selector('[data-testid="User-Name"] span')
            if name_element:
                display_name = await name_element.inner_text()
        except Exception:
            pass

        # Get timestamp
        timestamp = None
        try:
            time_element = await page.query_selector("time")
            if time_element:
                datetime_attr = await time_element.get_attribute("datetime")
                if datetime_attr:
                    timestamp = datetime.fromisoformat(datetime_attr.replace("Z", "+00:00"))
        except Exception:
            pass

        # Get any quoted tweets or media descriptions (for regular tweets only)
        quoted_tweets = []
        if not is_article:
            try:
                quoted_elements = await page.query_selector_all(
                    '[data-testid="tweet"] [data-testid="tweetText"]'
                )
                # Skip the first one (main tweet) if there are multiple
                if len(quoted_elements) > 1:
                    for elem in quoted_elements[1:]:
                        qt_text = await elem.inner_text()
                        if qt_text and qt_text not in content.split("\n\n"):
                            quoted_tweets.append(qt_text)
            except Exception:
                pass

        return {
            "author": expected_username,
            "display_name": display_name,
            "content": content,
            "quoted_tweets": quoted_tweets,
            "timestamp": timestamp,
            "title": title,
            "is_article": is_article,
        }

    def _create_article(self, tweet_data: dict, source_url: str) -> Article:
        """Create an Article from tweet data.

        Args:
            tweet_data: Extracted tweet data.
            source_url: Original URL.

        Returns:
            Article object.
        """
        content = tweet_data["content"]
        author = tweet_data["author"]
        display_name = tweet_data.get("display_name", author)
        timestamp = tweet_data.get("timestamp")
        quoted_tweets = tweet_data.get("quoted_tweets", [])
        is_article = tweet_data.get("is_article", False)
        article_title = tweet_data.get("title")

        # Create title - use article title if available, otherwise truncate content
        if article_title:
            title = article_title
        elif content:
            title = self._truncate_title(content)
        else:
            title = f"Tweet by @{author}"

        # Build HTML content
        date_str = timestamp.strftime("%Y-%m-%d %H:%M") if timestamp else ""

        if is_article:
            # Format as article with proper paragraphs
            # Split content into paragraphs
            paragraphs = content.split("\n")
            formatted_paragraphs = "\n".join(
                f"        <p>{p.strip()}</p>" for p in paragraphs if p.strip()
            )

            html_content = f"""<article class="twitter-article">
    <header class="article-header">
        <h1>{title}</h1>
        <div class="article-meta">
            <span class="displayname">{display_name}</span>
            <span class="username">@{author}</span>
            <span class="date">{date_str}</span>
        </div>
    </header>
    <div class="article-content">
{formatted_paragraphs}
    </div>
</article>"""
        else:
            # Regular tweet format
            html_content = f"""<div class="tweet">
    <div class="tweet-header">
        <span class="displayname">{display_name}</span>
        <span class="username">@{author}</span>
        <span class="date">{date_str}</span>
    </div>
    <div class="tweet-content">
        <p>{content}</p>
    </div>
</div>"""

            # Add quoted tweets if any
            for qt in quoted_tweets:
                html_content += f"""
<div class="quoted-tweet">
    <div class="tweet-content">
        <p>{qt}</p>
    </div>
</div>"""

        log.info(
            "tweet_converted_to_article",
            author=author,
            title=title,
            is_article=is_article,
        )

        return Article(
            title=title,
            author=author,
            content=html_content,
            published_at=timestamp,
            source_url=source_url,
            source_type="twitter_article" if is_article else "twitter",
        )

    def _truncate_title(self, text: str) -> str:
        """Truncate text to max title length.

        Args:
            text: Text to truncate.

        Returns:
            Truncated text with ellipsis if needed.
        """
        # Remove newlines for title
        text = text.replace("\n", " ").strip()
        if len(text) <= self.MAX_TITLE_LENGTH:
            return text
        return text[: self.MAX_TITLE_LENGTH - 3] + "..."
