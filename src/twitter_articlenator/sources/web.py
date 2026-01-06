"""Generic web article source for blogs and articles."""

import re
from datetime import datetime
from urllib.parse import urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from .base import Article, ContentSource

log = structlog.get_logger()


class WebArticleSource(ContentSource):
    """Fetch articles from generic web pages."""

    # Common article content selectors (in order of preference)
    CONTENT_SELECTORS = [
        "article",
        '[role="article"]',
        ".post-content",
        ".article-content",
        ".entry-content",
        ".content",
        "main",
        ".post",
        "#content",
    ]

    # Common title selectors
    TITLE_SELECTORS = [
        "h1.post-title",
        "h1.entry-title",
        "h1.article-title",
        "article h1",
        "main h1",
        "h1",
    ]

    # Common author selectors
    AUTHOR_SELECTORS = [
        '[rel="author"]',
        ".author",
        ".byline",
        'meta[name="author"]',
        ".post-author",
    ]

    # Common date selectors
    DATE_SELECTORS = [
        "time",
        ".date",
        ".published",
        ".post-date",
        'meta[property="article:published_time"]',
    ]

    def __init__(self, timeout: float = 30.0) -> None:
        """Initialize web article source.

        Args:
            timeout: HTTP request timeout in seconds.
        """
        self._timeout = timeout

    def can_handle(self, url: str) -> bool:
        """Check if URL is a valid web URL (not Twitter).

        Args:
            url: URL to check.

        Returns:
            True if URL is a valid HTTP(S) URL that's not Twitter.
        """
        if not url:
            return False

        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False

            # Exclude Twitter URLs (handled by TwitterSource)
            twitter_domains = ("twitter.com", "x.com", "www.twitter.com", "www.x.com")
            if parsed.netloc.lower() in twitter_domains:
                return False

            return bool(parsed.netloc)
        except Exception:
            return False

    async def fetch(self, url: str) -> Article:
        """Fetch a web article and convert to Article.

        Args:
            url: Web page URL.

        Returns:
            Article containing the page content.

        Raises:
            ValueError: If URL cannot be fetched or parsed.
        """
        log.info("fetching_web_article", url=url)

        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ArticleBot/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as e:
                raise ValueError(f"Failed to fetch URL: {e}") from e

        html = response.text
        soup = BeautifulSoup(html, "lxml")

        # Extract metadata
        title = self._extract_title(soup, url)
        author = self._extract_author(soup, url)
        published_at = self._extract_date(soup)
        content = self._extract_content(soup)

        if not content:
            raise ValueError(f"Could not extract article content from {url}")

        log.info(
            "web_article_fetched",
            url=url,
            title=title,
            author=author,
            content_length=len(content),
        )

        return Article(
            title=title,
            author=author,
            content=content,
            published_at=published_at,
            source_url=url,
            source_type="web",
        )

    def _extract_title(self, soup: BeautifulSoup, url: str) -> str:
        """Extract article title from HTML."""
        # Try meta tags first
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title["content"].strip()

        # Try common selectors
        for selector in self.TITLE_SELECTORS:
            element = soup.select_one(selector)
            if element and element.get_text(strip=True):
                return element.get_text(strip=True)

        # Fallback to <title> tag
        if soup.title and soup.title.string:
            return soup.title.string.strip()

        # Last resort: use domain
        return urlparse(url).netloc

    def _extract_author(self, soup: BeautifulSoup, url: str) -> str:
        """Extract article author from HTML."""
        # Try meta tags
        meta_author = soup.find("meta", attrs={"name": "author"})
        if meta_author and meta_author.get("content"):
            return meta_author["content"].strip()

        # Try common selectors
        for selector in self.AUTHOR_SELECTORS:
            element = soup.select_one(selector)
            if element:
                if element.name == "meta":
                    content = element.get("content", "")
                    if isinstance(content, str):
                        return content.strip()
                    continue
                text = element.get_text(strip=True)
                if text:
                    # Clean up common prefixes
                    text = re.sub(r"^(by|author:?)\s*", "", text, flags=re.IGNORECASE)
                    return text

        # Fallback to domain
        return urlparse(url).netloc

    def _extract_date(self, soup: BeautifulSoup) -> datetime | None:
        """Extract publication date from HTML."""
        # Try meta tags
        meta_date = soup.find("meta", property="article:published_time")
        if meta_date and meta_date.get("content"):
            return self._parse_date(meta_date["content"])

        # Try <time> element
        time_elem = soup.find("time")
        if time_elem:
            datetime_attr = time_elem.get("datetime")
            if isinstance(datetime_attr, str):
                return self._parse_date(datetime_attr)
            return self._parse_date(time_elem.get_text(strip=True))

        # Try common selectors
        for selector in self.DATE_SELECTORS:
            element = soup.select_one(selector)
            if element:
                content = element.get("content")
                text = content if isinstance(content, str) else element.get_text(strip=True)
                if text:
                    parsed = self._parse_date(text)
                    if parsed:
                        return parsed

        return None

    def _parse_date(self, date_str: str) -> datetime | None:
        """Parse a date string into datetime."""
        if not date_str:
            return None

        # Common date formats
        formats = [
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%B %d, %Y",
            "%b %d, %Y",
            "%d %B %Y",
            "%d %b %Y",
        ]

        # Clean the string
        date_str = date_str.strip()

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

        return None

    def _extract_content(self, soup: BeautifulSoup) -> str:
        """Extract article content as HTML."""
        # Remove unwanted elements
        for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        # Remove common non-content elements
        for selector in [".comments", ".sidebar", ".advertisement", ".ad", ".share"]:
            for elem in soup.select(selector):
                elem.decompose()

        # Try content selectors
        for selector in self.CONTENT_SELECTORS:
            content = soup.select_one(selector)
            if content:
                # Clean up the content
                html = self._clean_content(content)
                if html and len(html) > 100:  # Minimum content length
                    return html

        # Fallback: try to get body content
        body = soup.find("body")
        if body:
            return self._clean_content(body)

        return ""

    def _clean_content(self, element) -> str:
        """Clean up extracted content HTML."""
        # Remove empty paragraphs
        for p in element.find_all("p"):
            if not p.get_text(strip=True):
                p.decompose()

        # Get the HTML
        html = str(element)

        # Basic cleanup
        html = re.sub(r"\s+", " ", html)
        html = re.sub(r">\s+<", "><", html)

        return html.strip()
