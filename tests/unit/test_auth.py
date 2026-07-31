"""Authentication and account-management tests."""

from __future__ import annotations

import re

import pytest

from twitter_articlenator.app import create_app
from twitter_articlenator.auth import UserStore


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
    app.extensions["user_store"].create_user(
        "reader",
        "another correct horse battery staple",
    )
    return app


@pytest.fixture
def auth_client(auth_app):
    return auth_app.test_client()


def _csrf(response) -> str:
    match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def _login(client, username: str, password: str, *, next_path: str | None = None):
    path = "/login"
    if next_path is not None:
        path += f"?next={next_path}"
    token = _csrf(client.get(path))
    return client.post(
        path,
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=False,
    )


def test_user_store_hashes_password_and_authenticates_case_insensitively(tmp_path):
    store = UserStore(tmp_path / "users.sqlite3")
    store.initialize()

    created = store.create_user("Alice", "correct horse battery staple")

    assert created.username == "Alice"
    assert created.password_hash != "correct horse battery staple"
    assert store.authenticate("alice", "correct horse battery staple").id == created.id
    assert store.authenticate("ALICE", "wrong password") is None


def test_user_store_rejects_duplicate_and_weak_accounts(tmp_path):
    store = UserStore(tmp_path / "users.sqlite3")
    store.initialize()
    store.create_user("Alice", "correct horse battery staple")

    with pytest.raises(ValueError, match="already exists"):
        store.create_user("alice", "another correct horse battery staple")
    with pytest.raises(ValueError, match="at least 12"):
        store.create_user("Bob", "too-short")
    with pytest.raises(ValueError, match="letters, numbers"):
        store.create_user("../../escape", "correct horse battery staple")


def test_anonymous_pages_redirect_to_login_but_health_remains_public(auth_client):
    response = auth_client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login?next=/")
    assert auth_client.get("/api/health").status_code == 200


def test_anonymous_api_request_returns_401_json(auth_client):
    response = auth_client.post("/api/convert", json={})

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}


def test_authenticated_state_change_requires_csrf(auth_client):
    _login(auth_client, "admin", "correct horse battery staple")

    response = auth_client.post("/api/convert", json={})

    assert response.status_code == 403
    assert response.get_json() == {"error": "CSRF token missing or invalid"}


def test_login_rotates_session_and_allows_authenticated_routes(auth_client):
    with auth_client.session_transaction() as session:
        session["attacker_controlled"] = "discard me"

    response = _login(auth_client, "ADMIN", "correct horse battery staple")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    with auth_client.session_transaction() as session:
        assert "attacker_controlled" not in session
        assert session.get("user_id")
    assert auth_client.get("/").status_code == 200


def test_authenticated_pages_are_not_cacheable(auth_client):
    _login(auth_client, "admin", "correct horse battery staple")

    response = auth_client.get("/")

    assert response.headers["Cache-Control"] == "private, no-store"


def test_login_rejects_invalid_credentials_without_user_enumeration(auth_client):
    wrong_password = _login(auth_client, "admin", "wrong password")
    missing_user = _login(auth_client, "does-not-exist", "wrong password")

    assert wrong_password.status_code == 401
    assert missing_user.status_code == 401
    assert b"Invalid username or password" in wrong_password.data
    assert b"Invalid username or password" in missing_user.data


