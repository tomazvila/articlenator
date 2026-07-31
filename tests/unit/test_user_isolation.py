"""Per-user credential and artifact isolation tests."""

from __future__ import annotations

import json
import re

import pytest
from cryptography.fernet import Fernet

from twitter_articlenator.app import create_app
from twitter_articlenator.credentials import TwitterCookieStore
from twitter_articlenator.sources.channel_transcription import ChannelManifest
from twitter_articlenator.sources.transcription import Manifest
from twitter_articlenator.sources.youtube_cookies import YouTubeCookieStore
from twitter_articlenator.sources.youtube_oauth import YouTubeOAuthTokenStore
from twitter_articlenator.user_data import paths_for_user

VALID_COOKIES_A = "auth_token=" + "a" * 40 + "; ct0=" + "b" * 64
VALID_COOKIES_B = "auth_token=" + "c" * 40 + "; ct0=" + "d" * 64
VALID_YOUTUBE_COOKIES = (
    "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session-value\n"
)


def _csrf(response) -> str:
    match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def _login(client, username: str, password: str) -> None:
    token = _csrf(client.get("/login"))
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
    )
    assert response.status_code == 302


def _logout(client) -> None:
    token = _csrf(client.get("/"))
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 302


@pytest.fixture
def isolated_app(tmp_path):
    output_root = tmp_path / "output"
    config_root = tmp_path / "config"
    key = Fernet.generate_key().decode()
    app = create_app(
        test_config={
            "TESTING": True,
            "AUTH_REQUIRED": True,
            "USER_DATABASE_PATH": config_root / "articlenator.sqlite3",
            "OUTPUT_ROOT": output_root,
            "CONFIG_ROOT": config_root,
            "COOKIE_ENCRYPTION_KEY": key,
            "REQUIRE_COOKIE_ENCRYPTION": True,
        }
    )
    alice = app.extensions["user_store"].create_user("alice", "correct horse battery staple")
    bob = app.extensions["user_store"].create_user("bob", "another correct horse battery staple")
    app.config["TEST_USERS"] = {"alice": alice, "bob": bob}
    return app


def test_user_paths_are_namespaced_and_do_not_use_legacy_roots(tmp_path):
    alice = paths_for_user(
        "123e4567-e89b-12d3-a456-426614174000", tmp_path / "out", tmp_path / "cfg"
    )
    bob = paths_for_user("123e4567-e89b-12d3-a456-426614174001", tmp_path / "out", tmp_path / "cfg")

    assert alice.output_dir == tmp_path / "out" / "users" / alice.user_id
    assert alice.config_dir == tmp_path / "cfg" / "users" / alice.user_id
    assert alice.output_dir != bob.output_dir
    assert alice.twitter_cookie_path != bob.twitter_cookie_path
    assert alice.youtube_cookie_path != bob.youtube_cookie_path
    assert alice.youtube_oauth_token_path != bob.youtube_oauth_token_path


def test_twitter_cookie_store_encrypts_at_rest(tmp_path):
    path = tmp_path / "twitter-cookies.enc"
    store = TwitterCookieStore(
        path=path,
        encryption_key=Fernet.generate_key().decode(),
        require_encryption=True,
    )

    status = store.save(VALID_COOKIES_A)

    assert store.read() == VALID_COOKIES_A
    assert VALID_COOKIES_A.encode() not in path.read_bytes()
    assert status == {
        "configured": True,
        "encrypted": True,
        "cookie_names": ["auth_token", "ct0"],
    }


def test_twitter_cookie_store_refuses_plaintext_when_encryption_required(tmp_path):
    store = TwitterCookieStore(
        path=tmp_path / "twitter-cookies.enc",
        encryption_key=None,
        require_encryption=True,
    )

    with pytest.raises(ValueError, match="encryption key is required"):
        store.save(VALID_COOKIES_A)


def test_twitter_cookie_endpoints_are_isolated_between_users(isolated_app):
    client = isolated_app.test_client()
    _login(client, "alice", "correct horse battery staple")
    token = _csrf(client.get("/setup"))
    saved = client.post(
        "/api/cookies/validate",
        json={"cookies": VALID_COOKIES_A},
        headers={"X-CSRF-Token": token},
    )
    assert saved.status_code == 200
    assert saved.get_json()["configured"] is True
    _logout(client)

    _login(client, "bob", "another correct horse battery staple")
    assert client.get("/api/cookies/status").get_json()["configured"] is False
    token = _csrf(client.get("/setup"))
    client.post(
        "/api/cookies/validate",
        json={"cookies": VALID_COOKIES_B},
        headers={"X-CSRF-Token": token},
    )

    users = isolated_app.config["TEST_USERS"]
    alice_paths = paths_for_user(
        users["alice"].id,
        isolated_app.config["OUTPUT_ROOT"],
        isolated_app.config["CONFIG_ROOT"],
    )
    bob_paths = paths_for_user(
        users["bob"].id,
        isolated_app.config["OUTPUT_ROOT"],
        isolated_app.config["CONFIG_ROOT"],
    )
    assert (
        TwitterCookieStore(
            path=alice_paths.twitter_cookie_path,
            encryption_key=isolated_app.config["COOKIE_ENCRYPTION_KEY"],
            require_encryption=True,
        ).read()
        == VALID_COOKIES_A
    )
    assert (
        TwitterCookieStore(
            path=bob_paths.twitter_cookie_path,
            encryption_key=isolated_app.config["COOKIE_ENCRYPTION_KEY"],
            require_encryption=True,
        ).read()
        == VALID_COOKIES_B
    )


