"""YouTube OAuth token storage and liked-video retrieval."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .youtube_cookies import ENCRYPTION_PREFIX, YouTubeCookieEncryptionError

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
TOKEN_REFRESH_SKEW_SECONDS = 60


class YouTubeOAuthError(RuntimeError):
    """Base error for YouTube OAuth operations."""


class YouTubeOAuthConfigError(YouTubeOAuthError):
    """Raised when OAuth client configuration is missing."""


class YouTubeOAuthTokenError(YouTubeOAuthError):
    """Raised when OAuth tokens cannot be read or refreshed."""


def utc_now() -> datetime:
    """Return current UTC time."""
    return datetime.now(UTC)


def utc_now_iso() -> str:
    """Return current UTC time in ISO format."""
    return utc_now().isoformat()


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str = YOUTUBE_READONLY_SCOPE,
) -> str:
    """Build the Google OAuth consent URL."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_authorization_code(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    timeout_seconds: float = 20.0,
) -> dict[str, object]:
    """Exchange a Google OAuth authorization code for an access token."""
    return _post_token(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout_seconds=timeout_seconds,
    )


def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    timeout_seconds: float = 20.0,
) -> dict[str, object]:
    """Refresh a Google OAuth access token."""
    return _post_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout_seconds=timeout_seconds,
    )