def test_login_ignores_external_next_redirect(auth_client):
    response = _login(
        auth_client,
        "admin",
        "correct horse battery staple",
        next_path="https://attacker.invalid/steal",
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_login_rate_limits_repeated_failures(auth_client):
    for _ in range(5):
        assert _login(auth_client, "admin", "wrong password").status_code == 401

    blocked = _login(auth_client, "admin", "wrong password")

    assert blocked.status_code == 429
    assert b"Too many login attempts" in blocked.data


def test_successful_login_does_not_clear_other_failures_from_same_address(auth_client):
    for _ in range(4):
        assert _login(auth_client, "admin", "wrong password").status_code == 401

    assert _login(auth_client, "reader", "another correct horse battery staple").status_code == 302
    page = auth_client.get("/")
    auth_client.post("/logout", data={"csrf_token": _csrf(page)})

    assert _login(auth_client, "admin", "wrong password").status_code == 401
    assert _login(auth_client, "admin", "wrong password").status_code == 429


def test_login_limiter_bounds_tracked_addresses():
    from twitter_articlenator.auth import LoginRateLimiter

    limiter = LoginRateLimiter(
        max_failures=5,
        window_seconds=60,
        max_tracked_addresses=2,
    )
    limiter.record_failure("198.51.100.1")
    limiter.record_failure("198.51.100.2")
    limiter.record_failure("198.51.100.3")

    assert len(limiter._failures) == 2


def test_login_rate_limit_can_use_one_trusted_forwarded_address(tmp_path):
    app = create_app(
        test_config={
            "TESTING": True,
            "AUTH_REQUIRED": True,
            "TRUST_PROXY_HEADERS": True,
            "USER_DATABASE_PATH": tmp_path / "users.sqlite3",
        }
    )
    app.extensions["user_store"].create_user(
        "admin",
        "correct horse battery staple",
        is_admin=True,
    )
    client = app.test_client()

    for _ in range(5):
        token = _csrf(client.get("/login"))
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong password",
                "csrf_token": token,
            },
            headers={"X-Forwarded-For": "198.51.100.10"},
        )
        assert response.status_code == 401

    token = _csrf(client.get("/login"))
    other_address = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "wrong password",
            "csrf_token": token,
        },
        headers={"X-Forwarded-For": "198.51.100.11"},
    )
    assert other_address.status_code == 401


def test_logout_is_post_only_and_requires_csrf(auth_client):
    _login(auth_client, "admin", "correct horse battery staple")

    assert auth_client.get("/logout").status_code == 405
    assert auth_client.post("/logout").status_code == 400
    page = auth_client.get("/")
    response = auth_client.post("/logout", data={"csrf_token": _csrf(page)})

    assert response.status_code == 302
    assert auth_client.get("/", follow_redirects=False).status_code == 302


def test_admin_can_create_accounts_but_regular_user_cannot(auth_client, auth_app):
    _login(auth_client, "reader", "another correct horse battery staple")
    assert auth_client.get("/admin/users").status_code == 403

    page = auth_client.get("/")
    auth_client.post("/logout", data={"csrf_token": _csrf(page)})
    _login(auth_client, "admin", "correct horse battery staple")
    admin_page = auth_client.get("/admin/users")
    response = auth_client.post(
        "/admin/users",
        data={
            "username": "new-user",
            "password": "a newly generated secure password",
            "csrf_token": _csrf(admin_page),
        },
    )

    assert response.status_code == 302
    assert auth_app.extensions["user_store"].get_by_username("new-user") is not None


def test_cli_can_bootstrap_an_admin(auth_app):
    runner = auth_app.test_cli_runner()

    result = runner.invoke(
        args=["users", "create", "--username", "bootstrap", "--admin"],
        input="a bootstrap password long enough\na bootstrap password long enough\n",
    )

    assert result.exit_code == 0, result.output
    assert auth_app.extensions["user_store"].get_by_username("bootstrap").is_admin is True


def test_cli_can_reset_and_revoke_an_account(auth_app):
    runner = auth_app.test_cli_runner()

    reset = runner.invoke(
        args=["users", "set-password", "--username", "reader"],
        input="a replacement password long enough\na replacement password long enough\n",
    )
    disabled = runner.invoke(args=["users", "disable", "--username", "reader"])

    assert reset.exit_code == 0, reset.output
    assert disabled.exit_code == 0, disabled.output
    store = auth_app.extensions["user_store"]
    assert store.authenticate("reader", "another correct horse battery staple") is None
    assert store.authenticate("reader", "a replacement password long enough") is None
    assert store.get_by_username("reader").is_active is False

    enabled = runner.invoke(args=["users", "enable", "--username", "reader"])
    assert enabled.exit_code == 0, enabled.output
    assert store.authenticate("reader", "a replacement password long enough") is not None


def test_password_reset_invalidates_existing_session(auth_app):
    client = auth_app.test_client()
    _login(client, "reader", "another correct horse battery staple")

    auth_app.extensions["user_store"].set_password(
        "reader",
        "a replacement password long enough",
    )

    assert client.get("/", follow_redirects=False).status_code == 302
    with client.session_transaction() as session:
        assert "user_id" not in session
