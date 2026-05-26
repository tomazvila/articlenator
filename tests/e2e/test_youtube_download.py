"""E2E tests for the YouTube download feature."""

import hashlib
import json
import os

import pytest
from playwright.sync_api import Page, expect

from .pages import YouTubePage

PUBLIC_REAL_URL = os.environ.get(
    "TEST_YOUTUBE_PUBLIC_URL", "https://www.youtube.com/watch?v=fv7TlVMETP0"
)
LONG_REAL_URL = os.environ.get(
    "TEST_YOUTUBE_LONG_URL", "https://www.youtube.com/watch?v=tc82YJfvXZo"
)
AUTH_REAL_URL = os.environ.get("TEST_YOUTUBE_AUTH_URL")
AUTH_COOKIES_FILE = os.environ.get("TEST_YOUTUBE_COOKIES_FILE")

SAMPLE_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session-value\n"
)


def clear_fake_log(flask_server):
    """Clear fake downloader command log."""
    flask_server["youtube_fake_log"].write_text("", encoding="utf-8")


def read_fake_calls(flask_server):
    """Read fake downloader command calls."""
    log_path = flask_server["youtube_fake_log"]
    if not log_path.exists():
        return []
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    return [json.loads(line) for line in lines]


class TestYouTubePage:
    """Tests for the YouTube page UI."""

    def test_user_can_access_youtube_page(self, page: Page, base_url):
        """Test user can access the YouTube page."""
        page.goto(f"{base_url}/youtube")
        expect(page).to_have_title("YouTube Downloader")
        expect(page.locator("h1")).to_have_text("YouTube Downloads")

    def test_youtube_page_has_nav_link_without_replacing_videos(self, page: Page, base_url):
        """Test YouTube page is linked from navigation and Videos remains."""
        page.goto(base_url)

        youtube_link = page.locator("a[href='/youtube']")
        videos_link = page.locator("a[href='/videos']")

        expect(youtube_link).to_be_visible()
        expect(youtube_link).to_have_text("YouTube")
        expect(videos_link).to_be_visible()
        expect(videos_link).to_have_text("Videos")

    def test_batch_links_persist_and_clear(self, page: Page, base_url):
        """Test YouTube links persist and can be cleared."""
        youtube = YouTubePage(page)
        youtube.navigate(base_url)

        links = [
            "https://www.youtube.com/watch?v=fv7TlVMETP0",
            "https://youtu.be/tc82YJfvXZo",
            "https://www.youtube.com/shorts/abc123",
        ]
        youtube.enter_links(links)
        page.reload()

        expect(youtube.links_textarea).to_have_value("\n".join(links))

        youtube.click_clear()
        expect(youtube.links_textarea).to_be_empty()
        expect(youtube.results_section).not_to_be_visible()
        expect(youtube.error_div).not_to_be_visible()

    def test_empty_links_show_error_without_api_request(self, page: Page, base_url):
        """Test empty submit is blocked client-side."""
        requests = []
        page.on(
            "request",
            lambda request: requests.append(request.url)
            if "/api/youtube/download" in request.url
            else None,
        )

        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.click_clear()
        youtube.click_download()

        expect(youtube.error_div).to_be_visible(timeout=5000)
        expect(youtube.error_text).to_contain_text("at least one YouTube link")
        assert requests == []

    def test_youtube_cookie_upload_status_delete_and_no_local_storage(self, page: Page, base_url):
        """Test YouTube cookies are stored server-side and never in localStorage."""
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.save_pasted_cookies(SAMPLE_COOKIES)
        expect(youtube.cookie_status).to_contain_text("YouTube session stored", timeout=10000)
        expect(youtube.cookie_count).to_have_text("1")
        expect(youtube.cookies_textarea).to_be_empty()

        stored = page.evaluate("localStorage.getItem('articlenator_youtube_cookies')")
        assert stored is None

        page.reload()
        expect(youtube.cookie_status).to_contain_text("YouTube session stored", timeout=10000)
        expect(youtube.cookie_status).not_to_contain_text("secret-session-value")
        expect(youtube.cookie_message).not_to_contain_text("secret-session-value")

        youtube.delete_cookies()
        expect(youtube.cookie_status).to_contain_text("No YouTube session", timeout=10000)


