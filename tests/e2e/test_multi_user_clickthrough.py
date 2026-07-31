"""Authenticated browser clickthrough for every primary multi-user surface."""

from playwright.sync_api import Page, expect

from .pages import CookieGuidePage, IndexPage


def test_admin_navigation_clickthrough_reaches_every_primary_page(page: Page, base_url):
    """Click every authenticated navigation destination and verify its page title."""
    destinations = [
        ("/", "Article to PDF Converter"),
        ("/bookmarks", "Bookmarks"),
        ("/videos", "Video Downloader"),
        ("/youtube", "YouTube Downloader"),
        ("/transcription", "Transcribe · Twitter Article to PDF"),
        ("/channel", "Channels · Twitter Article to PDF"),
        ("/setup", "Setup Twitter Cookies"),
        ("/admin/users", "Users · Articlenator"),
    ]
    page.goto(base_url)

    for path, title in destinations:
        page.locator(f"nav a[href='{path}']").click()
        expect(page).to_have_url(f"{base_url}{path}")
        expect(page).to_have_title(title)


def test_admin_created_account_login_and_browser_server_state_are_isolated(
    page: Page,
    base_url,
):
    """Create a user through the UI, switch accounts, and verify private state isolation."""
    page.goto(f"{base_url}/admin/users")
    page.locator("#username").fill("click-reader")
    page.locator("#password").fill("reader correct horse battery staple")
    page.get_by_role("button", name="Create account").click()
    expect(page.locator(".success-message")).to_have_text("Account created")
    expect(page.locator(".account-table")).to_contain_text("click-reader")

    admin_index = IndexPage(page)
    admin_index.navigate(base_url)
    admin_index.links_textarea.fill("https://example.com/admin-only")

    guide = CookieGuidePage(page)
    guide.navigate(base_url)
    guide.enter_cookies("auth_token=" + "a" * 40 + "; ct0=" + "b" * 64)
    guide.click_save()
    expect(guide.success_message).to_be_visible(timeout=5000)

    page.locator("form.logout-form button").click()
    expect(page).to_have_url(f"{base_url}/login")
    page.locator("#username").fill("click-reader")
    page.locator("#password").fill("reader correct horse battery staple")
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(f"{base_url}/")

    reader_index = IndexPage(page)
    expect(reader_index.links_textarea).to_be_empty()
    expect(page.locator("nav a[href='/admin/users']")).to_have_count(0)
    assert page.request.get(f"{base_url}/admin/users").status == 403
    cookie_status = page.request.get(f"{base_url}/api/cookies/status")
    assert cookie_status.status == 200
    assert cookie_status.json()["configured"] is False
