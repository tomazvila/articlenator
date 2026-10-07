"""Tests for batch_channel.py - the resumable channel transcription pipeline.

Tests cover:
    - _channel_handle extraction from various YouTube URL formats
    - _elapsed time formatting
    - enumerate_channel error path
    - write_manifest
    - process_one_video edge cases
    - Argument parsing
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[2]
BATCH = HERE / "zettel_ralph" / "batch_channel.py"

SPEC = importlib.util.spec_from_file_location("batch_channel", BATCH)
batch = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(batch)


# ── _channel_handle ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/@sthenics_", "sthenics_"),
        ("https://www.youtube.com/@sthenics_/videos", "sthenics_"),
        ("https://www.youtube.com/@sthenics_/featured", "sthenics_"),
        ("@sthenics_", "sthenics_"),
        ("https://www.youtube.com/@SasaVenos", "SasaVenos"),
        ("https://youtube.com/@handle123", "handle123"),
        ("https://m.youtube.com/@handle", "handle"),
        # No @handle - falls back to full URL
        ("https://www.youtube.com/channel/UC123", "https://www.youtube.com/channel/UC123"),
    ],
)
def test_channel_handle(url, expected):
    assert batch._channel_handle(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "https://example.com",
    ],
)
def test_channel_handle_no_match_returns_url(url):
    """When no @handle is found, the full URL is returned."""
    result = batch._channel_handle(url)
    assert result == url


# ── _elapsed ─────────────────────────────────────────────────────────────────

def test_elapsed_seconds():
    result = batch._elapsed(time.time() - 45)
    assert "m" in result
    assert "s" in result


def test_elapsed_minutes():
    result = batch._elapsed(time.time() - 125)
    assert "m" in result


def test_elapsed_hours():
    result = batch._elapsed(time.time() - 7320)
    assert "h" in result


def test_elapsed_zero():
    result = batch._elapsed(time.time())
    assert "0m" in result or "m" in result


# ── write_manifest ───────────────────────────────────────────────────────────

def _broken_ytdlp(monkeypatch):
    """Point YTDLP_BIN at a non-existent binary so write_manifest
    fails fast (FileNotFoundError → caught by except → uses handle)."""
    monkeypatch.setattr(batch, "YTDLP_BIN", "/nonexistent-yt-dlp")
    monkeypatch.setattr(batch, "COOKIES_FILE", "/dev/null")


def test_write_manifest_creates_new(tmp_path, monkeypatch):
    """write_manifest creates a new manifest from scratch.

    Changed for package A: a job with one failed video is "partial", not
    "complete", and the failed entry keeps its error message. A "complete"
    entry needs transcript.txt on disk.
    """
    _broken_ytdlp(monkeypatch)
    job_dir = tmp_path / "sthenics_"
    job_dir.mkdir(parents=True)
    (job_dir / "videos" / "abc123").mkdir(parents=True)
    (job_dir / "videos" / "abc123" / "transcript.txt").write_text("hello.\n")

    videos = [
        {"video_id": "abc123", "title": "Test Video 1", "url": "https://youtube.com/watch?v=abc123", "duration": 300.0, "upload_date": "20240101"},
        {"video_id": "def456", "title": "Test Video 2", "url": "https://youtube.com/watch?v=def456", "duration": 600.0, "upload_date": "20240102"},
    ]
    results = [
        {"video_id": "abc123", "status": "ok", "error": None, "title": "Test Video 1", "chars": 1000},
        {"video_id": "def456", "status": "error", "error": "something broke", "title": "Test Video 2"},
    ]

    manifest = batch.write_manifest(
        job_dir, "https://www.youtube.com/@sthenics_/videos", videos, results
    )

    assert manifest["job_id"] == "sthenics_"
    assert manifest["status"] == "partial"
    assert manifest["done_videos"] == 1 and manifest["failed_videos"] == 1
    assert len(manifest["videos"]) == 2
    abc = next(v for v in manifest["videos"] if v["video_id"] == "abc123")
    assert abc["status"] == "complete"
    assert abc["asr_model"] == "unknown"  # older transcript: model not recorded
    defv = next(v for v in manifest["videos"] if v["video_id"] == "def456")
    assert defv["status"] == "error"
    assert defv["error"] == "something broke"


def test_manifest_written_to_disk(tmp_path, monkeypatch):
    """write_manifest writes manifest.json."""
    _broken_ytdlp(monkeypatch)
    job_dir = tmp_path / "sthenics_"
    job_dir.mkdir(parents=True)

    videos = [
        {"video_id": "abc123", "title": "Test Video", "url": "https://youtube.com/watch?v=abc123", "duration": 300.0, "upload_date": "20240101"},
    ]
    results = [
        {"video_id": "abc123", "status": "ok", "error": None, "title": "Test Video", "chars": 500},
    ]

    batch.write_manifest(
        job_dir, "https://www.youtube.com/@sthenics_/videos", videos, results
    )

    manifest_path = job_dir / "manifest.json"
    assert manifest_path.exists()
    loaded = json.loads(manifest_path.read_text())
    assert loaded["job_id"] == "sthenics_"
    assert loaded["videos"][0]["video_id"] == "abc123"


# ── process_one_video ────────────────────────────────────────────────────────

def test_process_one_video_skips_existing_transcript(tmp_path):
    """If transcript.txt exists, process_one_video returns already_done."""
    args = {
        "video": {"video_id": "abc123", "title": "Test"},
        "job_dir": str(tmp_path / "sthenics_"),
    }
    video_dir = tmp_path / "sthenics_" / "videos" / "abc123"
    video_dir.mkdir(parents=True)
    (video_dir / "transcript.txt").write_text("Hello world.\n")

    result = batch.process_one_video(args)
    assert result["status"] == "already_done"
    assert result["video_id"] == "abc123"


def test_process_one_video_missing_ytdlp(tmp_path):
    """When yt-dlp is not available, process_one_video returns an error."""
    video_dir = tmp_path / "test" / "videos" / "abc123"
    video_dir.mkdir(parents=True)

    args = {
        "video": {"video_id": "abc123", "title": "Test"},
        "job_dir": str(tmp_path / "test"),
        "ytdlp": "/nonexistent/yt-dlp",
        "timeout": 30,
    }

    result = batch.process_one_video(args)
    assert result["status"] == "error"
    assert result["video_id"] == "abc123"


# ── enumerate_channel (error path) ────────────────────────────────────────────

def test_enumerate_channel_no_ytdlp(monkeypatch):
    """When yt-dlp is not available, enumerate_channel raises FileNotFoundError."""
    monkeypatch.setattr(batch, "YTDLP_BIN", "/nonexistent/yt-dlp")
    with pytest.raises(FileNotFoundError):
        batch.enumerate_channel("https://www.youtube.com/@sthenics_/videos")


# ── Job directory structure ──────────────────────────────────────────────────

def test_job_dir_path(tmp_path):
    """The job directory is created with the channel handle under channels-dir."""
    channels_dir = tmp_path / "channels"
    handle = batch._channel_handle("https://www.youtube.com/@sthenics_/videos")
    job_dir = channels_dir / handle.lstrip("@")
    assert str(job_dir).endswith("sthenics_")


def test_video_dir_created(tmp_path):
    """The videos/<id> subdirectory is created when processing."""
    job_dir = tmp_path / "sthenics_"
    video_dir = job_dir / "videos" / "abc123"
    video_dir.mkdir(parents=True)
    assert video_dir.exists()
    assert video_dir.is_dir()


# ── FAILED_FILE constant ──────────────────────────────────────────────────────

def test_failed_file_constant():
    """FAILED_FILE should be a valid filename."""
    assert batch.FAILED_FILE == "failed.txt"


# ── Argument parsing ─────────────────────────────────────────────────────────

def test_main_parser_requires_channel():
    """The --channel argument is required by argparse."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--channels-dir", type=Path, default="/tmp")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--synthesize-only", action="store_true")
    parser.add_argument("--transcribe-only", action="store_true")

    # Without --channel should error
    with pytest.raises(SystemExit):
        parser.parse_args([])

    # With --channel should pass
    args = parser.parse_args(["--channel", "@sthenics_"])
    assert args.channel == "@sthenics_"


