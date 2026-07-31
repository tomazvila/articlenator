"""Guards for the iOS "Save to Photos" (Web Share API) flow on the YouTube page.

Mirrors the Videos-page guard, plus two YouTube-specific rules:

* Only real *video* files may reach Photos. The page also produces MP3 audio and
  a ZIP "download all" archive; neither is a video, so neither gets the button.
* YouTube files can be large and the whole blob is held in memory while sharing,
  so the button is gated on a byte ceiling; above it the user keeps the plain
  download link.

The tests parse ``youtube.html`` as text (no browser); the real behaviour is
checked separately with Playwright.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
YOUTUBE_HTML = ROOT / "src" / "twitter_articlenator" / "templates" / "youtube.html"


def _script_body(html: str) -> str:
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL))


@pytest.fixture(scope="module")
def youtube_html() -> str:
    return YOUTUBE_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def youtube_js(youtube_html) -> str:
    return _script_body(youtube_html)


def test_shares_a_video_file_to_native_sheet(youtube_js):
    """Positive space: invoke the native share sheet with a video File."""
    assert "navigator.share(" in youtube_js, "must invoke the Web Share API"
    assert re.search(r"navigator\.share\(\s*\{[^}]*files", youtube_js, re.DOTALL), (
        "navigator.share must be called with a `files` array"
    )
    assert "new File(" in youtube_js, "must wrap the downloaded blob in a File"
    assert "video/mp4" in youtube_js, "the shared File must carry a video mimetype"


def test_button_is_gated_on_canshare(youtube_js):
    """Gate: the control is only wired up when the capability probe passes."""
    assert "navigator.canShare" in youtube_js, (
        "the Save-to-Photos control must be gated on navigator.canShare"
    )


def test_only_video_files_get_the_button(youtube_js):
    """MP3 audio and the ZIP archive are not videos -- they must not be sharable.

    The button gating must distinguish video downloads from the rest (mode/folder
    or an .mp4 filename check), so audio rows never render a Photos button.
    """
    assert re.search(r"\.mp4", youtube_js), (
        "button gating should key on an .mp4 video file"
    )
    assert ("mp3" in youtube_js and "video" in youtube_js), (
        "the render must distinguish video items from mp3/audio items"
    )


def test_large_videos_are_size_guarded(youtube_js):
    """A byte ceiling must gate the button so a huge blob is not held in memory."""
    assert re.search(r"size_bytes", youtube_js), (
        "the size guard must consult the item's size_bytes"
    )
    assert re.search(
        r"\b\d[\d_]*\s*\*\s*1024\s*\*\s*1024\b|MAX_PHOTOS_SHARE_BYTES", youtube_js
    ), "expected a megabyte-scale ceiling constant gating the share button"


def test_fetches_the_blob_before_sharing(youtube_js):
    """The bytes must be fetched and read as a Blob to build the File."""
    assert "/download/youtube/" in youtube_js
    assert ".blob()" in youtube_js, "must read the fetched response as a Blob"


def test_download_link_fallback_retained(youtube_js):
    """Negative space: unsupported/oversized cases keep the direct download."""
    assert "/download/youtube/" in youtube_js, (
        "the direct download link must remain for unsupported devices and big files"
    )


def test_share_cancel_is_not_an_error(youtube_js):
    """Edge case: dismissing the share sheet raises AbortError -- not a failure."""
    assert "AbortError" in youtube_js, (
        "a dismissed share sheet (AbortError) must be handled distinctly"
    )


def test_inline_script_has_csp_nonce(youtube_html):
    """Security: the inline script must carry the CSP nonce."""
    assert re.search(
        r'<script[^>]*\bnonce="\{\{\s*csp_nonce\s*\}\}"', youtube_html
    ), "inline <script> must carry the csp_nonce attribute"
