"""Page Object Model for the videos page."""


class VideosPage:
    """Page object for the video downloader page."""

    def __init__(self, page):
        """Initialize with Playwright page."""
        self.page = page
        self.links_textarea = page.locator("#video-links-input")
        self.download_button = page.locator("#download-btn")
        self.clear_button = page.locator("#clear-btn")
        self.results_section = page.locator("#results")
        self.download_list = page.locator("#download-list")
        self.error_div = page.locator("#error")
        self.error_text = page.locator("#error-text")
        self.status_div = page.locator("#status")
        self.progress_section = page.locator("#progress-details")
        self.progress_list = page.locator("#progress-list")

    def navigate(self, base_url: str = "http://localhost:5000"):
        """Navigate to the videos page."""
        self.page.goto(f"{base_url}/videos")

    def enter_links(self, links: list[str]):
        """Enter links into the textarea."""
        self.links_textarea.fill("\n".join(links))

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
