"""13ft reader behavior and trust-boundary coverage."""

from unittest.mock import patch

import pytest

from twitter_articlenator.app import create_app


@pytest.fixture
def reader(tmp_path):
    app = create_app(
        {"TESTING": True, "AUTH_REQUIRED": True, "USER_DATABASE_PATH": tmp_path / "users.db"}
    )
    app.extensions["user_store"].create_user("reader", "test reader password")
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as session:
        token = session["_csrf_token"]
    client.post(
        "/login",
        data={"username": "reader", "password": "test reader password", "csrf_token": token},
    )
    client.get("/13ft")
    with client.session_transaction() as session:
        token = session["_csrf_token"]
    return app, client, {"X-CSRF-Token": token}


def test_navigation_auth_and_csrf(reader):
    app, client, headers = reader
    assert app.test_client().get("/13ft").status_code == 302
    assert app.test_client().post("/api/13ft", json={}).status_code == 401
    assert client.post("/api/13ft", json={}).status_code == 403
    assert b'href="/13ft"' in client.get("/youtube").data
    assert b"13ft Reader" in client.get("/13ft").data


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "file:///etc/passwd",
        "http://127.0.0.1",
        "http://[::1]",
        "http://169.254.169.254",
        "https://user:pass@example.com",
        "http://localhost:5001",
        "https://example.com\\@localhost",
        "http://10.0.0.1",
    ],
)
def test_invalid_urls(reader, url):
    _, client, headers = reader
    response = client.post("/api/13ft", json={"url": url}, headers=headers)
    assert response.status_code == 400
    assert response.json["error"]


def test_success_extracts_text_and_preserves_url(reader):
    _, client, headers = reader
    with (
        patch(
            "twitter_articlenator.sources.ladder_http.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ),
        patch(
            "twitter_articlenator.sources.ladder_core.bypass_paywall",
            return_value="<title>A title</title><article><h1>A title</h1><p>Hello reader.</p>"
            "<script>alert(1)</script><p>&lt;img onerror=alert(1)&gt;</p></article>",
        ),
    ):
        response = client.post("/api/13ft", json={"url": "example.com/a?x=1&y=2"}, headers=headers)
    assert response.status_code == 200
    assert response.json["url"] == "https://example.com/a?x=1&y=2"
    assert response.json["blocks"][1]["text"] == "Hello reader."
    assert all("alert(1)" != b["text"] for b in response.json["blocks"])


def test_private_redirect_is_blocked():
    from twitter_articlenator.sources import ladder_http

    with (
        patch.object(
            ladder_http.socket,
            "getaddrinfo",
            side_effect=[
                [(2, 1, 6, "", ("93.184.216.34", 80))],
                [(2, 1, 6, "", ("127.0.0.1", 80))],
            ],
        ),
        patch.object(ladder_http.http.client, "HTTPConnection") as connection,
    ):
        response = connection.return_value.getresponse.return_value
        response.status = 302
        response.getheader.return_value = "http://localhost/"
        with pytest.raises(ValueError, match="public"):
            ladder_http.get("http://example.com")
        assert connection.call_count == 1


@patch("twitter_articlenator.sources.ladder_core.fetch_via_browser", return_value=(None, None))
def test_fallback_and_unavailable(_browser):
    from twitter_articlenator.sources import ladder_core

    with (
        patch.object(
            ladder_core.requests,
            "get",
            return_value=type(
                "Response",
                (),
                {
                    "status_code": 403,
                    "text": "",
                    "url": "https://example.com",
                    "apparent_encoding": "utf-8",
                },
            )(),
        ),
        patch.object(
            ladder_core,
            "fetch_via_archive_org",
            return_value=("<p>Archived story</p>", "https://web.archive.org/story"),
        ),
    ):
        assert "Archived story" in ladder_core.bypass_paywall(
            "https://example.com", ladder_core._DEFAULT_STRINGS
        )
    with (
        patch.object(ladder_core, "fetch_via_freedium", return_value=(None, None)),
        patch.object(ladder_core, "fetch_via_archive_org", return_value=(None, None)),
        patch.object(ladder_core, "fetch_via_archive_ph", return_value=(None, None)),
    ):
        with pytest.raises(ladder_core.UserFacingError):
            ladder_core.bypass_paywall("https://medium.com/story", ladder_core._DEFAULT_STRINGS)


def test_timeout_and_bad_payload(reader):
    _, client, headers = reader
    assert client.post("/api/13ft", json=[], headers=headers).status_code == 400
    with patch("twitter_articlenator.sources.ladder_http.validate_url", side_effect=TimeoutError):
        assert (
            client.post("/api/13ft", json={"url": "example.com"}, headers=headers).status_code
            == 502
        )


def test_inline_script_processing():
    from twitter_articlenator.sources.ladder_core import process_html_document

    assert "text" in process_html_document(
        "<p>text</p><script>var a=1;</script>", "https://example.com"
    )


def test_pdf_export_uses_current_article_without_refetch(reader):
    import io
    from pypdf import PdfReader

    app, client, headers = reader
    payload = {
        "title": "Saved article",
        "url": "https://example.com/story",
        "blocks": [
            {"tag": "h2", "text": "A section"},
            {"tag": "p", "text": "The already fetched article body."},
        ],
    }
    assert app.test_client().post("/api/13ft/pdf", json=payload).status_code == 401
    assert client.post("/api/13ft/pdf", json=payload).status_code == 403
    with patch("twitter_articlenator.sources.ladder_core.bypass_paywall") as fetch:
        response = client.post("/api/13ft/pdf", json=payload, headers=headers)
        fetch.assert_not_called()
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert "attachment;" in response.headers["Content-Disposition"]
    assert response.data.startswith(b"%PDF-")
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(response.data)).pages)
    assert "Saved article" in text
    assert "already fetched article body" in text
    assert "https://example.com/story" in text


