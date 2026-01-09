"""E2E tests for complete user journeys."""

import json

from playwright.sync_api import Page, expect

from .pages import CookieGuidePage, IndexPage


class TestIndexPage:
    """Tests for the index page."""

    def test_user_can_access_index(self, page: Page, base_url):
        """Test user can access the index page."""
        page.goto(base_url)
        expect(page).to_have_title("Article to PDF Converter")

    def test_index_has_links_textarea(self, page: Page, base_url):
        """Test index page has textarea for links."""
        index = IndexPage(page)
        index.navigate(base_url)

        expect(index.links_textarea).to_be_visible()

    def test_index_has_convert_button(self, page: Page, base_url):
        """Test index page has convert button."""
        index = IndexPage(page)
        index.navigate(base_url)

        expect(index.convert_button).to_be_visible()
        expect(index.convert_button).to_have_text("Convert to PDF")

    def test_index_has_setup_link(self, page: Page, base_url):
        """Test index page has link to setup page."""
        index = IndexPage(page)
        index.navigate(base_url)

        expect(index.setup_link).to_be_visible()


class TestSetupPage:
    """Tests for the setup/cookie guide page."""

    def test_user_can_access_setup(self, page: Page, base_url):
        """Test user can access the setup page."""
        page.goto(f"{base_url}/setup")
        expect(page).to_have_title("Setup Twitter Cookies")

    def test_setup_has_instructions(self, page: Page, base_url):
        """Test setup page has cookie instructions."""
        page.goto(f"{base_url}/setup")

        # Should have DevTools instructions
        expect(page.locator("text=Developer Tools").first).to_be_visible()
        expect(page.locator("text=auth_token").first).to_be_visible()

    def test_setup_has_cookie_form(self, page: Page, base_url):
        """Test setup page has cookie input form."""
        guide = CookieGuidePage(page)
        guide.navigate(base_url)

        expect(guide.cookie_input).to_be_visible()
        expect(guide.save_button).to_be_visible()


class TestNavigationFlow:
    """Tests for navigation between pages."""

    def test_user_can_navigate_to_setup(self, page: Page, base_url):
        """Test user can navigate from index to setup."""
        index = IndexPage(page)
        index.navigate(base_url)
        index.click_setup_link()

        expect(page).to_have_url(f"{base_url}/setup")
        expect(page).to_have_title("Setup Twitter Cookies")

    def test_user_can_return_from_setup(self, page: Page, base_url):
        """Test user can navigate back from setup to index."""
        page.goto(f"{base_url}/setup")

        # Should have link back to index
        page.click("a[href='/']")
        expect(page).to_have_url(f"{base_url}/")


class TestCookieSaving:
    """Tests for saving cookies."""

    def test_user_can_save_cookies(self, page: Page, base_url, config_dir):
        """Test user can save cookies via setup page."""
        guide = CookieGuidePage(page)
        guide.navigate(base_url)

        # Enter cookies
        test_cookies = "auth_token=test123; ct0=csrf456"
        guide.enter_cookies(test_cookies)
        guide.click_save()

        # Wait for success message
        expect(guide.success_message).to_be_visible(timeout=5000)

        # Verify cookies were saved
        cookie_file = config_dir / "cookies.json"
        assert cookie_file.exists()
        data = json.loads(cookie_file.read_text())
        assert data["cookies"] == test_cookies

    def test_empty_cookies_shows_error(self, page: Page, base_url):
        """Test empty cookies shows error message."""
        guide = CookieGuidePage(page)
        guide.navigate(base_url)

        # Submit without entering cookies
        guide.click_save()

        # Should show error
        expect(guide.error_message).to_be_visible(timeout=5000)


class TestConversionFlow:
    """Tests for the conversion workflow."""

    def test_convert_without_cookies_shows_error(self, page: Page, base_url, config_dir):
        """Test converting without cookies shows setup prompt."""
        # Ensure no cookies exist for this test
        cookie_file = config_dir / "cookies.json"
        if cookie_file.exists():
            cookie_file.unlink()

        index = IndexPage(page)
        index.navigate(base_url)

        # Enter a valid Twitter URL
        index.enter_links(["https://x.com/testuser/status/123456789"])
        index.click_convert()

        # Should show error about missing cookies
        error_div = page.locator("#error")
        expect(error_div).to_be_visible(timeout=5000)
        expect(error_div).to_contain_text("cookie")

    def test_convert_with_invalid_url_shows_error(self, page: Page, base_url, config_dir):
        """Test converting invalid URL shows error."""
        # First save cookies
        cookie_file = config_dir / "cookies.json"
        cookie_file.write_text(json.dumps({"cookies": "auth_token=test; ct0=test"}))

        index = IndexPage(page)
        index.navigate(base_url)

        # Enter an invalid URL (non-existent page)
        index.enter_links(["https://example.com/not-twitter"])
        index.click_convert()

        # Should show error about failed conversion
        error_div = page.locator("#error")
        expect(error_div).to_be_visible(timeout=5000)
        expect(error_div).to_contain_text("failed")


class TestHealthCheck:
    """Tests for health check endpoint."""

    def test_health_endpoint(self, page: Page, base_url):
        """Test health endpoint returns OK."""
        response = page.request.get(f"{base_url}/api/health")
        assert response.status == 200

        data = response.json()
        assert data["status"] == "ok"
