"""PDF generation using WeasyPrint."""

import re
import unicodedata
from datetime import datetime
from pathlib import Path

import structlog
from weasyprint import HTML

from twitter_articlenator.config import get_config
from twitter_articlenator.sources.base import Article

log = structlog.get_logger()

# Maximum slug length for filenames
MAX_SLUG_LENGTH = 80

# Maximum content size in bytes to prevent memory issues
MAX_CONTENT_SIZE = 50_000_000  # 50MB


class ContentTooLargeError(Exception):
    """Raised when article content exceeds the maximum size limit."""

    def __init__(self, size: int, max_size: int = MAX_CONTENT_SIZE):
        self.size = size
        self.max_size = max_size
        super().__init__(
            f"Content too large for PDF generation: {size:,} bytes "
            f"(max: {max_size:,} bytes)"
        )


def generate_pdf(article: Article, output_dir: Path | None = None) -> Path:
    """Generate a PDF from an article.

    Args:
        article: The article to convert to PDF.
        output_dir: Directory to save the PDF. Defaults to config output dir.

    Returns:
        Path to the generated PDF file.

    Raises:
        ContentTooLargeError: If the article content exceeds MAX_CONTENT_SIZE.
    """
    # Check content size to prevent memory issues
    content_size = len(article.content.encode("utf-8"))
    if content_size > MAX_CONTENT_SIZE:
        log.warning(
            "content_too_large",
            size=content_size,
            max_size=MAX_CONTENT_SIZE,
            title=article.title,
        )
        raise ContentTooLargeError(content_size)

    if output_dir is None:
        output_dir = get_config().output_dir

    # Create output directory if it doesn't exist
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename
    slug = _slugify_title(article.title)
    date_str = ""
    if article.published_at:
        date_str = f"_{article.published_at.strftime('%Y%m%d')}"
    else:
        date_str = f"_{datetime.now().strftime('%Y%m%d')}"

    filename = f"{slug}{date_str}.pdf"
    pdf_path = output_dir / filename

    # Render HTML and convert to PDF
    html_content = _render_html(article)

    log.info(
        "generating_pdf",
        title=article.title,
        author=article.author,
        output_path=str(pdf_path),
    )

    HTML(string=html_content).write_pdf(pdf_path)

    log.info("pdf_generated", path=str(pdf_path), size=pdf_path.stat().st_size)

    return pdf_path


def _render_html(article: Article) -> str:
    """Render article to HTML using a template.

    Args:
        article: The article to render.

    Returns:
        Complete HTML string with embedded CSS.
    """
    css = _get_ereader_css()

    # Format date
    date_str = ""
    if article.published_at:
        date_str = article.published_at.strftime("%B %d, %Y at %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{article.title}</title>
    <style>
{css}
    </style>
</head>
<body>
    <article>
        <header>
            <h1 class="title">{article.title}</h1>
            <div class="meta">
                <span class="author">By @{article.author}</span>
                <span class="date">{date_str}</span>
                <span class="source">Source: {article.source_type}</span>
            </div>
        </header>
        <main class="content">
            {article.content}
        </main>
        <footer>
            <p class="source-url">Original: <a href="{article.source_url}">{article.source_url}</a></p>
        </footer>
    </article>
</body>
</html>"""

    return html


def _get_ereader_css() -> str:
    """Get CSS optimized for e-readers.

    Returns:
        CSS string with e-reader friendly styles.
    """
    return """
        @page {
            size: A5;
            margin: 1.5cm;
        }

        * {
            box-sizing: border-box;
        }

        body {
            font-family: Georgia, 'Times New Roman', serif;
            font-size: 14pt;
            line-height: 1.6;
            color: #1a1a1a;
            background: #ffffff;
            margin: 0;
            padding: 0;
        }

        article {
            max-width: 100%;
        }

        header {
            margin-bottom: 2em;
            padding-bottom: 1em;
            border-bottom: 1px solid #cccccc;
        }

        .title {
            font-size: 1.8em;
            font-weight: bold;
            margin: 0 0 0.5em 0;
            line-height: 1.2;
        }

        .meta {
            font-size: 0.85em;
            color: #666666;
        }

        .meta span {
            display: block;
            margin: 0.25em 0;
        }

        .author {
            font-weight: 500;
        }

        .content {
            margin: 1.5em 0;
        }

        .content p {
            margin: 1em 0;
            text-align: justify;
        }

        .tweet {
            margin: 1.5em 0;
            padding: 1em;
            border-left: 3px solid #1da1f2;
            background: #f8f9fa;
        }

        .tweet-header {
            font-size: 0.9em;
            color: #666666;
            margin-bottom: 0.5em;
        }

        .displayname {
            font-weight: bold;
            color: #1a1a1a;
        }

        .username {
            color: #888888;
            margin-left: 0.5em;
        }

        .tweet-content p {
            margin: 0.5em 0;
        }

        footer {
            margin-top: 2em;
            padding-top: 1em;
            border-top: 1px solid #cccccc;
            font-size: 0.8em;
            color: #666666;
        }

        .source-url a {
            color: #1da1f2;
            text-decoration: none;
            word-break: break-all;
        }

        a {
            color: #1da1f2;
        }

        img {
            max-width: 100%;
            height: auto;
        }

        blockquote {
            margin: 1em 0;
            padding-left: 1em;
            border-left: 3px solid #cccccc;
            font-style: italic;
            color: #555555;
        }

        code {
            font-family: 'Courier New', monospace;
            font-size: 0.9em;
            background: #f4f4f4;
            padding: 0.2em 0.4em;
        }

        pre {
            background: #f4f4f4;
            padding: 1em;
            overflow-x: auto;
            font-size: 0.85em;
        }
"""


def _slugify_title(title: str) -> str:
    """Create a filesystem-safe slug from title.

    Args:
        title: The title to slugify.

    Returns:
        Lowercase, hyphenated slug safe for filenames.
    """
    # Normalize unicode characters
    slug = unicodedata.normalize("NFKD", title)
    slug = slug.encode("ascii", "ignore").decode("ascii")

    # Convert to lowercase
    slug = slug.lower()

    # Replace spaces with hyphens
    slug = re.sub(r"\s+", "-", slug)

    # Remove special characters (keep alphanumeric and hyphens)
    slug = re.sub(r"[^a-z0-9\-]", "", slug)

    # Remove multiple consecutive hyphens
    slug = re.sub(r"-+", "-", slug)

    # Strip leading/trailing hyphens
    slug = slug.strip("-")

    # Truncate to max length
    if len(slug) > MAX_SLUG_LENGTH:
        slug = slug[:MAX_SLUG_LENGTH].rstrip("-")

    # Fallback for empty slug
    if not slug:
        slug = "article"

    return slug
