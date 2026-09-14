"""Anonymous clickthrough: public pages visible without login, gated pages are not."""

from playwright.sync_api import Page, expect


def _fresh_anonymous_page(browser, base_url):
    """Create a brand-new browser context with no session cookie."""
    context = browser.new_context()
    page = context.new_page()
    return context, page


def test_anonymous_visitor_sees_setup_guide_and_sign_in(browser, base_url):
    """A not-logged-in client can open the setup guide from the public nav."""
    context, page = _fresh_anonymous_page(browser, base_url)
    try:
        page.goto(f"{base_url}/setup")

        # Documentation is visible
        expect(page.locator("h1")).to_have_text("Setup Twitter Cookies")
        expect(page.get_by_role("heading", name="How to Get Your Cookies")).to_be_visible()

        # Account-specific management is not rendered for anonymous visitors
        expect(page.locator("#cookie-form-devtools")).to_have_count(0)
        expect(page.locator("#cookie-form-traditional")).to_have_count(0)
        expect(page.locator("#cookies-display")).to_have_count(0)

        # Public nav offers the guide and a way to sign in; no tool links
        expect(page.locator("nav a[href='/setup']")).to_be_visible()
        expect(page.locator("nav a.nav-sign-in")).to_be_visible()
        expect(page.locator("nav a[href='/bookmarks']")).to_have_count(0)
        expect(page.locator("nav a[href='/admin/users']")).to_have_count(0)

        # Build version is still shown in the footer
        expect(page.locator("footer .version")).not_to_have_text("")
    finally:
        context.close()


def test_anonymous_nav_sign_in_click_reaches_login_and_back_to_setup(browser, base_url):
    """Clicking Sign in in the nav lands on the login page."""
    context, page = _fresh_anonymous_page(browser, base_url)
    try:
        page.goto(f"{base_url}/setup")
        page.locator("nav a.nav-sign-in").click()

        expect(page).to_have_url(f"{base_url}/login?next=/setup")
        expect(page.locator("h1")).to_have_text("Sign in")
    finally:
        context.close()


def test_anonymous_gated_pages_redirect_to_login(browser, base_url):
    """Functional pages still demand login when visited directly."""
    context, page = _fresh_anonymous_page(browser, base_url)
    try:
        for path in [
            "/",
            "/bookmarks",
            "/videos",
            "/youtube",
            "/13ft",
            "/transcription",
            "/channel",
            "/admin/users",
            "/download/some-file.pdf",
        ]:
            page.goto(f"{base_url}{path}")
            expect(page).to_have_url(f"{base_url}/login?next={path}")
            expect(page.locator("h1")).to_have_text("Sign in")
    finally:
        context.close()


def test_anonymous_convert_api_is_rejected_from_the_browser(browser, base_url):
    """The setup page loads anonymously but its account APIs stay auth-only."""
    context, page = _fresh_anonymous_page(browser, base_url)
    try:
        page.goto(f"{base_url}/setup")
        response = page.evaluate("fetch('/api/cookies/status').then(r => r.status)")

        assert response == 401
    finally:
        context.close()


def test_login_reveals_account_sections_on_setup(page: Page, base_url):
    """After signing in, the same page shows the per-account cookie management.

    Note: the module-level autouse ``authenticated_page`` fixture (conftest.py)
    performs the real login click-through on this shared ``page`` fixture
    before the test body runs; this test then verifies the revealed state.
    """
    page.goto(f"{base_url}/setup")

    expect(page.locator("h1")).to_have_text("Setup Twitter Cookies")
    expect(page.locator("#cookie-form-devtools")).to_be_visible()
    expect(page.locator("#cookies-display")).to_be_visible()
    expect(page.locator("nav a[href='/bookmarks']")).to_be_visible()
