"""E2E tests for article-to-PDF conversion with images."""

import os

from playwright.sync_api import Page, expect

from .pages import IndexPage

COOKIES_KEY = "articlenator_cookies"

# Real cookies for testing
TEST_COOKIES = os.environ.get(
    "TEST_TWITTER_COOKIES",
    "auth_token=c9dcae216409b4b5f1c8c3139af9889843f50bdb; "
    "ct0=7eace32cd8d783b3d9a7f64c25e7c4a74519a829867e95b43cb595afea415c68"
    "ff6262ada0404975a43b386412f36896e532237c684d9daa963323f2682b69ddee"
    "abd58385f53d948aedcdba3d4f6b9f",
)

# Test article with images
TEST_ARTICLE_URL = "https://x.com/kevinxu/status/2007539219774972395"


class TestArticleWithImages:
    """E2E test for article conversion with inline images."""

    def test_article_with_images_produces_pdf(self, page: Page, base_url, output_dir):
        """Test that converting a Twitter article with images produces a PDF containing those images.

        Uses a real Twitter article URL that contains inline images.
        Verifies the resulting PDF is large enough to contain image data.
        """
        # Set up cookies in localStorage
        page.goto(base_url)
        page.evaluate(f"localStorage.setItem('{COOKIES_KEY}', `{TEST_COOKIES}`)")
        page.reload()

        index = IndexPage(page)

        # Enter the article URL
        index.enter_links([TEST_ARTICLE_URL])

        # Click convert
        index.click_convert()

        # Wait for results section to appear (article fetch + PDF gen can take a while)
        expect(index.results_section).to_be_visible(timeout=180000)

        # Verify download link appeared
        download_links = index.results_section.locator("a")
        expect(download_links.first).to_be_visible(timeout=10000)

        # Get the download href
        href = download_links.first.get_attribute("href")
        assert href and href.startswith("/download/"), f"Unexpected download link: {href}"
        assert href.endswith(".pdf"), f"Expected PDF download, got: {href}"

        # Verify PDF exists on disk and has substantial size (images make it much larger)
        pdf_files = list(output_dir.glob("*.pdf"))
        assert len(pdf_files) >= 1, "No PDF files found in output directory"

        # Find the most recent PDF
        latest_pdf = max(pdf_files, key=lambda p: p.stat().st_mtime)
        pdf_size = latest_pdf.stat().st_size

        # A text-only article PDF is typically 10-30KB.
        # With images, it should be significantly larger (100KB+).
        assert pdf_size > 100_000, (
            f"PDF is only {pdf_size:,} bytes — likely missing images. "
            f"Expected >100KB for an article with inline images."
        )

        # Verify we can download the PDF via the server
        response = page.request.get(f"{base_url}{href}")
        assert response.status == 200, f"PDF download returned status {response.status}"
        assert len(response.body()) > 100_000, (
            f"Downloaded PDF is only {len(response.body()):,} bytes — likely missing images."
        )
