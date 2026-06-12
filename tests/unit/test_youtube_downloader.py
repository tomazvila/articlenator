"""Tests for YouTube downloader helpers."""

from pathlib import Path

import pytest

from twitter_articlenator.sources.youtube_downloader import (
    _build_youtube_command,
    _find_downloaded_files,
    _only_skippable_playlist_errors,
    _snapshot_downloaded_files,
    is_supported_youtube_url,
    iter_youtube_download,
    youtube_url_kind,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=fv7TlVMETP0",
        "https://youtu.be/tc82YJfvXZo",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/live/abc123",
        "https://www.youtube.com/embed/abc123",
    ],
)
def test_is_supported_youtube_url_accepts_individual_video_urls(url):
    """Test supported individual YouTube URL shapes."""
    assert is_supported_youtube_url(url)
    assert youtube_url_kind(url) == "video"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PLabc123",
        "https://music.youtube.com/playlist?list=PLabc123",
        "https://www.youtube.com/watch?v=fv7TlVMETP0&list=PLabc123",
    ],
)
def test_is_supported_youtube_url_accepts_playlist_urls(url):
    """Test supported playlist URL shapes."""
    assert is_supported_youtube_url(url)
    assert youtube_url_kind(url) == "playlist"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist",
        "https://www.youtube.com/@channel",
        "https://example.com/watch?v=fv7TlVMETP0",
        "ftp://www.youtube.com/watch?v=fv7TlVMETP0",
        "not-a-url",
    ],
)
def test_is_supported_youtube_url_rejects_non_video_urls(url):
    """Test unsupported YouTube URL shapes."""
    assert not is_supported_youtube_url(url)


def test_build_video_command_uses_mp4_options():
    """Test video command asks yt-dlp for MP4 output."""
    cmd = _build_youtube_command(
        url="https://youtu.be/abc",
        mode="video",
        output_template=Path("/tmp/out_%(id)s.%(ext)s"),
        downloader_bin="yt-dlp",
    )

    assert "--no-playlist" in cmd
    assert "--yes-playlist" not in cmd
    assert cmd[cmd.index("--js-runtimes") + 1] == "node"
    assert cmd[cmd.index("-f") + 1] == "18/b[ext=mp4][protocol=https]/b"
    assert "--merge-output-format" in cmd
    assert "--remux-video" in cmd
    assert "mp4" in cmd


def test_build_mp3_command_uses_default_quality():
    """Test MP3 command avoids an explicit audio quality override."""
    cmd = _build_youtube_command(
        url="https://youtu.be/abc",
        mode="mp3",
        output_template=Path("/tmp/out_%(id)s.%(ext)s"),
        downloader_bin="yt-dlp",
    )

    assert "-x" in cmd
    assert cmd[cmd.index("--js-runtimes") + 1] == "node"
    assert cmd[cmd.index("-f") + 1] == "bestaudio/b"
    assert "--audio-format" in cmd
    assert "mp3" in cmd
    assert "--embed-metadata" in cmd
    assert "--embed-thumbnail" in cmd
    assert cmd[cmd.index("--convert-thumbnails") + 1] == "jpg"
    assert "--replace-in-metadata" in cmd

    metadata_rules = [
        cmd[index + 1] for index, value in enumerate(cmd) if value == "--parse-metadata"
    ]
    assert "title:%(artist)s - %(title)s" in metadata_rules
    assert "%(artist,uploader|)s:%(meta_artist)s" in metadata_rules
    assert "%(track,title|)s:%(meta_title)s" in metadata_rules
    assert "playlist_title:%(meta_album)s" not in metadata_rules
    assert "playlist_index:%(track_number)s" not in metadata_rules
    assert "--audio-quality" not in cmd


