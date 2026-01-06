"""Page Object Model for the index page."""


class IndexPage:
    """Page object for the main index page."""

    def __init__(self, page):
        """Initialize with Playwright page."""
        self.page = page
        self.links_textarea = page.locator("#links-input")
        self.convert_button = page.locator("#convert-btn")
        self.results_section = page.locator("#results")
        self.setup_link = page.locator("a[href='/setup']").first

    def navigate(self, base_url: str = "http://localhost:5000"):
        """Navigate to the index page."""
        self.page.goto(f"{base_url}/")

    def enter_links(self, links: list[str]):
        """Enter links into the textarea."""
        self.links_textarea.fill("\n".join(links))

    def click_convert(self):
        """Click the convert button."""
        self.convert_button.click()

    def get_download_links(self) -> list[str]:
        """Get all download link texts."""
        return self.results_section.locator("a").all_text_contents()

    def click_setup_link(self):
        """Click the setup link."""
        self.setup_link.click()
