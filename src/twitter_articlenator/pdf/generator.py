"""PDF generation using WeasyPrint."""

import gc
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

import structlog
from pypdf import PdfWriter
from weasyprint import HTML
from weasyprint.urls import default_url_fetcher

from twitter_articlenator.config import get_config
from twitter_articlenator.sources.base import Article

log = structlog.get_logger()

# Timeout for fetching remote resources (images, etc.)
URL_FETCH_TIMEOUT = 30

# Maximum slug length for filenames
MAX_SLUG_LENGTH = 80

# Maximum content size in bytes to prevent memory issues
MAX_CONTENT_SIZE = 500_000_000  # 500MB

# Number of articles per batch when generating large PDFs.
# Keeps WeasyPrint memory usage bounded (~50-100MB per batch).
PDF_BATCH_SIZE = 50


def _browser_url_fetcher(url, timeout=URL_FETCH_TIMEOUT, **kwargs):
    """URL fetcher with browser-like headers to avoid CDN blocks."""
    if url.startswith("http"):
        from urllib.request import Request, urlopen

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        try:
            req = Request(url, headers=headers)
            response = urlopen(req, timeout=timeout)  # noqa: S310
            return {
                "string": response.read(),
                "mime_type": response.headers.get_content_type(),
                "redirected_url": response.url,
            }
        except Exception as e:
            log.debug("url_fetch_failed", url=url[:120], error=str(e))
            return default_url_fetcher(url, timeout=timeout)
    return default_url_fetcher(url, timeout=timeout)


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

    For large article lists, generates in batches to avoid OOM and merges
    the partial PDFs using pypdf (lightweight, streaming merge).

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

    log.info(
        "generating_combined_pdf",
        article_count=len(articles),
        titles=[a.title for a in articles],
        output_path=str(pdf_path),
    )

    # Small batches: render directly in one go
    if len(articles) <= PDF_BATCH_SIZE:
        html_content = _render_combined_html(articles)
        HTML(string=html_content, url_fetcher=_browser_url_fetcher).write_pdf(pdf_path)
        log.info("pdf_generated", path=str(pdf_path), size=pdf_path.stat().st_size)
        return pdf_path

    # Large batches: render in chunks to avoid OOM, then merge
    log.info(
        "pdf_batched_generation",
        batch_size=PDF_BATCH_SIZE,
        num_batches=(len(articles) + PDF_BATCH_SIZE - 1) // PDF_BATCH_SIZE,
    )

    partial_paths = []
    skipped = 0
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            for batch_idx in range(0, len(articles), PDF_BATCH_SIZE):
                batch = articles[batch_idx : batch_idx + PDF_BATCH_SIZE]
                batch_num = batch_idx // PDF_BATCH_SIZE + 1
                partial_path = tmp / f"batch_{batch_num:04d}.pdf"

                log.info(
                    "pdf_batch_rendering",
                    batch=batch_num,
                    articles=len(batch),
                    start=batch_idx + 1,
                    end=batch_idx + len(batch),
                )

                try:
                    html_content = _render_combined_html(batch)
                    HTML(string=html_content, url_fetcher=_browser_url_fetcher).write_pdf(
                        partial_path
                    )
                    partial_paths.append(partial_path)
                except Exception as batch_err:
                    # Batch failed - try each article individually
                    log.warning(
                        "pdf_batch_failed_retrying_individually",
                        batch=batch_num,
                        error=str(batch_err),
                    )
                    for j, article in enumerate(batch):
                        article_num = batch_idx + j + 1
                        individual_path = tmp / f"article_{article_num:04d}.pdf"
                        try:
                            html_content = _render_combined_html([article])
                            HTML(string=html_content, url_fetcher=_browser_url_fetcher).write_pdf(
                                individual_path
                            )
                            partial_paths.append(individual_path)
                        except Exception as art_err:
                            skipped += 1
                            log.warning(
                                "pdf_article_skipped",
                                article_num=article_num,
                                title=article.title[:80],
                                error=str(art_err),
                            )

                # Free memory between batches
                gc.collect()

            if not partial_paths:
                raise RuntimeError("All articles failed PDF rendering")

            if skipped:
                log.info("pdf_skipped_articles", count=skipped)

            # Merge all partial PDFs
            log.info("pdf_merging", num_parts=len(partial_paths))
            writer = PdfWriter()
            for part in partial_paths:
                writer.append(str(part))
            writer.write(str(pdf_path))
            writer.close()

    except Exception:
        # Clean up partial output on failure
        if pdf_path.exists():
            pdf_path.unlink()
        raise

    log.info("pdf_generated", path=str(pdf_path), size=pdf_path.stat().st_size)
    return pdf_path


def _sanitize_html(content: str) -> str:
    """Sanitize HTML content to prevent WeasyPrint/cssselect2 crashes.

    Strips XML namespace-prefixed tags (e.g. <x:xmpmeta>, <dc:title>, <rdf:li>)
    that cause cssselect2 AssertionError when it expects Clark notation.
    """
    # Remove namespace-prefixed tags and their content where possible
    content = re.sub(r"</?[a-zA-Z]+:[a-zA-Z]+[^>]*>", "", content)
    return content


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

        sanitized_content = _sanitize_html(article.content)

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
                {sanitized_content}
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
            {_sanitize_html(article.content)}
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

        .article-image {
            margin: 1.5em 0;
            text-align: center;
        }

        .article-image img {
            max-width: 100%;
            height: auto;
            border-radius: 8px;
        }

        .code-block {
            margin: 1.2em 0;
            background: #f6f8fa;
            border: 1px solid #e1e4e8;
            border-radius: 6px;
            overflow: hidden;
            page-break-inside: avoid;
        }

        .code-lang {
            font-family: 'Courier New', monospace;
            font-size: 0.75em;
            color: #6a737d;
            background: #eef0f2;
            padding: 0.3em 0.8em;
            border-bottom: 1px solid #e1e4e8;
        }

        .code-block pre {
            margin: 0;
            padding: 0.8em;
            overflow-x: auto;
            font-size: 0.8em;
            line-height: 1.45;
            background: #f6f8fa;
        }

        .code-block code {
            font-family: 'Courier New', Consolas, monospace;
            font-size: 1em;
            background: transparent;
            padding: 0;
            white-space: pre;
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