def fetch_liked_videos(
    *,
    token_store: "YouTubeOAuthTokenStore",
    client_id: str,
    client_secret: str,
    max_results: int,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """Fetch liked YouTube videos for the authorized user."""
    if max_results < 1:
        return {"items": [], "links": [], "count": 0}

    token = _valid_access_token(
        token_store=token_store,
        client_id=client_id,
        client_secret=client_secret,
        timeout_seconds=timeout_seconds,
    )
    page_token = None
    liked_items: list[dict[str, str | None]] = []
    seen_ids: set[str] = set()

    while len(liked_items) < max_results:
        remaining = max_results - len(liked_items)
        response = _request_liked_page(
            access_token=token,
            page_token=page_token,
            page_size=min(50, remaining),
            timeout_seconds=timeout_seconds,
        )
        if response.status_code == 401:
            token = _refresh_stored_token(
                token_store=token_store,
                client_id=client_id,
                client_secret=client_secret,
                timeout_seconds=timeout_seconds,
            )
            response = _request_liked_page(
                access_token=token,
                page_token=page_token,
                page_size=min(50, remaining),
                timeout_seconds=timeout_seconds,
            )

        if response.status_code >= 400:
            raise YouTubeOAuthError(_google_error_message(response))

        try:
            payload = response.json()
        except ValueError as exc:
            raise YouTubeOAuthError("Google returned an invalid liked-video response") from exc
        if not isinstance(payload, dict):
            raise YouTubeOAuthError("Google returned an invalid liked-video response")

        for item in payload.get("items", []):
            video_id = str(item.get("id") or "").strip()
            if not video_id or video_id in seen_ids:
                continue
            seen_ids.add(video_id)
            snippet = item.get("snippet") or {}
            liked_items.append(
                {
                    "id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": snippet.get("title"),
                    "channel_title": snippet.get("channelTitle"),
                }
            )
            if len(liked_items) >= max_results:
                break

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    links = [str(item["url"]) for item in liked_items]
    return {"items": liked_items, "links": links, "count": len(liked_items)}


class YouTubeOAuthTokenStore:
    """Encrypted server-side storage for YouTube OAuth tokens."""

    def __init__(
        self,
        *,
        token_path: Path,
        encryption_key: str | None,
        require_encryption: bool,
    ) -> None:
        self.token_path = token_path
        self.encryption_key = encryption_key.strip() if encryption_key else None
        self.require_encryption = require_encryption

    def is_configured(self) -> bool:
        return self.token_path.exists()

    def status(self) -> dict[str, object]:
        """Return safe token metadata only."""
        if not self.token_path.exists():
            return self._empty_status()

        encrypted = self._stored_bytes_encrypted()
        try:
            token = self.read_token()
        except Exception as exc:
            status = self._empty_status()
            status["configured"] = True
            status["encrypted"] = encrypted
            status["error"] = _sanitize_message(str(exc))
            return status

        return {
            "configured": True,
            "encrypted": encrypted,
            "scope": token.get("scope"),
            "expires_at": token.get("expires_at"),
            "last_authorized_at": token.get("last_authorized_at"),
            "last_refreshed_at": token.get("last_refreshed_at"),
            "has_refresh_token": bool(token.get("refresh_token")),
        }

    def save_authorized_token(self, token_response: dict[str, object]) -> dict[str, object]:
        """Persist a newly authorized OAuth token response."""
        token = self._token_from_response(token_response)
        token["last_authorized_at"] = utc_now_iso()
        self._write_token(token)
        return self.status()

    def save_refreshed_token(self, token_response: dict[str, object]) -> dict[str, object]:
        """Persist a refreshed OAuth token response while preserving refresh token metadata."""
        existing = self.read_token()
        token = self._token_from_response(token_response, existing=existing)
        token["last_authorized_at"] = existing.get("last_authorized_at")
        token["last_refreshed_at"] = utc_now_iso()
        self._write_token(token)
        return token

    def read_token(self) -> dict[str, object]:
        """Read and decrypt the stored OAuth token."""
        if not self.token_path.exists():
            raise FileNotFoundError("No YouTube OAuth token is configured")

        data = self.token_path.read_bytes()
        if data.startswith(ENCRYPTION_PREFIX):
            if not self.encryption_key:
                raise YouTubeCookieEncryptionError("OAuth encryption key is not configured")
            try:
                data = Fernet(self.encryption_key.encode("utf-8")).decrypt(
                    data[len(ENCRYPTION_PREFIX) :]
                )
            except (InvalidToken, ValueError) as exc:
                raise YouTubeCookieEncryptionError(
                    "Stored OAuth token cannot be decrypted"
                ) from exc
        elif self.require_encryption:
            raise YouTubeCookieEncryptionError("Stored OAuth token is not encrypted")

        return json.loads(data.decode("utf-8"))

    def delete(self) -> None:
        self.token_path.unlink(missing_ok=True)

    def _token_from_response(
        self,
        token_response: dict[str, object],
        *,
        existing: dict[str, object] | None = None,
    ) -> dict[str, object]:
        access_token = str(token_response.get("access_token") or "")
        if not access_token:
            raise YouTubeOAuthTokenError("Google did not return an access token")

        expires_in = int(token_response.get("expires_in") or 0)
        expires_at = (utc_now() + timedelta(seconds=max(expires_in, 0))).isoformat()
        token = {
            "access_token": access_token,
            "refresh_token": token_response.get("refresh_token")
            or (existing or {}).get("refresh_token"),
            "token_type": token_response.get("token_type"),
            "scope": token_response.get("scope") or (existing or {}).get("scope"),
            "expires_at": expires_at,
        }
        return token

    def _write_token(self, token: dict[str, object]) -> None:
        if self.require_encryption and not self.encryption_key:
            raise YouTubeCookieEncryptionError("OAuth encryption key is required")

        data = (json.dumps(token, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if self.encryption_key:
            data = ENCRYPTION_PREFIX + Fernet(self.encryption_key.encode("utf-8")).encrypt(data)

        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.token_path.with_name(f".{self.token_path.name}.tmp")
        tmp_path.write_bytes(data)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self.token_path)
        os.chmod(self.token_path, 0o600)

    def _stored_bytes_encrypted(self) -> bool:
        return self.token_path.exists() and self.token_path.read_bytes().startswith(
            ENCRYPTION_PREFIX
        )

    @staticmethod
    def _empty_status() -> dict[str, object]:
        return {
            "configured": False,
            "encrypted": False,
            "scope": None,
            "expires_at": None,
            "last_authorized_at": None,
            "last_refreshed_at": None,
            "has_refresh_token": False,
        }


def _valid_access_token(
    *,
    token_store: YouTubeOAuthTokenStore,
    client_id: str,
    client_secret: str,
    timeout_seconds: float,
) -> str:
    token = token_store.read_token()
    expires_at = _parse_datetime(token.get("expires_at"))
    if expires_at and expires_at > utc_now() + timedelta(seconds=TOKEN_REFRESH_SKEW_SECONDS):
        return str(token["access_token"])

    return _refresh_stored_token(
        token_store=token_store,
        client_id=client_id,
        client_secret=client_secret,
        timeout_seconds=timeout_seconds,
    )


def _refresh_stored_token(
    *,
    token_store: YouTubeOAuthTokenStore,
    client_id: str,
    client_secret: str,
    timeout_seconds: float,
) -> str:
    token = token_store.read_token()
    refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        raise YouTubeOAuthTokenError("YouTube OAuth token needs a fresh Google connection")

    refreshed = refresh_access_token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        timeout_seconds=timeout_seconds,
    )
    updated = token_store.save_refreshed_token(refreshed)
    return str(updated["access_token"])


def _request_liked_page(
    *,
    access_token: str,
    page_token: str | None,
    page_size: int,
    timeout_seconds: float,
) -> httpx.Response:
    params = {
        "part": "id,snippet",
        "myRating": "like",
        "maxResults": str(page_size),
    }
    if page_token:
        params["pageToken"] = page_token

    try:
        return httpx.get(
            YOUTUBE_VIDEOS_URL,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise YouTubeOAuthError(f"Google API request failed: {_sanitize_message(str(exc))}") from exc


def _post_token(payload: dict[str, object], *, timeout_seconds: float) -> dict[str, object]:
    try:
        response = httpx.post(GOOGLE_TOKEN_URL, data=payload, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        raise YouTubeOAuthError(f"Google token request failed: {_sanitize_message(str(exc))}") from exc

    if response.status_code >= 400:
        raise YouTubeOAuthError(_google_error_message(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise YouTubeOAuthError("Google returned an invalid OAuth token response") from exc
    if not isinstance(data, dict):
        raise YouTubeOAuthError("Google returned an invalid OAuth token response")
    return data


def _google_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if isinstance(payload, dict):
        error = payload.get("error")
        description = payload.get("error_description")
        details = payload.get("error", {}).get("message") if isinstance(error, dict) else None
        message = description or details or error
        if message:
            return f"Google API error ({response.status_code}): {_sanitize_message(str(message))}"

    return f"Google API error ({response.status_code})"


def _sanitize_message(message: str) -> str:
    return message.replace("\n", " ").replace("\r", " ")[:300]


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
