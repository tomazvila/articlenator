"""Tests for the public (unauthenticated) page surface.

Only genuinely public pages may be reachable without login:
- /login (and /api/health, favicon, static) - already public
- /setup - static cookie-extraction documentation

Everything else (tools, per-user data, downloads, admin) must stay gated.
"""

from __future__ import annotations

import re

import pytest

from twitter_articlenator.app import create_app

GATED_PAGES = [
    "/",
    "/bookmarks",
    "/videos",
    "/youtube",
    "/13ft",
    "/transcription",
    "/channel",
    "/admin/users",
    "/download/some-file.pdf",
]

PUBLIC_GUIDE_MARKERS = [
    "How to Get Your Cookies",
    "auth_token",
    "ct0",
]

ACCOUNT_ONLY_MARKERS = [
    'id="cookie-form-devtools"',
    'id="cookie-form-traditional"',
    "Current Cookies</h2>",
    "Test Cookies",
]


@pytest.fixture
def auth_app(tmp_path):
    app = create_app(
        test_config={
            "TESTING": True,
            "AUTH_REQUIRED": True,
            "USER_DATABASE_PATH": tmp_path / "config" / "articlenator.sqlite3",
        }
    )
    app.extensions["user_store"].create_user(
        "admin",
        "correct horse battery staple",
        is_admin=True,
    )
    return app


@pytest.fixture
def auth_client(auth_app):
    return auth_app.test_client()


def _csrf(response) -> str:
    match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def _login(client, username: str, password: str):
    token = _csrf(client.get("/login"))
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=False,
    )


# --- Positive space: the setup guide is public -------------------------------


def test_anonymous_setup_guide_is_public(auth_client):
    response = auth_client.get("/setup")

    assert response.status_code == 200
    for marker in PUBLIC_GUIDE_MARKERS:
        assert marker in response.get_data(as_text=True)


def test_anonymous_setup_guide_hides_account_sections(auth_client):
    response = auth_client.get("/setup")
    html = response.get_data(as_text=True)

    for marker in ACCOUNT_ONLY_MARKERS:
        assert marker not in html


def test_anonymous_setup_guide_links_to_login(auth_client):
    response = auth_client.get("/setup")
    html = response.get_data(as_text=True)

    assert "/login" in html
    assert "Sign in" in html


def test_anonymous_nav_shows_public_links(auth_client):
    response = auth_client.get("/setup")
    html = response.get_data(as_text=True)

    assert 'href="/setup"' in html
    assert "Sign in" in html
    # No tool links leak into the anonymous nav
    assert 'href="/bookmarks"' not in html
    assert 'href="/youtube"' not in html
    assert 'href="/transcription"' not in html
    assert 'href="/admin/users"' not in html


# --- Negative space: everything else stays gated ------------------------------


@pytest.mark.parametrize("path", GATED_PAGES)
def test_anonymous_gated_pages_redirect_to_login(auth_client, path):
    response = auth_client.get(path, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/login?next={path}")


def test_anonymous_api_requests_stay_rejected(auth_client):
    assert auth_client.get("/api/cookies/status").status_code == 401
    assert (
        auth_client.post(
            "/api/cookies/validate", json={"cookies": "auth_token=x; ct0=y"}
        ).status_code
        == 401
    )
    assert auth_client.post("/api/convert", json={}).status_code == 401


def test_anonymous_setup_does_not_leak_cookie_status(auth_client):
    """The page loads, but the status endpoint it calls stays auth-only."""
    status = auth_client.get("/api/cookies/status")

    assert status.status_code == 401


def test_health_remains_public(auth_client):
    assert auth_client.get("/api/health").status_code == 200


# --- Edge cases ---------------------------------------------------------------


def test_authenticated_setup_shows_account_sections(auth_client):
    _login(auth_client, "admin", "correct horse battery staple")
    response = auth_client.get("/setup")
    html = response.get_data(as_text=True)

    for marker in PUBLIC_GUIDE_MARKERS + ACCOUNT_ONLY_MARKERS:
        assert marker in html
    assert "Sign in to manage cookies" not in html


def test_authenticated_nav_keeps_full_menu(auth_client):
    _login(auth_client, "admin", "correct horse battery staple")
    response = auth_client.get("/setup")
    html = response.get_data(as_text=True)

    assert 'href="/bookmarks"' in html
    assert 'href="/admin/users"' in html


def test_setup_preserves_next_target_for_sign_in_link(auth_client):
    response = auth_client.get("/setup")
    html = response.get_data(as_text=True)

    assert "/login?next=/setup" in html


def test_nav_sign_in_preserves_existing_next_query_param(auth_client):
    """An anonymous visitor on /login?next=/bookmarks keeps that destination."""
    response = auth_client.get("/login?next=/bookmarks")
    html = response.get_data(as_text=True)

    assert "next=/bookmarks" in html


def test_nav_sign_in_on_login_page_does_not_self_link_without_target(auth_client):
    """On /login the nav link points at the login flow with a sane default."""
    response = auth_client.get("/login")
    html = response.get_data(as_text=True)

    assert 'href="/login?next=/"' in html


def test_setup_is_not_cacheable_even_when_public(auth_client):
    """Logged-in cookie state must never leak through shared caches."""
    response = auth_client.get("/setup")

    assert "no-store" in response.headers.get("Cache-Control", "")


def test_stale_session_user_gets_public_setup_but_gated_elsewhere(auth_client):
    """A session referencing a deleted user degrades to anonymous, not to error."""
    with auth_client.session_transaction() as session:
        session["user_id"] = "no-such-user-id"
        session["auth_version"] = 1

    assert auth_client.get("/setup").status_code == 200
    response = auth_client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_csrf_still_enforced_for_authenticated_setup_actions(auth_client):
    _login(auth_client, "admin", "correct horse battery staple")

    response = auth_client.post("/api/cookies/validate", json={"cookies": "x"})

    assert response.status_code == 403


def test_public_setup_with_auth_disabled(auth_app):
    auth_app.config["AUTH_REQUIRED"] = False
    client = auth_app.test_client()

    response = client.get("/setup")

    # Auth disabled means single-user mode: full page including account sections.
    assert response.status_code == 200
    for marker in ACCOUNT_ONLY_MARKERS:
        assert marker in response.get_data(as_text=True)
