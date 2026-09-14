"""13ft retrieval core, adapted from wasi-master/13ft (MIT).

Upstream commit: d7505d639ae5b79b2361831d5526fc8e4b0c206e.
See LICENSE-13ft. UI, background jobs and shared cache are omitted.
Network calls use our bounded public-address transport. Progress is local to
this request; article HTML is converted to text blocks before display.
"""

import re
from urllib.parse import quote, urljoin, urlparse
from bs4 import BeautifulSoup
from . import ladder_http as requests
from .ladder_browser import fetch_via_browser


def set_step(job_id, step):
    """Compatibility hook; the app displays one loading state."""


_DEFAULT_STRINGS = {
    "heading": "Enter Website Link",
    "label": "Link of the website you want to remove paywall for:",
    "submit": "Submit",
    "toggle_dark_mode": "Toggle Dark Mode",
    "status_heading": "Fetching Article",
    "error_title": "Something went wrong",
    "retry_button": "Try Another URL",
    "elapsed_template": "{seconds}s elapsed",
    "connection_lost_error": "Connection to server lost. The page may be taking too long to load.",
    "step_connect": "Connecting to website...",
    "step_fetch": "Downloading page content...",
    "step_detect": "Checking for anti-bot challenges...",
    "step_fallback_freedium": "Trying Freedium (Medium bypass)...",
    "step_fallback_org": "Trying archive.org snapshot...",
    "step_fallback_ph": "Trying archive.today / archive.ph snapshot...",
    "step_process": "Processing article...",
    "step_cleanup": "Cleaning up & preparing view...",
    "step_done": "Done!",
    "invalid_url": "Invalid URL",
    "medium_challenge_error": (
        "Medium served an anti-bot challenge and Freedium / archive.org / "
        "archive.today all came back empty. The article may be too new to "
        "have been archived yet."
    ),
    "challenge_error": (
        "The site served an anti-bot challenge (Cloudflare or similar) "
        "the browser retry could not retrieve the article, and no usable archive snapshot was found. "
        "This site is actively blocking automated requests."
    ),
    "timeout_error": "The website took too long to respond (30s timeout). Try again later.",
    "connection_error": "Could not connect to the website. Check the URL and try again.",
    "request_error": "Failed to fetch the page.",
    "unexpected_error": "Unexpected error while fetching the page.",
}


googlebot_headers = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.119 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
}


class UserFacingError(Exception):
    def __init__(self, user_message):
        super().__init__(user_message)
        self.user_message = user_message


