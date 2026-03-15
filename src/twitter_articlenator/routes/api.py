"""API routes blueprint."""

import hashlib
import json as json_module
import re
import shutil
import time
import uuid

import structlog
from flask import Blueprint, Response, jsonify, request, current_app
from ..config import get_config, parse_cookie_input, validate_cookies
from ..pdf.generator import generate_combined_pdf
from ..sources import get_source_for_url
from ..sources.base import Article
from ..sources.twitter_playwright import TwitterPlaywrightSource

# Delay between processing URLs to avoid rate limiting (seconds)
URL_PROCESSING_DELAY = 2.0

# Resilient article fetch settings (mirrors video download resilience)
ARTICLE_BASE_DELAY = 2  # seconds between fetches (base)
ARTICLE_MAX_RETRIES = 3  # retries per URL before giving up
ARTICLE_RETRY_DELAYS = [15, 30, 60]  # escalating retry waits (seconds)

# Timeout for a single article fetch (seconds) — kills stuck Playwright fetches
FETCH_TIMEOUT = 120

# Auto-cleanup sessions older than this many days
SESSION_TTL_DAYS = 7

# Pattern for valid Twitter/X video URLs
TWITTER_URL_PATTERN = re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/(\w+)/status/(\d+)")

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


def _sleep_with_keepalive(seconds):
    """Sleep in chunks, yielding SSE keepalive comments to prevent connection timeout."""
    elapsed = 0
    while elapsed < seconds:
        chunk = min(10, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
        yield ": keepalive\n\n"


def _get_session_dir(session_id):
    """Get or create session directory for accumulating articles across reconnections."""
    config = get_config()
    session_dir = config.output_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _save_article(session_dir, url, article):
    """Save a fetched article to the session directory for reconnection support."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    data = {
        "url": url,
        "title": article.title,
        "author": article.author,
        "content": article.content,
        "published_at": article.published_at.isoformat() if article.published_at else None,
        "source_url": article.source_url,
        "source_type": article.source_type,
    }
    (session_dir / f"{url_hash}.json").write_text(
        json_module.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _load_session_articles(session_dir):
    """Load all previously saved articles from a session directory.

    Returns:
        Dict mapping URL to Article object.
    """
    from datetime import datetime

    articles = {}
    for path in session_dir.glob("*.json"):
        if path.name == "_meta.json":
            continue
        data = json_module.loads(path.read_text(encoding="utf-8"))
        published_at = None
        if data.get("published_at"):
            try:
                published_at = datetime.fromisoformat(data["published_at"])
            except (ValueError, TypeError):
                pass
        article = Article(
            title=data["title"],
            author=data["author"],
            content=data["content"],
            published_at=published_at,
            source_url=data["source_url"],
            source_type=data["source_type"],
        )
        articles[data["url"]] = article
    return articles


def _cleanup_session(session_dir):
    """Remove session directory after PDF generation."""
    try:
        shutil.rmtree(session_dir)
    except Exception:
        pass


def _save_session_meta(session_dir, urls, status="running"):
    """Save session metadata for recovery and management."""
    from datetime import datetime as dt, timezone

    now = dt.now(timezone.utc).isoformat()
    meta = {
        "urls": urls,
        "total": len(urls),
        "status": status,
        "created_at": now,
        "updated_at": now,
    }
    (session_dir / "_meta.json").write_text(
        json_module.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def _load_session_meta(session_dir):
    """Load session metadata. Returns None if not found or invalid."""
    meta_path = session_dir / "_meta.json"
    if not meta_path.exists():
        return None
    try:
        return json_module.loads(meta_path.read_text(encoding="utf-8"))
    except (json_module.JSONDecodeError, OSError):
        return None


def _update_session_status(session_dir, status, **extra):
    """Update session status and timestamp."""
    from datetime import datetime as dt, timezone

    meta = _load_session_meta(session_dir) or {}
    meta["status"] = status
    meta["updated_at"] = dt.now(timezone.utc).isoformat()
    meta.update(extra)
    (session_dir / "_meta.json").write_text(
        json_module.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def _cleanup_stale_sessions():
    """Remove session directories older than SESSION_TTL_DAYS."""
    from datetime import datetime as dt, timedelta, timezone

    config = get_config()
    sessions_dir = config.output_dir / "sessions"
    if not sessions_dir.exists():
        return

    cutoff = dt.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS)
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        meta = _load_session_meta(session_dir)
        if meta and meta.get("updated_at"):
            try:
                updated = dt.fromisoformat(meta["updated_at"])
                if updated < cutoff:
                    shutil.rmtree(session_dir)
                    log.info("stale_session_cleaned", session_id=session_dir.name)
            except (ValueError, TypeError):
                pass
        else:
            # No meta — check directory mtime
            try:
                mtime = dt.fromtimestamp(session_dir.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    shutil.rmtree(session_dir)
                    log.info("stale_session_cleaned_no_meta", session_id=session_dir.name)
            except OSError:
                pass


def _count_session_articles(session_dir):
    """Count saved article files in a session directory."""
    return len([f for f in session_dir.glob("*.json") if f.name != "_meta.json"])


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
    """POST /api/cookies/validate - Validate cookie format and optionally test live.

    Client sends cookies from localStorage for server-side validation.
    Pass ?live=true to also test cookies against Twitter's API.
    """
    cookies = _get_cookies_from_request()
    result = validate_cookies(cookies)

    if not result["valid"]:
        log.info("cookies_validated", status=result["status"])
        return jsonify(result)

    # If live validation requested, test against Twitter API
    live = request.args.get("live", "false").lower() == "true"
    if live and cookies:
        import httpx

        try:
            cookie_dict = {}
            for part in cookies.split(";"):
                part = part.strip()
                if "=" in part:
                    name, value = part.split("=", 1)
                    cookie_dict[name.strip()] = value.strip()

            # Use a lightweight page fetch to check auth — the v1.1 REST API
            # has been fully deprecated by X (returns 404).  Instead, request
            # the minimal HTML for the home timeline; an authenticated session
            # will return 200, while expired cookies get a 302/401 to /login.
            resp = httpx.get(
                "https://x.com/home",
                headers={
                    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                },
                cookies=cookie_dict,
                timeout=15,
                follow_redirects=False,
            )
            if resp.status_code == 200:
                result["live"] = True
                result["message"] = "Cookies valid — authentication confirmed."
                log.info("cookies_live_valid")
            elif resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if "login" in location:
                    result["valid"] = False
                    result["live"] = False
                    result["status"] = "expired"
                    result["message"] = (
                        "Cookies have expired or are invalid. Please get fresh cookies from Twitter."
                    )
                    log.warning(
                        "cookies_live_invalid", status_code=resp.status_code, location=location
                    )
                else:
                    result["live"] = True
                    result["message"] = "Cookies valid — authentication confirmed."
                    log.info("cookies_live_valid_redirect", location=location)
            else:
                result["live"] = None
                result["message"] += " (Could not verify live — unexpected status)"
                log.warning("cookies_live_unexpected", status_code=resp.status_code)
        except Exception as e:
            log.warning("cookies_live_check_failed", error=str(e))
            # Don't fail validation — just skip live check
            result["live"] = None
            result["message"] += " (Could not verify live — network error)"

    log.info("cookies_validated", status=result["status"])
    return jsonify(result)


@api_bp.route("/convert/stream", methods=["POST"])
def convert_stream():
    """POST /api/convert/stream - Process links with streaming progress updates.

    Expects cookies and links in request body.
    Uses resilient retry/backoff pattern for reliable batch processing of large link lists.
    """
    import queue as queue_module
    import random
    import threading

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

    # Session support: reuse session_id on reconnect to resume where we left off
    if request.is_json:
        session_id = (request.get_json() or {}).get("session_id") or str(uuid.uuid4())
    else:
        session_id = str(uuid.uuid4())
    session_dir = _get_session_dir(session_id)

    # Build sources for all URLs (lenient — unsupported URLs get source=None and are
    # skipped during processing instead of blocking the entire batch)
    sources_for_urls = []
    has_twitter_urls = False
    for url in links:
        source = get_source_for_url(url, cookies=cookies)
        sources_for_urls.append((url, source))
        if source and isinstance(source, TwitterPlaywrightSource):
            has_twitter_urls = True

    # Twitter URLs still require cookies
    if has_twitter_urls and not cookies:
        return (
            jsonify(
                {
                    "error": "Twitter cookies required. Please set up your cookies first.",
                    "setup_url": "/setup",
                }
            ),
            400,
        )

    def generate():
        """Generator function for SSE stream with resilient retry/backoff."""
        # Load articles already fetched in previous connections (reconnection support)
        # Only track URLs and titles — don't keep full article content in memory
        # to avoid OOM on large batches (600+ articles).
        saved = _load_session_articles(session_dir)
        processed_urls = set(saved.keys())
        processed_titles = {u: a.title for u, a in saved.items()}
        del saved  # Free article content from memory
        errors = []
        total = len(sources_for_urls)
        consecutive_failures = 0

        # Save session metadata for recovery
        _save_session_meta(session_dir, links, status="running")

        try:
            yield f"data: {json_module.dumps({'type': 'start', 'total': total, 'session_id': session_id, 'already_done': len(processed_urls)})}\n\n"

            for i, (url, source) in enumerate(sources_for_urls, 1):
                # Skip URLs already fetched in a previous connection
                if url in processed_urls:
                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'success', 'title': processed_titles.get(url, ''), 'resumed': True})}\n\n"
                    continue

                # Adaptive delay between requests to avoid rate limiting
                if i > 1:
                    is_twitter = source is not None and isinstance(source, TwitterPlaywrightSource)
                    delay = ARTICLE_BASE_DELAY + random.uniform(0, 2)
                    if is_twitter:
                        delay += 3  # Extra delay for Twitter rate limits
                    if consecutive_failures > 0:
                        delay += min(consecutive_failures * 10, 120)

                    log.info(
                        "article_throttle_delay",
                        delay=round(delay, 1),
                        item=f"{i}/{total}",
                    )
                    yield f"data: {json_module.dumps({'type': 'waiting', 'current': i, 'total': total, 'seconds': round(delay)})}\n\n"
                    yield from _sleep_with_keepalive(delay)

                yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'processing'})}\n\n"

                # Skip unsupported URLs gracefully
                if source is None:
                    errors.append({"url": url, "error": "Unsupported URL"})
                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': 'Unsupported URL'})}\n\n"
                    continue

                # Retry loop for each URL
                succeeded = False
                last_error = None

                for attempt in range(ARTICLE_MAX_RETRIES + 1):
                    try:
                        log.info(
                            "processing_url_stream",
                            url=url,
                            progress=f"{i}/{total}",
                            attempt=attempt + 1,
                        )

                        # Run fetch in a thread so we can send keepalive
                        # comments while it blocks, preventing the SSE
                        # connection from being dropped.
                        fetch_q = queue_module.Queue()

                        def _do_fetch(src=source, u=url):
                            try:
                                a = run_async(src.fetch(u))
                                fetch_q.put(("ok", a))
                            except Exception as exc:
                                fetch_q.put(("err", exc))

                        threading.Thread(target=_do_fetch, daemon=True).start()

                        # Wait with hard timeout to kill stuck fetches
                        fetch_start = time.time()
                        while True:
                            try:
                                fetch_result = fetch_q.get(timeout=10)
                                break
                            except queue_module.Empty:
                                if time.time() - fetch_start > FETCH_TIMEOUT:
                                    raise TimeoutError(f"Fetch timed out after {FETCH_TIMEOUT}s")
                                yield ": keepalive\n\n"

                        if fetch_result[0] == "err":
                            raise fetch_result[1]
                        article = fetch_result[1]

                        # Save to disk — don't keep article content in memory
                        _save_article(session_dir, url, article)
                        processed_urls.add(url)
                        processed_titles[url] = article.title

                        yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'success', 'title': article.title})}\n\n"

                        succeeded = True
                        consecutive_failures = 0
                        break

                    except Exception as e:
                        last_error = str(e)
                        log.warning(
                            "url_fetch_attempt_failed",
                            url=url,
                            attempt=attempt + 1,
                            max_attempts=ARTICLE_MAX_RETRIES + 1,
                            error=last_error,
                        )

                        if attempt < ARTICLE_MAX_RETRIES:
                            retry_delay = ARTICLE_RETRY_DELAYS[attempt] + random.uniform(0, 5)
                            log.info("article_retry_wait", seconds=round(retry_delay, 1))
                            yield f"data: {json_module.dumps({'type': 'retry', 'current': i, 'total': total, 'url': url, 'attempt': attempt + 2, 'max_attempts': ARTICLE_MAX_RETRIES + 1, 'wait_seconds': round(retry_delay)})}\n\n"
                            yield from _sleep_with_keepalive(retry_delay)

                if not succeeded:
                    consecutive_failures += 1
                    log.error(
                        "url_fetch_failed_all_retries",
                        url=url,
                        error=last_error,
                        retries=ARTICLE_MAX_RETRIES,
                    )
                    errors.append({"url": url, "error": last_error})

                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': last_error})}\n\n"

        except GeneratorExit:
            _update_session_status(
                session_dir, "interrupted",
                processed=len(processed_urls), errors=len(errors),
            )
            log.warning(
                "convert_stream_client_disconnected",
                completed=len(processed_urls),
                remaining=total - len(processed_urls) - len(errors),
            )
            return
        except Exception as e:
            _update_session_status(session_dir, "error", error=str(e))
            log.error("convert_stream_fatal_error", error=str(e), completed=len(processed_urls))
            try:
                yield f"data: {json_module.dumps({'type': 'error', 'error': f'Server error: {str(e)}'})}\n\n"
            except GeneratorExit:
                return

        # Generate PDF — reload articles from disk to avoid keeping them in memory
        if processed_urls:
            try:
                yield f"data: {json_module.dumps({'type': 'generating_pdf'})}\n\n"

                pdf_result_queue = queue_module.Queue()

                def _generate_pdf():
                    try:
                        saved_articles = _load_session_articles(session_dir)
                        article_objects = list(saved_articles.values())
                        pdf_path = generate_combined_pdf(article_objects)
                        pdf_result_queue.put(("success", pdf_path))
                    except Exception as exc:
                        pdf_result_queue.put(("error", str(exc)))

                pdf_thread = threading.Thread(target=_generate_pdf, daemon=True)
                pdf_thread.start()

                # Send keepalive while waiting for PDF generation.
                # No timeout — WeasyPrint fetches all remote images sequentially
                # during write_pdf(), which can legitimately take 20+ minutes
                # for 600 articles with images.
                while True:
                    try:
                        pdf_result = pdf_result_queue.get(timeout=10)
                        break
                    except queue_module.Empty:
                        yield ": keepalive\n\n"

                if pdf_result[0] == "success":
                    pdf_path = pdf_result[1]

                    results = [
                        {
                            "url": url,
                            "title": processed_titles.get(url, ""),
                            "status": "success",
                        }
                        for url in processed_urls
                    ]

                    final_result = {
                        "type": "complete",
                        "success": True,
                        "filename": pdf_path.name,
                        "articles": results,
                        "errors": errors if errors else None,
                        "summary": {
                            "total": total,
                            "succeeded": len(processed_urls),
                            "failed": len(errors),
                        },
                    }
                    _update_session_status(session_dir, "completed")
                    _cleanup_session(session_dir)
                    yield f"data: {json_module.dumps(final_result)}\n\n"
                else:
                    _update_session_status(session_dir, "pdf_failed", error=pdf_result[1])
                    yield f"data: {json_module.dumps({'type': 'error', 'error': f'PDF generation failed: {pdf_result[1]}'})}\n\n"

            except GeneratorExit:
                _update_session_status(session_dir, "interrupted_during_pdf")
                log.warning("convert_stream_disconnected_during_pdf")
                return
            except Exception as e:
                yield f"data: {json_module.dumps({'type': 'error', 'error': f'PDF generation failed: {str(e)}'})}\n\n"
        else:
            _update_session_status(session_dir, "all_failed")
            error_details = [{"url": e["url"], "error": e["error"]} for e in errors]
            yield f"data: {json_module.dumps({'type': 'error', 'error': 'All conversions failed', 'details': error_details})}\n\n"

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


@api_bp.route("/bookmarks/fetch", methods=["POST"])
def bookmarks_fetch():
    """POST /api/bookmarks/fetch - Scrape bookmarks with streaming progress.

    Expects cookies in request body. Returns SSE stream of bookmark entries.

    Uses a thread-safe queue so bookmark entries are streamed to the client
    as they are discovered (instead of waiting for the full scrape to finish,
    which can exceed the run_async timeout for large bookmark lists).
    """
    import queue
    import threading

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

    # Skip preflight API check — Twitter v1.1 API is deprecated (returns 404).
    # Authentication is verified by the Playwright scraper itself when it loads
    # x.com/home and checks for tweet elements.
    log.info("bookmark_fetch_starting", cookies_present=bool(cookies))

    from ..sources.bookmarks import BookmarkScraper

    scraper = BookmarkScraper(cookies=cookies)

    # Thread-safe queue for streaming bookmarks from the async scraper
    # to the SSE generator.
    bookmark_queue: queue.Queue = queue.Queue()

    def _on_bookmark(entry, total):
        """Callback invoked by the scraper for each new bookmark."""
        bookmark_queue.put(("bookmark", entry, total))

    def _run_scrape():
        """Run the scraper in a background thread so the SSE generator can stream."""
        try:
            # Use a long timeout — scraping 500+ bookmarks with API interception
            bookmarks = run_async(scraper.scrape(on_bookmark=_on_bookmark), timeout=1800)
            bookmark_queue.put(("complete", len(bookmarks), None))
        except TimeoutError:
            log.error("bookmark_fetch_timeout")
            bookmark_queue.put(("error", "Bookmark scrape timed out (30 min limit)", None))
        except Exception as e:
            log.error("bookmark_fetch_failed", error=str(e))
            bookmark_queue.put(("error", str(e) or type(e).__name__, None))

    def generate():
        """Generator function for SSE stream."""
        yield f"data: {json_module.dumps({'type': 'start'})}\n\n"

        # Start scraping in a background thread
        thread = threading.Thread(target=_run_scrape, daemon=True)
        thread.start()

        count = 0
        idle_seconds = 0
        while True:
            try:
                msg = bookmark_queue.get(timeout=10)
                idle_seconds = 0  # Reset on any message
            except queue.Empty:
                idle_seconds += 10
                if idle_seconds >= 300:
                    yield f"data: {json_module.dumps({'type': 'error', 'error': 'Scrape timed out (no activity for 5 minutes)'})}\n\n"
                    break
                yield ": keepalive\n\n"
                continue

            kind = msg[0]
            if kind == "bookmark":
                entry, total = msg[1], msg[2]
                count += 1
                yield f"data: {json_module.dumps({'type': 'bookmark', 'count': count, 'entry': entry.to_dict()})}\n\n"
            elif kind == "complete":
                total = msg[1]
                yield f"data: {json_module.dumps({'type': 'complete', 'total': total})}\n\n"
                break
            elif kind == "error":
                error_msg = msg[1]
                yield f"data: {json_module.dumps({'type': 'error', 'error': error_msg})}\n\n"
                break

    return Response(generate(), mimetype="text/event-stream")


@api_bp.route("/bookmarks/convert", methods=["POST"])
def bookmarks_convert():
    """POST /api/bookmarks/convert - Convert selected bookmark URLs to PDF.

    Expects cookies and urls list in request body. Returns SSE stream.
    Uses resilient retry/backoff pattern for reliable batch processing.
    """
    import queue as queue_module
    import random
    import threading

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

    # Session support for reconnection
    if request.is_json:
        session_id = (request.get_json() or {}).get("session_id") or str(uuid.uuid4())
    else:
        session_id = str(uuid.uuid4())
    session_dir = _get_session_dir(session_id)

    # Build sources for all URLs
    sources_for_urls = []
    for url in urls:
        source = get_source_for_url(url, cookies=cookies)
        sources_for_urls.append((url, source))

    def generate():
        """Generator function for SSE stream with resilient retry/backoff."""
        # Only track URLs and titles — don't keep full article content in memory
        saved = _load_session_articles(session_dir)
        processed_urls = set(saved.keys())
        processed_titles = {u: a.title for u, a in saved.items()}
        del saved
        errors = []
        total = len(sources_for_urls)
        consecutive_failures = 0

        _save_session_meta(session_dir, urls, status="running")

        try:
            yield f"data: {json_module.dumps({'type': 'start', 'total': total, 'session_id': session_id, 'already_done': len(processed_urls)})}\n\n"

            for i, (url, source) in enumerate(sources_for_urls, 1):
                if url in processed_urls:
                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'success', 'title': processed_titles.get(url, ''), 'resumed': True})}\n\n"
                    continue

                if i > 1:
                    is_twitter = source is not None and isinstance(source, TwitterPlaywrightSource)
                    delay = ARTICLE_BASE_DELAY + random.uniform(0, 2)
                    if is_twitter:
                        delay += 3
                    if consecutive_failures > 0:
                        delay += min(consecutive_failures * 10, 120)

                    yield f"data: {json_module.dumps({'type': 'waiting', 'current': i, 'total': total, 'seconds': round(delay)})}\n\n"
                    yield from _sleep_with_keepalive(delay)

                yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'processing'})}\n\n"

                if source is None:
                    errors.append({"url": url, "error": "Unsupported URL"})
                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': 'Unsupported URL'})}\n\n"
                    continue

                succeeded = False
                last_error = None

                for attempt in range(ARTICLE_MAX_RETRIES + 1):
                    try:
                        log.info(
                            "processing_bookmark_url",
                            url=url,
                            progress=f"{i}/{total}",
                            attempt=attempt + 1,
                        )

                        fetch_q = queue_module.Queue()

                        def _do_fetch(src=source, u=url):
                            try:
                                a = run_async(src.fetch(u))
                                fetch_q.put(("ok", a))
                            except Exception as exc:
                                fetch_q.put(("err", exc))

                        threading.Thread(target=_do_fetch, daemon=True).start()

                        fetch_start = time.time()
                        while True:
                            try:
                                fetch_result = fetch_q.get(timeout=10)
                                break
                            except queue_module.Empty:
                                if time.time() - fetch_start > FETCH_TIMEOUT:
                                    raise TimeoutError(f"Fetch timed out after {FETCH_TIMEOUT}s")
                                yield ": keepalive\n\n"

                        if fetch_result[0] == "err":
                            raise fetch_result[1]
                        article = fetch_result[1]

                        _save_article(session_dir, url, article)
                        processed_urls.add(url)
                        processed_titles[url] = article.title

                        yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'success', 'title': article.title})}\n\n"

                        succeeded = True
                        consecutive_failures = 0
                        break

                    except Exception as e:
                        last_error = str(e)
                        log.warning(
                            "bookmark_url_attempt_failed",
                            url=url,
                            attempt=attempt + 1,
                            max_attempts=ARTICLE_MAX_RETRIES + 1,
                            error=last_error,
                        )

                        if attempt < ARTICLE_MAX_RETRIES:
                            retry_delay = ARTICLE_RETRY_DELAYS[attempt] + random.uniform(0, 5)
                            yield f"data: {json_module.dumps({'type': 'retry', 'current': i, 'total': total, 'url': url, 'attempt': attempt + 2, 'max_attempts': ARTICLE_MAX_RETRIES + 1, 'wait_seconds': round(retry_delay)})}\n\n"
                            yield from _sleep_with_keepalive(retry_delay)

                if not succeeded:
                    consecutive_failures += 1
                    errors.append({"url": url, "error": last_error})
                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': last_error})}\n\n"

        except GeneratorExit:
            _update_session_status(
                session_dir, "interrupted",
                processed=len(processed_urls), errors=len(errors),
            )
            log.warning(
                "bookmark_convert_client_disconnected",
                completed=len(processed_urls),
                remaining=total - len(processed_urls) - len(errors),
            )
            return
        except Exception as e:
            _update_session_status(session_dir, "error", error=str(e))
            log.error("bookmark_convert_fatal_error", error=str(e), completed=len(processed_urls))
            try:
                yield f"data: {json_module.dumps({'type': 'error', 'error': f'Server error: {str(e)}'})}\n\n"
            except GeneratorExit:
                return

        if processed_urls:
            try:
                yield f"data: {json_module.dumps({'type': 'generating_pdf'})}\n\n"

                pdf_result_queue = queue_module.Queue()

                def _generate_pdf():
                    try:
                        saved_articles = _load_session_articles(session_dir)
                        article_objects = list(saved_articles.values())
                        pdf_path = generate_combined_pdf(article_objects)
                        pdf_result_queue.put(("success", pdf_path))
                    except Exception as exc:
                        pdf_result_queue.put(("error", str(exc)))

                pdf_thread = threading.Thread(target=_generate_pdf, daemon=True)
                pdf_thread.start()

                while True:
                    try:
                        pdf_result = pdf_result_queue.get(timeout=10)
                        break
                    except queue_module.Empty:
                        yield ": keepalive\n\n"

                if pdf_result[0] == "success":
                    pdf_path = pdf_result[1]

                    results = [
                        {
                            "url": url,
                            "title": processed_titles.get(url, ""),
                            "status": "success",
                        }
                        for url in processed_urls
                    ]

                    final_result = {
                        "type": "complete",
                        "success": True,
                        "filename": pdf_path.name,
                        "articles": results,
                        "errors": errors if errors else None,
                        "summary": {
                            "total": total,
                            "succeeded": len(processed_urls),
                            "failed": len(errors),
                        },
                    }
                    _update_session_status(session_dir, "completed")
                    _cleanup_session(session_dir)
                    yield f"data: {json_module.dumps(final_result)}\n\n"
                else:
                    _update_session_status(session_dir, "pdf_failed", error=pdf_result[1])
                    yield f"data: {json_module.dumps({'type': 'error', 'error': f'PDF generation failed: {pdf_result[1]}'})}\n\n"

            except GeneratorExit:
                _update_session_status(session_dir, "interrupted_during_pdf")
                log.warning("bookmark_convert_disconnected_during_pdf")
                return
            except Exception as e:
                yield f"data: {json_module.dumps({'type': 'error', 'error': f'PDF generation failed: {str(e)}'})}\n\n"
        else:
            _update_session_status(session_dir, "all_failed")
            error_details = [{"url": e["url"], "error": e["error"]} for e in errors]
            yield f"data: {json_module.dumps({'type': 'error', 'error': 'All conversions failed', 'details': error_details})}\n\n"

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


@api_bp.route("/videos/download", methods=["POST"])
def videos_download():
    """POST /api/videos/download - Download videos from Twitter/X links.

    Expects links in request body. Cookies are optional (help with private tweets).
    Returns SSE stream with progress.
    """
    if request.is_json:
        data = request.get_json() or {}
        links = data.get("links", [])
    else:
        links_text = request.form.get("links", "")
        links = [line.strip() for line in links_text.split("\n") if line.strip()]

    if not links:
        return jsonify({"error": "No links provided"}), 400

    cookies = _get_cookies_from_request()

    # Validate all URLs are Twitter/X status URLs
    invalid_urls = [url for url in links if not TWITTER_URL_PATTERN.match(url)]
    if invalid_urls:
        return (
            jsonify(
                {
                    "error": f"Invalid Twitter/X URLs: {', '.join(invalid_urls)}. "
                    "Only tweet URLs (x.com/user/status/ID) are supported."
                }
            ),
            400,
        )

    config = get_config()
    video_dir = config.output_dir / "videos"

    # Resilient download settings — prioritize completion over speed
    VIDEO_BASE_DELAY = 3  # seconds between successful downloads
    VIDEO_MAX_RETRIES = 3  # retries per video before giving up
    VIDEO_RETRY_DELAYS = [30, 60, 120]  # escalating retry waits (seconds)

    def vid_generate():
        """Generator function for SSE stream."""
        import random

        from ..sources.video_downloader import download_video

        videos = []
        errors = []
        total = len(links)
        consecutive_failures = 0

        try:
            yield f"data: {json_module.dumps({'type': 'start', 'total': total})}\n\n"

            for i, url in enumerate(links, 1):
                if i > 1:
                    # Adaptive delay: slow down more after failures
                    delay = VIDEO_BASE_DELAY + random.uniform(0, 5)
                    if consecutive_failures > 0:
                        delay += consecutive_failures * 15
                    log.info("video_throttle_delay", delay=round(delay, 1), item=f"{i}/{total}")

                    yield f"data: {json_module.dumps({'type': 'waiting', 'current': i, 'total': total, 'seconds': round(delay)})}\n\n"
                    yield from _sleep_with_keepalive(delay)

                yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'processing'})}\n\n"

                succeeded = False
                last_error = None

                for attempt in range(VIDEO_MAX_RETRIES + 1):
                    try:
                        log.info(
                            "downloading_video_stream",
                            url=url,
                            progress=f"{i}/{total}",
                            attempt=attempt + 1,
                        )
                        video_path = download_video(url, video_dir, cookies=cookies)
                        filename = video_path.name
                        size_bytes = video_path.stat().st_size

                        videos.append({"url": url, "filename": filename, "size_bytes": size_bytes})

                        yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'success', 'filename': filename})}\n\n"

                        succeeded = True
                        consecutive_failures = 0
                        break

                    except Exception as e:
                        last_error = str(e)
                        log.warning(
                            "video_download_attempt_failed",
                            url=url,
                            attempt=attempt + 1,
                            max_attempts=VIDEO_MAX_RETRIES + 1,
                            error=last_error,
                        )

                        if attempt < VIDEO_MAX_RETRIES:
                            retry_delay = VIDEO_RETRY_DELAYS[attempt] + random.uniform(0, 10)
                            log.info("video_retry_wait", seconds=round(retry_delay, 1))

                            yield f"data: {json_module.dumps({'type': 'retry', 'current': i, 'total': total, 'url': url, 'attempt': attempt + 2, 'max_attempts': VIDEO_MAX_RETRIES + 1, 'wait_seconds': round(retry_delay)})}\n\n"
                            yield from _sleep_with_keepalive(retry_delay)

                if not succeeded:
                    consecutive_failures += 1
                    log.error(
                        "video_download_failed_all_retries",
                        url=url,
                        error=last_error,
                        retries=VIDEO_MAX_RETRIES,
                    )
                    errors.append({"url": url, "error": last_error})

                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': last_error})}\n\n"

        except GeneratorExit:
            log.warning(
                "video_stream_client_disconnected",
                completed=len(videos),
                remaining=total - len(videos) - len(errors),
            )
            return
        except Exception as e:
            log.error("video_stream_fatal_error", error=str(e), completed=len(videos))
            try:
                yield f"data: {json_module.dumps({'type': 'error', 'error': f'Server error: {str(e)}'})}\n\n"
            except GeneratorExit:
                return

        final_result = {
            "type": "complete",
            "videos": videos,
            "errors": errors if errors else None,
            "summary": {
                "total": total,
                "succeeded": len(videos),
                "failed": len(errors),
            },
        }
        yield f"data: {json_module.dumps(final_result)}\n\n"

    response = Response(vid_generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


# --- Session Management Endpoints ---


@api_bp.route("/sessions", methods=["GET"])
def list_sessions():
    """GET /api/sessions - List all sessions with progress info."""
    config = get_config()
    sessions_dir = config.output_dir / "sessions"

    if not sessions_dir.exists():
        return jsonify({"sessions": []})

    sessions = []
    for session_dir in sorted(sessions_dir.iterdir(), reverse=True):
        if not session_dir.is_dir():
            continue
        meta = _load_session_meta(session_dir)
        saved = _count_session_articles(session_dir)
        sessions.append({
            "id": session_dir.name,
            "total": meta.get("total", 0) if meta else 0,
            "saved": saved,
            "status": meta.get("status", "unknown") if meta else "unknown",
            "created_at": meta.get("created_at") if meta else None,
            "updated_at": meta.get("updated_at") if meta else None,
        })

    return jsonify({"sessions": sessions})


@api_bp.route("/sessions/<session_id>", methods=["GET"])
def get_session(session_id):
    """GET /api/sessions/<id> - Get session details including saved/remaining URLs."""
    config = get_config()
    session_dir = config.output_dir / "sessions" / session_id

    if not session_dir.exists() or not session_dir.is_dir():
        return jsonify({"error": "Session not found"}), 404

    meta = _load_session_meta(session_dir)
    saved_articles = _load_session_articles(session_dir)
    saved_urls = set(saved_articles.keys())

    all_urls = meta.get("urls", []) if meta else []
    remaining_urls = [u for u in all_urls if u not in saved_urls]

    return jsonify({
        "id": session_id,
        "total": meta.get("total", 0) if meta else len(saved_urls),
        "saved": len(saved_urls),
        "status": meta.get("status", "unknown") if meta else "unknown",
        "saved_urls": list(saved_urls),
        "remaining_urls": remaining_urls,
        "created_at": meta.get("created_at") if meta else None,
        "updated_at": meta.get("updated_at") if meta else None,
    })


@api_bp.route("/sessions/<session_id>/pdf", methods=["POST"])
def session_pdf(session_id):
    """POST /api/sessions/<id>/pdf - Generate PDF from existing session articles."""
    config = get_config()
    session_dir = config.output_dir / "sessions" / session_id

    if not session_dir.exists() or not session_dir.is_dir():
        return jsonify({"error": "Session not found"}), 404

    saved_articles = _load_session_articles(session_dir)
    if not saved_articles:
        return jsonify({"error": "No articles saved in this session"}), 400

    try:
        article_objects = list(saved_articles.values())
        pdf_path = generate_combined_pdf(article_objects)

        _update_session_status(session_dir, "completed")

        return jsonify({
            "success": True,
            "filename": pdf_path.name,
            "article_count": len(article_objects),
        })
    except Exception as e:
        log.error("session_pdf_failed", session_id=session_id, error=str(e))
        return jsonify({"error": f"PDF generation failed: {str(e)}"}), 500


@api_bp.route("/sessions/<session_id>/resume", methods=["POST"])
def resume_session(session_id):
    """POST /api/sessions/<id>/resume - Resume processing a session.

    Loads the original URL list from session metadata, skips already-fetched
    articles, and continues processing the remaining URLs as an SSE stream.
    Requires cookies in request body.
    """
    import queue as queue_module
    import random
    import threading

    config = get_config()
    session_dir = config.output_dir / "sessions" / session_id

    if not session_dir.exists() or not session_dir.is_dir():
        return jsonify({"error": "Session not found"}), 404

    meta = _load_session_meta(session_dir)
    if not meta or not meta.get("urls"):
        return jsonify({"error": "Session has no metadata (cannot determine URL list)"}), 404

    run_async = _get_run_async()
    cookies = _get_cookies_from_request()
    urls = meta["urls"]

    sources_for_urls = []
    for url in urls:
        source = get_source_for_url(url, cookies=cookies)
        sources_for_urls.append((url, source))

    def generate():
        saved = _load_session_articles(session_dir)
        processed_urls = set(saved.keys())
        processed_titles = {u: a.title for u, a in saved.items()}
        del saved
        errors = []
        total = len(sources_for_urls)
        consecutive_failures = 0

        _update_session_status(session_dir, "running")

        try:
            yield f"data: {json_module.dumps({'type': 'start', 'total': total, 'session_id': session_id, 'already_done': len(processed_urls)})}\n\n"

            for i, (url, source) in enumerate(sources_for_urls, 1):
                if url in processed_urls:
                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'success', 'title': processed_titles.get(url, ''), 'resumed': True})}\n\n"
                    continue

                if i > 1:
                    is_twitter = source is not None and isinstance(source, TwitterPlaywrightSource)
                    delay = ARTICLE_BASE_DELAY + random.uniform(0, 2)
                    if is_twitter:
                        delay += 3
                    if consecutive_failures > 0:
                        delay += min(consecutive_failures * 10, 120)

                    yield f"data: {json_module.dumps({'type': 'waiting', 'current': i, 'total': total, 'seconds': round(delay)})}\n\n"
                    yield from _sleep_with_keepalive(delay)

                yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'processing'})}\n\n"

                if source is None:
                    errors.append({"url": url, "error": "Unsupported URL"})
                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': 'Unsupported URL'})}\n\n"
                    continue

                succeeded = False
                last_error = None

                for attempt in range(ARTICLE_MAX_RETRIES + 1):
                    try:
                        fetch_q = queue_module.Queue()

                        def _do_fetch(src=source, u=url):
                            try:
                                a = run_async(src.fetch(u))
                                fetch_q.put(("ok", a))
                            except Exception as exc:
                                fetch_q.put(("err", exc))

                        threading.Thread(target=_do_fetch, daemon=True).start()

                        fetch_start = time.time()
                        while True:
                            try:
                                fetch_result = fetch_q.get(timeout=10)
                                break
                            except queue_module.Empty:
                                if time.time() - fetch_start > FETCH_TIMEOUT:
                                    raise TimeoutError(f"Fetch timed out after {FETCH_TIMEOUT}s")
                                yield ": keepalive\n\n"

                        if fetch_result[0] == "err":
                            raise fetch_result[1]
                        article = fetch_result[1]

                        _save_article(session_dir, url, article)
                        processed_urls.add(url)
                        processed_titles[url] = article.title

                        yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'success', 'title': article.title})}\n\n"

                        succeeded = True
                        consecutive_failures = 0
                        break

                    except Exception as e:
                        last_error = str(e)
                        if attempt < ARTICLE_MAX_RETRIES:
                            retry_delay = ARTICLE_RETRY_DELAYS[attempt] + random.uniform(0, 5)
                            yield f"data: {json_module.dumps({'type': 'retry', 'current': i, 'total': total, 'url': url, 'attempt': attempt + 2, 'max_attempts': ARTICLE_MAX_RETRIES + 1, 'wait_seconds': round(retry_delay)})}\n\n"
                            yield from _sleep_with_keepalive(retry_delay)

                if not succeeded:
                    consecutive_failures += 1
                    errors.append({"url": url, "error": last_error})
                    yield f"data: {json_module.dumps({'type': 'progress', 'current': i, 'total': total, 'url': url, 'status': 'failed', 'error': last_error})}\n\n"

        except GeneratorExit:
            _update_session_status(
                session_dir, "interrupted",
                processed=len(processed_urls), errors=len(errors),
            )
            return
        except Exception as e:
            _update_session_status(session_dir, "error", error=str(e))
            try:
                yield f"data: {json_module.dumps({'type': 'error', 'error': f'Server error: {str(e)}'})}\n\n"
            except GeneratorExit:
                return

        if processed_urls:
            try:
                yield f"data: {json_module.dumps({'type': 'generating_pdf'})}\n\n"

                pdf_result_queue = queue_module.Queue()

                def _generate_pdf():
                    try:
                        saved_articles = _load_session_articles(session_dir)
                        article_objects = list(saved_articles.values())
                        pdf_path = generate_combined_pdf(article_objects)
                        pdf_result_queue.put(("success", pdf_path))
                    except Exception as exc:
                        pdf_result_queue.put(("error", str(exc)))

                threading.Thread(target=_generate_pdf, daemon=True).start()

                while True:
                    try:
                        pdf_result = pdf_result_queue.get(timeout=10)
                        break
                    except queue_module.Empty:
                        yield ": keepalive\n\n"

                if pdf_result[0] == "success":
                    pdf_path = pdf_result[1]
                    results = [
                        {"url": url, "title": processed_titles.get(url, ""), "status": "success"}
                        for url in processed_urls
                    ]
                    final_result = {
                        "type": "complete",
                        "success": True,
                        "filename": pdf_path.name,
                        "articles": results,
                        "errors": errors if errors else None,
                        "summary": {
                            "total": total,
                            "succeeded": len(processed_urls),
                            "failed": len(errors),
                        },
                    }
                    _update_session_status(session_dir, "completed")
                    _cleanup_session(session_dir)
                    yield f"data: {json_module.dumps(final_result)}\n\n"
                else:
                    _update_session_status(session_dir, "pdf_failed", error=pdf_result[1])
                    yield f"data: {json_module.dumps({'type': 'error', 'error': f'PDF generation failed: {pdf_result[1]}'})}\n\n"

            except GeneratorExit:
                _update_session_status(session_dir, "interrupted_during_pdf")
                return
            except Exception as e:
                yield f"data: {json_module.dumps({'type': 'error', 'error': f'PDF generation failed: {str(e)}'})}\n\n"
        else:
            _update_session_status(session_dir, "all_failed")
            error_details = [{"url": e["url"], "error": e["error"]} for e in errors]
            yield f"data: {json_module.dumps({'type': 'error', 'error': 'All conversions failed', 'details': error_details})}\n\n"

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response
