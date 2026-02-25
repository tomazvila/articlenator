"""API routes blueprint."""

import json as json_module
import time

import structlog
from flask import Blueprint, Response, jsonify, request, current_app
from ..config import parse_cookie_input, validate_cookies
from ..pdf.generator import generate_combined_pdf
from ..sources import get_source_for_url
from ..sources.twitter_playwright import TwitterPlaywrightSource

# Delay between processing URLs to avoid rate limiting (seconds)
URL_PROCESSING_DELAY = 2.0

log = structlog.get_logger()

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _get_run_async():
    """Get the run_async function from the app context."""
    return current_app.config.get("RUN_ASYNC")


def _get_cookies_from_request() -> str | None:
    """Extract cookies from the request body.

    Clients send cookies from their localStorage with each request.

    Returns:
        Normalized cookie string, or None if not provided.
    """
    if request.is_json:
        data = request.get_json() or {}
        raw = data.get("cookies", "")
    else:
        raw = request.form.get("cookies", "")

    if not raw or not raw.strip():
        return None

    return parse_cookie_input(raw.strip())


@api_bp.route("/health")
def health():
    """GET /api/health - Health check endpoint."""
    return jsonify({"status": "ok"})


@api_bp.route("/convert", methods=["POST"])
def convert():
    """POST /api/convert - Process links and return PDF paths.

    Expects cookies and links in request body.
    """
    run_async = _get_run_async()

    # Handle both JSON and form data
    if request.is_json:
        data = request.get_json() or {}
        links = data.get("links", [])
    else:
        # Form data - links comes as newline-separated text
        links_text = request.form.get("links", "")
        links = [line.strip() for line in links_text.split("\n") if line.strip()]

    if not links:
        return jsonify({"error": "No links provided"}), 400

    cookies = _get_cookies_from_request()

    # Validate URLs and find sources
    sources_for_urls = []
    unsupported_urls = []

    for url in links:
        source = get_source_for_url(url, cookies=cookies)
        if source:
            sources_for_urls.append((url, source))
        else:
            unsupported_urls.append(url)

    if unsupported_urls:
        return (
            jsonify({"error": f"Unsupported URLs: {', '.join(unsupported_urls)}"}),
            400,
        )

    # Check if Twitter URLs need cookies
    twitter_urls = [
        url for url, src in sources_for_urls if isinstance(src, TwitterPlaywrightSource)
    ]
    if twitter_urls and not cookies:
        return (
            jsonify(
                {
                    "error": f"Twitter cookies required for: {', '.join(twitter_urls)}. Please set up your cookies first.",
                    "setup_url": "/setup",
                }
            ),
            400,
        )

    log.info("convert_requested", link_count=len(links))

    # Fetch all articles first
    articles = []
    errors = []

    for i, (url, source) in enumerate(sources_for_urls):
        # Add delay between requests to avoid rate limiting (skip first)
        if i > 0 and isinstance(source, TwitterPlaywrightSource):
            time.sleep(URL_PROCESSING_DELAY)

        try:
            log.info("processing_url", url=url, source_type=type(source).__name__)

            # Fetch article (run async in sync context)
            article = run_async(source.fetch(url))
            articles.append({"url": url, "article": article})
            log.info("url_fetched", url=url, title=article.title)

        except Exception as e:
            log.error("url_processing_failed", url=url, error=str(e))
            errors.append({"url": url, "error": str(e)})

    if not articles and errors:
        error_details = "\n".join([f"- {e['url']}: {e['error']}" for e in errors])
        return (
            jsonify({"error": f"All conversions failed:\n{error_details}"}),
            500,
        )

    # Generate single combined PDF from all articles
    try:
        article_objects = [a["article"] for a in articles]
        pdf_path = generate_combined_pdf(article_objects)

        results = [
            {
                "url": a["url"],
                "title": a["article"].title,
                "author": a["article"].author,
                "status": "success",
            }
            for a in articles
        ]

        log.info("combined_pdf_generated", pdf=pdf_path.name, article_count=len(articles))

        total_count = len(articles) + len(errors)
        return jsonify(
            {
                "success": True,
                "filename": pdf_path.name,
                "articles": results,
                "errors": errors if errors else None,
                "summary": {
                    "total": total_count,
                    "succeeded": len(articles),
                    "failed": len(errors),
                },
            }
        )
    except Exception as e:
        log.error("pdf_generation_failed", error=str(e))
        return jsonify({"error": f"PDF generation failed: {str(e)}"}), 500


