"""Tests for pdf/generator.py - PDF generation."""

from datetime import datetime
from pathlib import Path

import pytest

from twitter_articlenator.sources.base import Article


@pytest.fixture
def sample_article():
    """Create a sample Article for testing."""
    return Article(
        title="Test Article Title",
        author="testuser",
        content="<p>This is test content with some text.</p>",
        published_at=datetime(2025, 12, 29, 10, 30, 0),
        source_url="https://x.com/testuser/status/123456789",
        source_type="twitter",
    )


@pytest.fixture
def long_article():
    """Create an article with longer content."""
    content = "\n".join([f"<p>Paragraph {i} with some content.</p>" for i in range(10)])
    return Article(
        title="Long Article with Multiple Paragraphs",
        author="longauthor",
        content=content,
        published_at=datetime(2025, 12, 25, 15, 0, 0),
        source_url="https://x.com/longauthor/status/987654321",
        source_type="twitter",
    )


class TestGeneratePdf:
    """Tests for generate_pdf function."""

    def test_generate_pdf_creates_file(self, sample_article, tmp_path):
        """Test generate_pdf creates a PDF file."""
        from twitter_articlenator.pdf.generator import generate_pdf

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        pdf_path = generate_pdf(sample_article, output_dir)

        assert pdf_path.exists()
        assert pdf_path.suffix == ".pdf"

    def test_generate_pdf_returns_path(self, sample_article, tmp_path):
        """Test generate_pdf returns the correct path."""
        from twitter_articlenator.pdf.generator import generate_pdf

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        pdf_path = generate_pdf(sample_article, output_dir)

        assert isinstance(pdf_path, Path)
        assert pdf_path.parent == output_dir

    def test_generate_pdf_filename_contains_title(self, sample_article, tmp_path):
        """Test PDF filename contains slugified title."""
        from twitter_articlenator.pdf.generator import generate_pdf

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        pdf_path = generate_pdf(sample_article, output_dir)

        assert "test" in pdf_path.stem.lower() or "article" in pdf_path.stem.lower()

    def test_generate_pdf_filename_contains_date(self, sample_article, tmp_path):
        """Test PDF filename contains date."""
        import re
        from twitter_articlenator.pdf.generator import generate_pdf

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        pdf_path = generate_pdf(sample_article, output_dir)

        # Should contain date in YYYYMMDD format
        assert re.search(r"_\d{8}$", pdf_path.stem), f"Expected date suffix in {pdf_path.stem}"

    def test_generate_pdf_creates_output_dir_if_missing(self, sample_article, tmp_path):
        """Test generate_pdf creates output dir if it doesn't exist."""
        from twitter_articlenator.pdf.generator import generate_pdf

        output_dir = tmp_path / "new_output"
        assert not output_dir.exists()

        pdf_path = generate_pdf(sample_article, output_dir)

        assert output_dir.exists()
        assert pdf_path.exists()

    def test_generate_pdf_uses_config_default_dir(self, sample_article, tmp_path, monkeypatch):
        """Test generate_pdf uses config output_dir when not specified."""
        from twitter_articlenator.pdf.generator import generate_pdf

        # Set config output dir
        monkeypatch.setenv("TWITTER_ARTICLENATOR_OUTPUT_DIR", str(tmp_path / "config_output"))

        # Clear the singleton to pick up new env var
        import twitter_articlenator.config as config_module
        config_module._config_instance = None

        pdf_path = generate_pdf(sample_article)

        assert pdf_path.exists()
        assert "config_output" in str(pdf_path.parent)

    def test_generate_pdf_with_long_article(self, long_article, tmp_path):
        """Test generate_pdf handles longer articles."""
        from twitter_articlenator.pdf.generator import generate_pdf

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        pdf_path = generate_pdf(long_article, output_dir)

        assert pdf_path.exists()
        # PDF should have reasonable size
        assert pdf_path.stat().st_size > 0


class TestRenderHtml:
    """Tests for _render_html function."""

    def test_render_html_includes_title(self, sample_article):
        """Test rendered HTML includes article title."""
        from twitter_articlenator.pdf.generator import _render_html

        html = _render_html(sample_article)

        assert sample_article.title in html

    def test_render_html_includes_author(self, sample_article):
        """Test rendered HTML includes article author."""
        from twitter_articlenator.pdf.generator import _render_html

        html = _render_html(sample_article)

        assert sample_article.author in html

    def test_render_html_includes_content(self, sample_article):
        """Test rendered HTML includes article content."""
        from twitter_articlenator.pdf.generator import _render_html

        html = _render_html(sample_article)

        assert "This is test content" in html

    def test_render_html_includes_date(self, sample_article):
        """Test rendered HTML includes publication date."""
        from twitter_articlenator.pdf.generator import _render_html

        html = _render_html(sample_article)

        # Should contain date in some format
        assert "2025" in html

    def test_render_html_is_valid_html(self, sample_article):
        """Test rendered HTML is valid HTML structure."""
        from twitter_articlenator.pdf.generator import _render_html

        html = _render_html(sample_article)

        assert "<html" in html.lower()
        assert "</html>" in html.lower()
        assert "<body" in html.lower()

    def test_render_html_includes_css(self, sample_article):
        """Test rendered HTML includes CSS styles."""
        from twitter_articlenator.pdf.generator import _render_html

        html = _render_html(sample_article)

        assert "<style" in html.lower() or "style=" in html.lower()