def test_downloads_cannot_cross_user_boundaries(isolated_app):
    users = isolated_app.config["TEST_USERS"]
    alice_paths = paths_for_user(
        users["alice"].id,
        isolated_app.config["OUTPUT_ROOT"],
        isolated_app.config["CONFIG_ROOT"],
    )
    alice_paths.output_dir.mkdir(parents=True)
    (alice_paths.output_dir / "private.pdf").write_bytes(b"alice-only")

    client = isolated_app.test_client()
    _login(client, "bob", "another correct horse battery staple")

    assert client.get("/download/private.pdf").status_code == 404


def test_youtube_in_memory_jobs_cannot_cross_user_boundaries(isolated_app):
    import twitter_articlenator.routes.api as api_routes

    users = isolated_app.config["TEST_USERS"]
    alice_paths = paths_for_user(
        users["alice"].id,
        isolated_app.config["OUTPUT_ROOT"],
        isolated_app.config["CONFIG_ROOT"],
    )
    job = api_routes.YouTubeDownloadJob(
        links=["https://www.youtube.com/watch?v=abcdEFGHijk"],
        mode="video",
        user_id=users["alice"].id,
        output_root=alice_paths.youtube_dir,
        cookie_store=YouTubeCookieStore(
            cookie_path=alice_paths.youtube_cookie_path,
            encryption_key=isolated_app.config["COOKIE_ENCRYPTION_KEY"],
            require_encryption=True,
            max_bytes=262144,
        ),
    )
    with api_routes._youtube_download_jobs_lock:
        api_routes._youtube_download_jobs[job.job_id] = job
    try:
        client = isolated_app.test_client()
        _login(client, "alice", "correct horse battery staple")
        assert client.get(f"/api/youtube/download/jobs/{job.job_id}").status_code == 200
        _logout(client)

        _login(client, "bob", "another correct horse battery staple")
        assert client.get(f"/api/youtube/download/jobs/{job.job_id}").status_code == 404
    finally:
        with api_routes._youtube_download_jobs_lock:
            api_routes._youtube_download_jobs.pop(job.job_id, None)


def test_youtube_cookie_status_is_per_user(isolated_app):
    users = isolated_app.config["TEST_USERS"]
    alice_paths = paths_for_user(
        users["alice"].id,
        isolated_app.config["OUTPUT_ROOT"],
        isolated_app.config["CONFIG_ROOT"],
    )
    YouTubeCookieStore(
        cookie_path=alice_paths.youtube_cookie_path,
        encryption_key=isolated_app.config["COOKIE_ENCRYPTION_KEY"],
        require_encryption=True,
        max_bytes=262144,
    ).save(VALID_YOUTUBE_COOKIES)
    YouTubeOAuthTokenStore(
        token_path=alice_paths.youtube_oauth_token_path,
        encryption_key=isolated_app.config["COOKIE_ENCRYPTION_KEY"],
        require_encryption=True,
    ).save_authorized_token(
        {
            "access_token": "alice-access-token",
            "refresh_token": "alice-refresh-token",
            "expires_in": 3600,
        }
    )

    client = isolated_app.test_client()
    _login(client, "alice", "correct horse battery staple")
    assert client.get("/api/youtube/cookies/status").get_json()["configured"] is True
    assert client.get("/api/youtube/oauth/status").get_json()["configured"] is True
    _logout(client)

    _login(client, "bob", "another correct horse battery staple")
    assert client.get("/api/youtube/cookies/status").get_json()["configured"] is False
    assert client.get("/api/youtube/oauth/status").get_json()["configured"] is False