@api_bp.route("/cookies/validate", methods=["POST"])
def validate_cookies_endpoint():
    """POST /api/cookies/validate - Validate cookie format.

    Client sends cookies from localStorage for server-side validation.
    """
    cookies = _get_cookies_from_request()
    result = validate_cookies(cookies)

    log.info("cookies_validated", status=result["status"])
    return jsonify(result)


@api_bp.route("/convert/stream", methods=["POST"])
def convert_stream():
    """POST /api/convert/stream - Process links with streaming progress updates.

    Expects cookies and links in request body.
    """
    run_async = _get_run_async()

    # Handle both JSON and form data
    if request.is_json:
        data = request.get_json() or {}
        links = data.get("links", [])
    else:
        links_text = request.form.get("links", "")
        links = [line.strip() for line in links_text.split("\n") if line.strip()]

    if not links:
        return jsonify({"error": "No links provided"}), 400

    cookies = _get_cookies_from_request()

    # Validate URLs and find sources
    sources_for_urls = []
    unsupported_urls = []

    for url in links:
        source = get_source_for_url(url, cookies=cookies)
        if source:
            sources_for_urls.append((url, source))
        else:
            unsupported_urls.append(url)

    if unsupported_urls:
        return (
            jsonify({"error": f"Unsupported URLs: {', '.join(unsupported_urls)}"}),
            400,
        )

    # Check if Twitter URLs need cookies
    twitter_urls = [
        url for url, src in sources_for_urls if isinstance(src, TwitterPlaywrightSource)
    ]
    if twitter_urls and not cookies:
        return (
            jsonify(
                {
                    "error": f"Twitter cookies required for: {', '.join(twitter_urls)}",
                    "setup_url": "/setup",
                }
            ),
            400,
        )

    def generate():
        """Generator function for SSE stream."""
        articles = []
        errors = []
        total = len(sources_for_urls)

        # Send initial progress
        yield f"data: {json_module.dumps({'type': 'start', 'total': total})}\n\n"

        for i, (url, source) in enumerate(sources_for_urls, 1):
            # Add delay between requests to avoid rate limiting (skip first)
            if i > 1 and isinstance(source, TwitterPlaywrightSource):
                time.sleep(URL_PROCESSING_DELAY)

            # Send progress update
            yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'processing'})}\n\n"

            try:
                log.info("processing_url_stream", url=url, progress=f"{i}/{total}")
                article = run_async(source.fetch(url))
                articles.append({"url": url, "article": article})

                yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'success', 'title': article.title})}\n\n"

            except Exception as e:
                log.error("url_processing_failed_stream", url=url, error=str(e))
                errors.append({"url": url, "error": str(e)})

                yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': str(e)})}\n\n"

        # Generate PDF if we have articles
        if articles:
            try:
                yield f"data: {json_module.dumps({'type': 'generating_pdf'})}\n\n"

                article_objects = [a["article"] for a in articles]
                pdf_path = generate_combined_pdf(article_objects)

                results = [
                    {
                        "url": a["url"],
                        "title": a["article"].title,
                        "author": a["article"].author,
                        "status": "success",
                    }
                    for a in articles
                ]

                final_result = {
                    "type": "complete",
                    "success": True,
                    "filename": pdf_path.name,
                    "articles": results,
                    "errors": errors if errors else None,
                    "summary": {
                        "total": total,
                        "succeeded": len(articles),
                        "failed": len(errors),
                    },
                }
                yield f"data: {json_module.dumps(final_result)}\n\n"

            except Exception as e:
                yield f"data: {json_module.dumps({'type': 'error', 'error': f'PDF generation failed: {str(e)}'})}\n\n"
        else:
            error_details = [{"url": e["url"], "error": e["error"]} for e in errors]
            yield f"data: {json_module.dumps({'type': 'error', 'error': 'All conversions failed', 'details': error_details})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@api_bp.route("/bookmarks/fetch", methods=["POST"])
