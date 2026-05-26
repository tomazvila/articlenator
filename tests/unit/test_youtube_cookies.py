"""Tests for server-side YouTube cookie storage."""

import os
import stat

import pytest

from twitter_articlenator.sources.youtube_cookies import (
    YouTubeCookieEncryptionError,
    YouTubeCookieError,
    YouTubeCookieStore,
    validate_youtube_cookie_text,
    verify_youtube_cookie_file,
)

FERNET_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
WRONG_FERNET_KEY = "Hh8dHBsaGRgXFhUUExIREA8ODQwLCgkIBwYFBAMCAQA="
SAMPLE_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session-value\n"
)


def make_store(tmp_path, *, key=FERNET_KEY, require_encryption=False) -> YouTubeCookieStore:
    return YouTubeCookieStore(
        cookie_path=tmp_path / "config" / "youtube-cookies.txt",
        encryption_key=key,
        require_encryption=require_encryption,
        max_bytes=4096,
    )


def test_validate_valid_netscape_file_returns_metadata_without_secrets():
    """Test valid YouTube cookies produce safe metadata."""
    normalized, metadata = validate_youtube_cookie_text(SAMPLE_COOKIES, max_bytes=4096)

    assert normalized.startswith("# Netscape HTTP Cookie File")
    assert metadata.cookie_count == 1
    assert metadata.youtube_cookie_count == 1
    assert metadata.session_cookie_count == 1
    assert "secret-session-value" not in str(metadata.to_dict())


def test_validate_rejects_malformed_rows():
    """Test malformed Netscape rows are rejected."""
    with pytest.raises(YouTubeCookieError, match="Malformed Netscape cookie row"):
        validate_youtube_cookie_text("bad\trow", max_bytes=4096)


def test_validate_rejects_expired_only_rows():
    """Test expired-only cookies are rejected."""
    expired = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1\tSID\tsecret-session-value\n"
    )
    with pytest.raises(YouTubeCookieError, match="expired"):
        validate_youtube_cookie_text(expired, max_bytes=4096, now=100)


def test_validate_rejects_non_youtube_domains():
    """Test unrelated cookie domains are rejected."""
    unrelated = (
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session-value\n"
    )
    with pytest.raises(YouTubeCookieError, match="Unsupported cookie domain"):
        validate_youtube_cookie_text(unrelated, max_bytes=4096)


def test_validate_rejects_file_larger_than_limit():
    """Test oversized cookie uploads are rejected."""
    with pytest.raises(YouTubeCookieError, match="too large"):
        validate_youtube_cookie_text(SAMPLE_COOKIES, max_bytes=10)


def test_store_encrypts_cookie_file_and_returns_metadata_only(tmp_path):
    """Test encrypted storage does not persist plaintext cookie values."""
    store = make_store(tmp_path)

    metadata = store.save(SAMPLE_COOKIES)

    stored_bytes = store.cookie_path.read_bytes()
    assert metadata["configured"] is True
    assert metadata["encrypted"] is True
    assert b"secret-session-value" not in stored_bytes
    assert "secret-session-value" not in str(metadata)
    assert store.read_text() == SAMPLE_COOKIES


def test_store_sets_private_permissions(tmp_path):
    """Test cookie storage uses restrictive directory and file permissions."""
    store = make_store(tmp_path)
    store.save(SAMPLE_COOKIES)

    assert stat.S_IMODE(store.cookie_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.cookie_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.metadata_path.stat().st_mode) == 0o600


def test_store_wrong_encryption_key_fails(tmp_path):
    """Test encrypted cookies cannot be read with the wrong key."""
    store = make_store(tmp_path)
    store.save(SAMPLE_COOKIES)

    wrong_store = make_store(tmp_path, key=WRONG_FERNET_KEY)
    with pytest.raises(YouTubeCookieEncryptionError):
        wrong_store.read_text()


def test_store_missing_key_fails_when_encryption_required(tmp_path):
    """Test production-style required encryption rejects plaintext storage."""
    store = make_store(tmp_path, key=None, require_encryption=True)

    with pytest.raises(YouTubeCookieEncryptionError, match="required"):
        store.save(SAMPLE_COOKIES)


def test_temporary_cookie_file_is_removed_after_use(tmp_path):
    """Test decrypted yt-dlp temp file is cleaned up."""
    store = make_store(tmp_path)
    store.save(SAMPLE_COOKIES)

    with store.temporary_cookie_file() as cookie_file:
        assert cookie_file.exists()
        assert cookie_file.read_text(encoding="utf-8") == SAMPLE_COOKIES
        temp_path = cookie_file

    assert not temp_path.exists()


def test_delete_removes_cookie_and_metadata_files(tmp_path):
    """Test deleting the store removes both persistent files."""
    store = make_store(tmp_path)
    store.save(SAMPLE_COOKIES)

    store.delete()

    assert not store.cookie_path.exists()
    assert not store.metadata_path.exists()
    assert store.status()["configured"] is False


def test_verify_youtube_cookie_file_success(tmp_path):
    """Test yt-dlp verification accepts real-looking format output."""
    fake_ytdlp = tmp_path / "fake-ytdlp"
    fake_ytdlp.write_text(
        "#!/usr/bin/env python3\n"
        "print('ID EXT RESOLUTION')\n"
        "print('18 mp4 640x360')\n",
        encoding="utf-8",
    )
    os.chmod(fake_ytdlp, 0o755)
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(SAMPLE_COOKIES, encoding="utf-8")

    ok, message = verify_youtube_cookie_file(
        cookie_file,
        url="https://www.youtube.com/watch?v=fv7TlVMETP0",
        downloader_bin=str(fake_ytdlp),
        timeout_seconds=5,
    )

    assert ok is True
    assert "downloadable media formats" in message


def test_verify_youtube_cookie_file_failure_sanitizes_message(tmp_path):
    """Test yt-dlp verification failure does not expose cookie values."""
    fake_ytdlp = tmp_path / "fake-ytdlp"
    fake_ytdlp.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "cookie_path = sys.argv[sys.argv.index('--cookies') + 1]\n"
        "print('verification failed ' + cookie_path, file=sys.stderr)\n"
        "raise SystemExit(3)\n",
        encoding="utf-8",
    )
    os.chmod(fake_ytdlp, 0o755)
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(SAMPLE_COOKIES, encoding="utf-8")

    ok, message = verify_youtube_cookie_file(
        cookie_file,
        url="https://www.youtube.com/watch?v=fv7TlVMETP0",
        downloader_bin=str(fake_ytdlp),
        timeout_seconds=5,
    )

    assert ok is False
    assert "verification failed" in message
    assert str(cookie_file) not in message
    assert "secret-session-value" not in message