def test_main_parser_limit_default():
    """--limit defaults to 0 (all videos)."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(["--channel", "@test"])
    assert args.limit == 0


def test_main_parser_limit_custom():
    """--limit can be set to a positive integer."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(["--channel", "@test", "--limit", "5"])
    assert args.limit == 5


def test_main_parser_preview_flag():
    """--preview should be a boolean flag."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--preview", action="store_true")
    # Without --preview
    args = parser.parse_args(["--channel", "@test"])
    assert args.preview is False
    # With --preview
    args = parser.parse_args(["--channel", "@test", "--preview"])
    assert args.preview is True


def test_main_parser_synthesize_only():
    """--synthesize-only should be a boolean flag."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--synthesize-only", action="store_true")
    args = parser.parse_args(["--channel", "@test", "--synthesize-only"])
    assert args.synthesize_only is True


def test_main_parser_transcribe_only():
    """--transcribe-only should be a boolean flag."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--transcribe-only", action="store_true")
    args = parser.parse_args(["--channel", "@test", "--transcribe-only"])
    assert args.transcribe_only is True


def test_main_parser_channel_url_shortcuts():
    """Channel URL expansion: handle without http should get prefix."""
    handle = "@sthenics_"
    if not handle.startswith("http"):
        handle = f"https://www.youtube.com/{handle}/videos"
    assert handle == "https://www.youtube.com/@sthenics_/videos"

# ── package A: model, whisper output, titles, failures ───────────────────────

import os  # noqa: E402
import stat  # noqa: E402
import wave  # noqa: E402

FAKE_WHISPER = """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
prefix = args[args.index("-of") + 1]
model = args[args.index("-m") + 1]
open(sys.argv[0] + ".args", "w").write(json.dumps(args))
lines = [f"In part {i} we train the planche lean for {i + 2} sets." for i in range(30)]
open(prefix + ".txt", "w").write("\\n".join(lines) + "\\n")
doc = {"params": {"model": model},
       "transcription": [{"offsets": {"from": i * 5000, "to": (i + 1) * 5000}, "text": " " + t}
                         for i, t in enumerate(lines)]}
