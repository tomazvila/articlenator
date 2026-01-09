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
MAX_CONTENT_SIZE = 500_000_000  # 500MB


class ContentTooLargeError(Exception):
    """Raised when article content exceeds the maximum size limit."""

    def __init__(self, size: int, max_size: int = MAX_CONTENT_SIZE):
        self.size = size
        self.max_size = max_size
        super().__init__(
            f"Content too large for PDF generation: {size:,} bytes (max: {max_size:,} bytes)"
        )


def generate_pdf(article: Article, output_dir: Path | None = None) -> Path:
    """Generate a PDF from a single article.

    Args:
        article: The article to convert to PDF.
        output_dir: Directory to save the PDF. Defaults to config output dir.

    Returns:
        Path to the generated PDF file.

    Raises:
        ContentTooLargeError: If the article content exceeds MAX_CONTENT_SIZE.
    """
    return generate_combined_pdf([article], output_dir)


def generate_combined_pdf(articles: list[Article], output_dir: Path | None = None) -> Path:
    """Generate a single PDF from multiple articles.

    Args:
        articles: List of articles to combine into one PDF.
        output_dir: Directory to save the PDF. Defaults to config output dir.

    Returns:
        Path to the generated PDF file.

    Raises:
        ContentTooLargeError: If the combined content exceeds MAX_CONTENT_SIZE.
        ValueError: If no articles are provided.
    """
    if not articles:
        raise ValueError("At least one article is required")

    # Check combined content size
    total_size = sum(len(a.content.encode("utf-8")) for a in articles)
    if total_size > MAX_CONTENT_SIZE:
        log.warning(
            "content_too_large",
            size=total_size,
            max_size=MAX_CONTENT_SIZE,
            article_count=len(articles),
        )
        raise ContentTooLargeError(total_size)

    if output_dir is None:
        output_dir = get_config().output_dir

    # Create output directory if it doesn't exist
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename based on first article or combined name
    if len(articles) == 1:
        slug = _slugify_title(articles[0].title)
    else:
        slug = _slugify_title(f"{articles[0].title}-and-{len(articles) - 1}-more")

    date_str = f"_{datetime.now().strftime('%Y%m%d')}"
    filename = f"{slug}{date_str}.pdf"
    pdf_path = output_dir / filename

    # Render combined HTML
    html_content = _render_combined_html(articles)

    log.info(
        "generating_combined_pdf",
        article_count=len(articles),
        titles=[a.title for a in articles],
        output_path=str(pdf_path),
    )

    HTML(string=html_content).write_pdf(pdf_path)

    log.info("pdf_generated", path=str(pdf_path), size=pdf_path.stat().st_size)

    return pdf_path


def _render_combined_html(articles: list[Article]) -> str:
    """Render multiple articles to a single HTML document.

    Args:
        articles: List of articles to render.

    Returns:
        Complete HTML string with all articles and page breaks between them.
    """
    css = _get_ereader_css()

    # Build article sections
    article_sections = []
    for i, article in enumerate(articles):
        date_str = ""
        if article.published_at:
            date_str = article.published_at.strftime("%B %d, %Y at %H:%M")

        # Add page-break-before for all articles except the first
        page_break = 'style="page-break-before: always;"' if i > 0 else ""

        section = f"""
        <article {page_break}>
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
        </article>"""
        article_sections.append(section)

    # Combine all sections
    all_articles = "\n".join(article_sections)

    # Generate title for the document
    if len(articles) == 1:
        doc_title = articles[0].title
    else:
        doc_title = f"{articles[0].title} (+{len(articles) - 1} more)"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{doc_title}</title>
    <style>
{css}
    </style>
</head>
<body>
    {all_articles}
</body>
</html>"""

    return html


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

        .tweet-images {
            margin-top: 1em;
        }

        .tweet-images img {
            max-width: 100%;
            height: auto;
            margin: 0.5em 0;
            border-radius: 8px;
        }

        .main-tweet {
            border-left-width: 4px;
            background: #f0f7ff;
        }

        .replies-section {
            margin-top: 2em;
            padding-top: 1em;
            border-top: 1px solid #e0e0e0;
        }

        .replies-header {
            font-size: 1.2em;
            color: #444444;
            margin-bottom: 1em;
        }

        .reply {
            margin: 1em 0;
            padding: 0.8em;
            border-left: 2px solid #cccccc;
            background: #fafafa;
            font-size: 0.95em;
        }

        .op-reply {
            border-left-color: #1da1f2;
            background: #f0f7ff;
        }

        .op-badge {
            display: inline-block;
            background: #1da1f2;
            color: white;
            font-size: 0.7em;
            padding: 0.1em 0.4em;
            border-radius: 3px;
            margin-left: 0.5em;
            vertical-align: middle;
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