class TestYouTubeFakeDownloadWorkflow:
    """Deterministic E2E tests using the fake YouTube downloader."""

    def test_download_single_video_with_fake_downloader(self, page: Page, base_url, flask_server):
        """Test YouTube video download workflow."""
        clear_fake_log(flask_server)
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.enter_links(["https://www.youtube.com/watch?v=fv7TlVMETP0"])
        youtube.click_download()

        expect(youtube.status_div).to_be_visible(timeout=10000)
        expect(youtube.results_section).to_be_visible(timeout=30000)

        links = youtube.get_download_links()
        assert len(links) == 1
        assert links[0].startswith("/download/youtube/video/")
        assert links[0].endswith(".mp4")

        response = page.request.get(f"{base_url}{links[0]}")
        assert response.status == 200
        assert response.headers["content-type"].startswith("video/mp4")
        assert len(response.body()) > 100

        calls = read_fake_calls(flask_server)
        assert calls[-1]["mode"] == "video"
        assert "--merge-output-format" in calls[-1]["args"]
        assert "--remux-video" in calls[-1]["args"]

    def test_download_mp3_with_fake_downloader(self, page: Page, base_url, flask_server):
        """Test YouTube MP3-only workflow."""
        clear_fake_log(flask_server)
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.select_mp3()
        youtube.enter_links(["https://youtu.be/tc82YJfvXZo"])
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=30000)

        links = youtube.get_download_links()
        assert len(links) == 1
        assert links[0].startswith("/download/youtube/audio/")
        assert links[0].endswith(".mp3")

        response = page.request.get(f"{base_url}{links[0]}")
        assert response.status == 200
        assert response.headers["content-type"].startswith("audio/mpeg")
        assert len(response.body()) > 100

        calls = read_fake_calls(flask_server)
        assert calls[-1]["mode"] == "mp3"
        assert "-x" in calls[-1]["args"]
        assert "--audio-format" in calls[-1]["args"]
        assert "--audio-quality" not in calls[-1]["args"]

    def test_cookie_file_is_passed_to_fake_downloader(self, page: Page, base_url, flask_server):
        """Test YouTube cookies are passed as a temporary Netscape cookie file."""
        clear_fake_log(flask_server)
        download_payloads = []
        page.on(
            "request",
            lambda request: download_payloads.append(request.post_data or "")
            if "/api/youtube/download" in request.url
            else None,
        )

        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.save_pasted_cookies(SAMPLE_COOKIES)
        expect(youtube.cookie_status).to_contain_text("YouTube session stored", timeout=10000)
        youtube.enter_links(["https://www.youtube.com/watch?v=fv7TlVMETP0"])
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=30000)
        expect(youtube.results_section).not_to_contain_text("secret-session-value")
        expect(youtube.warning_div).not_to_contain_text("secret-session-value")

        calls = read_fake_calls(flask_server)
        args = calls[-1]["args"]
        cookie_info = calls[-1]["cookies"]
        assert calls[-1]["url"] == "https://www.youtube.com/watch?v=fv7TlVMETP0"
        assert args[args.index("--js-runtimes") + 1] == "node"
        assert args.index("--cookies") < args.index("https://www.youtube.com/watch?v=fv7TlVMETP0")
        assert cookie_info["exists"] is True
        assert cookie_info["sha256"] == hashlib.sha256(SAMPLE_COOKIES.encode()).hexdigest()
        assert download_payloads
        assert "secret-session-value" not in download_payloads[-1]
        assert "cookies" not in json.loads(download_payloads[-1])

    def test_stored_cookie_verification_uses_fake_downloader(self, page: Page, base_url, flask_server):
        """Test stored YouTube cookies can be verified without leaking values."""
        clear_fake_log(flask_server)
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.save_pasted_cookies(SAMPLE_COOKIES)
        expect(youtube.cookie_status).to_contain_text("YouTube session stored", timeout=10000)

        youtube.verify_cookies()
        expect(youtube.cookie_message).to_contain_text("downloadable media formats", timeout=10000)
        expect(youtube.cookie_message).not_to_contain_text("secret-session-value")

        calls = read_fake_calls(flask_server)
        assert calls[-1]["mode"] == "verify"
        assert calls[-1]["cookies"]["exists"] is True

    def test_batch_failure_does_not_stop_later_links(self, page: Page, base_url, flask_server):
        """Test one failed YouTube URL does not stop the batch."""
        clear_fake_log(flask_server)
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.enter_links(
            [
                "https://youtu.be/success-one",
                "https://youtu.be/fail-this-one",
                "https://youtu.be/success-two",
            ]
        )
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=30000)
        expect(youtube.warning_div).to_be_visible(timeout=5000)

        links = youtube.get_download_links()
        assert len(links) == 2
        expect(youtube.download_list).to_contain_text("2 downloaded, 1 failed of 3 total")
        expect(youtube.warning_text).to_contain_text("fail-this-one")

    def test_slow_fake_download_completes_without_frozen_ui(self, page: Page, base_url):
        """Test a slow fake podcast-style download keeps the UI processing and completes."""
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.enter_links(["https://youtu.be/slow-podcast"])
        youtube.click_download()

        expect(youtube.status_div).to_be_visible(timeout=10000)
        expect(youtube.status_text).to_contain_text("Downloading", timeout=10000)
        expect(youtube.results_section).to_be_visible(timeout=30000)


