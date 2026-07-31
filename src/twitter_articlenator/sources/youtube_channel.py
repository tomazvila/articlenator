"""YouTube channel enumeration.

Turns a channel URL (``youtube.com/@handle``, ``/channel/UC...``, ``/c/<name>``,
``/user/<name>``) into the list of its uploaded videos as individual watch URLs,
without downloading any media. Uses yt-dlp's flat-playlist JSON dump on the
channel's ``/videos`` tab (regular uploads only — Shorts and live streams live
on separate tabs and are excluded).

Kept deliberately separate from ``youtube_downloader.youtube_url_kind`` so the
existing single-video / playlist download and transcription flows are
unaffected: those classify only ``video``/``playlist`` and would mishandle a
channel URL.
"""

from __future__ import annotations

import calendar
import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import structlog

from .youtube_downloader import YOUTUBE_VIDEO_HOSTS

log = structlog.get_logger()

# Path segments that introduce a channel by id/name (followed by the value).
_CHANNEL_PREFIXES = {"channel", "c", "user"}
_TITLE_TAB_SUFFIX = re.compile(r"\s*[-–—]\s*Videos\s*$", re.IGNORECASE)

# Tab-delimited fields for dated (non-flat) enumeration. Title is LAST so a
# stray tab inside it cannot corrupt the earlier fields (split has maxsplit=5).
_SINCE_FIELDS = "%(id)s\t%(upload_date)s\t%(duration)s\t%(channel)s\t%(webpage_url)s\t%(title)s"
# yt-dlp exit code when --break-on-reject stops the playlist early (expected on
# a newest-first channel once we pass the date cutoff) — not a failure.
_BREAK_EXIT_CODE = 101


@dataclass(frozen=True)
class ChannelVideo:
    """One uploaded video discovered on a channel."""

    video_id: str
    title: str
    url: str
    duration: float | None = None
    upload_date: str | None = None  # YYYYMMDD when known (dated enumeration only)


def months_ago_yyyymmdd(today: date, months: int) -> str:
    """Return ``YYYYMMDD`` for ``months`` calendar months before ``today``.

    The day is clamped to the target month's length (e.g. May 31 − 3 months →
    Feb 28/29). Raises ValueError for ``months < 1``.
    """
    if months < 1:
        raise ValueError(f"months must be >= 1, got {months}")
    month = today.month - months
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return f"{year:04d}{month:02d}{day:02d}"


def _is_number(value: object) -> bool:
    try:
        float(value)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class ChannelInfo:
    """A channel and the videos enumerated from its /videos tab."""

    channel_id: str | None
    channel_title: str
    channel_handle: str | None
    source_url: str
    videos: list[ChannelVideo]


