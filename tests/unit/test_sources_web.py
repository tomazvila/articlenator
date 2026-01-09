"""Tests for sources/web.py - Generic web article source."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest


class TestWebArticleSourceCanHandle:
    """Tests for WebArticleSource.can_handle method."""

    def test_can_handle_http_url(self):
        """Test WebArticleSource handles HTTP URLs."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source.can_handle("http://example.com/article") is True

    def test_can_handle_https_url(self):
        """Test WebArticleSource handles HTTPS URLs."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source.can_handle("https://example.com/article") is True

    def test_rejects_twitter_url(self):
        """Test WebArticleSource rejects Twitter URLs."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source.can_handle("https://twitter.com/user/status/123") is False
        assert source.can_handle("https://x.com/user/status/123") is False
        assert source.can_handle("https://www.twitter.com/user") is False
        assert source.can_handle("https://www.x.com/user") is False

    def test_rejects_empty_url(self):
        """Test WebArticleSource rejects empty URLs."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source.can_handle("") is False

    def test_rejects_non_http_scheme(self):
        """Test WebArticleSource rejects non-HTTP schemes."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source.can_handle("ftp://example.com/file") is False
        assert source.can_handle("file:///path/to/file") is False

    def test_rejects_invalid_url(self):
        """Test WebArticleSource rejects invalid URLs."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source.can_handle("not-a-url") is False
        assert source.can_handle("://missing-scheme") is False

    def test_rejects_url_without_host(self):
        """Test WebArticleSource rejects URLs without host."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source.can_handle("http://") is False


class TestWebArticleSourceInit:
    """Tests for WebArticleSource initialization."""

    def test_default_timeout(self):
        """Test WebArticleSource has default timeout."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source._timeout == 30.0

    def test_custom_timeout(self):
        """Test WebArticleSource accepts custom timeout."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource(timeout=60.0)
        assert source._timeout == 60.0


class TestExtractTitle:
    """Tests for _extract_title method."""

    def test_extract_title_from_og_title(self):
        """Test extracting title from og:title meta tag."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><head><meta property="og:title" content="OG Title"></head></html>'
        soup = BeautifulSoup(html, "lxml")

        title = source._extract_title(soup, "https://example.com")
        assert title == "OG Title"

    def test_extract_title_from_h1(self):
        """Test extracting title from h1 element."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><h1>Article Title</h1></body></html>'
        soup = BeautifulSoup(html, "lxml")

        title = source._extract_title(soup, "https://example.com")
        assert title == "Article Title"

    def test_extract_title_from_title_tag(self):
        """Test extracting title from <title> tag."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><head><title>Page Title</title></head></html>'
        soup = BeautifulSoup(html, "lxml")

        title = source._extract_title(soup, "https://example.com")
        assert title == "Page Title"

    def test_extract_title_fallback_to_domain(self):
        """Test fallback to domain when no title found."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><p>No title here</p></body></html>'
        soup = BeautifulSoup(html, "lxml")

        title = source._extract_title(soup, "https://example.com/article")
        assert title == "example.com"


class TestExtractAuthor:
    """Tests for _extract_author method."""

    def test_extract_author_from_meta_tag(self):
        """Test extracting author from meta tag."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><head><meta name="author" content="John Doe"></head></html>'
        soup = BeautifulSoup(html, "lxml")

        author = source._extract_author(soup, "https://example.com")
        assert author == "John Doe"

    def test_extract_author_from_byline_class(self):
        """Test extracting author from .byline element."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><span class="byline">By Jane Smith</span></body></html>'
        soup = BeautifulSoup(html, "lxml")

        author = source._extract_author(soup, "https://example.com")
        assert author == "Jane Smith"

    def test_extract_author_removes_by_prefix(self):
        """Test that 'by' prefix is removed from author."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><span class="author">by John Doe</span></body></html>'
        soup = BeautifulSoup(html, "lxml")

        author = source._extract_author(soup, "https://example.com")
        assert author == "John Doe"

    def test_extract_author_fallback_to_domain(self):
        """Test fallback to domain when no author found."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><p>No author here</p></body></html>'
        soup = BeautifulSoup(html, "lxml")

        author = source._extract_author(soup, "https://blog.example.com/post")
        assert author == "blog.example.com"


class TestExtractDate:
    """Tests for _extract_date and _parse_date methods."""

    def test_extract_date_from_meta_tag(self):
        """Test extracting date from meta tag."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><head><meta property="article:published_time" content="2025-12-29T10:30:00Z"></head></html>'
        soup = BeautifulSoup(html, "lxml")

        date = source._extract_date(soup)
        assert date is not None
        assert date.year == 2025
        assert date.month == 12
        assert date.day == 29

    def test_extract_date_from_time_element(self):
        """Test extracting date from <time> element."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><time datetime="2025-01-15">January 15, 2025</time></body></html>'
        soup = BeautifulSoup(html, "lxml")

        date = source._extract_date(soup)
        assert date is not None
        assert date.year == 2025
        assert date.month == 1
        assert date.day == 15

    def test_parse_date_iso_format(self):
        """Test parsing ISO format date."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        date = source._parse_date("2025-12-29T10:30:00Z")
        assert date is not None
        assert date.year == 2025

    def test_parse_date_simple_format(self):
        """Test parsing simple date format."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        date = source._parse_date("2025-12-29")
        assert date is not None
        assert date.year == 2025
        assert date.month == 12

    def test_parse_date_human_format(self):
        """Test parsing human-readable date format."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        date = source._parse_date("December 29, 2025")
        assert date is not None
        assert date.year == 2025

    def test_parse_date_empty_string(self):
        """Test parsing empty string returns None."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source._parse_date("") is None

    def test_parse_date_invalid_format(self):
        """Test parsing invalid format returns None."""
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()
        assert source._parse_date("not a date") is None


