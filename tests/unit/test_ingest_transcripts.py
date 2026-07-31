"""Tests for the video-transcript Phase-A ingest (zettel_ralph/ingest_transcripts.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ZR = Path(__file__).resolve().parents[2] / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

import ingest_transcripts as ing  # noqa: E402


def _make_channel(channels_dir, job, channel_title, videos):
    """videos: list of (video_id, title, status, upload_date, transcript_text_or_None)."""
    jd = channels_dir / job
    (jd / "videos").mkdir(parents=True)
    manifest = {"job_id": job, "channel_title": channel_title, "videos": []}
    for vid, title, status, upd, tx in videos:
        manifest["videos"].append(
            {
                "video_id": vid,
                "title": title,
                "status": status,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "upload_date": upd,
            }
        )
        if tx is not None:
            (jd / "videos" / vid).mkdir(parents=True, exist_ok=True)
            (jd / "videos" / vid / "transcript.txt").write_text(tx, encoding="utf-8")
    (jd / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return jd


def test_iter_skips_incomplete_and_missing_transcript(tmp_path):
    ch = tmp_path / "channels"
    ch.mkdir()
    _make_channel(
        ch,
        "j1",
        "Test Chan",
        [
            ("aaa", "A", "complete", "20260101", "hello world."),
            ("bbb", "B", "pending", "20260102", "not complete -> skip"),
            ("ccc", "C", "complete", None, None),  # complete but no transcript -> skip
        ],
    )
    got = [v["video_id"] for _, v, _ in ing.iter_transcribed_videos(ch, include_tests=False)]
    assert got == ["aaa"]


def test_excludes_jawed_test_clips_unless_requested(tmp_path):
    ch = tmp_path / "channels"
    ch.mkdir()
    _make_channel(ch, "jt", "jawed", [("jNQXAC9IVRw", "Me at the zoo", "complete", "20050423", "x.")])
    _make_channel(ch, "real", "Real Chan", [("xyz", "Real", "complete", "20260101", "content.")])
    assert [v["video_id"] for _, v, _ in ing.iter_transcribed_videos(ch, include_tests=False)] == ["xyz"]
    assert sorted(v["video_id"] for _, v, _ in ing.iter_transcribed_videos(ch, include_tests=True)) == [
        "jNQXAC9IVRw",
        "xyz",
    ]


def test_write_lit_note_format(tmp_path):
    stg = tmp_path / "stg"
    video = {
        "video_id": "vid1",
        "title": "Why X Matters",
        "url": "https://www.youtube.com/watch?v=vid1",
        "upload_date": "20260321",
    }
    p = ing.write_lit_note(stg, "Chan Name", video, "  Body text here.  ")
    assert p == stg / "lit" / "vid-vid1.md"
    txt = p.read_text(encoding="utf-8")
    assert 'id: "vid-vid1"' in txt
    assert 'kind: "video"' in txt
    assert 'author: "Chan Name"' in txt
    assert 'title: "Why X Matters"' in txt
    assert 'source_url: "https://www.youtube.com/watch?v=vid1"' in txt
    assert 'published_at: "2026-03-21"' in txt
    assert "# Why X Matters" in txt
    assert "Body text here." in txt


def test_published_iso():
    assert ing._published_iso("20260321") == "2026-03-21"
    assert ing._published_iso(None) is None
    assert ing._published_iso("garbage") is None


def test_main_end_to_end_and_idempotent(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    _make_channel(
        ch,
        "j1",
        "Chan",
        [
            ("v1", "One", "complete", "20260101", "first transcript."),
            ("v2", "Two", "complete", "20260102", "second transcript."),
        ],
    )
    stg = tmp_path / "stg"
    argv = ["ingest_transcripts.py", "--channels-dir", str(ch), "--staging", str(stg)]
    monkeypatch.setattr(sys, "argv", argv)
    ing.main()

    queue = json.loads((stg / "queue.json").read_text())
    items = {it["id"]: it for it in queue["items"]}
    assert set(items) == {"vid-v1", "vid-v2"}
    assert all(it["stage"] == "extracted" and it["kind"] == "video" for it in items.values())
    assert (stg / "lit" / "vid-v1.md").exists()
    # Phase-B state seeded so the synthesis loop can run standalone.
    for name in ("concept-index.json", "provenance.json", "STATE.md", "DECISIONS.md"):
        assert (stg / name).exists()

    # Idempotent: a second run stages nothing new.
    monkeypatch.setattr(sys, "argv", argv)
    ing.main()
    queue2 = json.loads((stg / "queue.json").read_text())
    assert len(queue2["items"]) == 2
