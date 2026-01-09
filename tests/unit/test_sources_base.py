"""Tests for sources/base.py - Article dataclass and ContentSource Protocol."""

from datetime import datetime


class TestArticle:
    """Tests for the Article dataclass."""

    def test_article_creation(self):
        """Test Article dataclass can be instantiated."""
        from twitter_articlenator.sources.base import Article

        article = Article(
            title="Test Title",
            author="testuser",
            content="<p>Test content</p>",
            published_at=datetime(2025, 12, 29, 10, 0, 0),
            source_url="https://x.com/testuser/status/123",
            source_type="twitter",
        )
        assert article is not None

    def test_article_fields(self):
        """Test Article has all required fields with correct values."""
        from twitter_articlenator.sources.base import Article

        article = Article(
            title="My Title",
            author="author1",
            content="<h1>Hello</h1>",
            published_at=datetime(2025, 1, 1),
            source_url="https://example.com",
            source_type="test",
        )
        assert article.title == "My Title"
        assert article.author == "author1"
        assert article.content == "<h1>Hello</h1>"
        assert article.published_at == datetime(2025, 1, 1)
        assert article.source_url == "https://example.com"
        assert article.source_type == "test"

    def test_article_optional_published_at(self):
        """Test Article allows None for published_at."""
        from twitter_articlenator.sources.base import Article

        article = Article(
            title="Test",
            author="user",
            content="content",
            published_at=None,
            source_url="https://x.com/user/status/1",
            source_type="twitter",
        )
        assert article.published_at is None


class TestContentSource:
    """Tests for the ContentSource Protocol.

    Note: Protocol uses structural subtyping (duck typing), not nominal typing.
    Classes don't need to explicitly inherit from ContentSource - they just
    need to implement the required methods. The @runtime_checkable decorator
    allows isinstance() checks.
    """

    def test_content_source_is_protocol(self):
        """Test ContentSource is a Protocol class."""
        from typing import Protocol
        from twitter_articlenator.sources.base import ContentSource

        # ContentSource should be a Protocol
        assert issubclass(type(ContentSource), type(Protocol))

    def test_content_source_is_runtime_checkable(self):
        """Test ContentSource can be used with isinstance()."""
        from twitter_articlenator.sources.base import Article, ContentSource

        class ValidSource:
            """A class that implements the ContentSource protocol."""

            def can_handle(self, url: str) -> bool:
                return True

            async def fetch(self, url: str) -> Article:
                return Article(
                    title="Test",
                    author="test",
                    content="",
                    published_at=None,
                    source_url=url,
                    source_type="test",
                )

        source = ValidSource()
        assert isinstance(source, ContentSource)

    def test_incomplete_class_not_content_source(self):
        """Test incomplete class is not recognized as ContentSource."""
        from twitter_articlenator.sources.base import ContentSource

        class IncompleteSource:
            """A class missing the fetch method."""

            def can_handle(self, url: str) -> bool:
                return True

        source = IncompleteSource()
        # Should NOT be recognized as ContentSource due to missing fetch
        assert not isinstance(source, ContentSource)

    def test_valid_subclass_can_be_instantiated(self):
        """Test a valid ContentSource implementation can be instantiated."""
        from twitter_articlenator.sources.base import Article, ContentSource

        class ValidSource:
            def can_handle(self, url: str) -> bool:
                return True

            async def fetch(self, url: str) -> Article:
                return Article(
                    title="Test",
                    author="test",
                    content="",
                    published_at=None,
                    source_url=url,
                    source_type="test",
                )

        source = ValidSource()
        assert source.can_handle("https://example.com") is True
        assert isinstance(source, ContentSource)
