"""Integration tests for Flask application."""

import json

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Create Flask test application with temp directories."""
    monkeypatch.setenv("TWITTER_ARTICLENATOR_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")

    import twitter_articlenator.config as config_module

    config_module._config_instance = None

    from twitter_articlenator.app import create_app

    app = create_app(test_config={"TESTING": True})
    yield app


@pytest.fixture
def client(app):
    """Create Flask test client."""
    return app.test_client()


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

    def test_security_headers_on_html_pages(self, client):
        """Test security headers are set on HTML pages too."""
        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"


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
