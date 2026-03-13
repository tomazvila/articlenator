"""Page Object Model for the bookmarks page."""


class BookmarksPage:
    """Page object for the bookmark fetcher page."""

    def __init__(self, page):
        """Initialize with Playwright page."""
        self.page = page
        self.fetch_button = page.locator("#fetch-btn")
        self.convert_button = page.locator("#convert-btn")
        self.fetch_status = page.locator("#fetch-status")
        self.fetch_status_text = page.locator("#fetch-status-text")
        self.bookmarks_section = page.locator("#bookmarks-section")
        self.bookmarks_title = page.locator("#bookmarks-title")
        self.bookmarks_list = page.locator("#bookmarks-list")
        self.filter_articles = page.locator('[data-filter="articles"]')
        self.filter_all = page.locator('[data-filter="all"]')
        self.filter_tweets = page.locator('[data-filter="tweets"]')
        self.select_all_btn = page.locator("#select-all-btn")
        self.select_none_btn = page.locator("#select-none-btn")
        self.selection_count = page.locator("#selection-count")
        self.results_section = page.locator("#results")
        self.error_div = page.locator("#error")
        self.error_text = page.locator("#error-text")

    def navigate(self, base_url: str = "http://localhost:5000"):
        """Navigate to the bookmarks page."""
        self.page.goto(f"{base_url}/bookmarks")

    def click_fetch(self):
        """Click the Fetch Bookmarks button."""
        self.fetch_button.click()

    def click_convert(self):
        """Click the Convert Selected to PDF button."""
        self.convert_button.click()

    def get_bookmark_count(self) -> int:
        """Get the number of bookmark items in the list."""
        return self.bookmarks_list.locator(".bookmark-item").count()

    def get_article_count(self) -> int:
        """Get the number of article items (with has-article class)."""
        return self.bookmarks_list.locator(".bookmark-item.has-article").count()

    def get_checked_count(self) -> int:
        """Get the number of checked bookmark checkboxes."""
        return self.page.locator(".bookmark-check:checked").count()