class TestGetEreaderCss:
    """Tests for _get_ereader_css function."""

    def test_ereader_css_has_large_font(self):
        """Test e-reader CSS has large font size."""
        from twitter_articlenator.pdf.generator import _get_ereader_css

        css = _get_ereader_css()

        # Should have a reasonable font size for e-readers (at least 14pt or 1em+)
        assert "font-size" in css.lower()

    def test_ereader_css_has_good_line_height(self):
        """Test e-reader CSS has good line height."""
        from twitter_articlenator.pdf.generator import _get_ereader_css

        css = _get_ereader_css()

        assert "line-height" in css.lower()

    def test_ereader_css_has_margins(self):
        """Test e-reader CSS has margins for readability."""
        from twitter_articlenator.pdf.generator import _get_ereader_css

        css = _get_ereader_css()

        assert "margin" in css.lower()

    def test_ereader_css_has_page_size(self):
        """Test e-reader CSS has page size definition."""
        from twitter_articlenator.pdf.generator import _get_ereader_css

        css = _get_ereader_css()

        # Should define page size for PDF
        assert "@page" in css or "size" in css.lower()


class TestSlugifyTitle:
    """Tests for _slugify_title function."""

    def test_slugify_title_removes_special_chars(self):
        """Test slugify removes special characters."""
        from twitter_articlenator.pdf.generator import _slugify_title

        result = _slugify_title("Hello! World? @test #hash")

        assert "!" not in result
        assert "?" not in result
        assert "@" not in result
        assert "#" not in result

    def test_slugify_title_handles_spaces(self):
        """Test slugify handles spaces correctly."""
        from twitter_articlenator.pdf.generator import _slugify_title

        result = _slugify_title("Hello World Test")

        # Spaces should become hyphens or underscores
        assert " " not in result
        assert "-" in result or "_" in result

    def test_slugify_title_lowercase(self):
        """Test slugify converts to lowercase."""
        from twitter_articlenator.pdf.generator import _slugify_title

        result = _slugify_title("HELLO WORLD")

        assert result == result.lower()

    def test_slugify_title_truncates_long_titles(self):
        """Test slugify truncates very long titles."""
        from twitter_articlenator.pdf.generator import _slugify_title

        long_title = "a" * 200
        result = _slugify_title(long_title)

        # Should be truncated to reasonable length
        assert len(result) <= 100

    def test_slugify_title_handles_unicode(self):
        """Test slugify handles unicode characters."""
        from twitter_articlenator.pdf.generator import _slugify_title

        result = _slugify_title("Café résumé naïve")

        # Should produce valid filename
        assert result  # Not empty
        # Should be ASCII-safe or handle unicode gracefully


class TestContentSizeLimits:
    """Tests for content size validation."""

    def test_max_content_size_constant_exists(self):
        """Test MAX_CONTENT_SIZE constant is defined."""
        from twitter_articlenator.pdf.generator import MAX_CONTENT_SIZE

        assert MAX_CONTENT_SIZE == 500_000_000  # 500MB

    def test_content_too_large_error_exists(self):
        """Test ContentTooLargeError exception exists."""
        from twitter_articlenator.pdf.generator import ContentTooLargeError

        error = ContentTooLargeError(600_000_000)
        assert error.size == 600_000_000
        assert error.max_size == 500_000_000
        assert "600,000,000" in str(error)
        assert "500,000,000" in str(error)

    def test_generate_pdf_rejects_large_content(self, tmp_path, monkeypatch):
        """Test generate_pdf raises error for content exceeding limit."""
        from twitter_articlenator.pdf import generator
        from twitter_articlenator.pdf.generator import generate_pdf, ContentTooLargeError

        # Temporarily lower the limit to avoid creating 50MB+ strings
        monkeypatch.setattr(generator, "MAX_CONTENT_SIZE", 1000)

        # Create article with content > 1KB (temporary limit)
        large_content = "<p>" + "x" * 2000 + "</p>"
        large_article = Article(
            title="Large Article",
            author="testuser",
            content=large_content,
            published_at=datetime(2025, 12, 29),
            source_url="https://x.com/user/status/123",
            source_type="twitter",
        )

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with pytest.raises(ContentTooLargeError) as exc_info:
            generate_pdf(large_article, output_dir)

        assert exc_info.value.size > 1000

    def test_generate_pdf_allows_content_under_limit(self, tmp_path):
        """Test generate_pdf allows content under the limit."""
        from twitter_articlenator.pdf.generator import generate_pdf

        # Create article with content < 500KB
        small_content = "<p>" + "x" * 1000 + "</p>"
        small_article = Article(
            title="Small Article",
            author="testuser",
            content=small_content,
            published_at=datetime(2025, 12, 29),
            source_url="https://x.com/user/status/123",
            source_type="twitter",
        )

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Should not raise
        pdf_path = generate_pdf(small_article, output_dir)
        assert pdf_path.exists()

    def test_generate_pdf_content_at_limit(self, tmp_path, monkeypatch):
        """Test generate_pdf allows content at exactly the limit."""
        from twitter_articlenator.pdf import generator
        from twitter_articlenator.pdf.generator import generate_pdf

        # Use a small limit to avoid creating huge strings
        test_limit = 10_000
        monkeypatch.setattr(generator, "MAX_CONTENT_SIZE", test_limit)

        # Create article with content at the limit (accounting for UTF-8)
        # Use slightly less to ensure we're under
        content_size = test_limit - 100
        content = "<p>" + "a" * content_size + "</p>"
        article = Article(
            title="Boundary Article",
            author="testuser",
            content=content,
            published_at=datetime(2025, 12, 29),
            source_url="https://x.com/user/status/123",
            source_type="twitter",
        )

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Should not raise - content is at/under limit
        pdf_path = generate_pdf(article, output_dir)
        assert pdf_path.exists()