def parse_channel_url(url: str) -> str | None:
    """Return the canonical ``/videos`` URL for a channel URL, else ``None``.

    Accepts ``@handle``, ``channel/<id>``, ``c/<name>`` and ``user/<name>``
    forms (with or without a trailing tab such as ``/featured``/``/streams``)
    and normalises them to ``https://www.youtube.com/<base>/videos``. Returns
    ``None`` for video, playlist, shorts, live, embed and non-YouTube URLs.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in YOUTUBE_VIDEO_HOSTS:
        return None

    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None

    first = parts[0]
    if first.startswith("@") and len(first) > 1:
        base = first
    elif first in _CHANNEL_PREFIXES and len(parts) >= 2 and parts[1]:
        base = f"{first}/{parts[1]}"
    else:
        return None

    return f"https://www.youtube.com/{base}/videos"


def is_youtube_channel_url(url: str) -> bool:
    """Return whether ``url`` is a supported YouTube channel URL."""
    return parse_channel_url(url) is not None


def _iter_video_entries(entries: object) -> Iterator[dict]:
    """Yield video entry dicts, descending into any nested entry lists."""
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("entries")
        if nested:
            yield from _iter_video_entries(nested)
        elif entry.get("id"):
            yield entry


def _parse_channel_json(data: dict, source_url: str) -> ChannelInfo:
    """Build a :class:`ChannelInfo` from yt-dlp's ``--dump-single-json`` output.

    Pure (no subprocess) so it is unit-testable against captured fixtures.
    Entries without a usable video id are dropped.
    """
    raw_title = data.get("title") or ""
    channel_title = (
        data.get("channel")
        or data.get("uploader")
        or _TITLE_TAB_SUFFIX.sub("", raw_title).strip()
        or "channel"
    )
    channel_handle = data.get("uploader_id")
    channel_id = data.get("channel_id") or data.get("id")

    videos: list[ChannelVideo] = []
    seen: set[str] = set()
    for entry in _iter_video_entries(data.get("entries")):
        video_id = str(entry["id"])
        if video_id in seen:
            continue
        seen.add(video_id)
        title = entry.get("title") or video_id
        url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
        duration = entry.get("duration")
        videos.append(
            ChannelVideo(
                video_id=video_id,
                title=str(title),
                url=str(url),
                duration=float(duration) if isinstance(duration, int | float) else None,
            )
        )

    return ChannelInfo(
        channel_id=channel_id,
        channel_title=str(channel_title),
        channel_handle=channel_handle,
        source_url=source_url,
        videos=videos,
    )


def _handle_from_canonical(canonical: str) -> str | None:
    """Extract the ``@handle`` from a canonical channel URL, if present."""
    parts = [p for p in urlparse(canonical).path.split("/") if p]
    return parts[0] if parts and parts[0].startswith("@") else None


def _parse_channel_lines(text: str, source_url: str, handle: str | None) -> ChannelInfo:
    """Build a :class:`ChannelInfo` from tab-delimited dated-enumeration output."""
    videos: list[ChannelVideo] = []
    seen: set[str] = set()
    channel_title: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 5)
        if len(parts) < 6:
            continue
        video_id, upload_date, duration, channel, webpage_url, title = parts
        if not video_id or video_id == "NA" or video_id in seen:
            continue
        seen.add(video_id)
        if not channel_title and channel and channel != "NA":
            channel_title = channel
        url = (
            webpage_url
            if webpage_url and webpage_url != "NA"
            else f"https://www.youtube.com/watch?v={video_id}"
        )
        videos.append(
            ChannelVideo(
                video_id=video_id,
                title=title or video_id,
                url=url,
                duration=float(duration) if _is_number(duration) else None,
                upload_date=upload_date if upload_date and upload_date != "NA" else None,
            )
        )
    return ChannelInfo(
        channel_id=None,
        channel_title=channel_title or handle or "channel",
        channel_handle=handle,
        source_url=source_url,
        videos=videos,
    )


def _list_channel_videos_since(
    canonical: str,
    original_url: str,
    after_date: str,
    *,
    downloader_bin: str,
    timeout_seconds: int,
) -> ChannelInfo:
    """Enumerate only videos uploaded on/after ``after_date`` (YYYYMMDD).

    Uses a non-flat extraction (flat-playlist omits upload dates) with
    ``--dateafter`` and ``--break-on-reject`` so a newest-first channel stops
    as soon as it passes the cutoff, instead of scanning the whole back catalog.

    Cookies are intentionally NOT passed here: a logged-in session can surface a
    members-only / upcoming item (with no upload date) at the top of the listing,
    which ``--break-on-reject`` then treats as the first rejection and stops on,
    yielding zero results. Listing is public metadata and needs no auth; the
    per-video download step still uses cookies.
    """
    cmd = [
        downloader_bin,
        "--no-warnings",
        "--skip-download",
        "--dateafter",
        after_date,
        "--break-on-reject",
        "--socket-timeout",
        "30",
        "--print",
        _SINCE_FIELDS,
        canonical,
    ]

    log.info("channel_dated_enumeration_starting", url=original_url, after_date=after_date)
    try:
        result = subprocess.run(
            cmd, text=True, capture_output=True, timeout=timeout_seconds, check=False
        )
    except (subprocess.SubprocessError, TimeoutError) as exc:
        raise RuntimeError(f"yt-dlp dated channel enumeration failed: {exc}") from exc
    if result.returncode not in (0, _BREAK_EXIT_CODE):
        raise RuntimeError(
            f"yt-dlp dated channel enumeration failed: {result.stderr or result.returncode}"
        )

    info = _parse_channel_lines(result.stdout, canonical, _handle_from_canonical(canonical))
    if not info.videos:
        raise RuntimeError(f"No videos newer than {after_date} for channel: {original_url}")
    log.info(
        "channel_dated_enumeration_complete",
        url=original_url,
        after_date=after_date,
        video_count=len(info.videos),
    )
    return info


def _list_channel_videos_all(
    canonical: str,
    original_url: str,
    *,
    cookie_file: Path | None,
    downloader_bin: str,
    timeout_seconds: int,
) -> ChannelInfo:
    """Enumerate every uploaded video via a fast flat-playlist JSON dump."""
    cmd = [
        downloader_bin,
        "--no-warnings",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--js-runtimes",
        "node",
        "--socket-timeout",
        "30",
    ]
    if cookie_file:
        cmd.extend(["--cookies", str(cookie_file)])
    cmd.append(canonical)

    log.info("channel_enumeration_starting", url=original_url, canonical=canonical)
    try:
        result = subprocess.run(
            cmd, text=True, capture_output=True, timeout=timeout_seconds, check=True
        )
        data = json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"yt-dlp channel enumeration failed: {exc.stderr or exc}") from exc
    except (subprocess.SubprocessError, TimeoutError) as exc:
        raise RuntimeError(f"yt-dlp channel enumeration failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp returned invalid channel JSON: {exc}") from exc

    return _parse_channel_json(data, canonical)


def filter_videos_by_title(videos: list[ChannelVideo], query: str) -> list[ChannelVideo]:
    """Return videos whose title contains ``query`` (case-insensitive)."""
    needle = query.casefold().strip()
    return [v for v in videos if needle in v.title.casefold()]


def list_channel_videos(
    url: str,
    *,
    after_date: str | None = None,
    title_contains: str | None = None,
    cookie_file: Path | None = None,
    downloader_bin: str = "yt-dlp",
    timeout_seconds: int = 600,
) -> ChannelInfo:
    """Enumerate a channel's uploaded videos without downloading media.

    Optional filters (combinable):
      * ``after_date`` (YYYYMMDD): only videos uploaded on/after that date
        (newest-first channels stop early at the cutoff).
      * ``title_contains``: only videos whose title contains this string
        (case-insensitive).

    Raises:
        ValueError: If ``url`` is not a supported channel URL.
        RuntimeError: If yt-dlp fails, times out, returns invalid JSON, or no
            (matching) videos remain after filtering.
    """
    canonical = parse_channel_url(url)
    if canonical is None:
        raise ValueError(f"Not a YouTube channel URL: {url}")

    if after_date:
        info = _list_channel_videos_since(
            canonical,
            url,
            after_date,
            downloader_bin=downloader_bin,
            timeout_seconds=timeout_seconds,
        )
    else:
        info = _list_channel_videos_all(
            canonical,
            url,
            cookie_file=cookie_file,
            downloader_bin=downloader_bin,
            timeout_seconds=timeout_seconds,
        )

    if title_contains:
        info = replace(info, videos=filter_videos_by_title(info.videos, title_contains))

    if not info.videos:
        detail = f" matching '{title_contains}'" if title_contains else ""
        raise RuntimeError(f"No videos{detail} found for channel: {url}")
    log.info(
        "channel_enumeration_complete",
        url=url,
        channel=info.channel_title,
        video_count=len(info.videos),
    )
    return info