def process_html_document(html_content, original_url):
    """
    Parses HTML, injects the <base> tag to fix relative links, and applies
    site-specific modifications (like stripping paywall JS) where needed.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    parsed_url = urlparse(original_url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}/"

    # Handle paths that are not root, e.g., "https://x.com/some/path/w.html"
    if parsed_url.path and not parsed_url.path.endswith("/"):
        base_url = urljoin(base_url, parsed_url.path.rsplit("/", 1)[0] + "/")

    base_tag = soup.find("base")
    if base_tag:
        # Always overwrite — sites may have <base href="/"> which would resolve
        # root-relative asset paths to the 13ft server instead of the original domain.
        base_tag["href"] = base_url
    else:
        new_base_tag = soup.new_tag("base", href=base_url)
        if soup.head:
            soup.head.insert(0, new_base_tag)
        else:
            head_tag = soup.new_tag("head")
            head_tag.insert(0, new_base_tag)
            soup.insert(0, head_tag)

    domain = parsed_url.netloc.lower()

    # --- GENERAL FIXES ---

    # Strip anti-hotlinking, anti-embedding, and frame-busting scripts.
    for script in soup.find_all("script", src=False):
        content = script.get_text()

        # Target the specific hostname check
        if "location.hostname" in content and (
            "location.href" in content or "location.replace" in content
        ):
            script.decompose()

        # Optional: Catch common frame-busting scripts if you are viewing this via an iframe
        elif "top.location" in content or "window.top" in content:
            script.decompose()

    # --- SITE SPECIFIC FIXES ---

    # The Seattle Times
    if "seattletimes.com" in domain:
        # Seattle Times uses specific JS bundles to trigger the paywall modal and ads.
        # Removing these scripts prevents the paywall from locking the screen.
        for script in soup.find_all("script", src=True):
            if "st-user-messaging" in script["src"] or "st-advertising" in script["src"]:
                script.decompose()

        # Ensure the body isn't locked from scrolling by JS-injected inline styles
        if soup.body:
            current_style = soup.body.get("style", "")
            soup.body["style"] = (
                f"{current_style}; overflow: auto !important; position: static !important;"
            )

    # --- FAVICON OVERRIDE ---
    # Remove any existing favicon links from the source page and inject the
    # 13ft favicon so the browser tab always shows the 13ft logo.
    for link_tag in soup.find_all(
        "link",
        rel=lambda r: (
            r
            and any(
                v.lower()
                in (
                    "icon",
                    "shortcut icon",
                    "apple-touch-icon",
                    "apple-touch-icon-precomposed",
                    "mask-icon",
                )
                for v in (r if isinstance(r, list) else [r])
            )
        ),
    ):
        link_tag.decompose()

    favicon_tag = soup.new_tag("link", rel="icon", href="/favicon.ico", type="image/png")
    if soup.head:
        soup.head.append(favicon_tag)

    return str(soup)


CHALLENGE_SIGNATURES = [
    "just a moment",
    "checking your browser",
    "cloudflare",
    "cf-challenge",
    "ddos protection",
    "attention required",
    "enable javascript and cookies to continue",
    "please verify you are a human",
    # Imperva / Incapsula block pages (e.g. bizjournals.com). These are also
    # captured by archive.org, so an archived snapshot can silently be a block
    # page rather than the article — detect it and fall through to an error.
    "_incapsula_resource",
    "incapsula incident",
    "request unsuccessful",
    # DataDome block pages (e.g. nytimes.com, ft.com, wsj.com).
    "datadome",
    "enable js and disable any ad blocker",
]


def is_challenge_page(html_text):
    if not html_text:
        return False
    lower = html_text[:8000].lower()
    hits = sum(1 for sig in CHALLENGE_SIGNATURES if sig in lower)
    if hits >= 1 and len(html_text) < 20000:
        return True
    return hits >= 2


REAL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


ARCHIVE_PH_MIRRORS = ["archive.ph", "archive.today", "archive.is", "archive.li"]


FREEDIUM_MIRRORS = ["freedium-mirror.cfd", "freedium.cfd"]


def is_medium_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return host == "medium.com" or host.endswith(".medium.com")
    except Exception:
        return False


def fetch_via_freedium(url, job_id=None):
    """Freedium is a Medium-specific paywall bypass service."""
    set_step(job_id, "fallback_freedium")
    clean_url = url.split("?")[0]
    for mirror in FREEDIUM_MIRRORS:
        try:
            freedium_url = f"https://{mirror}/{clean_url}"
            resp = requests.get(
                freedium_url,
                headers=REAL_BROWSER_HEADERS,
                timeout=30,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                continue
            resp.encoding = resp.apparent_encoding
            text = resp.text
            if is_challenge_page(text):
                continue
            if "main-content" not in text or len(text) < 2000:
                continue
            return text, resp.url
        except Exception:
            continue
    return None, None


def fetch_via_archive_org(url, job_id=None):
    set_step(job_id, "fallback_org")
    wayback_api = "https://archive.org/wayback/available"
    try:
        meta = requests.get(wayback_api, params={"url": url}, timeout=15).json()
        snapshot = meta.get("archived_snapshots", {}).get("closest", {})
        if not snapshot.get("available"):
            return None, None
        archived_url = snapshot["url"]
        if archived_url.startswith("http://"):
            archived_url = "https://" + archived_url[len("http://") :]
        if "/web/" in archived_url and "id_/" not in archived_url:
            archived_url = archived_url.replace(
                archived_url.split("/web/")[1].split("/")[0],
                archived_url.split("/web/")[1].split("/")[0] + "id_",
                1,
            )
        resp = requests.get(archived_url, headers=googlebot_headers, timeout=30)
        resp.encoding = resp.apparent_encoding
        return resp.text, resp.url
    except Exception:
        return None, None


def fetch_via_archive_ph(url, job_id=None):
    """Try archive.today / archive.ph / archive.is — all mirrors of the same service."""
    set_step(job_id, "fallback_ph")
    for mirror in ARCHIVE_PH_MIRRORS:
        try:
            newest_url = f"https://{mirror}/newest/{quote(url, safe=':/')}"
            resp = requests.get(
                newest_url,
                headers=REAL_BROWSER_HEADERS,
                timeout=20,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                continue

            final = resp.url
            if not re.search(r"https?://archive\.(ph|today|is|li)/[A-Za-z0-9]{4,}", final):
                continue
            if re.search(r"archive\.(ph|today|is|li)/newest/", final):
                continue

            text = resp.text
            if is_challenge_page(text):
                continue

            lower_head = text[:5000].lower()
            if "no results" in lower_head or "0 captures" in lower_head:
                continue

            return text, final
        except Exception:
            continue
    return None, None


def bypass_paywall(url, strings, job_id=None):
    set_step(job_id, "connect")

    if not url.startswith("http"):
        url = "https://" + url

    medium = is_medium_url(url)
    html_text = ""
    final_url = url

    if medium:
        freedium_html, freedium_final = fetch_via_freedium(url, job_id)
        if freedium_html:
            set_step(job_id, "process")
            result = process_html_document(freedium_html, freedium_final)
            set_step(job_id, "cleanup")
            return result
    else:
        set_step(job_id, "fetch")
        try:
            response = requests.get(url, headers=googlebot_headers, timeout=30)
            response.encoding = response.apparent_encoding
            html_text = response.text
            final_url = response.url

            # Treat error status codes as a challenge so the archive fallbacks trigger.
            # Sites like News24 return 403 with their homepage HTML (which contains a
            # JS redirect back to the homepage), causing the browser to redirect away.
            if response.status_code in (401, 403, 407, 429) or response.status_code >= 500:
                html_text = ""
        except (OSError, TimeoutError):
            html_text = ""

    set_step(job_id, "detect")
    if not html_text or is_challenge_page(html_text):
        recovered = False

        browser_html, browser_url = fetch_via_browser(url, job_id)
        if browser_html and not is_challenge_page(browser_html):
            html_text = browser_html
            final_url = browser_url
            recovered = True

        if not recovered:
            archived_html, archived_url = fetch_via_archive_org(url, job_id)
            if archived_html and not is_challenge_page(archived_html):
                html_text = archived_html
                final_url = archived_url
                recovered = True

        if not recovered:
            archived_html, archived_url = fetch_via_archive_ph(url, job_id)
            if archived_html and not is_challenge_page(archived_html):
                html_text = archived_html
                final_url = archived_url
                recovered = True

        if not recovered:
            if medium:
                msg = strings["medium_challenge_error"]
            else:
                msg = strings["challenge_error"]
            raise UserFacingError(msg)

    set_step(job_id, "process")
    result = process_html_document(html_text, final_url)

    set_step(job_id, "cleanup")
    return result
