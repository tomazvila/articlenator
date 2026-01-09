"""Integration tests for Flask application."""

import json

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Create Flask test application with temp directories."""
    # Set temp directories for testing
    monkeypatch.setenv("TWITTER_ARTICLENATOR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TWITTER_ARTICLENATOR_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")

    # Clear config singleton
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

        # Should mention at least one browser
        browsers = ["chrome", "firefox", "safari", "browser"]
        assert any(browser in html.lower() for browser in browsers)

    def test_setup_has_form_for_cookies(self, client):
        """Test setup page has form to submit cookies."""
        response = client.get("/setup")
        html = response.data.decode("utf-8")

        assert "<form" in html.lower()


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
        # Should return error for unsupported URL scheme
        assert response.status_code == 400

    def test_convert_accepts_valid_twitter_url(self, client):
        """Test /api/convert accepts valid Twitter URL format."""
        # This will fail without cookies, but should not fail URL validation
        response = client.post("/api/convert", json={"links": ["https://x.com/user/status/123"]})
        # Should fail because no cookies, not because of URL validation
        data = json.loads(response.data)
        # Either 400 (no cookies) or a message about cookies
        assert response.status_code in [400, 401, 500] or "cookie" in str(data).lower()

    def test_convert_returns_json(self, client):
        """Test /api/convert returns JSON response."""
        response = client.post("/api/convert", json={"links": ["https://x.com/user/status/123"]})
        assert response.content_type == "application/json"


class TestCookiesStatusRoute:
    """Tests for GET /api/cookies/status route."""

    def test_status_returns_not_configured_when_no_cookies(self, client):
        """Test status returns not_configured when no cookies saved."""
        response = client.get("/api/cookies/status")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["configured"] is False
        assert data["status"] == "not_configured"

    def test_status_returns_configured_after_saving_cookies(self, client):
        """Test status returns configured after cookies are saved."""
        # First save cookies
        client.post("/api/cookies", json={"cookies": "auth_token=test; ct0=test"})

        # Check status
        response = client.get("/api/cookies/status")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["configured"] is True
        assert data["status"] == "configured"

    def test_status_returns_json(self, client):
        """Test status endpoint returns JSON."""
        response = client.get("/api/cookies/status")
        assert response.content_type == "application/json"

    def test_status_with_test_param_not_configured(self, client):
        """Test status with test=true when no cookies configured."""
        response = client.get("/api/cookies/status?test=true")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["configured"] is False
        assert data["status"] == "not_configured"

    def test_status_with_test_validates_valid_cookies(self, client):
        """Test status with test=true validates properly formatted cookies."""
        # Save valid cookies (long enough tokens)
        valid_cookies = "auth_token=" + "a" * 30 + "; ct0=" + "b" * 30
        client.post("/api/cookies", json={"cookies": valid_cookies})

        response = client.get("/api/cookies/status?test=true")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["configured"] is True
        assert data["status"] == "working"

    def test_status_with_test_rejects_short_tokens(self, client):
        """Test status with test=true rejects short tokens."""
        # Save cookies with short tokens
        client.post("/api/cookies", json={"cookies": "auth_token=short; ct0=short"})

        response = client.get("/api/cookies/status?test=true")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["status"] == "invalid"


class TestCookiesCurrentRoute:
    """Tests for GET /api/cookies/current route."""

    def test_current_returns_not_configured_when_empty(self, client):
        """Test current returns not configured when no cookies."""
        response = client.get("/api/cookies/current")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["configured"] is False
        assert data["cookies"] == []

    def test_current_returns_masked_cookies(self, client):
        """Test current masks cookie values for security."""
        client.post(
            "/api/cookies", json={"cookies": "auth_token=secretvalue123456; ct0=anothersecret789"}
        )

        response = client.get("/api/cookies/current")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["configured"] is True
        assert len(data["cookies"]) == 2

        # Check that values are masked
        for cookie in data["cookies"]:
            assert "..." in cookie["value_masked"]
            assert "length" in cookie

    def test_current_returns_json(self, client):
        """Test current endpoint returns JSON."""
        response = client.get("/api/cookies/current")
        assert response.content_type == "application/json"


class TestCookiesRoute:
    """Tests for POST /api/cookies route."""

    def test_save_cookies_stores_cookies(self, client, tmp_path):
        """Test POST /api/cookies stores cookies."""
        response = client.post("/api/cookies", json={"cookies": "auth_token=abc123; ct0=xyz789"})
        assert response.status_code == 200

    def test_save_cookies_requires_cookies_field(self, client):
        """Test POST /api/cookies requires cookies field."""
        response = client.post("/api/cookies", json={})
        assert response.status_code == 400

    def test_save_cookies_rejects_empty_cookies(self, client):
        """Test POST /api/cookies rejects empty cookies."""
        response = client.post("/api/cookies", json={"cookies": ""})
        assert response.status_code == 400

    def test_save_cookies_returns_success_message(self, client):
        """Test POST /api/cookies returns success message."""
        response = client.post("/api/cookies", json={"cookies": "auth_token=test; ct0=test"})
        data = json.loads(response.data)
        assert "success" in str(data).lower() or "saved" in str(data).lower()


class TestDownloadRoute:
    """Tests for GET /download/<filename> route."""

    def test_download_returns_404_for_missing(self, client):
        """Test download returns 404 for missing file."""
        response = client.get("/download/nonexistent.pdf")
        assert response.status_code == 404

    def test_download_rejects_path_traversal(self, client):
        """Test download rejects path traversal attempts."""
        response = client.get("/download/../../../etc/passwd")
        # Should return 404 or 400, not serve the file
        assert response.status_code in [400, 404]

    def test_download_only_serves_pdfs(self, client, tmp_path, monkeypatch):
        """Test download only serves PDF files."""
        # Create a non-PDF file in output dir
        output_dir = tmp_path / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "test.txt").write_text("not a pdf")

        response = client.get("/download/test.txt")
        # Should not serve non-PDF files
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
        # Should fail because no cookies, not because of form parsing
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
        # Should process both links (fail on cookies)
        assert response.status_code in [400, 500]

    def test_cookies_accepts_form_data(self, client):
        """Test /api/cookies accepts form data."""
        response = client.post(
            "/api/cookies",
            data={"cookies": "auth_token=test; ct0=test"},
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code == 200


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
