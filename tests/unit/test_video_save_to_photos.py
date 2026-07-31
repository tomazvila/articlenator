"""Guards for the iOS "Save to Photos" (Web Share API) flow on the Videos page.

A plain ``<a href="/download/video/...">`` lands a video in the iOS *Files* app,
never in Photos. iOS only routes media into Photos through the native share sheet,
which a web page reaches via the Web Share API (``navigator.share({files:[...]})``).
These tests pin the wiring so it cannot silently regress:

* positive: the page hands a *video* ``File`` to ``navigator.share``.
* gate: the control is offered only when ``navigator.canShare`` confirms support
  (the API is undefined outside a secure context), so HTTP/desktop users are
  unaffected and never see a dead button.
* fallback / negative space: the direct ``/download/video/`` link stays, so
  unsupported devices keep the Files download.
* edge: a dismissed share sheet (``AbortError``) is not treated as a failure.
* security: the inline script keeps its CSP nonce.

The tests parse the template/CSS as text (no browser) so they stay fast and
deterministic; the real end-to-end behaviour is checked separately with Playwright.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VIDEOS_HTML = ROOT / "src" / "twitter_articlenator" / "templates" / "videos.html"
STYLE_CSS = ROOT / "src" / "twitter_articlenator" / "static" / "style.css"


def _script_body(html: str) -> str:
    """All inline ``<script>`` content, so assertions target JS not markup."""
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL))


@pytest.fixture(scope="module")
def videos_html() -> str:
    return VIDEOS_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def videos_js(videos_html) -> str:
    return _script_body(videos_html)


@pytest.fixture(scope="module")
def style_css() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


def test_shares_a_video_file_to_native_sheet(videos_js):
    """Positive space: the page invokes the native share sheet with a video File."""
    assert "navigator.share(" in videos_js, (
        "must invoke the Web Share API to reach the iOS share sheet (the only "
        "web path that routes a video into Photos)"
    )
    assert re.search(r"navigator\.share\(\s*\{[^}]*files", videos_js, re.DOTALL), (
        "navigator.share must be called with a `files` array -- iOS only offers "
        "'Save Video' for shared files, not for text/URLs"
    )
    assert "new File(" in videos_js, "must wrap the downloaded blob in a File object"
    assert "video/mp4" in videos_js, (
        "the shared File must carry a video mimetype so iOS offers 'Save Video'"
    )


def test_button_is_gated_on_canshare(videos_js):
    """Gate: the control is only wired up when the capability probe passes."""
    assert "navigator.canShare" in videos_js, (
        "the Save-to-Photos control must be gated on navigator.canShare, which is "
        "undefined outside a secure context -- otherwise HTTP/desktop users get a "
        "button that throws"
    )


def test_fetches_the_blob_before_sharing(videos_js):
    """The video bytes must be pulled down and read as a Blob to build the File."""
    assert "/download/video/" in videos_js
    assert ".blob()" in videos_js, (
        "must read the fetched response as a Blob to construct the shareable File"
    )


def test_download_link_fallback_retained(videos_js):
    """Negative space: unsupported devices must still get the direct download."""
    assert "/download/video/" in videos_js, (
        "the direct download link must remain so devices without Web Share still "
        "get the file"
    )


def test_share_cancel_is_not_an_error(videos_js):
    """Edge case: dismissing the share sheet raises AbortError -- not a failure."""
    assert "AbortError" in videos_js, (
        "a dismissed share sheet (AbortError) must be handled distinctly, not "
        "surfaced to the user as a failed save"
    )


def test_inline_script_has_csp_nonce(videos_html):
    """Security: the inline script must carry the CSP nonce to run under the policy."""
    assert re.search(
        r'<script[^>]*\bnonce="\{\{\s*csp_nonce\s*\}\}"', videos_html
    ), "inline <script> must carry the csp_nonce attribute"


def test_save_photos_button_is_styled(style_css):
    """The Save-to-Photos control must have a dedicated, themed style rule."""
    assert ".save-photos-btn" in style_css, "expected a .save-photos-btn style rule"
