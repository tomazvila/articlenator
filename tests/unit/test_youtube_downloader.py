"""Tests for YouTube downloader helpers."""

from pathlib import Path

import pytest

from twitter_articlenator.sources.youtube_downloader import (
    _build_youtube_command,
    is_supported_youtube_url,
    iter_youtube_download,
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


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=abc",
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
    assert cmd[cmd.index("-f") + 1] == "18/b[ext=mp4][protocol=https]/b"
    assert "--audio-format" in cmd
    assert "mp3" in cmd
    assert "--audio-quality" not in cmd


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
