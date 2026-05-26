"""YouTube downloader using yt-dlp."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import structlog

log = structlog.get_logger()

YouTubeDownloadMode = Literal["video", "mp3"]

YOUTUBE_VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
YOUTUBE_NOCOOKIE_HOSTS = {"youtube-nocookie.com", "www.youtube-nocookie.com"}
YOUTU_BE_HOSTS = {"youtu.be", "www.youtu.be"}
SUPPORTED_MODES = {"video", "mp3"}


@dataclass(frozen=True)
class YouTubeDownloadUpdate:
    """Progress update from a YouTube download process."""

    kind: Literal["keepalive", "complete"]
    path: Path | None = None


def is_supported_youtube_url(url: str) -> bool:
    """Return whether the URL is an individual YouTube video URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if host in YOUTU_BE_HOSTS:
        return bool(path_parts)

    if host not in YOUTUBE_VIDEO_HOSTS and host not in YOUTUBE_NOCOOKIE_HOSTS:
        return False

    if parsed.path == "/watch":
        return bool(parse_qs(parsed.query).get("v", [""])[0])

    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "live"}:
        return bool(path_parts[1])

    if len(path_parts) >= 2 and path_parts[0] == "embed":
        return bool(path_parts[1])

    return False


def iter_youtube_download(
    url: str,
    output_dir: Path,
    *,
    mode: YouTubeDownloadMode,
    cookies: str | None = None,
    cookie_file_path: Path | None = None,
    downloader_bin: str = "yt-dlp",
    timeout_seconds: int = 14400,
    keepalive_seconds: float = 10.0,
) -> Generator[YouTubeDownloadUpdate, None, None]:
    """Download one YouTube URL and yield keepalive updates while it runs."""
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported YouTube download mode: {mode}")

    if not is_supported_youtube_url(url):
        raise ValueError(f"Invalid YouTube URL: {url}")

    if cookies and cookie_file_path:
        raise ValueError("Use either raw cookies or cookie_file_path, not both")

    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"youtube_{mode}_{hashlib.sha256(url.encode()).hexdigest()[:12]}"
    output_template = output_dir / f"{prefix}_%(id)s.%(ext)s"

    cmd = _build_youtube_command(
        url=url,
        mode=mode,
        output_template=output_template,
        downloader_bin=downloader_bin,
    )

    cookie_file = None
    if cookie_file_path:
        cmd[-1:-1] = ["--cookies", str(cookie_file_path)]
    elif cookies:
        cookie_file = _write_youtube_cookie_file(cookies)
        cmd[-1:-1] = ["--cookies", cookie_file.name]

    log.info("youtube_download_starting", mode=mode, url=url)

    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
                process = subprocess.Popen(
                    cmd,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                )
                started_at = time.monotonic()

                while process.poll() is None:
                    elapsed = time.monotonic() - started_at
                    if elapsed > timeout_seconds:
                        process.kill()
                        process.wait(timeout=5)
                        raise TimeoutError(
                            f"YouTube download exceeded {timeout_seconds} seconds"
                        )

                    time.sleep(min(keepalive_seconds, max(timeout_seconds - elapsed, 0.1)))
                    yield YouTubeDownloadUpdate(kind="keepalive")

                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read().strip()
                stderr = stderr_file.read().strip()

        if process.returncode != 0:
            log.error("youtube_yt_dlp_failed", mode=mode, url=url, stderr=stderr)
            raise RuntimeError(f"yt-dlp failed: {stderr or stdout or 'unknown error'}")

        output_path = _find_downloaded_file(output_dir, prefix, mode)
        file_size = output_path.stat().st_size
        log.info(
            "youtube_downloaded",
            mode=mode,
            path=str(output_path),
            size_bytes=file_size,
        )
        yield YouTubeDownloadUpdate(kind="complete", path=output_path)

    finally:
        if cookie_file:
            try:
                Path(cookie_file.name).unlink(missing_ok=True)
            except Exception:
                pass


def _build_youtube_command(
    *,
    url: str,
    mode: YouTubeDownloadMode,
    output_template: Path,
    downloader_bin: str,
) -> list[str]:
    """Build the yt-dlp command for a YouTube download."""
    cmd = [
        downloader_bin,
        "--no-warnings",
        "--no-playlist",
        "--js-runtimes",
        "node",
        "--force-overwrites",
        "--socket-timeout",
        "30",
        "--retries",
        "5",
        "--fragment-retries",
        "10",
        "--retry-sleep",
        "http:exp=1:20",
        "--retry-sleep",
        "fragment:exp=1:20",
        "-o",
        str(output_template),
    ]

    if mode == "video":
        cmd.extend(
            [
                "-f",
                "18/b[ext=mp4][protocol=https]/b",
                "--merge-output-format",
                "mp4",
                "--remux-video",
                "mp4",
            ]
        )
    else:
        cmd.extend(
            [
                "-f",
                "18/b[ext=mp4][protocol=https]/b",
                "-x",
                "--audio-format",
                "mp3",
            ]
        )

    cmd.append(url)
    return cmd


def _find_downloaded_file(output_dir: Path, prefix: str, mode: YouTubeDownloadMode) -> Path:
    """Find the file produced by yt-dlp for a known URL prefix."""
    extension = ".mp4" if mode == "video" else ".mp3"
    candidates = sorted(output_dir.glob(f"{prefix}_*{extension}"))
    if not candidates:
        raise RuntimeError(f"yt-dlp completed but no {extension} output was found")
    return candidates[-1]


def _write_youtube_cookie_file(cookies: str) -> tempfile.NamedTemporaryFile:
    """Write raw Netscape-format YouTube cookies to a temporary file."""
    cookie_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    text = cookies.strip()
    if not text.startswith("# Netscape HTTP Cookie File"):
        cookie_file.write("# Netscape HTTP Cookie File\n")
    cookie_file.write(text)
    if not text.endswith("\n"):
        cookie_file.write("\n")
    cookie_file.flush()
    return cookie_file
