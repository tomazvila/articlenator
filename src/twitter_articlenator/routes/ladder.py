"""Authenticated 13ft reader page and retrieval endpoint."""

import time

from bs4 import BeautifulSoup
from flask import Response, Blueprint, jsonify, render_template, request

from ..resource_limits import limit_resource
from ..sources import ladder_core, ladder_http

ladder_bp = Blueprint("ladder", __name__)


def article_blocks(html):
    """Return plain-text semantic blocks; third-party markup never reaches the DOM."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else "Article"
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    body = (
        soup.select_one('[data-testid="article-body"], [class*="article-body-module__content"]')
        or soup.find("article")
        or soup.find("main")
        or soup.body
        or soup
    )
    blocks = []
    tags = {"h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre"}

    def is_block(tag):
        return tag.name in tags or str(tag.get("data-testid", "")).startswith("paragraph-")

    for tag in body.find_all(is_block):
        if any(is_block(parent) for parent in tag.parents if parent is not body):
            continue
        text = tag.get_text(" ", strip=True)
        if text:
            blocks.append(
                {
                    "tag": tag.name if tag.name in tags - {"li"} else "p",
                    "text": ("• " if tag.name == "li" else "") + text,
                }
            )
    if not blocks:
        text = body.get_text(" ", strip=True)
        if text:
            blocks = [{"tag": "p", "text": text}]
    if not blocks:
        raise ValueError("No readable article text was found at that URL.")
    return {"title": title, "blocks": blocks}


@ladder_bp.get("/13ft")
def page():
    return render_template("ladder.html")


@ladder_bp.post("/api/13ft")
@limit_resource("download")
def fetch_article():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Enter a website URL."), 400
    token = ladder_http.DEADLINE.set(time.monotonic() + 60)
    try:
        url, *_ = ladder_http.validate_url(data.get("url"))
        html = ladder_core.bypass_paywall(url, ladder_core._DEFAULT_STRINGS)
        return jsonify(**article_blocks(html), url=url)
    except (ValueError, ladder_core.UserFacingError) as exc:
        return jsonify(error=str(exc)), 400
    except (TimeoutError, OSError):
        return jsonify(error="Could not reach the website in time. Try again later."), 502
    except Exception:
        return jsonify(error="Could not fetch this article. Try another URL."), 502
    finally:
        ladder_http.DEADLINE.reset(token)


@ladder_bp.post("/api/13ft/pdf")
@limit_resource("download")
def export_pdf():
    """Export the displayed text without fetching the source again."""
    from urllib.parse import urlsplit

    from slugify import slugify
    from weasyprint import HTML

    from ..pdf.generator import _get_ereader_css

    data = request.get_json(silent=True)
    try:
        if not isinstance(data, dict):
            raise ValueError
        title, url, blocks = data.get("title"), data.get("url"), data.get("blocks")
        if not isinstance(title, str) or not title.strip() or len(title) > 1000:
            raise ValueError
        if not isinstance(url, str) or len(url) > 8192:
            raise ValueError
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 10000:
            raise ValueError
        for block in blocks:
            if (
                not isinstance(block, dict)
                or block.get("tag") not in {"h1", "h2", "h3", "h4", "p", "blockquote", "pre"}
                or not isinstance(block.get("text"), str)
            ):
                raise ValueError
        if sum(len(block["text"]) for block in blocks) > 500000:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify(error="Load an article before downloading its PDF."), 400

    def no_external_resources(url, **kwargs):
        raise ValueError("PDF exports contain text only.")

    try:
        html = render_template(
            "ladder_pdf.html", title=title, url=url, blocks=blocks, pdf_css=_get_ereader_css()
        )
        pdf = HTML(string=html, url_fetcher=no_external_resources).write_pdf()
        download_name = (slugify(title)[:100] or "article") + ".pdf"
        # Response with explicit length, not send_file(BytesIO): a response
        # without Content-Length counts as streamed, which would defer the
        # download resource lease to response close and wedge the limit.
        return Response(
            pdf,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{download_name}"',
                "Content-Length": str(len(pdf)),
            },
        )
    except Exception:
        return jsonify(error="Could not create the PDF. Please try again."), 500
