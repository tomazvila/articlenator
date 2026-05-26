"""Server-side storage and validation for YouTube cookies."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

COOKIE_HEADER = "# Netscape HTTP Cookie File"
ENCRYPTION_PREFIX = b"fernet:v1:"
SOON_EXPIRING_SECONDS = 14 * 24 * 60 * 60

YOUTUBE_DOMAINS = {"youtube.com", "youtube-nocookie.com"}
ALLOWED_COOKIE_DOMAINS = {
    "youtube.com",
    "youtube-nocookie.com",
    "google.com",
    "accounts.google.com",
    "googleusercontent.com",
    "ytimg.com",
}
SESSION_COOKIE_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
}


class YouTubeCookieError(ValueError):
    """Base error for YouTube cookie validation and storage failures."""


class YouTubeCookieEncryptionError(RuntimeError):
    """Raised when encrypted cookie storage cannot be read or written."""


@dataclass(frozen=True)
class YouTubeCookieMetadata:
    """Safe metadata for stored YouTube cookies."""

    configured: bool
    encrypted: bool
    cookie_count: int
    youtube_cookie_count: int
    expired_count: int
    soon_expiring_count: int
    session_cookie_count: int
    last_uploaded_at: str | None = None
    last_verified_at: str | None = None
    last_verification_ok: bool | None = None
    last_verification_message: str | None = None

    @classmethod
    def empty(cls) -> "YouTubeCookieMetadata":
        return cls(
            configured=False,
            encrypted=False,
            cookie_count=0,
            youtube_cookie_count=0,
            expired_count=0,
            soon_expiring_count=0,
            session_cookie_count=0,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_youtube_cookie_text(
    raw_text: str,
    *,
    max_bytes: int,
    now: float | None = None,
) -> tuple[str, YouTubeCookieMetadata]:
    """Validate and normalize Netscape-format YouTube cookies."""
    text = raw_text.strip()
    if not text:
        raise YouTubeCookieError("Cookie file is empty")

    size_bytes = len(text.encode("utf-8"))
    if size_bytes > max_bytes:
        raise YouTubeCookieError(f"Cookie file is too large; maximum is {max_bytes} bytes")

    now = time.time() if now is None else now
    normalized_lines: list[str] = []
    cookie_count = 0
    youtube_cookie_count = 0
    expired_count = 0
    soon_expiring_count = 0
    session_cookie_count = 0

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            if line == COOKIE_HEADER:
                continue
            normalized_lines.append(line)
            continue

        fields = line.split("\t")
        if len(fields) != 7:
            raise YouTubeCookieError(
                f"Malformed Netscape cookie row on line {line_number}: expected 7 tab-separated fields"
            )

        domain, include_subdomains, path, secure, expires, name, value = fields
        domain_name = _canonical_cookie_domain(domain)
        if not _is_allowed_domain(domain_name):
            raise YouTubeCookieError(f"Unsupported cookie domain on line {line_number}: {domain}")

        if include_subdomains.upper() not in {"TRUE", "FALSE"}:
            raise YouTubeCookieError(f"Invalid include-subdomains flag on line {line_number}")
        if secure.upper() not in {"TRUE", "FALSE"}:
            raise YouTubeCookieError(f"Invalid secure flag on line {line_number}")
        if not path:
            raise YouTubeCookieError(f"Cookie path is empty on line {line_number}")
        if not name:
            raise YouTubeCookieError(f"Cookie name is empty on line {line_number}")
        if not value:
            raise YouTubeCookieError(f"Cookie value is empty on line {line_number}")

        try:
            expires_at = int(expires)
        except ValueError as exc:
            raise YouTubeCookieError(
                f"Cookie expiry is not an integer on line {line_number}"
            ) from exc

        cookie_count += 1
        if _is_youtube_domain(domain_name):
            youtube_cookie_count += 1
        if expires_at > 0 and expires_at < now:
            expired_count += 1
        elif expires_at > 0 and expires_at <= now + SOON_EXPIRING_SECONDS:
            soon_expiring_count += 1
        if _is_session_cookie_name(name):
            session_cookie_count += 1

        normalized_lines.append(line)

    if cookie_count == 0:
        raise YouTubeCookieError("No cookie rows found")
    if youtube_cookie_count == 0:
        raise YouTubeCookieError("No YouTube cookie rows found")
    if expired_count == cookie_count:
        raise YouTubeCookieError("All cookie rows are expired")
    if session_cookie_count == 0:
        raise YouTubeCookieError("No YouTube session cookie rows found")

    normalized = COOKIE_HEADER + "\n" + "\n".join(normalized_lines).strip() + "\n"
    metadata = YouTubeCookieMetadata(
        configured=True,
        encrypted=False,
        cookie_count=cookie_count,
        youtube_cookie_count=youtube_cookie_count,
        expired_count=expired_count,
        soon_expiring_count=soon_expiring_count,
        session_cookie_count=session_cookie_count,
    )
    return normalized, metadata


class YouTubeCookieStore:
    """Encrypted-or-plain server-side YouTube cookie storage."""

    def __init__(
        self,
        *,
        cookie_path: Path,
        encryption_key: str | None,
        require_encryption: bool,
        max_bytes: int,
    ) -> None:
        self.cookie_path = cookie_path
        self.metadata_path = cookie_path.with_suffix(".json")
        self.encryption_key = encryption_key.strip() if encryption_key else None
        self.require_encryption = require_encryption
        self.max_bytes = max_bytes

    def is_configured(self) -> bool:
        return self.cookie_path.exists()

    def status(self) -> dict[str, object]:
        """Return metadata only; never raw cookie content."""
        if not self.cookie_path.exists():
            return YouTubeCookieMetadata.empty().to_dict()

        metadata = self._load_metadata()
        if metadata is not None:
            metadata["configured"] = True
            metadata["encrypted"] = self._stored_bytes_encrypted()
            return _metadata_without_secrets(metadata)

        try:
            text = self.read_text()
            _, parsed = validate_youtube_cookie_text(text, max_bytes=self.max_bytes)
            metadata = parsed.to_dict()
            metadata["configured"] = True
            metadata["encrypted"] = self._stored_bytes_encrypted()
            return metadata
        except Exception as exc:
            status = YouTubeCookieMetadata.empty().to_dict()
            status["configured"] = True
            status["encrypted"] = self._stored_bytes_encrypted()
            status["last_verification_ok"] = False
            status["last_verification_message"] = _sanitize_message(str(exc))
            return status

    def save(self, raw_text: str) -> dict[str, object]:
        normalized, metadata = validate_youtube_cookie_text(raw_text, max_bytes=self.max_bytes)
        if self.require_encryption and not self.encryption_key:
            raise YouTubeCookieEncryptionError("Cookie encryption key is required")

        encrypted = bool(self.encryption_key)
        stored_bytes = (
            self._encrypt(normalized.encode("utf-8")) if encrypted else normalized.encode()
        )
        self._write_private_file(self.cookie_path, stored_bytes)

        metadata_dict = metadata.to_dict()
        metadata_dict.update(
            {
                "configured": True,
                "encrypted": encrypted,
                "last_uploaded_at": _utc_now(),
                "last_verified_at": None,
                "last_verification_ok": None,
                "last_verification_message": None,
            }
        )
        self._write_private_file(
            self.metadata_path,
            (json.dumps(metadata_dict, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        return metadata_dict

    def delete(self) -> None:
        self.cookie_path.unlink(missing_ok=True)
        self.metadata_path.unlink(missing_ok=True)

    def read_text(self) -> str:
        if not self.cookie_path.exists():
            raise FileNotFoundError("No YouTube cookie file is configured")

        data = self.cookie_path.read_bytes()
        if data.startswith(ENCRYPTION_PREFIX):
            if not self.encryption_key:
                raise YouTubeCookieEncryptionError("Cookie encryption key is not configured")
            try:
                data = Fernet(self.encryption_key.encode("utf-8")).decrypt(
                    data[len(ENCRYPTION_PREFIX) :]
                )
            except (InvalidToken, ValueError) as exc:
                raise YouTubeCookieEncryptionError(
                    "Stored cookie file cannot be decrypted"
                ) from exc
        return data.decode("utf-8")

    @contextmanager
    def temporary_cookie_file(self) -> Iterator[Path]:
        """Write decrypted cookies to a short-lived file for yt-dlp."""
        text = self.read_text()
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        temp_path = Path(handle.name)
        try:
            os.chmod(temp_path, 0o600)
            handle.write(text)
            handle.flush()
            handle.close()
            yield temp_path
        finally:
            try:
                handle.close()
            except Exception:
                pass
            temp_path.unlink(missing_ok=True)

    def verify(
        self,
        *,
        url: str,
        downloader_bin: str,
        timeout_seconds: int,
    ) -> dict[str, object]:
        """Run a live yt-dlp format probe with the stored cookies."""
        with self.temporary_cookie_file() as cookie_file:
            success, message = verify_youtube_cookie_file(
                cookie_file,
                url=url,
                downloader_bin=downloader_bin,
                timeout_seconds=timeout_seconds,
            )

        metadata = self.status()
        metadata["last_verified_at"] = _utc_now()
        metadata["last_verification_ok"] = success
        metadata["last_verification_message"] = message
        self._write_private_file(
            self.metadata_path,
            (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        return metadata

    def _encrypt(self, data: bytes) -> bytes:
        try:
            return ENCRYPTION_PREFIX + Fernet(self.encryption_key.encode("utf-8")).encrypt(data)
        except (ValueError, TypeError) as exc:
            raise YouTubeCookieEncryptionError(
                "Cookie encryption key must be a valid Fernet key"
            ) from exc

    def _stored_bytes_encrypted(self) -> bool:
        return self.cookie_path.exists() and self.cookie_path.read_bytes().startswith(
            ENCRYPTION_PREFIX
        )

    def _load_metadata(self) -> dict[str, object] | None:
        if not self.metadata_path.exists():
            return None
        try:
            return json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_private_file(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = Path(tmp.name)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)


def verify_youtube_cookie_file(
    cookie_file: Path,
    *,
    url: str,
    downloader_bin: str,
    timeout_seconds: int,
) -> tuple[bool, str]:
    """Verify cookies by asking yt-dlp for available formats."""
    cmd = [
        downloader_bin,
        "--no-warnings",
        "--no-playlist",
        "--js-runtimes",
        "node",
        "--cookies",
        str(cookie_file),
        "-F",
        url,
    ]
    try:
        result = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"Verification exceeded {timeout_seconds} seconds"
    except OSError as exc:
        return False, _sanitize_message(str(exc))

    output = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
    output = output.replace(str(cookie_file), "[temp-file]")
    if result.returncode != 0:
        return False, _sanitize_message(output or "yt-dlp verification failed")

    if not _looks_like_real_format_output(output):
        return False, "yt-dlp did not return downloadable media formats"

    return True, "yt-dlp returned downloadable media formats"


def _canonical_cookie_domain(domain: str) -> str:
    if domain.startswith("#HttpOnly_"):
        domain = domain.removeprefix("#HttpOnly_")
    return domain.lower().lstrip(".")


def _is_allowed_domain(domain: str) -> bool:
    return any(
        domain == allowed or domain.endswith(f".{allowed}") for allowed in ALLOWED_COOKIE_DOMAINS
    )


def _is_youtube_domain(domain: str) -> bool:
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in YOUTUBE_DOMAINS)


def _is_session_cookie_name(name: str) -> bool:
    return (
        name in SESSION_COOKIE_NAMES
        or name.endswith("SID")
        or (name.startswith("__Secure-") and "SID" in name)
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _metadata_without_secrets(metadata: dict[str, object]) -> dict[str, object]:
    safe = YouTubeCookieMetadata.empty().to_dict()
    for key in safe:
        if key in metadata:
            safe[key] = metadata[key]
    return safe


def _sanitize_message(message: str) -> str:
    sanitized = re.sub(r"/(?:private/)?tmp/[^\s]+", "[temp-file]", message)
    sanitized = re.sub(r"[\w.-]+/youtube-cookies[^\s]*", "[cookie-file]", sanitized)
    return sanitized[:500]


def _looks_like_real_format_output(output: str) -> bool:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("ID ") and "EXT" in stripped:
            return True
        if re.match(r"^[\w-]+\s+(mp4|webm|m4a|mp3)\b", stripped):
            return True
    return False
