"""Integration tests for Flask application."""

import json
import re

import pytest

SAMPLE_YOUTUBE_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session-value\n"
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Create Flask test application with temp directories."""
    monkeypatch.setenv("TWITTER_ARTICLENATOR_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")
    monkeypatch.setenv(
        "TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    )

    import twitter_articlenator.config as config_module

    config_module._config_instance = None

    from twitter_articlenator.app import create_app

    app = create_app(test_config={"TESTING": True})
    yield app


@pytest.fixture
def client(app):
    """Create Flask test client."""
    return app.test_client()


def csrf_headers(client) -> dict[str, str]:
    """Return headers with the current session CSRF token."""
    response = client.get("/youtube")
    html = response.get_data(as_text=True)
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', html).group(1)
    return {"X-CSRF-Token": token}


class TestIndexRoute:
    """Tests for GET / route."""

    def test_index_returns_200(self, client):
        """Test GET / returns 200."""
        response = client.get("/")
        assert response.status_code == 200

    def test_index_contains_form(self, client):
        """Test index page contains link input form."""
        response = client.get("/")
        html = response.data.decode("utf-8")

        assert "<form" in html.lower()
        assert "links" in html.lower() or "url" in html.lower()

    def test_index_contains_textarea(self, client):
        """Test index page has textarea for multiple links."""
        response = client.get("/")
        html = response.data.decode("utf-8")

        assert "<textarea" in html.lower() or "<input" in html.lower()

    def test_index_has_submit_button(self, client):
        """Test index page has submit button."""
        response = client.get("/")
        html = response.data.decode("utf-8")

        assert "submit" in html.lower() or "convert" in html.lower()


class TestSetupRoute:
    """Tests for GET /setup route."""

    def test_setup_returns_200(self, client):
        """Test GET /setup returns 200."""
        response = client.get("/setup")
        assert response.status_code == 200

    def test_setup_contains_instructions(self, client):
        """Test setup page contains cookie instructions."""
        response = client.get("/setup")
        html = response.data.decode("utf-8")

        assert "cookie" in html.lower()

    def test_setup_mentions_browser(self, client):
        """Test setup page mentions browser."""
        response = client.get("/setup")
        html = response.data.decode("utf-8")

        browsers = ["chrome", "firefox", "safari", "browser"]
        assert any(browser in html.lower() for browser in browsers)

    def test_setup_has_form_for_cookies(self, client):
        """Test setup page has form to submit cookies."""
        response = client.get("/setup")
        html = response.data.decode("utf-8")

        assert "<form" in html.lower()


class TestBookmarksRoute:
    """Tests for GET /bookmarks route."""

    def test_bookmarks_returns_200(self, client):
        """Test GET /bookmarks returns 200."""
        response = client.get("/bookmarks")
        assert response.status_code == 200

    def test_bookmarks_contains_fetch_button(self, client):
        """Test bookmarks page has fetch button."""
        response = client.get("/bookmarks")
        html = response.data.decode("utf-8")
        assert "fetch" in html.lower()

    def test_bookmarks_contains_convert_button(self, client):
        """Test bookmarks page has convert button."""
        response = client.get("/bookmarks")
        html = response.data.decode("utf-8")
        assert "convert" in html.lower()


class TestYouTubeRoute:
    """Tests for GET /youtube route."""

    def test_youtube_returns_200(self, client):
        """Test GET /youtube returns 200."""
        response = client.get("/youtube")
        assert response.status_code == 200

    def test_youtube_contains_download_form(self, client):
        """Test YouTube page has expected form controls."""
        response = client.get("/youtube")
        html = response.data.decode("utf-8")

        assert "youtube-download-form" in html
        assert "youtube-links-input" in html
        assert "mode-mp3" in html


class TestHealthRoute:
    """Tests for GET /api/health route."""

    def test_health_returns_200(self, client):
        """Test GET /api/health returns 200."""
        response = client.get("/api/health")
        assert response.status_code == 200

    def test_health_returns_json(self, client):
        """Test health endpoint returns JSON."""
        response = client.get("/api/health")
        assert response.content_type == "application/json"

    def test_health_contains_status(self, client):
        """Test health response contains status field."""
        response = client.get("/api/health")
        data = json.loads(response.data)

        assert "status" in data
        assert data["status"] == "ok"


class TestConvertRoute:
    """Tests for POST /api/convert route."""

    def test_convert_requires_links(self, client):
        """Test POST /api/convert requires links parameter."""
        response = client.post("/api/convert", json={})
        assert response.status_code == 400

    def test_convert_rejects_empty_links(self, client):
        """Test /api/convert rejects empty links list."""
        response = client.post("/api/convert", json={"links": []})
        assert response.status_code == 400

    def test_convert_validates_urls(self, client):
        """Test /api/convert validates URLs."""
        response = client.post("/api/convert", json={"links": ["ftp://invalid-protocol.com/file"]})
        assert response.status_code == 400

    def test_convert_accepts_valid_twitter_url(self, client):
        """Test /api/convert accepts valid Twitter URL format."""
        response = client.post("/api/convert", json={"links": ["https://x.com/user/status/123"]})
        data = json.loads(response.data)
        assert response.status_code in [400, 401, 500] or "cookie" in str(data).lower()

    def test_convert_returns_json(self, client):
        """Test /api/convert returns JSON response."""
        response = client.post("/api/convert", json={"links": ["https://x.com/user/status/123"]})
        assert response.content_type == "application/json"

    def test_convert_with_cookies_in_body(self, client):
        """Test /api/convert reads cookies from request body."""
        response = client.post(
            "/api/convert",
            json={
                "links": ["https://x.com/user/status/123"],
                "cookies": "auth_token=abc123; ct0=xyz789",
            },
        )
        # Should not complain about missing cookies
        data = json.loads(response.data)
        assert "cookie" not in str(data.get("error", "")).lower() or response.status_code == 500


class TestCookiesValidateRoute:
    """Tests for POST /api/cookies/validate route."""

    def test_validate_with_no_cookies(self, client):
        """Test validation with no cookies."""
        response = client.post("/api/cookies/validate", json={})
        assert response.status_code == 200

        data = json.loads(response.data)
        assert not data["valid"]
        assert data["status"] == "not_configured"

    def test_validate_with_valid_cookies(self, client):
        """Test validation with valid cookies."""
        valid_cookies = "auth_token=" + "a" * 30 + "; ct0=" + "b" * 30
        response = client.post("/api/cookies/validate", json={"cookies": valid_cookies})
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["valid"]
        assert data["status"] == "valid"

    def test_validate_with_short_tokens(self, client):
        """Test validation rejects short tokens."""
        response = client.post(
            "/api/cookies/validate", json={"cookies": "auth_token=short; ct0=short"}
        )
        assert response.status_code == 200

        data = json.loads(response.data)
        assert not data["valid"]
        assert data["status"] == "invalid"

    def test_validate_returns_json(self, client):
        """Test validate endpoint returns JSON."""
        response = client.post("/api/cookies/validate", json={})
        assert response.content_type == "application/json"


class TestDownloadRoute:
    """Tests for GET /download/<filename> route."""

    def test_download_returns_404_for_missing(self, client):
        """Test download returns 404 for missing file."""
        response = client.get("/download/nonexistent.pdf")
        assert response.status_code == 404

    def test_download_rejects_path_traversal(self, client):
        """Test download rejects path traversal attempts."""
        response = client.get("/download/../../../etc/passwd")
        assert response.status_code in [400, 404]

    def test_download_only_serves_pdfs(self, client, tmp_path, monkeypatch):
        """Test download only serves PDF files."""
        output_dir = tmp_path / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "test.txt").write_text("not a pdf")

        response = client.get("/download/test.txt")
        assert response.status_code in [400, 404]


class TestYouTubeDownloadRoute:
    """Tests for GET /download/youtube/<mode>/<filename> route."""

    def test_download_youtube_rejects_invalid_mode(self, client):
        """Test YouTube download route rejects invalid mode."""
        response = client.get("/download/youtube/other/file.mp4")
        assert response.status_code == 400

    def test_download_youtube_rejects_path_traversal(self, client):
        """Test YouTube download route rejects path traversal attempts."""
        response = client.get("/download/youtube/video/../../../etc/passwd")
        assert response.status_code in [400, 404]

    def test_download_youtube_serves_mp4(self, client, tmp_path, monkeypatch):
        """Test YouTube video download route serves MP4 files."""
        output_dir = tmp_path / "output"
        video_dir = output_dir / "youtube" / "videos"
        video_dir.mkdir(parents=True, exist_ok=True)
        (video_dir / "sample.mp4").write_bytes(b"fake mp4")

        monkeypatch.setenv("TWITTER_ARTICLENATOR_OUTPUT_DIR", str(output_dir))
        import twitter_articlenator.config as config_module

        config_module._config_instance = None

        response = client.get("/download/youtube/video/sample.mp4")
        assert response.status_code == 200
        assert response.content_type == "video/mp4"

    def test_download_youtube_serves_mp3(self, client, tmp_path, monkeypatch):
        """Test YouTube audio download route serves MP3 files."""
        output_dir = tmp_path / "output"
        audio_dir = output_dir / "youtube" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "sample.mp3").write_bytes(b"fake mp3")

        monkeypatch.setenv("TWITTER_ARTICLENATOR_OUTPUT_DIR", str(output_dir))
        import twitter_articlenator.config as config_module

        config_module._config_instance = None

        response = client.get("/download/youtube/audio/sample.mp3")
        assert response.status_code == 200
        assert response.content_type == "audio/mpeg"

    def test_download_youtube_rejects_wrong_extension(self, client):
        """Test YouTube download route rejects unsupported extensions."""
        response = client.get("/download/youtube/audio/sample.mp4")
        assert response.status_code == 400


class TestYouTubeDownloadApi:
    """Tests for POST /api/youtube/download validation."""

    def test_youtube_download_requires_links(self, client):
        """Test YouTube download API requires links."""
        response = client.post(
            "/api/youtube/download",
            json={"links": []},
            headers=csrf_headers(client),
        )
        assert response.status_code == 400

    def test_youtube_download_rejects_invalid_mode(self, client):
        """Test YouTube download API rejects invalid mode."""
        response = client.post(
            "/api/youtube/download",
            json={"links": ["https://youtu.be/abc"], "mode": "wav"},
            headers=csrf_headers(client),
        )
        assert response.status_code == 400

    def test_youtube_download_rejects_non_youtube_url(self, client):
        """Test YouTube download API validates URL type."""
        response = client.post(
            "/api/youtube/download",
            json={"links": ["https://example.com/watch?v=abc"], "mode": "video"},
            headers=csrf_headers(client),
        )
        assert response.status_code == 400

    def test_youtube_download_rejects_raw_cookie_payload(self, client):
        """Test YouTube download API no longer accepts raw cookies."""
        response = client.post(
            "/api/youtube/download",
            json={
                "links": ["https://youtu.be/abc"],
                "mode": "video",
                "cookies": SAMPLE_YOUTUBE_COOKIES,
            },
            headers=csrf_headers(client),
        )
        assert response.status_code == 400
        assert "Upload YouTube cookies" in response.get_json()["error"]

    def test_youtube_download_requires_csrf(self, client):
        """Test YouTube download API requires CSRF."""
        response = client.post(
            "/api/youtube/download",
            json={"links": ["https://youtu.be/abc"], "mode": "video"},
        )
        assert response.status_code == 403


class TestYouTubeCookiesApi:
    """Tests for YouTube cookie management API."""

    def test_status_is_metadata_only_without_cookie(self, client):
        """Test status returns no raw cookie values."""
        response = client.get("/api/youtube/cookies/status")
        assert response.status_code == 200
        data = response.get_json()
        assert data["configured"] is False
        assert "secret-session-value" not in response.get_data(as_text=True)

    def test_upload_valid_cookies_and_status_is_metadata_only(self, client):
        """Test uploading valid cookies stores metadata only."""
        response = client.post(
            "/api/youtube/cookies",
            data={"cookies": SAMPLE_YOUTUBE_COOKIES},
            headers=csrf_headers(client),
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["configured"] is True
        assert data["encrypted"] is True
        assert data["cookie_count"] == 1
        assert data["youtube_cookie_count"] == 1
        assert "secret-session-value" not in response.get_data(as_text=True)

        status = client.get("/api/youtube/cookies/status")
        assert status.status_code == 200
        assert "secret-session-value" not in status.get_data(as_text=True)

    def test_upload_requires_csrf(self, client):
        """Test cookie upload requires CSRF."""
        response = client.post("/api/youtube/cookies", data={"cookies": SAMPLE_YOUTUBE_COOKIES})
        assert response.status_code == 403

    def test_upload_rejects_malformed_cookies(self, client):
        """Test malformed cookie text is rejected."""
        response = client.post(
            "/api/youtube/cookies",
            data={"cookies": "not\ta\tnetscape\tcookie"},
            headers=csrf_headers(client),
        )
        assert response.status_code == 400

    def test_delete_removes_cookie_status(self, client):
        """Test delete removes stored YouTube cookies."""
        headers = csrf_headers(client)
        upload = client.post(
            "/api/youtube/cookies",
            data={"cookies": SAMPLE_YOUTUBE_COOKIES},
            headers=headers,
        )
        assert upload.status_code == 200

        response = client.delete("/api/youtube/cookies", headers=headers)
        assert response.status_code == 200
        assert response.get_json()["configured"] is False

    def test_verify_without_cookies_returns_404(self, client):
        """Test verify requires stored cookies."""
        response = client.post("/api/youtube/cookies/verify", headers=csrf_headers(client))
        assert response.status_code == 404


class TestSecurityHeaders:
    """Tests for security headers on all responses."""

    def test_x_content_type_options_header(self, client):
        """Test X-Content-Type-Options header is set."""
        response = client.get("/api/health")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options_header(self, client):
        """Test X-Frame-Options header is set."""
        response = client.get("/api/health")
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_x_xss_protection_header(self, client):
        """Test X-XSS-Protection header is set."""
        response = client.get("/api/health")
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_referrer_policy_header(self, client):
        """Test Referrer-Policy header is set."""
        response = client.get("/api/health")
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_permissions_policy_header(self, client):
        """Test Permissions-Policy header is set."""
        response = client.get("/api/health")
        assert "geolocation=()" in response.headers.get("Permissions-Policy", "")

    def test_content_security_policy_header(self, client):
        """Test Content-Security-Policy header is set with a script nonce."""
        response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "script-src 'self' 'nonce-" in csp
        assert "frame-ancestors 'none'" in csp

    def test_security_headers_on_html_pages(self, client):
        """Test security headers are set on HTML pages too."""
        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_session_cookie_flags_in_production_style_config(self, monkeypatch):
        """Test production-style session cookie settings are hardened."""
        monkeypatch.setenv("TWITTER_ARTICLENATOR_SESSION_COOKIE_SECURE", "true")
        monkeypatch.setenv("TWITTER_ARTICLENATOR_SECRET_KEY", "test-secret")

        from twitter_articlenator.app import create_app

        app = create_app(test_config={"TESTING": True})
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
        assert app.config["SESSION_COOKIE_SECURE"] is True


class TestFormDataHandling:
    """Tests for form data (non-JSON) handling."""

    def test_convert_accepts_form_data(self, client):
        """Test /api/convert accepts form data."""
        response = client.post(
            "/api/convert",
            data={"links": "https://x.com/user/status/123"},
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code in [400, 500]
        data = json.loads(response.data)
        assert "cookie" in str(data).lower() or "error" in data

    def test_convert_parses_multiline_links(self, client):
        """Test /api/convert parses newline-separated links."""
        response = client.post(
            "/api/convert",
            data={"links": "https://x.com/user/status/123\nhttps://x.com/user/status/456"},
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code in [400, 500]

    def test_validate_accepts_form_data(self, client):
        """Test /api/cookies/validate accepts form data."""
        response = client.post(
            "/api/cookies/validate",
            data={"cookies": "auth_token=" + "a" * 30 + "; ct0=" + "b" * 30},
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["valid"]


class TestAppFactory:
    """Tests for create_app function."""

    def test_create_app_returns_flask_app(self, tmp_path, monkeypatch):
        """Test create_app returns Flask application."""
        monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")

        from twitter_articlenator.app import create_app

        app = create_app(test_config={"TESTING": True})
        assert app is not None
        assert hasattr(app, "test_client")

    def test_create_app_configures_testing_mode(self, tmp_path, monkeypatch):
        """Test create_app sets testing mode."""
        monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")

        from twitter_articlenator.app import create_app

        app = create_app(test_config={"TESTING": True})
        assert app.config["TESTING"] is True

    def test_create_app_production_mode(self, tmp_path, monkeypatch):
        """Test create_app works in production mode."""
        monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")

        from twitter_articlenator.app import create_app

        app = create_app()
        assert app is not None

    def test_create_app_registers_blueprints(self, tmp_path, monkeypatch):
        """Test create_app registers API and pages blueprints."""
        monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")

        from twitter_articlenator.app import create_app

        app = create_app(test_config={"TESTING": True})
        blueprint_names = [bp.name for bp in app.blueprints.values()]
        assert "api" in blueprint_names
        assert "pages" in blueprint_names

    def test_create_app_stores_run_async(self, tmp_path, monkeypatch):
        """Test create_app stores run_async in config."""
        monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")

        from twitter_articlenator.app import create_app

        app = create_app(test_config={"TESTING": True})
        assert "RUN_ASYNC" in app.config
        assert callable(app.config["RUN_ASYNC"])