def test_pdf_export_response_has_explicit_length(reader):
    """Without Content-Length the response counts as streamed, which defers
    the download lease to response close."""
    _, client, headers = reader
    payload = {
        "title": "Saved article",
        "url": "https://example.com/story",
        "blocks": [{"tag": "p", "text": "Body text."}],
    }
    response = client.post("/api/13ft/pdf", json=payload, headers=headers)
    assert response.status_code == 200
    assert int(response.headers["Content-Length"]) == len(response.data)


def test_pdf_export_releases_download_slot(reader):
    """Regression: the PDF export used to wedge the shared download limit,
    making every later download request (e.g. YouTube) fail with 429."""
    app, client, headers = reader
    payload = {
        "title": "Saved article",
        "url": "https://example.com/story",
        "blocks": [{"tag": "p", "text": "Body text."}],
    }
    assert client.post("/api/13ft/pdf", json=payload, headers=headers).status_code == 200
    assert app.extensions["resource_limiter"].active("download")["global"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"title": "x", "url": "file:///etc/passwd", "blocks": [{"tag": "p", "text": "x"}]},
        {"title": "x", "url": "https://example.com", "blocks": []},
        {"title": "x", "url": "https://example.com", "blocks": [{"tag": "img", "text": "x"}]},
        {"title": "x", "url": "https://example.com", "blocks": [{"tag": "p", "text": 42}]},
    ],
)
def test_pdf_export_validates_payload(reader, payload):
    _, client, headers = reader
    assert client.post("/api/13ft/pdf", json=payload, headers=headers).status_code == 400


def test_pdf_export_escapes_markup(reader):
    import io
    from pypdf import PdfReader

    _, client, headers = reader
    response = client.post(
        "/api/13ft/pdf",
        headers=headers,
        json={
            "title": '<img src="file:///etc/passwd">',
            "url": "https://example.com",
            "blocks": [{"tag": "p", "text": "<script>literal text</script>"}],
        },
    )
    assert response.status_code == 200
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(response.data)).pages)
    assert "<script>literal text</script>" in text