def test_sessions_and_persisted_jobs_cannot_cross_user_boundaries(isolated_app):
    users = isolated_app.config["TEST_USERS"]
    alice_paths = paths_for_user(
        users["alice"].id,
        isolated_app.config["OUTPUT_ROOT"],
        isolated_app.config["CONFIG_ROOT"],
    )
    session_id = "alice-session"
    session_dir = alice_paths.sessions_dir / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "_meta.json").write_text(
        json.dumps(
            {
                "urls": ["https://example.com/alice"],
                "total": 1,
                "status": "running",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )

    transcription_id = "a" * 32
    transcription_dir = alice_paths.transcription_dir / transcription_id
    transcription_dir.mkdir(parents=True)
    (transcription_dir / "manifest.json").write_text(
        json.dumps(
            Manifest(
                job_id=transcription_id,
                url="https://www.youtube.com/watch?v=abcdEFGHijk",
            ).to_dict()
        )
    )

    channel_id = "b" * 32
    channel_dir = alice_paths.channel_dir / channel_id
    channel_dir.mkdir(parents=True)
    (channel_dir / "manifest.json").write_text(
        json.dumps(
            ChannelManifest(
                job_id=channel_id,
                url="https://www.youtube.com/@alice",
            ).to_dict()
        )
    )

    client = isolated_app.test_client()
    _login(client, "alice", "correct horse battery staple")
    assert client.get(f"/api/sessions/{session_id}").status_code == 200
    assert client.get(f"/api/transcription/{transcription_id}").status_code == 200
    assert client.get(f"/api/channel/{channel_id}").status_code == 200
    _logout(client)

    _login(client, "bob", "another correct horse battery staple")
    assert client.get("/api/sessions").get_json() == {"sessions": []}
    assert client.get(f"/api/sessions/{session_id}").status_code == 404
    assert client.get("/api/transcriptions").get_json() == {"jobs": []}
    assert client.get(f"/api/transcription/{transcription_id}").status_code == 404
    assert client.get("/api/channels").get_json() == {"jobs": []}
    assert client.get(f"/api/channel/{channel_id}").status_code == 404


def test_transcription_limits_apply_per_user_and_globally(isolated_app, monkeypatch):
    import twitter_articlenator.routes.transcription as transcription_routes

    leases = []

    def capture_start(*args, **kwargs):
        leases.append(kwargs["lease"])

    monkeypatch.setattr(transcription_routes, "_start_thread", capture_start)
    client = isolated_app.test_client()
    _login(client, "alice", "correct horse battery staple")
    token = _csrf(client.get("/transcription"))
    payload = {"url": "https://www.youtube.com/watch?v=abcdEFGHijk"}

    assert (
        client.post(
            "/api/transcription",
            json=payload,
            headers={"X-CSRF-Token": token},
        ).status_code
        == 202
    )
    assert (
        client.post(
            "/api/transcription",
            json=payload,
            headers={"X-CSRF-Token": token},
        ).status_code
        == 429
    )
    _logout(client)

    _login(client, "bob", "another correct horse battery staple")
    token = _csrf(client.get("/transcription"))
    assert (
        client.post(
            "/api/transcription",
            json=payload,
            headers={"X-CSRF-Token": token},
        ).status_code
        == 429
    )

    leases[0].release()
    assert (
        client.post(
            "/api/transcription",
            json=payload,
            headers={"X-CSRF-Token": token},
        ).status_code
        == 202
    )
    leases[-1].release()


def test_streaming_playwright_limit_is_held_until_response_closes(isolated_app):
    client = isolated_app.test_client()
    _login(client, "alice", "correct horse battery staple")
    token = _csrf(client.get("/"))
    request_kwargs = {
        "json": {"links": ["unsupported://example"]},
        "headers": {"X-CSRF-Token": token},
        "buffered": False,
    }

    response = client.post("/api/convert/stream", **request_kwargs)
    assert response.status_code == 200
    assert isolated_app.extensions["resource_limiter"].active("playwright") == {
        "global": 1,
        "users": {isolated_app.config["TEST_USERS"]["alice"].id: 1},
    }
    assert client.post("/api/convert/stream", **request_kwargs).status_code == 429

    response.close()
    assert isolated_app.extensions["resource_limiter"].active("playwright") == {
        "global": 0,
        "users": {},
    }


def test_download_limits_cover_twitter_and_youtube_jobs(isolated_app):
    client = isolated_app.test_client()
    _login(client, "alice", "correct horse battery staple")
    token = _csrf(client.get("/videos"))
    user_id = isolated_app.config["TEST_USERS"]["alice"].id
    lease = isolated_app.extensions["resource_limiter"].try_acquire("download", user_id)
    assert lease is not None

    headers = {"X-CSRF-Token": token}
    try:
        twitter_response = client.post(
            "/api/videos/download",
            json={"links": ["https://x.com/example/status/123"]},
            headers=headers,
        )
        youtube_response = client.post(
            "/api/youtube/download",
            json={
                "links": ["https://www.youtube.com/watch?v=abcdEFGHijk"],
                "mode": "video",
            },
            headers=headers,
        )
    finally:
        lease.release()

    assert twitter_response.status_code == 429
    assert twitter_response.get_json()["resource"] == "download"
    assert youtube_response.status_code == 429
    assert youtube_response.get_json()["resource"] == "download"
