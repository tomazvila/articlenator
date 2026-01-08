"""API routes blueprint."""

import structlog
from flask import Blueprint, jsonify, request, current_app
from ..config import get_config
from ..pdf.generator import generate_combined_pdf
from ..sources import get_source_for_url
from ..sources.twitter_playwright import TwitterPlaywrightSource

log = structlog.get_logger()

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _get_run_async():
    """Get the run_async function from the app context."""
    return current_app.config.get("RUN_ASYNC")


@api_bp.route("/health")
def health():
    """GET /api/health - Health check endpoint."""
    return jsonify({"status": "ok"})


@api_bp.route("/convert", methods=["POST"])
def convert():
    """POST /api/convert - Process links and return PDF paths."""
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

    config = get_config()
    cookies = config.load_cookies()

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

    for url, source in sources_for_urls:
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
            }
            for a in articles
        ]

        log.info("combined_pdf_generated", pdf=pdf_path.name, article_count=len(articles))

        return jsonify(
            {
                "success": True,
                "filename": pdf_path.name,
                "articles": results,
                "errors": errors if errors else None,
            }
        )
    except Exception as e:
        log.error("pdf_generation_failed", error=str(e))
        return jsonify({"error": f"PDF generation failed: {str(e)}"}), 500


@api_bp.route("/cookies/status")
def cookies_status():
    """GET /api/cookies/status - Check cookie status and optionally test them."""
    config = get_config()
    cookies = config.load_cookies()

    if not cookies:
        return jsonify(
            {
                "configured": False,
                "status": "not_configured",
                "message": "No cookies configured",
            }
        )

    # Check if test parameter is set
    test_cookies = request.args.get("test", "").lower() == "true"

    if not test_cookies:
        return jsonify(
            {
                "configured": True,
                "status": "configured",
                "message": "Cookies are configured (not tested)",
            }
        )

    # Validate cookie format (required cookies present with reasonable length)
    cookie_dict = {}
    for part in cookies.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            cookie_dict[name.strip()] = value.strip()

    has_auth_token = "auth_token" in cookie_dict and len(cookie_dict["auth_token"]) > 20
    has_ct0 = "ct0" in cookie_dict and len(cookie_dict["ct0"]) > 20

    if has_auth_token and has_ct0:
        log.info(
            "cookies_validated",
            auth_token_len=len(cookie_dict["auth_token"]),
            ct0_len=len(cookie_dict["ct0"]),
        )
        return jsonify(
            {
                "configured": True,
                "status": "working",
                "message": "Cookies validated (auth_token and ct0 present).",
            }
        )
    else:
        missing = []
        if not has_auth_token:
            missing.append("auth_token (missing or too short)")
        if not has_ct0:
            missing.append("ct0 (missing or too short)")
        log.warning("cookies_invalid", missing=missing)
        return jsonify(
            {
                "configured": True,
                "status": "invalid",
                "message": f"Invalid cookies: {', '.join(missing)}",
            }
        )


@api_bp.route("/cookies/current")
def get_cookies():
    """GET /api/cookies/current - Get current cookies (masked for security)."""
    config = get_config()
    cookies = config.load_cookies()

    if not cookies:
        return jsonify({"configured": False, "cookies": []})

    # Parse cookies and mask values for security
    parsed = []
    for part in cookies.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip()
            # Mask the value, showing first 4 and last 4 chars
            if len(value) > 12:
                masked = value[:4] + "..." + value[-4:]
            elif len(value) > 4:
                masked = value[:2] + "..." + value[-2:]
            else:
                masked = "****"
            parsed.append({"name": name, "value_masked": masked, "length": len(value)})

    return jsonify({"configured": True, "cookies": parsed})


@api_bp.route("/cookies", methods=["POST"])
def save_cookies():
    """POST /api/cookies - Save Twitter cookies."""
    # Handle both JSON and form data
    if request.is_json:
        data = request.get_json() or {}
        cookies = data.get("cookies", "")
    else:
        cookies = request.form.get("cookies", "")

    if not cookies or not cookies.strip():
        return jsonify({"error": "No cookies provided"}), 400

    config = get_config()
    config.save_cookies(cookies.strip())

    log.info("cookies_saved")

    return jsonify({"success": True, "message": "Cookies saved successfully"})