def test_build_playlist_command_explicitly_downloads_playlist():
    """Test playlist command asks yt-dlp to download the whole playlist."""
    cmd = _build_youtube_command(
        url="https://www.youtube.com/playlist?list=PLabc123",
        mode="mp3",
        output_template=Path("/tmp/out_%(id)s.%(ext)s"),
        downloader_bin="yt-dlp",
        playlist=True,
    )

    assert "--yes-playlist" in cmd
    assert "--no-abort-on-error" in cmd
    assert "--no-playlist" not in cmd
    assert cmd[-1] == "https://www.youtube.com/playlist?list=PLabc123"

    metadata_rules = [
        cmd[index + 1] for index, value in enumerate(cmd) if value == "--parse-metadata"
    ]
    assert "playlist_title:%(meta_album)s" in metadata_rules
    assert "playlist_index:%(track_number)s" in metadata_rules


def test_find_downloaded_mp3_files_uses_created_or_changed_human_names(tmp_path):
    """Test MP3 discovery supports artist/title filenames without URL prefixes."""
    existing = tmp_path / "System Of A Down - Toxicity [old].mp3"
    existing.write_bytes(b"old")
    before = _snapshot_downloaded_files(tmp_path, "mp3")

    downloaded = tmp_path / "System Of A Down - B.Y.O.B [zUzd9KyIDrM].mp3"
    downloaded.write_bytes(b"new")
    existing.write_bytes(b"changed")

    assert _find_downloaded_files(
        tmp_path,
        "youtube_mp3_unused",
        "mp3",
        before_outputs=before,
    ) == [downloaded, existing]


def test_only_skippable_playlist_errors_accepts_unavailable_videos():
    """Test playlist partial success only tolerates unavailable video errors."""
    stderr = "\n".join(
        [
            "ERROR: [youtube] abc: Video unavailable. This video is not available",
            "ERROR: [youtube] def: Video unavailable. This video is not available",
        ]
    )

    assert _only_skippable_playlist_errors(stderr)


def test_only_skippable_playlist_errors_accepts_any_per_video_error():
    """Test every per-video extraction error wording is tolerated, not just one phrase."""
    stderr = "\n".join(
        [
            "ERROR: [youtube] fDWFVI8PQOI: This video has been removed for violating "
            "YouTube's Terms of Service",
            "ERROR: [youtube] QZSDbUTgGXM: Video unavailable. This video is no longer "
            "available due to a copyright claim by Manners McDade Music Publishing",
            "ERROR: [youtube] i5CnUpxUgNE: Video unavailable. This video is no longer "
            "available because the YouTube account associated with this video has been "
            "terminated.",
            "ERROR: [youtube] M-FlrqHE5MY: Video unavailable. This video is not available",
            "ERROR: [youtube] _0-aZb9c1D2: Private video. Sign in if you've been granted "
            "access to this video",
        ]
    )

    assert _only_skippable_playlist_errors(stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "WARNING: [youtube] abc: Video unavailable. This video is not available",
        "ERROR: Postprocessing: audio conversion failed: Conversion failed!",
        "ERROR: [Errno 28] No space left on device",
        "ERROR: unable to download video data: <urlopen error [Errno -3] Temporary failure>",
        "\n".join(
            [
                "ERROR: [youtube] abc: Video unavailable. This video is not available",
                "ERROR: [Errno 28] No space left on device",
            ]
        ),
    ],
)
def test_only_skippable_playlist_errors_rejects_operational_failures(stderr):
    """Test storage and post-processing errors still fail the playlist source."""
    assert not _only_skippable_playlist_errors(stderr)


def test_iter_youtube_download_rejects_invalid_mode(tmp_path):
    """Test invalid modes are rejected before running a command."""
    with pytest.raises(ValueError, match="Unsupported"):
        list(
            iter_youtube_download(
                "https://youtu.be/abc",
                tmp_path,
                mode="wav",  # type: ignore[arg-type]
            )
        )


def test_iter_youtube_download_rejects_raw_and_file_cookies_together(tmp_path):
    """Test callers cannot provide both raw cookies and a cookie file path."""
    with pytest.raises(ValueError, match="either raw cookies or cookie_file_path"):
        list(
            iter_youtube_download(
                "https://youtu.be/abc",
                tmp_path,
                mode="video",
                cookies="raw",
                cookie_file_path=tmp_path / "cookies.txt",
            )
        )
