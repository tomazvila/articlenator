"""E2E tests for the video download feature."""

import os

from playwright.sync_api import Page, expect

from .pages import VideosPage

COOKIES_KEY = "articlenator_cookies"

# Real cookies for testing - loaded from env or hardcoded for CI
TEST_COOKIES = os.environ.get(
    "TEST_TWITTER_COOKIES",
    "auth_token=c9dcae216409b4b5f1c8c3139af9889843f50bdb; "
    "ct0=7eace32cd8d783b3d9a7f64c25e7c4a74519a829867e95b43cb595afea415c68"
    "ff6262ada0404975a43b386412f36896e532237c684d9daa963323f2682b69ddee"
    "abd58385f53d948aedcdba3d4f6b9f",
)

# Test video URL
TEST_VIDEO_URL = "https://x.com/catshealdeprsn/status/2031385297305612771"


class TestVideosPage:
    """Tests for the videos page UI."""

    def test_user_can_access_videos_page(self, page: Page, base_url):
        """Test user can access the videos page."""
        page.goto(f"{base_url}/videos")
        expect(page).to_have_title("Video Downloader")

    def test_videos_page_has_textarea(self, page: Page, base_url):
        """Test videos page has textarea for links."""
        videos = VideosPage(page)
        videos.navigate(base_url)
        expect(videos.links_textarea).to_be_visible()

    def test_videos_page_has_download_button(self, page: Page, base_url):
        """Test videos page has download button."""
        videos = VideosPage(page)
        videos.navigate(base_url)
        expect(videos.download_button).to_be_visible()
        expect(videos.download_button).to_have_text("Download Videos")

    def test_videos_page_has_nav_link(self, page: Page, base_url):
        """Test videos page is linked from navigation."""
        page.goto(base_url)
        nav_link = page.locator("a[href='/videos']")
        expect(nav_link).to_be_visible()
        expect(nav_link).to_have_text("Videos")

    def test_download_without_cookies_shows_error(self, page: Page, base_url):
        """Test downloading without cookies shows error."""
        page.goto(f"{base_url}/videos")
        page.evaluate(f"localStorage.removeItem('{COOKIES_KEY}')")
        page.reload()

        videos = VideosPage(page)
        videos.enter_links([TEST_VIDEO_URL])
        videos.click_download()

        expect(videos.error_div).to_be_visible(timeout=5000)
        expect(videos.error_div).to_contain_text("cookie")

    def test_download_with_empty_links_shows_error(self, page: Page, base_url):
        """Test downloading with empty links shows error."""
        page.goto(f"{base_url}/videos")
        page.evaluate(
            f"localStorage.setItem('{COOKIES_KEY}', "
            f"'auth_token=test12345678901234567890; ct0=test12345678901234567890')"
        )
        page.reload()

        videos = VideosPage(page)
        videos.click_download()

        expect(videos.error_div).to_be_visible(timeout=5000)
        expect(videos.error_div).to_contain_text("at least one link")

    def test_clear_button_clears_textarea(self, page: Page, base_url):
        """Test clear button clears the textarea."""
        videos = VideosPage(page)
        videos.navigate(base_url)

        videos.enter_links(["https://x.com/test/status/123"])
        expect(videos.links_textarea).not_to_be_empty()

        videos.click_clear()
        expect(videos.links_textarea).to_be_empty()


class TestVideoDownloadWorkflow:
    """E2E test for the full video download workflow with real Twitter cookies."""

    def test_download_single_video(self, page: Page, base_url, output_dir):
        """Test downloading a single video from Twitter/X end-to-end."""
        # Set up cookies
        page.goto(f"{base_url}/videos")
        page.evaluate(f"localStorage.setItem('{COOKIES_KEY}', `{TEST_COOKIES}`)")
        page.reload()

        videos = VideosPage(page)

        # Enter the video link
        videos.enter_links([TEST_VIDEO_URL])

        # Click download
        videos.click_download()

        # Wait for the status to show processing
        expect(videos.status_div).to_be_visible(timeout=10000)

        # Wait for results - video download can take up to 5 minutes
        expect(videos.results_section).to_be_visible(timeout=300000)

        # Verify download links appear
        download_links = videos.get_download_links()
        assert len(download_links) >= 1, f"Expected at least 1 download link, got {len(download_links)}"

        # Verify the download link points to a video file
        for link in download_links:
            assert "/download/video/" in link, f"Expected video download link, got {link}"
            assert link.endswith(".mp4"), f"Expected .mp4 file, got {link}"

        # Verify the file actually exists on disk
        video_dir = output_dir / "videos"
        assert video_dir.exists(), "Videos directory should exist"

        mp4_files = list(video_dir.glob("*.mp4"))
        assert len(mp4_files) >= 1, f"Expected at least 1 mp4 file, got {len(mp4_files)}"

        # Verify the file has non-trivial size (at least 10KB)
        for mp4 in mp4_files:
            size = mp4.stat().st_size
            assert size > 10000, f"Video file {mp4.name} is too small ({size} bytes)"

        # Verify we can actually download the file via the server
        first_link = download_links[0]
        response = page.request.get(f"{base_url}{first_link}")
        assert response.status == 200, f"Download returned status {response.status}"
        assert len(response.body()) > 10000, "Downloaded file is too small"
