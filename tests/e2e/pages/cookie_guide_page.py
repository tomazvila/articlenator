"""Page Object Model for the cookie guide page."""


class CookieGuidePage:
    """Page object for the cookie setup guide page."""

    def __init__(self, page):
        """Initialize with Playwright page."""
        self.page = page
        self.cookie_input = page.locator("#cookie-input")
        self.save_button = page.locator("#save-cookies-btn")
        self.success_message = page.locator("#save-success")
        self.error_message = page.locator("#save-error")

    def navigate(self, base_url: str = "http://localhost:5000"):
        """Navigate to the setup page."""
        self.page.goto(f"{base_url}/setup")

    def enter_cookies(self, cookies: str):
        """Enter cookies into the input."""
        self.cookie_input.fill(cookies)

    def click_save(self):
        """Click the save cookies button."""
        self.save_button.click()

    def has_success_message(self) -> bool:
        """Check if success message is visible."""
        return self.success_message.is_visible()

    def has_error_message(self) -> bool:
        """Check if error message is visible."""
        return self.error_message.is_visible()
