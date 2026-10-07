"""Tests for the video-transcript Phase-A ingest (zettel_ralph/ingest_transcripts.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ZR = Path(__file__).resolve().parents[2] / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

import ingest_transcripts as ing  # noqa: E402


def _make_channel(channels_dir, job, channel_title, videos, *, meta=True):
    """videos: list of (video_id, title, status, upload_date, transcript_text_or_None).

    ``meta=True`` writes videos/<id>/meta.json with the same title (a title that
    batch_channel.py verified by id). ``meta=False`` gives a hand-written manifest.
    """
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
        if meta:
            (jd / "videos" / vid).mkdir(parents=True, exist_ok=True)
            (jd / "videos" / vid / "meta.json").write_text(
                json.dumps({"video_id": vid, "title": title, "upload_date": upd}), encoding="utf-8"
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
    # Changed in round 2: provenance.json is not seeded (the harness owns provenance).
    assert not (stg / "provenance.json").exists()
    assert (stg / "transcripts.json").exists()
    for name in ("concept-index.json", "STATE.md", "DECISIONS.md"):
        assert (stg / name).exists()

    # Idempotent: a second run stages nothing new.
    monkeypatch.setattr(sys, "argv", argv)
    ing.main()
    queue2 = json.loads((stg / "queue.json").read_text())
    assert len(queue2["items"]) == 2


# ── package A: quality check, timestamps, speakers, titles, failures ─────────


def _normal_lines(n):
    return [f"In part {i} we train the planche lean for {i % 6 + 2} sets of holds." for i in range(n)]


def _frontmatter(path):
    text = path.read_text(encoding="utf-8")
    head = text.split("\n---\n", 1)[0].removeprefix("---\n")
    out = {}
    for line in head.splitlines():
        key, _, value = line.partition(": ")
        out[key] = json.loads(value)
    return out, text


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["ingest_transcripts.py", *map(str, argv)])
    ing.main()


def test_degraded_transcript_is_held_unless_allowed(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    looped = "\n".join(_normal_lines(30) + ["You have to have a very high level of muscle strength."] * 300)
    _make_channel(ch, "j", "sthenics_", [
        ("good", "Good", "complete", "20260101", "\n".join(_normal_lines(100))),
        ("loop", "Looped", "complete", "20260102", looped),
    ])
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    items = {it["id"]: it for it in json.loads((stg / "queue.json").read_text())["items"]}
    assert items["vid-good"]["stage"] == "extracted"
    assert items["vid-loop"]["stage"] == "held"
    assert "asr_quality degraded" in items["vid-loop"]["error"]
    fm, _ = _frontmatter(stg / "lit" / "vid-loop.md")
    assert fm["asr_quality"] == "degraded"
    # Round 3: lit body line numbers (transcript line + 3: blank, "# title", blank).
    assert any(i.startswith("loop: lines 34-333") for i in fm["asr_issues"])
    assert fm["asr_damaged_spans"] == ["lines 34-333"]
    report = json.loads((stg / "ingest_report.json").read_text())
    assert [h["id"] for h in report["held"]] == ["vid-loop"]

    # The override flag queues it for synthesis; the lit note still says degraded.
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg, "--allow-degraded")
    items = {it["id"]: it for it in json.loads((stg / "queue.json").read_text())["items"]}
    assert items["vid-loop"]["stage"] == "extracted"
    assert items["vid-loop"]["allowed_degraded"] is True
    assert _frontmatter(stg / "lit" / "vid-loop.md")[0]["asr_quality"] == "degraded"
    assert len(items) == 2


def test_claimed_items_are_not_moved(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    looped = "\n".join(["I'm going to do dynamic exercises."] * 100)
    _make_channel(ch, "j", "c", [("v", "V", "complete", None, looped)])
    stg = tmp_path / "stg"
    stg.mkdir()
    (stg / "queue.json").write_text(json.dumps({"version": 1, "items": [
        {"id": "vid-v", "stage": "synthesized", "lit_note": "x", "notes_emitted": ["A"]}]}))
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    item = json.loads((stg / "queue.json").read_text())["items"][0]
    assert item["stage"] == "synthesized" and item["notes_emitted"] == ["A"]
    assert item["asr_quality"] == "degraded"
    report = json.loads((stg / "ingest_report.json").read_text())
    assert report["degraded_already_processed"][0]["id"] == "vid-v"
    assert not (stg / "lit" / "vid-v.md").exists()


def test_lit_note_contract_with_timestamps_speakers_and_model(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    lines = _normal_lines(80)
    jd = _make_channel(ch, "sthenics_", "sthenics_", [
        ("abc", "Debunking Calisthenics Myths w. @saša venos Ep.1", "complete", "20260105",
         "\n".join(lines)),
    ])
    _make_channel(ch, "SasaVenos", "SasaVenos", [])
    vdir = jd / "videos" / "abc"
    meta = json.loads((vdir / "meta.json").read_text())
    meta["duration"] = 400.0  # 80 segments of 5 s
    (vdir / "meta.json").write_text(json.dumps(meta))
    (vdir / "whisper.json").write_text(json.dumps({
        "params": {"model": "/nix/store/hash-ggml-large-v3.bin"},
        "transcription": [{"offsets": {"from": i * 5000, "to": i * 5000 + 4000}, "text": t}
                          for i, t in enumerate(lines)],
    }))
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    fm, text = _frontmatter(stg / "lit" / "vid-abc.md")
    assert fm["id"] == "vid-abc" and fm["video_id"] == "abc"
    assert fm["source_url"] == "https://www.youtube.com/watch?v=abc"
    assert fm["channel"] == "sthenics_" and fm["author"] == "sthenics_"
    assert fm["speakers"] == ["sthenics_", "SasaVenos"]
    assert fm["speakers_source"] == "title-mention"
    assert fm["published_at"] == "2026-01-05"
    assert fm["asr_model"] == "ggml-large-v3"
    assert fm["asr_quality"] == "ok" and fm["asr_issues"] == []
    assert fm["title_trusted"] is True and fm["multi_speaker"] is True
    assert fm["speakers_found_by"] == ["channel-owner", "title-mention"]
    assert fm["asr_source"] == str(vdir) and fm["canonical"] is True
    assert fm["timestamps"] is True
    assert "speakers: [\"sthenics_\", \"SasaVenos\"]" in text
    assert "\n[00:00] In part 0 we train" in text
    assert "\n[00:15] In part 3 we train" in text


def test_speaker_override_from_manifest(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    jd = _make_channel(ch, "j", "Radoslav Radev ⎮ Calisthenics Mastery", [
        ("a", "Talk", "complete", None, "\n".join(_normal_lines(40))),
        ("b", "Interview", "complete", None, "\n".join(_normal_lines(40))),
    ])
    m = json.loads((jd / "manifest.json").read_text())
    m["videos"][1]["speakers"] = ["Guest Coach", "Radoslav Radev"]
    (jd / "manifest.json").write_text(json.dumps(m))
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    assert _frontmatter(stg / "lit" / "vid-a.md")[0]["speakers"] == ["Radoslav Radev"]
    fm_b = _frontmatter(stg / "lit" / "vid-b.md")[0]
    assert fm_b["speakers"] == ["Guest Coach", "Radoslav Radev"]
    assert fm_b["speakers_source"] == "manual"  # round 2: summary value of the contract
    assert fm_b["speakers_found_by"] == ["manifest", "manifest"]


def test_title_from_meta_json_wins_over_wrong_manifest_title(tmp_path, monkeypatch):
    """Regression for the sthenics_ defect: a manifest title on the wrong id."""
    ch = tmp_path / "channels"
    ch.mkdir()
    jd = _make_channel(ch, "sthenics_", "sthenics_", [
        ("9yFK1MTYVFE", "How To Structure A Calisthenics Program", "complete", None,
         "\n".join(_normal_lines(40))),
    ])
    (jd / "videos" / "9yFK1MTYVFE" / "meta.json").write_text(json.dumps({
        "video_id": "9yFK1MTYVFE",
        "title": "Stop Following Methods, Start Understanding Training"}))
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    fm, text = _frontmatter(stg / "lit" / "vid-9yFK1MTYVFE.md")
    assert fm["title"] == "Stop Following Methods, Start Understanding Training"
    assert fm["title_source"] == "meta.json"
    assert fm["title_manifest"] == "How To Structure A Calisthenics Program"
    assert "# Stop Following Methods" in text
    report = json.loads((stg / "ingest_report.json").read_text())
    assert report["title_mismatch"][0]["id"] == "vid-9yFK1MTYVFE"


def test_failed_and_partial_videos_are_reported_not_staged(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    jd = _make_channel(ch, "j", "c", [
        ("ok1", "OK", "complete", None, "\n".join(_normal_lines(40))),
        ("err", "Err", "error", None, None),
        ("part", "Partial", "complete", None, "half a transcript."),
    ])
    m = json.loads((jd / "manifest.json").read_text())
    m["videos"][1]["error"] = "timeout"
    (jd / "manifest.json").write_text(json.dumps(m))
    (jd / "videos" / "part" / "manifest.json").write_text(
        json.dumps({"status": "transcribing", "error": None}))
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    ids = [it["id"] for it in json.loads((stg / "queue.json").read_text())["items"]]
    assert ids == ["vid-ok1"]
    reasons = {s["id"]: s["reason"] for s in
               json.loads((stg / "ingest_report.json").read_text())["skipped_incomplete"]}
    assert reasons["vid-err"] == "manifest status 'error': timeout"
    assert "partial transcription" in reasons["vid-part"]


def test_legacy_constant_model_label_is_not_trusted(tmp_path, monkeypatch):
    """Old per-video manifests say model "large-v3" although small.en ran."""
    ch = tmp_path / "channels"
    ch.mkdir()
    jd = _make_channel(ch, "r", "Radoslav Radev", [
        ("x", "X", "complete", None, " ".join(_normal_lines(40)))])
    (jd / "videos" / "x" / "manifest.json").write_text(
        json.dumps({"status": "complete", "model": "large-v3", "duration": 240.0}))
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    fm, _ = _frontmatter(stg / "lit" / "vid-x.md")
    assert fm["asr_model"] == "unknown"
    assert fm["asr_model_claimed"] == "large-v3"
    assert fm["timestamps"] is False


def test_asr_dir_overlay_uses_retranscribed_files(tmp_path, monkeypatch):
    """Layout of retranscribe_large_v3/<id>/audio.{txt,vtt,json}."""
    ch = tmp_path / "channels"
    ch.mkdir()
    _make_channel(ch, "r", "Radoslav Radev ⎮ Calisthenics Mastery", [
        ("x", "Old Title", "complete", None, "old plunge transcript."),
        ("y", "Not redone", "complete", None, "\n".join(_normal_lines(40))),
    ])
    lines = [f"the planche needs protraction in part {i} of the talk" for i in range(50)]
    od = tmp_path / "retranscribe" / "x"
    od.mkdir(parents=True)
    (od / "audio.txt").write_text("\n".join(lines) + "\n")
    (od / "audio.json").write_text(json.dumps({
        "params": {"model": "/nix/store/27cj-ggml-large-v3.bin"},
        "transcription": [{"offsets": {"from": 110000 + i * 4000, "to": 113000 + i * 4000},
                           "text": " " + t} for i, t in enumerate(lines)],
    }))
    (tmp_path / "retranscribe" / "y").mkdir()  # started, not finished: ignored
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg,
         "--asr-dir", tmp_path / "retranscribe")
    fm, text = _frontmatter(stg / "lit" / "vid-x.md")
    assert fm["asr_model"] == "ggml-large-v3"
    assert fm["timestamps"] is True
    assert fm["speakers"] == ["Radoslav Radev"]
    assert fm["asr_source"].endswith("retranscribe/x")
    assert "[01:50] the planche needs protraction in part 0" in text
    assert "plunge" not in text
    fm_y, _ = _frontmatter(stg / "lit" / "vid-y.md")
    assert fm_y["asr_model"] == "unknown"


# ── round 2: title trust, speakers file, partial, replace, upgrade ───────────


def _queue(stg):
    return {it["id"]: it for it in json.loads((stg / "queue.json").read_text())["items"]}


def test_hand_written_title_is_untrusted_and_held(tmp_path, monkeypatch):
    """sthenics_ case: manifest titles typed by hand, no meta.json."""
    ch = tmp_path / "channels"
    ch.mkdir()
    jd = _make_channel(ch, "sthenics_", "sthenics_", [
        ("aqiTfDMlF0Q", "Stop Being A Pussy When Training Calisthenics (Mindset)", "complete",
         None, "\n".join(_normal_lines(40))),
        ("nvyvp6nk8zQ", "Deloads & Tapering: Auto-Regulation, Common Mistakes, and Peaking",
         "complete", None, "\n".join(_normal_lines(40))),
        ("OYFU5AyLl9w", "Debunking Calisthenics Myths Ep.7", "complete", None,
         "\n".join(_normal_lines(40))),
    ], meta=False)
    # Run log written by batch_channel.py (id-keyed) and failed.txt.
    (jd / "pipeline.log").write_text(
        "  [008/10] ✓ aqiTfDMlF0Q  Stop Training Like Instagram Told You To | Calisth  (11h03m)\n"
    )
    (jd / "failed.txt").write_text(
        "nvyvp6nk8zQ\tDeloads & Tapering: Auto-Regulation, Common Mistakes, and Peaking"
        "\ttimeout\t2026-09-12\n"
    )
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    q = _queue(stg)
    assert q["vid-aqiTfDMlF0Q"]["stage"] == "held"
    assert "title not verified" in q["vid-aqiTfDMlF0Q"]["error"]
    assert "Stop Training Like Instagram" in q["vid-aqiTfDMlF0Q"]["error"]
    assert q["vid-nvyvp6nk8zQ"]["stage"] == "extracted"  # matches failed.txt
    assert q["vid-OYFU5AyLl9w"]["stage"] == "held"  # no evidence at all
    fm, _ = _frontmatter(stg / "lit" / "vid-aqiTfDMlF0Q.md")
    assert fm["title_trusted"] is False and "run log" in fm["title_trust_reason"]
    assert fm["title_run_log"].startswith("Stop Training Like Instagram")
    report = json.loads((stg / "ingest_report.json").read_text())
    assert {u["id"] for u in report["untrusted_titles"]} == {"vid-aqiTfDMlF0Q", "vid-OYFU5AyLl9w"}

    _run(monkeypatch, "--channels-dir", ch, "--staging", stg, "--allow-untrusted-titles")
    q = _queue(stg)
    assert q["vid-aqiTfDMlF0Q"]["stage"] == "extracted"
    assert q["vid-aqiTfDMlF0Q"]["title_trusted"] is False


def test_speakers_yaml_and_interview_title(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    jd = _make_channel(ch, "SasaVenos", "SasaVenos", [
        ("lkcLJc2c0NQ", "The David Packer Interview (Calisthenics Skills)", "complete", None,
         "\n".join(_normal_lines(40))),
        ("u_kMa8Ejv-M", "Calisthenics Q&A: How to train", "complete", None,
         "\n".join(_normal_lines(40))),
        ("2o5uPfmsO48", "How To Warm Up", "complete", None, "\n".join(_normal_lines(40))),
    ])
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    fm, _ = _frontmatter(stg / "lit" / "vid-lkcLJc2c0NQ.md")
    assert fm["speakers"] == ["SasaVenos", "David Packer"]
    assert fm["speakers_source"] == "title-mention"
    assert fm["speakers_found_by"] == ["channel-owner", "title-interview"]
    assert fm["multi_speaker"] is True
    fm, _ = _frontmatter(stg / "lit" / "vid-u_kMa8Ejv-M.md")
    assert fm["speakers"] == ["SasaVenos"] and fm["multi_speaker"] == "unknown"

    (jd / "speakers.yaml").write_text(
        "videos:\n  2o5uPfmsO48:\n    - sthenics_\n    - saša venos\n", encoding="utf-8")
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    fm, _ = _frontmatter(stg / "lit" / "vid-2o5uPfmsO48.md")
    assert fm["speakers"] == ["sthenics_", "SasaVenos"]  # alias from the SasaVenos channel
    assert fm["speakers_source"] == "manual"
    assert fm["speakers_found_by"] == ["speakers-file", "speakers-file"]


def test_partial_transcript_is_queued_with_spans(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    lines = _normal_lines(100)
    lines[50:50] = ["If you get your deep handstand push-ups from 3 to 7 reps do this."] * 12
    _make_channel(ch, "j", "SasaVenos", [("BBuB0XMVhDw", "Programming", "complete", None,
                                           "\n".join(lines))])
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    q = _queue(stg)
    assert q["vid-BBuB0XMVhDw"]["stage"] == "extracted"
    assert q["vid-BBuB0XMVhDw"]["asr_quality"] == "partial"
    assert q["vid-BBuB0XMVhDw"]["asr_damaged_spans"] == ["lines 54-65"]  # lit body lines


def _retranscribed(asr_dir, vid, lines, start=0.0):
    od = asr_dir / vid
    od.mkdir(parents=True)
    (od / "audio.txt").write_text("\n".join(lines) + "\n")
    (od / "audio.json").write_text(json.dumps({
        "params": {"model": "/nix/store/x-ggml-large-v3.bin"},
        "transcription": [{"offsets": {"from": int((start + i * 5) * 1000),
                                       "to": int((start + i * 5 + 4) * 1000)}, "text": t}
                          for i, t in enumerate(lines)],
    }))


def test_replace_transcripts_keeps_stage_and_supersedes(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    looped = "\n".join(_normal_lines(10) + ["You have to have a very high level."] * 200)
    _make_channel(ch, "r", "Radoslav Radev", [
        ("done", "Done", "complete", None, "old plunge transcript " + " ".join(_normal_lines(30))),
        ("skip", "Skip", "complete", None, looped),
        ("music", "Music", "complete", None, "\n".join(_normal_lines(30))),
    ])
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    q = json.loads((stg / "queue.json").read_text())
    for it in q["items"]:
        if it["id"] == "vid-done":
            it.update(stage="synthesized", notes_emitted=["A Note"])
        if it["id"] == "vid-skip":
            it.update(stage="skipped", error="asr_quality degraded: no note (NOTE_CONTRACT.md)")
        if it["id"] == "vid-music":
            it.update(stage="skipped", error="no reusable idea: music")
    (stg / "queue.json").write_text(json.dumps(q))

    rt = tmp_path / "rt"
    for vid in ("done", "skip", "music"):
        _retranscribed(rt, vid, [f"{vid} planche line {i} of the new talk" for i in range(40)])

    # Without the flag: nothing moves, the report says a newer transcript exists.
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg, "--asr-dir", rt)
    assert _queue(stg)["vid-done"].get("transcript_replaced_at") is None
    report = json.loads((stg / "ingest_report.json").read_text())
    assert set(report["replace_available"]) == {"vid-done", "vid-skip", "vid-music"}

    _run(monkeypatch, "--channels-dir", ch, "--staging", stg, "--asr-dir", rt,
         "--replace-transcripts")
    q = _queue(stg)
    assert q["vid-done"]["stage"] == "synthesized" and q["vid-done"]["notes_emitted"] == ["A Note"]
    rep = q["vid-done"]["transcript_replaced"]
    assert (rep["old_model"], rep["new_model"]) == ("unknown", "ggml-large-v3")
    assert q["vid-done"]["transcript_replaced_at"]
    assert q["vid-skip"]["stage"] == "extracted"  # skipped only for degraded ASR
    assert q["vid-music"]["stage"] == "skipped"  # skipped for content: unchanged
    fm, text = _frontmatter(stg / "lit" / "vid-done.md")
    assert fm["asr_model"] == "ggml-large-v3" and fm["canonical"] is True
    assert fm["asr_source"] == str(rt / "done")
    assert "[00:05] done planche line 1" in text
    old = stg / "lit" / "_superseded" / "vid-done.unknown.md"
    assert old.exists()
    old_fm, old_text = _frontmatter(old)
    assert old_fm["canonical"] is False and old_fm["superseded_by"] == "lit/vid-done.md"
    assert "old plunge transcript" in old_text
    index = json.loads((stg / "transcripts.json").read_text())
    assert index["items"]["vid-done"]["lit_note"] == "lit/vid-done.md"
    assert index["items"]["vid-done"]["superseded"][0]["path"] == "lit/_superseded/vid-done.unknown.md"

    # A second run is a no-op for replaced items.
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg, "--asr-dir", rt,
         "--replace-transcripts")
    assert len(list((stg / "lit" / "_superseded").glob("vid-done.*"))) == 1


def test_upgrade_frontmatter_keeps_body_and_stage(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    _make_channel(ch, "s", "SasaVenos", [
        ("lkcLJc2c0NQ", "The David Packer Interview", "complete", None,
         "\n".join(_normal_lines(40)))])
    stg = tmp_path / "stg"
    (stg / "lit").mkdir(parents=True)
    old_body = "# The David Packer Interview\n\nold body text kept as is.\n"
    (stg / "lit" / "vid-lkcLJc2c0NQ.md").write_text(
        '---\nid: "vid-lkcLJc2c0NQ"\nkind: "video"\nauthor: "SasaVenos"\n'
        'extracted_at: "2026-09-12T00:00:00+00:00"\n---\n\n' + old_body)
    (stg / "queue.json").write_text(json.dumps({"version": 1, "items": [
        {"id": "vid-lkcLJc2c0NQ", "stage": "synthesized", "lit_note": "x"}]}))
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg, "--upgrade-frontmatter")
    fm, text = _frontmatter(stg / "lit" / "vid-lkcLJc2c0NQ.md")
    assert fm["speakers"] == ["SasaVenos", "David Packer"]
    assert fm["extracted_at"] == "2026-09-12T00:00:00+00:00"
    assert fm["timestamps"] is False and fm["frontmatter_upgraded_at"]
    assert text.endswith(old_body)
    assert _queue(stg)["vid-lkcLJc2c0NQ"]["stage"] == "synthesized"


# ── round 3: lit-body span numbering, notes on disk, templates, bad speakers ──


def _body_lines(path):
    """Lit body lines as README defines them: line 1 = first line after the closing ---."""
    text = path.read_text(encoding="utf-8")
    after = text.split("\n---\n", 1)[1]
    return after.split("\n")


def test_text_spans_use_lit_body_line_numbers_and_char_offsets(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    lines = _normal_lines(40)
    lines[19] = lines[19] + " and then you go"
    lines[20:20] = [" ".join(["to the next set and then you go"] * 8)]
    # Leading blank lines and spaces in the transcript must not shift the numbering.
    _make_channel(ch, "j", "c", [("v", "V", "complete", None, "\n\n  " + "\n".join(lines))])
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    path = stg / "lit" / "vid-v.md"
    fm, _ = _frontmatter(path)
    assert fm["asr_span_unit"] == "lit-body-line"
    (d,) = fm["asr_damaged_spans_detail"]
    body = _body_lines(path)
    assert body[1].startswith("# V")
    assert fm["asr_damaged_spans"] == [f"lines {d['start_line']}-{d['end_line']}"]
    first = body[d["start_line"] - 1]
    last = body[d["end_line"] - 1]
    assert first[d["start_char"]:].startswith("and then you go")
    assert first[: d["start_char"]].endswith("holds. ")
    assert 0 < d["end_char"] <= len(last)
    assert d["end_line"] == d["start_line"] + 1  # the loop line follows
    assert any(i.startswith(f"loop: lines {d['start_line']}-") for i in fm["asr_issues"])


def test_timed_span_detail_points_into_marker_lines(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    lines = _normal_lines(60)
    lines[30:30] = ["You have to have a very high level of muscle strength."] * 10
    jd = _make_channel(ch, "j", "c", [("v", "V", "complete", None, "\n".join(lines))])
    (jd / "videos" / "v" / "whisper.json").write_text(json.dumps({"transcription": [
        {"offsets": {"from": i * 5000, "to": i * 5000 + 4000}, "text": t}
        for i, t in enumerate(lines)]}))
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    path = stg / "lit" / "vid-v.md"
    fm, _ = _frontmatter(path)
    assert fm["asr_span_unit"] == "timestamp"
    assert fm["asr_damaged_spans"] == [["02:30", "03:19"]]
    (d,) = fm["asr_damaged_spans_detail"]
    body = _body_lines(path)
    assert body[d["start_line"] - 1].startswith("[02:30] You have to have")
    assert d["start_char"] == len("[02:30] ")
    assert body[d["end_line"] - 1][: d["end_char"]].endswith("muscle strength.")
    assert (d["start"], d["end"]) == ("02:30", "03:19")


def test_replaced_items_record_which_notes_exist(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    _make_channel(ch, "r", "Radoslav Radev", [
        ("x", "X", "complete", None, "\n".join(_normal_lines(30)))])
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    q = json.loads((stg / "queue.json").read_text())
    q["items"][0].update(stage="synthesized", notes_emitted=["Kept Note", "Renamed Note"],
                         published_notes=["01 Permanent Notes/Other.md"])
    (stg / "queue.json").write_text(json.dumps(q))
    vault = tmp_path / "vault"
    (vault / "01 Permanent Notes").mkdir(parents=True)
    (vault / "01 Permanent Notes" / "Kept Note.md").write_text("x")
    rt = tmp_path / "rt"
    _retranscribed(rt, "x", [f"planche line {i} of the talk here" for i in range(30)])
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg, "--asr-dir", rt,
         "--replace-transcripts", "--vault", vault)
    item = _queue(stg)["vid-x"]
    assert item["notes_on_disk"] == [
        {"note": "01 Permanent Notes/Other.md", "exists": False},
        {"note": "01 Permanent Notes/Kept Note.md", "exists": True},
        {"note": "01 Permanent Notes/Renamed Note.md", "exists": False},
    ]
    assert item["notes_checked_vault"] == str(vault)


def test_bad_speakers_file_stops_ingest_with_line(tmp_path, monkeypatch, capsys):
    ch = tmp_path / "channels"
    ch.mkdir()
    jd = _make_channel(ch, "j", "c", [("v", "V", "complete", None, "a b c.")])
    (jd / "speakers.yaml").write_text("videos:\n  v: [c, d\n", encoding="utf-8")
    import pytest

    with pytest.raises(SystemExit) as info:
        _run(monkeypatch, "--channels-dir", ch, "--staging", tmp_path / "stg")
    assert info.value.code == 2
    assert "speakers.yaml:2: unclosed '['" in capsys.readouterr().err
    assert not (tmp_path / "stg" / "queue.json").exists()


def test_speakers_template_and_podcast_channel(tmp_path, monkeypatch):
    ch = tmp_path / "channels"
    ch.mkdir()
    _make_channel(ch, "SasaVenos", "SasaVenos", [])
    jd = _make_channel(ch, "sthenics_", "sthenics_", [
        ("a1", "Debunking Calisthenics Myths Ep.7", "complete", None, "\n".join(_normal_lines(30))),
        ("a2", "Periodize Skills Ep.4 w.@SasaVenos", "complete", None, "\n".join(_normal_lines(30))),
        ("a3", "How I Arch My Back", "complete", None, "\n".join(_normal_lines(30))),
    ])
    out = tmp_path / "templates"
    _run(monkeypatch, "--channels-dir", ch, "--speakers-template", out)
    text = (out / "sthenics_" / "speakers.yaml").read_text(encoding="utf-8")
    assert "channel_kind: podcast" in text
    assert '  a2: ["sthenics_", "SasaVenos"]' in text
    assert '  # a1: ["sthenics_"]  # UNKNOWN' in text  # inactive until edited
    # The template is valid for the strict parser and usable as is.
    (jd / "speakers.yaml").write_text(text, encoding="utf-8")
    stg = tmp_path / "stg"
    _run(monkeypatch, "--channels-dir", ch, "--staging", stg)
    assert _frontmatter(stg / "lit" / "vid-a1.md")[0]["multi_speaker"] == "unknown"
    # Podcast channel: one name from the title is never certain.
    assert _frontmatter(stg / "lit" / "vid-a3.md")[0]["multi_speaker"] == "unknown"
    fm_a2 = _frontmatter(stg / "lit" / "vid-a2.md")[0]
    assert fm_a2["speakers_source"] == "manual" and fm_a2["multi_speaker"] is True