def bookmarks_fetch():
    """POST /api/bookmarks/fetch - Scrape bookmarks with streaming progress.

    Expects cookies in request body. Returns SSE stream of bookmark entries.
    """
    run_async = _get_run_async()
    cookies = _get_cookies_from_request()

    if not cookies:
        return jsonify(
            {
                "error": "Twitter cookies required. Please set up your cookies first.",
                "setup_url": "/setup",
            }
        ), 400

    validation = validate_cookies(cookies)
    if not validation["valid"]:
        return jsonify({"error": validation["message"]}), 400

    from ..sources.bookmarks import BookmarkScraper

    scraper = BookmarkScraper(cookies=cookies)

    def generate():
        """Generator function for SSE stream."""
        try:
            yield f"data: {json_module.dumps({'type': 'start'})}\n\n"

            bookmarks = run_async(scraper.scrape())

            for i, entry in enumerate(bookmarks, 1):
                yield f"data: {json_module.dumps({'type': 'bookmark', 'count': i, 'entry': entry.to_dict()})}\n\n"

            yield f"data: {json_module.dumps({'type': 'complete', 'total': len(bookmarks)})}\n\n"

        except Exception as e:
            log.error("bookmark_fetch_failed", error=str(e))
            yield f"data: {json_module.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@api_bp.route("/bookmarks/convert", methods=["POST"])
def bookmarks_convert():
    """POST /api/bookmarks/convert - Convert selected bookmark URLs to PDF.

    Expects cookies and urls list in request body. Returns SSE stream.
    """
    run_async = _get_run_async()

    if request.is_json:
        data = request.get_json() or {}
        urls = data.get("urls", [])
    else:
        urls_text = request.form.get("urls", "")
        urls = [line.strip() for line in urls_text.split("\n") if line.strip()]

    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    cookies = _get_cookies_from_request()

    # Build sources for all URLs
    sources_for_urls = []
    for url in urls:
        source = get_source_for_url(url, cookies=cookies)
        if source:
            sources_for_urls.append((url, source))
        else:
            sources_for_urls.append((url, None))

    def generate():
        """Generator function for SSE stream."""
        articles = []
        errors = []
        total = len(sources_for_urls)

        yield f"data: {json_module.dumps({'type': 'start', 'total': total})}\n\n"

        for i, (url, source) in enumerate(sources_for_urls, 1):
            if i > 1:
                time.sleep(URL_PROCESSING_DELAY)

            yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'processing'})}\n\n"

            if source is None:
                errors.append({"url": url, "error": "Unsupported URL"})
                yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': 'Unsupported URL'})}\n\n"
                continue

            try:
                log.info("processing_bookmark_url", url=url, progress=f"{i}/{total}")
                article = run_async(source.fetch(url))
                articles.append({"url": url, "article": article})

                yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'success', 'title': article.title})}\n\n"

            except Exception as e:
                log.error("bookmark_url_failed", url=url, error=str(e))
                errors.append({"url": url, "error": str(e)})
                yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': str(e)})}\n\n"

        if articles:
            try:
                yield f"data: {json_module.dumps({'type': 'generating_pdf'})}\n\n"

                article_objects = [a["article"] for a in articles]
                pdf_path = generate_combined_pdf(article_objects)

                results = [
                    {
                        "url": a["url"],
                        "title": a["article"].title,
                        "author": a["article"].author,
                        "status": "success",
                    }
                    for a in articles
                ]

                final_result = {
                    "type": "complete",
                    "success": True,
                    "filename": pdf_path.name,
                    "articles": results,
                    "errors": errors if errors else None,
                    "summary": {
                        "total": total,
                        "succeeded": len(articles),
                        "failed": len(errors),
                    },
                }
                yield f"data: {json_module.dumps(final_result)}\n\n"

            except Exception as e:
                yield f"data: {json_module.dumps({'type': 'error', 'error': f'PDF generation failed: {str(e)}'})}\n\n"
        else:
            error_details = [{"url": e["url"], "error": e["error"]} for e in errors]
            yield f"data: {json_module.dumps({'type': 'error', 'error': 'All conversions failed', 'details': error_details})}\n\n"

    return Response(generate(), mimetype="text/event-stream")