open(prefix + ".json", "w").write(json.dumps(doc))
sys.stderr.write("whisper_model_load: type = 5 (large v3)\\n")
sys.exit(int(open(sys.argv[0] + ".rc").read()) if __import__("os").path.exists(sys.argv[0] + ".rc") else 0)
"""


def _fake_whisper(tmp_path, rc=0):
    exe = tmp_path / "fake-whisper-cli"
    exe.write_text(FAKE_WHISPER)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    if rc:
        (tmp_path / "fake-whisper-cli.rc").write_text(str(rc))
    return exe


def _wav(path, seconds=150):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000 * seconds)


def _model(tmp_path, name="ggml-large-v3.bin"):
    m = tmp_path / "models" / name
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_bytes(b"model")
    return m


def test_check_whisper_model_missing_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not fall back"):
        batch.check_whisper_model(str(tmp_path / "ggml-large-v3.bin"))


def test_process_one_video_records_real_model_segments_and_quality(tmp_path):
    job = tmp_path / "job"
    vdir = job / "videos" / "vid1"
    _wav(vdir / "audio.wav")
    model = _model(tmp_path, "ggml-small.en.bin")
    exe = _fake_whisper(tmp_path)
    r = batch.process_one_video({
        "video": {"video_id": "vid1", "title": "Real Title", "duration": 150.0,
                  "url": "https://www.youtube.com/watch?v=vid1", "upload_date": "20260101"},
        "job_dir": str(job), "whisper_bin": str(exe), "whisper_model": str(model),
        "prompt": "Glossary: planche.", "language": "en", "timeout": 30,
    })
    assert r["status"] == "ok", r
    assert r["asr_model"] == "ggml-small.en"  # from the file, not a constant
    assert r["asr_quality"] == "ok"
    args = json.loads((tmp_path / "fake-whisper-cli.args").read_text())
    assert "-oj" in args and "-otxt" in args
    assert args[args.index("--prompt") + 1] == "Glossary: planche."
    for name in ("transcript.txt", "transcript.json", "transcript.vtt", "asr.json",
                 "meta.json", "whisper.log"):
        assert (vdir / name).exists(), name
    tj = json.loads((vdir / "transcript.json").read_text())
    assert tj["segments"][1]["start"] == 5.0
    assert tj["model_file"] == "ggml-small.en.bin"
    meta = json.loads((vdir / "meta.json").read_text())
    assert meta["video_id"] == "vid1" and meta["title"] == "Real Title"


def test_process_one_video_whisper_failure_is_an_error_without_transcript(tmp_path):
    job = tmp_path / "job"
    vdir = job / "videos" / "vid1"
    _wav(vdir / "audio.wav")
    exe = _fake_whisper(tmp_path, rc=3)
    r = batch.process_one_video({
        "video": {"video_id": "vid1", "title": "T"}, "job_dir": str(job),
        "whisper_bin": str(exe), "whisper_model": str(_model(tmp_path)), "timeout": 30,
    })
    assert r["status"] == "error"
    assert "exit code 3" in r["error"]
    assert not (vdir / "transcript.txt").exists()
    assert not (vdir / "whisper.json").exists()


def test_process_one_video_missing_model_does_not_transcribe(tmp_path):
    job = tmp_path / "job"
    _wav(job / "videos" / "vid1" / "audio.wav")
    exe = _fake_whisper(tmp_path)
    r = batch.process_one_video({
        "video": {"video_id": "vid1", "title": "T"}, "job_dir": str(job),
        "whisper_bin": str(exe), "whisper_model": str(tmp_path / "nope.bin"), "timeout": 30,
    })
    assert r["status"] == "error" and "not found" in r["error"]
    assert not (tmp_path / "fake-whisper-cli.args").exists()


def test_manifest_titles_come_from_meta_json_by_id(tmp_path, monkeypatch):
    """Regression: sthenics_ had 4 of 10 titles on the wrong ids.

    Cause: the inline manifest builder in run_channel_sthenics.sh got 0 videos from a
    second yt-dlp call, and the manifest was then written by hand. Now titles come
    from meta.json (same yt-dlp entry as the id) and a rebuild needs no network.
    """
    _broken_ytdlp(monkeypatch)
    job = tmp_path / "sthenics_"
    right = {
        "9yFK1MTYVFE": "Stop Following Methods, Start Understanding Training",
        "2o5uPfmsO48": "Debunking Calisthenics Myths w. @saša venos Ep.1",
    }
    for vid, title in right.items():
        vdir = job / "videos" / vid
        vdir.mkdir(parents=True)
        (vdir / "transcript.txt").write_text("x y z.\n")
        batch.write_video_meta(vdir, {"video_id": vid, "title": title}, source="test")
    # A hand-written manifest with swapped titles (the real defect).
    (job / "manifest.json").write_text(json.dumps({"videos": [
        {"video_id": "9yFK1MTYVFE", "title": "How To Structure A Calisthenics Program",
         "status": "complete", "speakers": ["Host", "Guest"]},
        {"video_id": "2o5uPfmsO48", "title": "How To Warm Up For Calisthenics (Full Routine)",
         "status": "complete"},
    ]}))
    m = batch.write_manifest(job, "https://www.youtube.com/@sthenics_/videos", [], [])
    got = {v["video_id"]: v for v in m["videos"]}
    for vid, title in right.items():
        assert got[vid]["title"] == title
        assert got[vid]["title_source"] == "meta.json"
    assert got["9yFK1MTYVFE"]["speakers"] == ["Host", "Guest"]  # operator field kept
    assert m["status"] == "complete"


def test_manifest_keeps_pending_and_error_apart(tmp_path, monkeypatch):
    _broken_ytdlp(monkeypatch)
    job = tmp_path / "c"
    videos = [{"video_id": "a", "title": "A"}, {"video_id": "b", "title": "B"}]
    m = batch.write_manifest(job, "https://www.youtube.com/@c/videos", videos,
                             [{"video_id": "a", "status": "timeout", "error": "timeout"}])
    got = {v["video_id"]: v for v in m["videos"]}
    assert got["a"]["status"] == "error" and got["a"]["error"] == "timeout"
    assert got["b"]["status"] == "pending"
    assert m["status"] == "partial" and m["pending_videos"] == 1


def test_enumerate_channel_failure_is_not_an_empty_channel(tmp_path, monkeypatch):
    exe = tmp_path / "fail-yt-dlp"
    exe.write_text("#!/bin/sh\necho 'Sign in to confirm you are not a bot' >&2\nexit 1\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(batch, "YTDLP_BIN", str(exe))
    with pytest.raises(RuntimeError, match="not a bot"):
        batch.enumerate_channel("https://www.youtube.com/@x/videos")


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"prompt": "none"}, None),
        ({"prompt": "Custom words."}, "Custom words."),
        ({"vocabulary": "none"}, None),
        ({"vocabulary": "planche, maltese"}, "Glossary: planche, maltese."),
        ({"previous": ""}, None),
        ({"previous": "Saved prompt."}, "Saved prompt."),
    ],
)
def test_resolve_prompt(kwargs, expected):
    base = {"prompt": None, "vocabulary": None, "vocabulary_file": None, "previous": None}
    base.update(kwargs)
    assert batch.resolve_prompt(**base) == expected


def test_resolve_prompt_default_is_calisthenics_glossary():
    p = batch.resolve_prompt(prompt=None, vocabulary=None, vocabulary_file=None, previous=None)
    assert "planche" in p and "front lever" in p


def test_parser_accepts_new_flags():
    args = batch.build_parser().parse_args([
        "--channel", "@x", "--whisper-model", "/m.bin", "--vocabulary", "none",
        "--manifest-only", "--allow-degraded", "--channel-owner", "Someone",
    ])
    assert args.whisper_model == "/m.bin" and args.manifest_only and args.allow_degraded
    assert os.path.basename(batch.__file__) == "batch_channel.py"
