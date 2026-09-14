"""13ft browser clickthrough, including a real public-page retrieval."""

from playwright.sync_api import expect


def test_13ft_reader_clickthrough(page, base_url):
    page.goto(base_url)
    page.get_by_role("link", name="13ft", exact=True).click()
    expect(page).to_have_title("13ft Reader · Articlenator")
    expect(page.locator('nav a[aria-current="page"]')).to_have_text("13ft")
    page.locator("#ladder-url").fill("http://127.0.0.1")
    page.get_by_role("button", name="Read article", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Only public websites", timeout=10000)
    page.locator("#ladder-url").fill("https://example.com")
    page.get_by_role("button", name="Read article", exact=True).click()
    expect(page.locator("#ladder-result")).to_be_visible(timeout=75000)
    expect(page.locator("#ladder-title")).to_have_text("Example Domain")
    expect(page.locator("#ladder-content")).to_contain_text("documentation")
    expect(page.locator("footer .version")).not_to_have_text("")
    page.screenshot(path="/tmp/articlenator-13ft-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="/tmp/articlenator-13ft-mobile.png", full_page=True)
    page.get_by_role("button", name="Clear", exact=True).click()
    expect(page.locator("#ladder-url")).to_be_empty()
    expect(page.locator("#ladder-result")).to_be_hidden()
    page.get_by_role("link", name="YouTube", exact=True).click()
    expect(page).to_have_title("YouTube Downloader")


def test_reuters_browser_fallback(page, base_url):
    page.goto(base_url)
    page.get_by_role("link", name="13ft", exact=True).click()
    page.locator("#ladder-url").fill(
        "https://www.reuters.com/world/europe/"
        "nato-allies-foil-russian-subsea-cable-sabotage-plot-2026-09-10/"
    )
    # Live-network fetch: reuters intermittently serves challenges, so retry
    # through the UI's own Clear control before giving up.
    for attempt in range(3):
        page.get_by_role("button", name="Read article", exact=True).click()
        try:
            expect(page.locator("#ladder-result")).to_be_visible(timeout=75000)
            break
        except AssertionError:
            if attempt == 2:
                raise
            expect(page.locator("#ladder-error")).to_be_visible()
            page.locator("#ladder-clear").click()
            expect(page.locator("#ladder-url")).to_have_value("")
        page.locator("#ladder-url").fill(
            "https://www.reuters.com/world/europe/"
            "nato-allies-foil-russian-subsea-cable-sabotage-plot-2026-09-10/"
        )
    expect(page.locator("#ladder-title")).to_contain_text(
        "NATO allies foil Russian subsea cable sabotage plot"
    )
    expect(page.locator("#ladder-content")).to_contain_text("Svalbard")
    assert len(page.locator("#ladder-content").inner_text()) > 2000
    page.screenshot(path="/tmp/articlenator-reuters-botasaurus.png", full_page=True)

    with page.expect_download() as download_event:
        page.get_by_role("button", name="Download PDF", exact=True).click()
    download = download_event.value
    assert download.suggested_filename.endswith(".pdf")
    download.save_as("/tmp/articlenator-reuters.pdf")
    from pypdf import PdfReader

    pdf = PdfReader("/tmp/articlenator-reuters.pdf")
    text = "\n".join(p.extract_text() for p in pdf.pages)
    assert "Svalbard" in text
    assert "NATO allies" in text
    assert "reuters.com" in text
    assert len(text) > 6000
