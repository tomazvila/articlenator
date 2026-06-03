"""Page Object Model for the YouTube downloader page."""


class YouTubePage:
    """Page object for the YouTube downloader page."""

    def __init__(self, page):
        """Initialize with Playwright page."""
        self.page = page
        self.links_textarea = page.locator("#youtube-links-input")
        self.cookies_textarea = page.locator("#youtube-cookies-input")
        self.cookies_file = page.locator("#youtube-cookies-file")
        self.cookie_status = page.locator("#youtube-cookie-status")
        self.cookie_message = page.locator("#youtube-cookie-message")
        self.cookie_state = page.locator("#youtube-session-state")
        self.cookie_state_title = page.locator("#youtube-session-state-title")
        self.cookie_state_detail = page.locator("#youtube-session-state-detail")
        self.cookie_count = page.locator("#youtube-cookie-count")
        self.verify_cookies_button = page.locator("#youtube-verify-cookies-btn")
        self.delete_cookies_button = page.locator("#youtube-delete-cookies-btn")
        self.save_cookies_button = page.locator("#youtube-save-cookies-btn")
        self.liked_download_button = page.locator("#youtube-liked-download-btn")
        self.oauth_disconnect_button = page.locator("#youtube-oauth-disconnect-btn")
        self.oauth_message = page.locator("#youtube-oauth-message")
        self.oauth_state = page.locator("#youtube-oauth-state")
        self.oauth_state_title = page.locator("#youtube-oauth-state-title")
        self.oauth_state_detail = page.locator("#youtube-oauth-state-detail")
        self.video_mode = page.locator("#mode-video")
        self.mp3_mode = page.locator("#mode-mp3")
        self.download_button = page.locator("#youtube-download-btn")
        self.clear_button = page.locator("#youtube-clear-btn")
        self.results_section = page.locator("#results")
        self.download_actions = page.locator("#youtube-download-actions")
        self.download_all_link = page.locator("#youtube-download-all")
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
        """Paste YouTube cookies into the upload control."""
        if not self.cookies_textarea.is_visible():
            self.page.locator(".youtube-session-paste summary").click()
        self.cookies_textarea.fill(cookies)

    def save_pasted_cookies(self, cookies: str):
        """Paste and save YouTube cookies to the server."""
        self.enter_cookies(cookies)
        self.save_cookies_button.click()

    def delete_cookies(self):
        """Delete stored YouTube cookies."""
        self.delete_cookies_button.click()

    def verify_cookies(self):
        """Verify stored YouTube cookies."""
        self.verify_cookies_button.click()

    def click_liked_download(self):
        """Click the liked-video MP3 download button."""
        self.liked_download_button.click()

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

    def get_download_all_href(self) -> str | None:
        """Get the batch archive link href."""
        return self.download_all_link.get_attribute("href")
