"""E2E tests for the YouTube download feature."""

import hashlib
import io
import json
import os
import subprocess
import zipfile
from urllib.parse import unquote

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
    "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session-value\n"
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


def read_mp3_tags(path):
    """Read selected MP3 container tags with ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags=title,artist,album,track,purl",
            "-of",
            "default=nw=1:nk=0",
            str(path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    tags = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        tags[key] = value
    return tags


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

    def test_youtube_oauth_unconfigured_state_is_obvious(self, page: Page, base_url):
        """Test liked-video OAuth availability is visible, not hidden in metadata numbers."""
        youtube = YouTubePage(page)
        youtube.navigate(base_url)

        expect(youtube.oauth_state_title).to_have_text(
            "YouTube OAuth is not configured", timeout=10000
        )
        expect(youtube.oauth_state).to_have_attribute("data-state", "problem")
        expect(youtube.oauth_state_detail).to_contain_text("Google OAuth client ID")
        expect(youtube.liked_download_button).to_be_disabled()

    def test_youtube_cookie_upload_status_delete_and_no_local_storage(self, page: Page, base_url):
        """Test YouTube cookies are stored server-side and never in localStorage."""
        youtube = YouTubePage(page)
        youtube.navigate(base_url)

        expect(youtube.cookie_state_title).to_have_text("No YouTube session saved", timeout=10000)
        expect(youtube.cookie_state).to_have_attribute("data-state", "missing")
        expect(youtube.cookie_state_detail).to_contain_text("restricted videos need")

        youtube.save_pasted_cookies(SAMPLE_COOKIES)
        expect(youtube.cookie_status).to_contain_text("YouTube session stored", timeout=10000)
        expect(youtube.cookie_state_title).to_have_text("YouTube cookies are saved")
        expect(youtube.cookie_state).to_have_attribute("data-state", "saved")
        expect(youtube.cookie_state_detail).to_contain_text("encrypted server-side session")
        expect(youtube.cookie_count).to_have_text("1")
        expect(youtube.cookies_textarea).to_be_empty()

        stored = page.evaluate("localStorage.getItem('articlenator_youtube_cookies')")
        assert stored is None

        page.reload()
        expect(youtube.cookie_status).to_contain_text("YouTube session stored", timeout=10000)
        expect(youtube.cookie_state_title).to_have_text("YouTube cookies are saved")
        expect(youtube.cookie_state).to_have_attribute("data-state", "saved")
        expect(youtube.cookie_status).not_to_contain_text("secret-session-value")
        expect(youtube.cookie_message).not_to_contain_text("secret-session-value")
        expect(youtube.cookie_state_detail).not_to_contain_text("secret-session-value")

        youtube.delete_cookies()
        expect(youtube.cookie_status).to_contain_text("No YouTube session", timeout=10000)
        expect(youtube.cookie_state_title).to_have_text("No YouTube session saved")
        expect(youtube.cookie_state).to_have_attribute("data-state", "missing")


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
        filename = unquote(links[0].rsplit("/", 1)[-1])
        assert filename.startswith("Fake Artist - Fake Track 1 [")
        assert "youtube_mp3_" not in filename
        assert "youtu.be" not in filename
        expect(youtube.download_list).to_contain_text("Fake Artist - Fake Track 1")

        response = page.request.get(f"{base_url}{links[0]}")
        assert response.status == 200
        assert response.headers["content-type"].startswith("audio/mpeg")
        assert len(response.body()) > 100

        calls = read_fake_calls(flask_server)
        assert calls[-1]["mode"] == "mp3"
        assert "-x" in calls[-1]["args"]
        assert "--audio-format" in calls[-1]["args"]
        assert "--embed-metadata" in calls[-1]["args"]
        assert "--embed-thumbnail" in calls[-1]["args"]
        assert calls[-1]["args"][calls[-1]["args"].index("--convert-thumbnails") + 1] == "jpg"
        assert calls[-1]["args"][calls[-1]["args"].index("-f") + 1] == "bestaudio/b"
        metadata_rules = [
            calls[-1]["args"][index + 1]
            for index, value in enumerate(calls[-1]["args"])
            if value == "--parse-metadata"
        ]
        assert "title:%(artist)s - %(title)s" in metadata_rules
        assert "%(artist,uploader|)s:%(meta_artist)s" in metadata_rules
        assert "%(track,title|)s:%(meta_title)s" in metadata_rules
        assert "--audio-quality" not in calls[-1]["args"]

    def test_playlist_mp3_download_expands_items_and_tags_metadata(
        self, page: Page, base_url, flask_server
    ):
        """Test a playlist URL downloads every item as tagged MP3 outputs."""
        clear_fake_log(flask_server)
        playlist_url = "https://www.youtube.com/playlist?list=PLfakeSongs"
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.select_mp3()
        youtube.enter_links([playlist_url])
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=30000)
        expect(youtube.download_actions).to_be_visible(timeout=5000)
        expect(youtube.download_all_link).to_have_text("Download all 3 files as ZIP")
        expect(youtube.download_list).to_contain_text("3 files downloaded from 1 source, 0 failed")

        links = youtube.get_download_links()
        assert len(links) == 3
        assert all(link.startswith("/download/youtube/audio/") for link in links)
        assert all(link.endswith(".mp3") for link in links)

        archive_href = youtube.get_download_all_href()
        assert archive_href is not None
        response = page.request.get(f"{base_url}{archive_href}")
        assert response.status == 200
        with zipfile.ZipFile(io.BytesIO(response.body())) as archive:
            names = sorted(archive.namelist())
            assert len(names) == 3
            assert all(name.endswith(".mp3") for name in names)
            assert names[0].startswith("001 - Fake Artist - Fake Track 1 [")
            assert names[1].startswith("002 - Fake Artist - Fake Track 2 [")
            assert names[2].startswith("003 - Fake Artist - Fake Track 3 [")
            assert all("youtube_mp3_" not in name for name in names)

        calls = read_fake_calls(flask_server)
        args = calls[-1]["args"]
        metadata_rules = [
            args[index + 1] for index, value in enumerate(args) if value == "--parse-metadata"
        ]
        assert calls[-1]["url"] == playlist_url
        assert calls[-1]["output_count"] == 3
        assert "--yes-playlist" in args
        assert "--no-playlist" not in args
        assert "--embed-metadata" in args
        assert "--embed-thumbnail" in args
        assert args[args.index("--convert-thumbnails") + 1] == "jpg"
        assert "title:%(artist)s - %(title)s" in metadata_rules
        assert "%(artist,uploader|)s:%(meta_artist)s" in metadata_rules
        assert "%(track,title|)s:%(meta_title)s" in metadata_rules
        assert "playlist_title:%(meta_album)s" in metadata_rules
        assert "playlist_index:%(track_number)s" in metadata_rules

    def test_playlist_progress_shows_song_count_while_downloading(
        self, page: Page, base_url, flask_server
    ):
        """Test playlist progress shows songs, not just one pasted source."""
        clear_fake_log(flask_server)
        playlist_url = "https://www.youtube.com/playlist?list=PLslowSongs"
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.select_mp3()
        youtube.enter_links([playlist_url])
        youtube.click_download()

        expect(youtube.status_div).to_be_visible(timeout=10000)
        expect(youtube.status_text).to_contain_text("3 songs", timeout=10000)
        expect(youtube.progress_list).to_contain_text("Playlist: 3 songs queued")
        expect(youtube.progress_list).to_contain_text("Playlist: 0 / 3 songs downloaded")

        expect(youtube.results_section).to_be_visible(timeout=30000)
        expect(youtube.progress_list).to_contain_text("Playlist: 3 / 3 songs downloaded")

        calls = read_fake_calls(flask_server)
        assert any(call["mode"] == "playlist_probe" and call["output_count"] == 3 for call in calls)
        assert calls[-1]["mode"] == "mp3"

    def test_playlist_partial_unavailable_items_still_returns_downloaded_mp3s(
        self, page: Page, base_url, flask_server
    ):
        """Test playlist unavailable items do not discard successfully downloaded MP3s."""
        clear_fake_log(flask_server)
        playlist_url = "https://www.youtube.com/playlist?list=PLpartial-fail"
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.select_mp3()
        youtube.enter_links([playlist_url])
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=30000)
        expect(youtube.warning_div).not_to_be_visible()
        expect(youtube.download_all_link).to_have_text("Download all 3 files as ZIP")
        expect(youtube.download_list).to_contain_text("3 files downloaded from 1 source, 0 failed")

        links = youtube.get_download_links()
        assert len(links) == 3
        assert all(link.startswith("/download/youtube/audio/") for link in links)
        assert all(link.endswith(".mp3") for link in links)

        calls = read_fake_calls(flask_server)
        args = calls[-1]["args"]
        assert calls[-1]["url"] == playlist_url
        assert calls[-1]["output_count"] == 3
        assert "--yes-playlist" in args
        assert "--no-abort-on-error" in args

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

    def test_stored_cookie_verification_uses_fake_downloader(
        self, page: Page, base_url, flask_server
    ):
        """Test stored YouTube cookies can be verified without leaking values."""
        clear_fake_log(flask_server)
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.save_pasted_cookies(SAMPLE_COOKIES)
        expect(youtube.cookie_status).to_contain_text("YouTube session stored", timeout=10000)

        youtube.verify_cookies()
        expect(youtube.cookie_message).to_contain_text("downloadable media formats", timeout=10000)
        expect(youtube.cookie_state_title).to_have_text("YouTube cookies are saved and verified")
        expect(youtube.cookie_state).to_have_attribute("data-state", "verified")
        expect(youtube.cookie_state_detail).to_contain_text("Restricted YouTube downloads")
        expect(youtube.cookie_message).not_to_contain_text("secret-session-value")
        expect(youtube.cookie_state_detail).not_to_contain_text("secret-session-value")

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

    def test_batch_video_downloads_can_be_downloaded_as_one_zip(
        self, page: Page, base_url, flask_server
    ):
        """Test successful batch video outputs expose one ZIP download."""
        clear_fake_log(flask_server)
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.enter_links(
            [
                "https://youtu.be/archive-video-one",
                "https://youtu.be/archive-video-two",
            ]
        )
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=30000)
        expect(youtube.download_actions).to_be_visible(timeout=5000)
        expect(youtube.download_all_link).to_have_text("Download all 2 files as ZIP")

        archive_href = youtube.get_download_all_href()
        assert archive_href is not None
        assert archive_href.startswith("/download/youtube/video/archive/")
        assert archive_href.endswith(".zip")

        response = page.request.get(f"{base_url}{archive_href}")
        assert response.status == 200
        assert response.headers["content-type"].startswith("application/zip")

        with zipfile.ZipFile(io.BytesIO(response.body())) as archive:
            names = sorted(archive.namelist())
            assert len(names) == 2
            assert all(name.endswith(".mp4") for name in names)
            assert all(len(archive.read(name)) > 100 for name in names)

    def test_batch_mp3_downloads_can_be_downloaded_as_one_zip(
        self, page: Page, base_url, flask_server
    ):
        """Test successful batch MP3 outputs expose one ZIP download."""
        clear_fake_log(flask_server)
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.select_mp3()
        youtube.enter_links(
            [
                "https://youtu.be/archive-audio-one",
                "https://youtu.be/archive-audio-two",
            ]
        )
        youtube.click_download()

        expect(youtube.results_section).to_be_visible(timeout=30000)
        expect(youtube.download_actions).to_be_visible(timeout=5000)
        expect(youtube.download_all_link).to_have_text("Download all 2 files as ZIP")

        archive_href = youtube.get_download_all_href()
        assert archive_href is not None
        assert archive_href.startswith("/download/youtube/audio/archive/")
        assert archive_href.endswith(".zip")

        response = page.request.get(f"{base_url}{archive_href}")
        assert response.status == 200
        assert response.headers["content-type"].startswith("application/zip")

        with zipfile.ZipFile(io.BytesIO(response.body())) as archive:
            names = sorted(archive.namelist())
            assert len(names) == 2
            assert all(name.endswith(".mp3") for name in names)
            assert all(len(archive.read(name)) > 100 for name in names)

    def test_liked_video_oauth_feeds_mp3_batch_and_generates_zip(
        self, page: Page, base_url, flask_server
    ):
        """Test connected YouTube likes load into MP3 mode and produce one archive link."""
        clear_fake_log(flask_server)
        liked_links = [
            "https://www.youtube.com/watch?v=liked-slow-one",
            "https://www.youtube.com/watch?v=liked-two",
        ]
        liked_requests = []

        page.route(
            "**/api/youtube/oauth/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "configured": True,
                        "encrypted": True,
                        "client_configured": True,
                        "has_refresh_token": True,
                        "max_liked_results": 2,
                    }
                ),
            ),
        )
        page.route(
            "**/api/youtube/oauth/liked",
            lambda route: (
                liked_requests.append(route.request.post_data or ""),
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "items": [
                                {
                                    "id": "liked-one",
                                    "url": liked_links[0],
                                    "title": "Liked Track One",
                                    "channel_title": "Liked Artist",
                                },
                                {
                                    "id": "liked-two",
                                    "url": liked_links[1],
                                    "title": "Liked Track Two",
                                    "channel_title": "Liked Artist",
                                },
                            ],
                            "links": liked_links,
                            "count": 2,
                        }
                    ),
                ),
            ),
        )

        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        expect(youtube.oauth_state_title).to_have_text("YouTube is connected", timeout=10000)

        youtube.click_liked_download()

        expect(youtube.links_textarea).to_have_value("\n".join(liked_links), timeout=10000)
        expect(youtube.mp3_mode).to_be_checked()
        expect(youtube.oauth_state_title).to_have_text("MP3 download running", timeout=10000)
        expect(youtube.oauth_state_detail).to_contain_text("Download progress is shown below")
        expect(youtube.liked_download_button).to_be_disabled()
        expect(youtube.status_div).to_be_visible(timeout=10000)
        expect(youtube.status_text).to_contain_text("Downloading", timeout=10000)
        expect(youtube.results_section).to_be_visible(timeout=30000)
        expect(youtube.download_all_link).to_have_text("Download all 2 files as ZIP")
        expect(youtube.oauth_state_title).to_have_text("Liked MP3 download complete")

        links = youtube.get_download_links()
        assert len(links) == 2
        assert all(link.startswith("/download/youtube/audio/") for link in links)
        assert all(link.endswith(".mp3") for link in links)

        archive_href = youtube.get_download_all_href()
        assert archive_href is not None
        response = page.request.get(f"{base_url}{archive_href}")
        assert response.status == 200
        assert response.headers["content-type"].startswith("application/zip")
        with zipfile.ZipFile(io.BytesIO(response.body())) as archive:
            names = sorted(archive.namelist())
            assert len(names) == 2
            assert all(name.endswith(".mp3") for name in names)

        calls = [call for call in read_fake_calls(flask_server) if call["mode"] == "mp3"]
        assert [call["url"] for call in calls[-2:]] == liked_links
        assert len(liked_requests) == 1

    def test_slow_fake_download_completes_without_frozen_ui(self, page: Page, base_url):
        """Test a slow fake podcast-style download keeps the UI processing and completes."""
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.enter_links(["https://youtu.be/slow-podcast"])
        youtube.click_download()

        expect(youtube.status_div).to_be_visible(timeout=10000)
        expect(youtube.status_text).to_contain_text("Downloading", timeout=10000)
        expect(youtube.results_section).to_be_visible(timeout=30000)

    def test_server_job_continues_after_browser_reload(self, page: Page, base_url, flask_server):
        """Test a dropped browser stream can reattach to a still-running server job."""
        clear_fake_log(flask_server)
        youtube = YouTubePage(page)
        youtube.navigate(base_url)
        youtube.enter_links(
            [
                "https://youtu.be/slow-reload-one",
                "https://youtu.be/slow-reload-two",
            ]
        )
        youtube.click_download()

        expect(youtube.status_div).to_be_visible(timeout=10000)
        page.wait_for_function("localStorage.getItem('articlenator_youtube_active_job')")
        job_id = page.evaluate("localStorage.getItem('articlenator_youtube_active_job')")

        page.reload()

        expect(youtube.results_section).to_be_visible(timeout=60000)
        expect(youtube.download_all_link).to_have_text("Download all 2 files as ZIP")
        assert page.evaluate("localStorage.getItem('articlenator_youtube_active_job')") is None

        archive_href = youtube.get_download_all_href()
        assert archive_href is not None
        response = page.request.get(f"{base_url}{archive_href}")
        assert response.status == 200
        with zipfile.ZipFile(io.BytesIO(response.body())) as archive:
            assert len(archive.namelist()) == 2

        status = page.request.get(f"{base_url}/api/youtube/download/jobs/{job_id}")
        assert status.status == 200
        status_data = status.json()
        assert status_data["state"] == "complete"
        assert status_data["completed"] == 2


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
        mp3_files = list((output_dir / "youtube" / "audio").glob("*.mp3"))
        assert mp3_files
        assert all("youtube_mp3_" not in path.name for path in mp3_files)
        assert all("youtube.com" not in path.name.lower() for path in mp3_files)
        tags = read_mp3_tags(mp3_files[0])
        assert tags.get("TAG:title")
        assert tags.get("TAG:artist")
        assert "youtube.com" not in tags.get("TAG:title", "").lower()

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
