"""YouTube downloader using yt-dlp."""

from __future__ import annotations

import hashlib
import json
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
YouTubeURLKind = Literal["video", "playlist"]

YOUTUBE_VIDEO_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "www.music.youtube.com",
}
YOUTUBE_NOCOOKIE_HOSTS = {"youtube-nocookie.com", "www.youtube-nocookie.com"}
YOUTU_BE_HOSTS = {"youtu.be", "www.youtu.be"}
SUPPORTED_MODES = {"video", "mp3"}


@dataclass(frozen=True)
class YouTubeDownloadUpdate:
    """Progress update from a YouTube download process."""

    kind: Literal["keepalive", "complete"]
    path: Path | None = None
    file_count: int | None = None


def youtube_url_kind(url: str) -> YouTubeURLKind | None:
    """Classify a supported YouTube URL as a single video or playlist."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None

    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    playlist_id = query.get("list", [""])[0]

    if host in YOUTU_BE_HOSTS:
        return "video" if path_parts else None

    if host not in YOUTUBE_VIDEO_HOSTS and host not in YOUTUBE_NOCOOKIE_HOSTS:
        return None

    if parsed.path in {"/playlist", "/watch"} and playlist_id:
        return "playlist"

    if parsed.path == "/watch":
        return "video" if query.get("v", [""])[0] else None

    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "live"}:
        return "video" if path_parts[1] else None

    if len(path_parts) >= 2 and path_parts[0] == "embed":
        return "video" if path_parts[1] else None

    return None


def is_supported_youtube_url(url: str) -> bool:
    """Return whether the URL is a supported YouTube video or playlist URL."""
    return youtube_url_kind(url) is not None


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

    url_kind = youtube_url_kind(url)
    if url_kind is None:
        raise ValueError(f"Invalid YouTube URL: {url}")

    if cookies and cookie_file_path:
        raise ValueError("Use either raw cookies or cookie_file_path, not both")

    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"youtube_{mode}_{hashlib.sha256(url.encode()).hexdigest()[:12]}"
    if mode == "mp3":
        output_template = (
            output_dir
            / "%(playlist_index&{} - |)s%(artist,uploader|Unknown Artist).80s - %(track,title).140s [%(id)s].%(ext)s"
        )
    else:
        output_template = output_dir / f"{prefix}_%(playlist_index&{{}}_|)s%(id)s.%(ext)s"
    for stale_file in output_dir.glob(f"{prefix}_*"):
        if stale_file.is_file():
            stale_file.unlink(missing_ok=True)
    before_outputs = _snapshot_downloaded_files(output_dir, mode)

    cmd = _build_youtube_command(
        url=url,
        mode=mode,
        output_template=output_template,
        downloader_bin=downloader_bin,
        playlist=url_kind == "playlist",
    )

    cookie_file = None
    if cookie_file_path:
        cmd[-1:-1] = ["--cookies", str(cookie_file_path)]
    elif cookies:
        cookie_file = _write_youtube_cookie_file(cookies)
        cmd[-1:-1] = ["--cookies", cookie_file.name]

    log.info("youtube_download_starting", mode=mode, url=url, url_kind=url_kind)

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
                        raise TimeoutError(f"YouTube download exceeded {timeout_seconds} seconds")

                    time.sleep(min(keepalive_seconds, max(timeout_seconds - elapsed, 0.1)))
                    current_outputs = _find_downloaded_files(
                        output_dir,
                        prefix,
                        mode,
                        before_outputs=before_outputs,
                        required=False,
                    )
                    yield YouTubeDownloadUpdate(kind="keepalive", file_count=len(current_outputs))

                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read().strip()
                stderr = stderr_file.read().strip()

        if process.returncode != 0:
            output_paths = _find_downloaded_files(
                output_dir, prefix, mode, before_outputs=before_outputs, required=False
            )
            if not (
                url_kind == "playlist" and output_paths and _only_skippable_playlist_errors(stderr)
            ):
                log.error("youtube_yt_dlp_failed", mode=mode, url=url, stderr=stderr)
                raise RuntimeError(f"yt-dlp failed: {stderr or stdout or 'unknown error'}")
            log.warning(
                "youtube_playlist_download_partially_succeeded",
                mode=mode,
                url=url,
                output_count=len(output_paths),
                stderr=stderr,
            )
        else:
            output_paths = _find_downloaded_files(
                output_dir, prefix, mode, before_outputs=before_outputs
            )

        for output_path in output_paths:
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
    playlist: bool = False,
) -> list[str]:
    """Build the yt-dlp command for a YouTube download."""
    cmd = [
        downloader_bin,
        "--no-warnings",
        "--yes-playlist" if playlist else "--no-playlist",
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
    if playlist:
        cmd.append("--no-abort-on-error")

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
                "bestaudio/b",
                "-x",
                "--audio-format",
                "mp3",
                "--embed-metadata",
                "--embed-thumbnail",
                "--convert-thumbnails",
                "jpg",
                "--parse-metadata",
                "title:%(artist)s - %(title)s",
                "--replace-in-metadata",
                "title",
                r"(?i)\s*[\[(](?:official\s*)?(?:music\s*)?(?:video|audio|lyric(?:s)?|hd|4k|visualizer)[^\])]*[\])]\s*$",
                "",
                "--replace-in-metadata",
                "title",
                r"(?i)\s*-\s*(?:official\s*)?(?:music\s*)?(?:video|audio|lyric(?:s)?|hd|4k|visualizer)\s*$",
                "",
                "--replace-in-metadata",
                "title",
                r"\s{2,}",
                " ",
                "--parse-metadata",
                "%(artist,uploader|)s:%(meta_artist)s",
                "--parse-metadata",
                "%(track,title|)s:%(meta_title)s",
            ]
        )
        if playlist:
            cmd.extend(
                [
                    "--parse-metadata",
                    "playlist_title:%(meta_album)s",
                    "--parse-metadata",
                    "playlist_index:%(track_number)s",
                ]
            )

    cmd.append(url)
    return cmd


def get_youtube_playlist_item_count(
    url: str,
    *,
    cookie_file_path: Path | None = None,
    downloader_bin: str = "yt-dlp",
    timeout_seconds: int = 120,
) -> int | None:
    """Return the number of entries in a YouTube playlist without downloading media."""
    if youtube_url_kind(url) != "playlist":
        return None

    cmd = [
        downloader_bin,
        "--no-warnings",
        "--yes-playlist",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--js-runtimes",
        "node",
        "--socket-timeout",
        "30",
    ]
    if cookie_file_path:
        cmd.extend(["--cookies", str(cookie_file_path)])
    cmd.append(url)

    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=True,
        )
        metadata = json.loads(result.stdout)
    except (json.JSONDecodeError, subprocess.SubprocessError, TimeoutError) as exc:
        log.warning("youtube_playlist_count_failed", url=url, error=str(exc))
        return None

    entries = metadata.get("entries")
    if isinstance(entries, list):
        return len(entries)

    for key in ("playlist_count", "n_entries"):
        value = metadata.get(key)
        if isinstance(value, int) and value >= 0:
            return value

    return None


def _only_skippable_playlist_errors(stderr: str) -> bool:
    """Return whether yt-dlp errors only describe unavailable playlist items."""
    error_lines = [line for line in stderr.splitlines() if line.startswith("ERROR:")]
    return bool(error_lines) and all("Video unavailable" in line for line in error_lines)


def _snapshot_downloaded_files(
    output_dir: Path,
    mode: YouTubeDownloadMode,
) -> dict[Path, tuple[int, int]]:
    """Snapshot existing final outputs before invoking yt-dlp."""
    extension = ".mp4" if mode == "video" else ".mp3"
    return {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in output_dir.glob(f"*{extension}")
        if path.is_file()
    }


def _find_downloaded_files(
    output_dir: Path,
    prefix: str,
    mode: YouTubeDownloadMode,
    *,
    before_outputs: dict[Path, tuple[int, int]] | None = None,
    required: bool = True,
) -> list[Path]:
    """Find all files produced by yt-dlp for the current request."""
    extension = ".mp4" if mode == "video" else ".mp3"
    if mode == "video" or before_outputs is None:
        candidates = sorted(output_dir.glob(f"{prefix}_*{extension}"))
    else:
        candidates = []
        for path in sorted(output_dir.glob(f"*{extension}")):
            if not path.is_file():
                continue
            current = (path.stat().st_mtime_ns, path.stat().st_size)
            if before_outputs.get(path) != current:
                candidates.append(path)
    if required and not candidates:
        raise RuntimeError(f"yt-dlp completed but no {extension} output was found")
    return candidates


def _find_downloaded_file(output_dir: Path, prefix: str, mode: YouTubeDownloadMode) -> Path:
    """Find the last file produced by yt-dlp for a known URL prefix."""
    return _find_downloaded_files(output_dir, prefix, mode)[-1]


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