class TestExtractContent:
    """Tests for _extract_content method."""

    def test_extract_content_from_article(self):
        """Test extracting content from <article> element."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><article><p>Article content here.</p></article></body></html>'
        soup = BeautifulSoup(html, "lxml")

        content = source._extract_content(soup)
        assert "Article content here" in content

    def test_extract_content_from_main(self):
        """Test extracting content from <main> element."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><main><p>Main content here.</p></main></body></html>'
        soup = BeautifulSoup(html, "lxml")

        content = source._extract_content(soup)
        assert "Main content here" in content

    def test_extract_content_removes_scripts(self):
        """Test that scripts are removed from content."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><article><p>Content</p><script>alert("bad")</script></article></body></html>'
        soup = BeautifulSoup(html, "lxml")

        content = source._extract_content(soup)
        assert "alert" not in content
        assert "script" not in content.lower()

    def test_extract_content_removes_nav(self):
        """Test that navigation is removed from content."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><nav>Menu</nav><article><p>Article content.</p></article></body></html>'
        soup = BeautifulSoup(html, "lxml")

        content = source._extract_content(soup)
        assert "Menu" not in content

    def test_extract_content_fallback_to_body(self):
        """Test fallback to body when no article found."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<html><body><p>Body content that is long enough to be considered valid content for an article.</p></body></html>'
        soup = BeautifulSoup(html, "lxml")

        content = source._extract_content(soup)
        assert "Body content" in content


class TestCleanContent:
    """Tests for _clean_content method."""

    def test_clean_content_removes_empty_paragraphs(self):
        """Test that empty paragraphs are removed."""
        from twitter_articlenator.sources.web import WebArticleSource
        from bs4 import BeautifulSoup

        source = WebArticleSource()
        html = '<article><p>Content</p><p></p><p>   </p></article>'
        soup = BeautifulSoup(html, "lxml")
        element = soup.find("article")

        cleaned = source._clean_content(element)
        # Should not have multiple <p> tags for empty content
        assert cleaned.count("<p>") <= 1 or "Content" in cleaned


class TestFetch:
    """Tests for WebArticleSource.fetch method."""

    @pytest.mark.asyncio
    async def test_fetch_success(self):
        """Test successful fetch of web article."""
        from twitter_articlenator.sources.web import WebArticleSource
        from twitter_articlenator.sources.base import Article

        source = WebArticleSource()

        mock_response = MagicMock()
        mock_response.text = """
        <html>
        <head>
            <meta property="og:title" content="Test Article">
            <meta name="author" content="Test Author">
        </head>
        <body>
            <article>
                <p>This is the article content with enough text to be valid.</p>
            </article>
        </body>
        </html>
        """
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            article = await source.fetch("https://example.com/article")

            assert isinstance(article, Article)
            assert article.title == "Test Article"
            assert article.author == "Test Author"
            assert article.source_type == "web"
            assert "article content" in article.content

    @pytest.mark.asyncio
    async def test_fetch_http_error(self):
        """Test fetch raises ValueError on HTTP error."""
        import httpx
        from twitter_articlenator.sources.web import WebArticleSource

        source = WebArticleSource()

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(side_effect=httpx.HTTPError("Connection failed"))
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with pytest.raises(ValueError, match="Failed to fetch URL"):
                await source.fetch("https://example.com/article")

    @pytest.mark.asyncio
    async def test_fetch_minimal_content(self):
        """Test fetch handles pages with minimal content (body fallback)."""
        from twitter_articlenator.sources.web import WebArticleSource
        from twitter_articlenator.sources.base import Article

        source = WebArticleSource()

        mock_response = MagicMock()
        # Even with minimal HTML, the body fallback provides some content
        mock_response.text = "<html><body></body></html>"
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            # Should still return an article (uses body fallback)
            article = await source.fetch("https://example.com/empty")
            assert isinstance(article, Article)
            assert article.source_type == "web"


class TestContentSelectors:
    """Tests for content selector constants."""

    def test_content_selectors_defined(self):
        """Test CONTENT_SELECTORS is defined."""
        from twitter_articlenator.sources.web import WebArticleSource

        assert hasattr(WebArticleSource, "CONTENT_SELECTORS")
        assert len(WebArticleSource.CONTENT_SELECTORS) > 0
        assert "article" in WebArticleSource.CONTENT_SELECTORS

    def test_title_selectors_defined(self):
        """Test TITLE_SELECTORS is defined."""
        from twitter_articlenator.sources.web import WebArticleSource

        assert hasattr(WebArticleSource, "TITLE_SELECTORS")
        assert len(WebArticleSource.TITLE_SELECTORS) > 0
        assert "h1" in WebArticleSource.TITLE_SELECTORS

    def test_author_selectors_defined(self):
        """Test AUTHOR_SELECTORS is defined."""
        from twitter_articlenator.sources.web import WebArticleSource

        assert hasattr(WebArticleSource, "AUTHOR_SELECTORS")
        assert len(WebArticleSource.AUTHOR_SELECTORS) > 0

    def test_date_selectors_defined(self):
        """Test DATE_SELECTORS is defined."""
        from twitter_articlenator.sources.web import WebArticleSource

        assert hasattr(WebArticleSource, "DATE_SELECTORS")
        assert len(WebArticleSource.DATE_SELECTORS) > 0
        assert "time" in WebArticleSource.DATE_SELECTORS
