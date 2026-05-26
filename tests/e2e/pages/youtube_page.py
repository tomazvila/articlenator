"""Page Object Model for the YouTube downloader page."""


class YouTubePage:
    """Page object for the YouTube downloader page."""

    def __init__(self, page):
        """Initialize with Playwright page."""
        self.page = page
        self.links_textarea = page.locator("#youtube-links-input")
        self.cookies_textarea = page.locator("#youtube-cookies-input")
        self.cookie_status = page.locator("#youtube-cookie-status")
        self.video_mode = page.locator("#mode-video")
        self.mp3_mode = page.locator("#mode-mp3")
        self.download_button = page.locator("#youtube-download-btn")
        self.clear_button = page.locator("#youtube-clear-btn")
        self.results_section = page.locator("#results")
        self.download_list = page.locator("#download-list")
        self.error_div = page.locator("#error")
        self.error_text = page.locator("#error-text")
        self.warning_div = page.locator("#warning")
        self.warning_text = page.locator("#warning-text")
        self.status_div = page.locator("#status")
        self.status_text = page.locator("#status-text")
        self.progress_section = page.locator("#progress-details")
        self.progress_list = page.locator("#progress-list")

    def navigate(self, base_url: str = "http://localhost:5000"):
        """Navigate to the YouTube page."""
        self.page.goto(f"{base_url}/youtube")

    def enter_links(self, links: list[str]):
        """Enter links into the textarea."""
        self.links_textarea.fill("\n".join(links))

    def enter_cookies(self, cookies: str):
        """Enter YouTube cookies."""
        if not self.cookies_textarea.is_visible():
            self.page.locator(".youtube-session summary").click()
        self.cookies_textarea.fill(cookies)

    def select_mp3(self):
        """Select MP3 mode."""
        self.mp3_mode.check()

    def select_video(self):
        """Select video mode."""
        self.video_mode.check()

    def click_download(self):
        """Click the download button."""
        self.download_button.click()

    def click_clear(self):
        """Click the clear button."""
        self.clear_button.click()

    def get_download_links(self) -> list[str]:
        """Get all download link hrefs."""
        return [a.get_attribute("href") for a in self.download_list.locator("a").all()]

    def get_download_texts(self) -> list[str]:
        """Get all download link texts."""
        return self.download_list.locator("a").all_text_contents()
