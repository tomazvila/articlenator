"""Browser fallback ordering, challenge rejection and public-only egress."""

from unittest.mock import patch

import pytest

from twitter_articlenator.sources import ladder_core


def blocked_response():
    return type(
        "Response",
        (),
        {
            "status_code": 403,
            "text": "",
            "url": "https://example.com",
            "apparent_encoding": "utf-8",
        },
    )()


def test_browser_recovers_before_archives():
    with (
        patch.object(ladder_core.requests, "get", return_value=blocked_response()),
        patch.object(
            ladder_core,
            "fetch_via_browser",
            return_value=("<article><p>Browser article</p></article>", "https://example.com"),
        ) as browser,
        patch.object(ladder_core, "fetch_via_archive_org") as archive,
    ):
        assert "Browser article" in ladder_core.bypass_paywall("https://example.com", {})
        browser.assert_called_once()
        archive.assert_not_called()


def test_browser_challenge_continues_to_archive():
    with (
        patch.object(ladder_core.requests, "get", return_value=blocked_response()),
        patch.object(
            ladder_core,
            "fetch_via_browser",
            return_value=("<p>Checking your browser</p>", "https://example.com"),
        ),
        patch.object(
            ladder_core,
            "fetch_via_archive_org",
            return_value=("<p>Archived article</p>", "https://web.archive.org/story"),
        ),
    ):
        assert "Archived article" in ladder_core.bypass_paywall("https://example.com", {})


def test_direct_failure_uses_browser():
    with (
        patch.object(ladder_core.requests, "get", side_effect=TimeoutError),
        patch.object(
            ladder_core,
            "fetch_via_browser",
            return_value=("<p>Browser article</p>", "https://example.com"),
        ),
    ):
        assert "Browser article" in ladder_core.bypass_paywall("https://example.com", {})


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/", "https://10.0.0.1/"]
)
def test_proxy_rejects_private_destinations(url):
    from twitter_articlenator.sources.ladder_browser import public_proxy

    with public_proxy() as proxy:
        # HTTP CONNECT, independent of environment proxy bypass settings.
        import http.client
        from urllib.parse import urlsplit

        host = urlsplit(url).hostname
        connection = http.client.HTTPConnection("127.0.0.1", int(proxy.rsplit(":", 1)[1]))
        connection.set_tunnel(host, 443)
        with pytest.raises(OSError, match="403"):
            connection.request("GET", "/")
        connection.close()


def test_deadline_skips_browser_launch():
    import time
    from twitter_articlenator.sources import ladder_http, ladder_browser

    token = ladder_http.DEADLINE.set(time.monotonic() - 1)
    try:
        with patch.object(ladder_browser.subprocess, "Popen") as process:
            assert ladder_browser.fetch_via_browser("https://example.com") == (None, None)
            process.assert_not_called()
    finally:
        ladder_http.DEADLINE.reset(token)


def test_reuters_div_paragraphs_are_extracted_in_order():
    from twitter_articlenator.routes.ladder import article_blocks

    result = article_blocks("""<title>Example story</title><article>
        <div class="article-body-module__content__hash">
          <div data-testid="paragraph-0">First <a>linked</a> paragraph.</div>
          <h2>Section</h2>
          <div data-testid="paragraph-1">Second paragraph.</div>
        </div><p>Read next: unrelated story</p></article>""")
    assert result["blocks"] == [
        {"tag": "p", "text": "First linked paragraph."},
        {"tag": "h2", "text": "Section"},
        {"tag": "p", "text": "Second paragraph."},
    ]
