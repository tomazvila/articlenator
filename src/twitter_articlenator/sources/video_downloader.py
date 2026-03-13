"""Twitter/X video downloader using yt-dlp."""

import re
import subprocess
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger()

TWITTER_VIDEO_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/(\w+)/status/(\d+)"
)


def download_video(url: str, output_dir: Path, cookies: str | None = None) -> Path:
    """Download video from a tweet URL using yt-dlp.

    Args:
        url: Twitter/X status URL containing a video.
        output_dir: Directory to save the video file.
        cookies: Optional Twitter cookies string (auth_token=...; ct0=...).

    Returns:
        Path to the downloaded video file.

    Raises:
        ValueError: If URL is invalid or no video found.
        RuntimeError: If yt-dlp fails.
    """
    match = TWITTER_VIDEO_URL_PATTERN.match(url)
    if not match:
        raise ValueError(f"Invalid Twitter URL: {url}")

    username = match.group(1)
    tweet_id = match.group(2)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{username}_{tweet_id}.mp4"

    log.info("downloading_video", tweet_id=tweet_id, url=url)

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--merge-output-format",
        "mp4",
        "--force-overwrites",
        "-o",
        str(output_path),
        url,
    ]

    # Pass cookies if provided
    cookie_file = None
    if cookies:
        cookie_file = _write_cookie_file(cookies)
        cmd.extend(["--cookies", cookie_file.name])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            log.error("yt_dlp_failed", url=url, stderr=stderr)
            raise RuntimeError(f"yt-dlp failed: {stderr}")

        if not output_path.exists():
            raise RuntimeError(f"yt-dlp completed but output file not found: {output_path}")

        file_size = output_path.stat().st_size
        log.info("video_downloaded", path=str(output_path), size_bytes=file_size)
        return output_path

    finally:
        if cookie_file:
            try:
                Path(cookie_file.name).unlink(missing_ok=True)
            except Exception:
                pass


def _write_cookie_file(cookies: str) -> tempfile.NamedTemporaryFile:
    """Write cookies to a Netscape cookie file for yt-dlp.

    Args:
        cookies: Cookie string (auth_token=xxx; ct0=yyy).

    Returns:
        Temporary file containing cookies in Netscape format.
    """
    cookie_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    cookie_file.write("# Netscape HTTP Cookie File\n")

    for part in cookies.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip()
            cookie_file.write(f".x.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}\n")

    cookie_file.flush()
    return cookie_file
