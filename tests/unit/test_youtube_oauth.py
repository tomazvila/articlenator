"""Tests for YouTube OAuth token storage and liked-video retrieval."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from twitter_articlenator.sources import youtube_oauth
from twitter_articlenator.sources.youtube_cookies import ENCRYPTION_PREFIX
from twitter_articlenator.sources.youtube_oauth import (
    GOOGLE_AUTH_URL,
    YOUTUBE_READONLY_SCOPE,
    YouTubeOAuthError,
    YouTubeOAuthTokenStore,
    build_authorization_url,
    exchange_authorization_code,
    fetch_liked_videos,
)


def test_build_authorization_url_requests_readonly_offline_consent():
    """Test the Google consent URL has the required OAuth controls."""
    url = build_authorization_url(
        client_id="client-id",
        redirect_uri="https://twitter.example/api/youtube/oauth/callback",
        state="state-value",
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == GOOGLE_AUTH_URL
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://twitter.example/api/youtube/oauth/callback"]
    assert query["state"] == ["state-value"]
    assert query["scope"] == [YOUTUBE_READONLY_SCOPE]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]


def test_token_store_encrypts_token_and_status_hides_secret_values(tmp_path):
    """Test OAuth tokens are encrypted server-side and status is metadata-only."""
    store = YouTubeOAuthTokenStore(
        token_path=tmp_path / "youtube-oauth-token.json",
        encryption_key=Fernet.generate_key().decode("utf-8"),
        require_encryption=True,
    )

    status = store.save_authorized_token(
        {
            "access_token": "secret-access-token",
            "refresh_token": "secret-refresh-token",
            "expires_in": 3600,
            "scope": YOUTUBE_READONLY_SCOPE,
            "token_type": "Bearer",
        }
    )

    stored_bytes = store.token_path.read_bytes()
    assert stored_bytes.startswith(ENCRYPTION_PREFIX)
    assert b"secret-access-token" not in stored_bytes
    assert b"secret-refresh-token" not in stored_bytes
    assert status["configured"] is True
    assert status["encrypted"] is True
    assert status["has_refresh_token"] is True
    assert "access_token" not in status
    assert "refresh_token" not in status
    assert "secret-access-token" not in str(status)
    assert store.read_token()["access_token"] == "secret-access-token"


def test_fetch_liked_videos_paginates_and_returns_watch_links(monkeypatch, tmp_path):
    """Test liked-video retrieval follows YouTube Data API pagination."""
    store = YouTubeOAuthTokenStore(
        token_path=tmp_path / "youtube-oauth-token.json",
        encryption_key=None,
        require_encryption=False,
    )
    store.save_authorized_token(
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": YOUTUBE_READONLY_SCOPE,
        }
    )

    calls = []

    def fake_get(url, *, params, headers, timeout):
        calls.append({"url": url, "params": dict(params), "headers": dict(headers)})
        if "pageToken" not in params:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "video-one",
                            "snippet": {
                                "title": "First Song",
                                "channelTitle": "First Artist",
                            },
                        },
                        {
                            "id": "video-two",
                            "snippet": {
                                "title": "Second Song",
                                "channelTitle": "Second Artist",
                            },
                        },
                    ],
                    "nextPageToken": "next-page",
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "video-three",
                        "snippet": {
                            "title": "Third Song",
                            "channelTitle": "Third Artist",
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr(youtube_oauth.httpx, "get", fake_get)

    result = fetch_liked_videos(
        token_store=store,
        client_id="client-id",
        client_secret="client-secret",
        max_results=3,
    )

    assert result["count"] == 3
    assert result["links"] == [
        "https://www.youtube.com/watch?v=video-one",
        "https://www.youtube.com/watch?v=video-two",
        "https://www.youtube.com/watch?v=video-three",
    ]
    assert calls[0]["params"]["myRating"] == "like"
    assert calls[0]["params"]["part"] == "id,snippet"
    assert calls[0]["headers"]["Authorization"] == "Bearer access-token"
    assert calls[1]["params"]["pageToken"] == "next-page"


def test_fetch_liked_videos_refreshes_expired_token(monkeypatch, tmp_path):
    """Test expired access tokens are refreshed before reading liked videos."""
    store = YouTubeOAuthTokenStore(
        token_path=tmp_path / "youtube-oauth-token.json",
        encryption_key=None,
        require_encryption=False,
    )
    store.save_authorized_token(
        {
            "access_token": "expired-access-token",
            "refresh_token": "refresh-token",
            "expires_in": 0,
            "scope": YOUTUBE_READONLY_SCOPE,
        }
    )
    expired = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    token = store.read_token()
    token["expires_at"] = expired
    store._write_token(token)

    token_posts = []
    get_headers = []

    def fake_post(url, *, data, timeout):
        token_posts.append({"url": url, "data": dict(data)})
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-access-token",
                "expires_in": 3600,
                "scope": YOUTUBE_READONLY_SCOPE,
            },
        )

    def fake_get(url, *, params, headers, timeout):
        get_headers.append(dict(headers))
        return httpx.Response(
            200,
            json={"items": [{"id": "fresh-video", "snippet": {"title": "Fresh"}}]},
        )

    monkeypatch.setattr(youtube_oauth.httpx, "post", fake_post)
    monkeypatch.setattr(youtube_oauth.httpx, "get", fake_get)

    result = fetch_liked_videos(
        token_store=store,
        client_id="client-id",
        client_secret="client-secret",
        max_results=1,
    )

    assert token_posts[0]["data"]["grant_type"] == "refresh_token"
    assert token_posts[0]["data"]["refresh_token"] == "refresh-token"
    assert get_headers[0]["Authorization"] == "Bearer fresh-access-token"
    assert result["links"] == ["https://www.youtube.com/watch?v=fresh-video"]


def test_token_exchange_network_error_is_reported(monkeypatch):
    """Test Google token network failures become safe OAuth errors."""

    def fake_post(url, *, data, timeout):
        raise httpx.ConnectError("connect failed")

    monkeypatch.setattr(youtube_oauth.httpx, "post", fake_post)

    with pytest.raises(YouTubeOAuthError, match="Google token request failed"):
        exchange_authorization_code(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://twitter.example/api/youtube/oauth/callback",
            code="code",
        )