@pytest.mark.skipif(
    os.environ.get("RUN_REAL_YOUTUBE_E2E") != "1",
    reason="Set RUN_REAL_YOUTUBE_E2E=1 to run real YouTube E2E",
)
class TestYouTubeRealDownloadWorkflow:
    """Real YouTube E2E tests, enabled explicitly by environment."""

    def test_public_real_video_download(self, page: Page, base_url, output_dir):
        """Test downloading a real public YouTube video."""
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.enter_links([PUBLIC_REAL_URL])
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=300000)
        links = youtube.get_download_links()
        assert any(link.endswith(".mp4") for link in links)
        assert list((output_dir / "youtube" / "videos").glob("*.mp4"))

    def test_public_real_mp3_download(self, page: Page, base_url, output_dir):
        """Test downloading a real public YouTube video as MP3."""
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.select_mp3()
        youtube.enter_links([PUBLIC_REAL_URL])
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=300000)
        links = youtube.get_download_links()
        assert any(link.endswith(".mp3") for link in links)
        assert list((output_dir / "youtube" / "audio").glob("*.mp3"))

    @pytest.mark.skipif(
        not AUTH_REAL_URL or not AUTH_COOKIES_FILE,
        reason="Set TEST_YOUTUBE_AUTH_URL and TEST_YOUTUBE_COOKIES_FILE",
    )
    def test_authenticated_real_video_download(self, page: Page, base_url, output_dir):
        """Test downloading a real YouTube URL with session cookies."""
        cookies = open(AUTH_COOKIES_FILE, encoding="utf-8").read()
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.save_pasted_cookies(cookies)
        expect(youtube.cookie_status).to_contain_text("YouTube session stored", timeout=10000)
        youtube.enter_links([AUTH_REAL_URL])
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=300000)
        assert list((output_dir / "youtube" / "videos").glob("*.mp4"))

    @pytest.mark.skipif(
        os.environ.get("RUN_SLOW_YOUTUBE_E2E") != "1",
        reason="Set RUN_SLOW_YOUTUBE_E2E=1 to run long podcast E2E",
    )
    def test_long_real_podcast_download(self, page: Page, base_url, output_dir):
        """Test downloading a real long-running YouTube podcast."""
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.enter_links([LONG_REAL_URL])
        youtube.click_download()

        expect(youtube.status_div).to_be_visible(timeout=10000)
        expect(youtube.results_section).to_be_visible(timeout=1800000)
        assert list((output_dir / "youtube" / "videos").glob("*.mp4"))
