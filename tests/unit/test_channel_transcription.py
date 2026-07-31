"""Tests for the resumable channel transcription → PDF batch job."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from twitter_articlenator.sources.channel_transcription import (
    STATUS_ENUMERATING,
    ChannelJob,
    ChannelManifest,
    VideoEntry,
    _paragraphize,
    _transcript_to_article,
    load_channel_manifest,
)
from twitter_articlenator.sources.transcription import (
    STATUS_COMPLETE,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_TRANSCRIBING,
)
from twitter_articlenator.sources.youtube_channel import ChannelInfo, ChannelVideo


def make_info(ids, *, title="Test Channel", handle="@test"):
    return ChannelInfo(
        channel_id="UC123",
        channel_title=title,
        channel_handle=handle,
        source_url="https://www.youtube.com/@test/videos",
        videos=[
            ChannelVideo(video_id=i, title=f"Title {i}", url=f"https://www.youtube.com/watch?v={i}")
            for i in ids
        ],
    )


class FakeVideoJob:
    """Stand-in for a per-video TranscriptionJob: writes transcript.txt or fails."""

    def __init__(self, video_dir, *, fail=False, text="Hello world. This is a transcript."):
        self.dir = video_dir
        self.fail = fail
        self.text = text

    def run(self, url=None, *, on_progress=None):
        if self.fail:
            raise RuntimeError(f"boom for {self.dir.name}")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "transcript.txt").write_text(self.text + "\n", encoding="utf-8")


def make_factory(fail_ids=()):
    fail = set(fail_ids)
    created = []

    def factory(video_dir):
        created.append(video_dir.name)
        return FakeVideoJob(video_dir, fail=video_dir.name in fail)

    factory.created = created
    return factory


def make_pdf_builder(job_dir):
    calls = []

    def builder(articles, packaging):
        calls.append({"count": len(articles), "packaging": packaging, "articles": list(articles)})
        if packaging == "per_item":
            paths = []
            for i in range(len(articles)):
                p = job_dir / f"video-{i}.pdf"
                p.write_bytes(b"%PDF-1.4 fake")
                paths.append(p)
            return paths
        p = job_dir / "combined.pdf"
        p.write_bytes(b"%PDF-1.4 fake")
        return [p]

    builder.calls = calls
    return builder


def build_job(job_dir, *, enumerator, factory=None, pdf_builder=None, packaging="combined"):
    return ChannelJob(
        job_dir,
        enumerator=enumerator,
        transcriber_factory=factory or make_factory(),
        pdf_builder=pdf_builder or make_pdf_builder(job_dir),
        packaging=packaging,
    )


def test_full_run_combined(tmp_path):
    job_dir = tmp_path / "job"
    builder = make_pdf_builder(job_dir)
    job = build_job(job_dir, enumerator=lambda url: make_info(["a", "b", "c"]), pdf_builder=builder)

    manifest = job.run("https://www.youtube.com/@test")

    assert manifest.status == STATUS_COMPLETE
    assert manifest.done_videos == 3
    assert manifest.failed_videos == 0
    assert manifest.pdf_files == ["combined.pdf"]
    assert all(v.status == STATUS_COMPLETE for v in manifest.videos)
    assert len(builder.calls) == 1
    assert builder.calls[0]["packaging"] == "combined"
    assert builder.calls[0]["count"] == 3
    # Articles carry channel + per-video metadata.
    article = builder.calls[0]["articles"][0]
    assert article.source_type == "youtube_channel"
    assert article.author == "Test Channel"


def test_per_item_packaging(tmp_path):
    job_dir = tmp_path / "job"
    builder = make_pdf_builder(job_dir)
    job = build_job(
        job_dir,
        enumerator=lambda url: make_info(["a", "b"]),
        pdf_builder=builder,
        packaging="per_item",
    )
    manifest = job.run("https://www.youtube.com/@test")
    assert manifest.status == STATUS_COMPLETE
    assert manifest.pdf_files == ["video-0.pdf", "video-1.pdf"]
    assert builder.calls[0]["packaging"] == "per_item"


def test_per_video_error_is_tolerated(tmp_path):
    job_dir = tmp_path / "job"
    builder = make_pdf_builder(job_dir)
    job = build_job(
        job_dir,
        enumerator=lambda url: make_info(["a", "b", "c"]),
        factory=make_factory(fail_ids=["b"]),
        pdf_builder=builder,
    )
    manifest = job.run("https://www.youtube.com/@test")

    assert manifest.status == STATUS_COMPLETE
    assert manifest.done_videos == 2
    assert manifest.failed_videos == 1
    bad = next(v for v in manifest.videos if v.video_id == "b")
    assert bad.status == STATUS_ERROR
    assert "boom" in (bad.error or "")
    # PDF rendered from the two successful videos only.
    assert builder.calls[0]["count"] == 2


def test_all_videos_fail_marks_job_error(tmp_path):
    job_dir = tmp_path / "job"
    job = build_job(
        job_dir,
        enumerator=lambda url: make_info(["a", "b"]),
        factory=make_factory(fail_ids=["a", "b"]),
    )
    with pytest.raises(RuntimeError, match="No videos"):
        job.run("https://www.youtube.com/@test")

    manifest = load_channel_manifest(job_dir)
    assert manifest.status == STATUS_ERROR
    assert manifest.error


def test_resume_skips_completed_videos(tmp_path):
    job_dir = tmp_path / "job"
    # Pre-seed a partially-finished job: a complete, b/c pending.
    (job_dir / "videos" / "a").mkdir(parents=True)
    (job_dir / "videos" / "a" / "transcript.txt").write_text("done already.\n", encoding="utf-8")
    manifest = ChannelManifest(
        job_id="job",
        url="https://www.youtube.com/@test",
        channel_title="Test Channel",
        status=STATUS_TRANSCRIBING,
        videos=[
            VideoEntry(video_id="a", title="A", url="u/a", status=STATUS_COMPLETE),
            VideoEntry(video_id="b", title="B", url="u/b", status=STATUS_PENDING),
            VideoEntry(video_id="c", title="C", url="u/c", status=STATUS_PENDING),
        ],
    )
    (job_dir).mkdir(parents=True, exist_ok=True)
    (job_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    factory = make_factory()
    job = build_job(
        job_dir,
        enumerator=lambda url: pytest.fail("enumerator must not run on resume"),
        factory=factory,
    )
    result = job.run()

    assert result.status == STATUS_COMPLETE
    assert "a" not in factory.created  # completed video was skipped
    assert set(factory.created) == {"b", "c"}


def test_resume_does_not_reenumerate_and_retries_render(tmp_path):
    job_dir = tmp_path / "job"
    calls = {"enumerate": 0, "render": 0}

    def enumerator(url):
        calls["enumerate"] += 1
        return make_info(["a", "b"])

    def flaky_builder(articles, packaging):
        calls["render"] += 1
        if calls["render"] == 1:
            raise RuntimeError("render flaked")
        p = job_dir / "combined.pdf"
        p.write_bytes(b"%PDF-1.4 fake")
        return [p]

    # First run: enumerate + transcribe succeed, render fails -> job error.
    job1 = build_job(job_dir, enumerator=enumerator, pdf_builder=flaky_builder)
    with pytest.raises(RuntimeError, match="render flaked"):
        job1.run("https://www.youtube.com/@test")

    # Second run (resume): no re-enumeration, no re-transcription, render retried.
    factory2 = make_factory()
    job2 = ChannelJob(
        job_dir,
        enumerator=enumerator,
        transcriber_factory=factory2,
        pdf_builder=flaky_builder,
    )
    result = job2.run()

    assert calls["enumerate"] == 1  # enumerated once across both runs
    assert factory2.created == []  # both videos already complete -> skipped
    assert result.status == STATUS_COMPLETE
    assert result.pdf_files == ["combined.pdf"]


def test_idempotent_complete_run(tmp_path):
    job_dir = tmp_path / "job"
    builder = make_pdf_builder(job_dir)
    job = build_job(job_dir, enumerator=lambda url: make_info(["a"]), pdf_builder=builder)
    job.run("https://www.youtube.com/@test")
    assert len(builder.calls) == 1

    # Re-running a complete job whose PDFs exist does no work.
    job2 = build_job(job_dir, enumerator=lambda url: make_info(["a"]), pdf_builder=builder)
    job2.run()
    assert len(builder.calls) == 1


def test_new_job_requires_url(tmp_path):
    job = build_job(tmp_path / "job", enumerator=lambda url: make_info(["a"]))
    with pytest.raises(ValueError, match="url is required"):
        job.run()


def test_enumeration_status_persisted_before_videos(tmp_path):
    job_dir = tmp_path / "job"
    seen_status = {}

    def enumerator(url):
        # When the enumerator runs, the manifest on disk should already be
        # marked ENUMERATING (so a crash mid-enumeration is observable).
        seen_status["value"] = load_channel_manifest(job_dir).status
        return make_info(["a"])

    job = build_job(job_dir, enumerator=enumerator)
    job.run("https://www.youtube.com/@test")
    assert seen_status["value"] == STATUS_ENUMERATING


def test_transcript_to_article_escapes_html():
    entry = VideoEntry(video_id="x", title="Dangerous <b>", url="https://y/x")
    article = _transcript_to_article(
        "<script>alert(1)</script> & friends. Second sentence here.",
        entry,
        "My Channel",
    )
    assert "<script>" not in article.content
    assert "&lt;script&gt;" in article.content
    assert article.content.startswith("<p>")
    assert article.title == "Dangerous <b>"  # title escaping is the PDF layer's job
    assert article.author == "My Channel"
    assert article.source_url == "https://y/x"


def test_transcript_to_article_empty():
    entry = VideoEntry(video_id="x", title="T", url="u")
    article = _transcript_to_article("   ", entry, "C")
    assert article.content == "<p>(no transcript)</p>"


def test_upload_date_sets_article_published_at():
    dated = VideoEntry(video_id="x", title="T", url="u", upload_date="20260321")
    assert _transcript_to_article("Hello.", dated, "C").published_at == datetime(2026, 3, 21)
    undated = VideoEntry(video_id="y", title="T", url="u")
    assert _transcript_to_article("Hello.", undated, "C").published_at is None
    bad = VideoEntry(video_id="z", title="T", url="u", upload_date="not-a-date")
    assert _transcript_to_article("Hello.", bad, "C").published_at is None


def test_channel_job_persists_published_after(tmp_path):
    job_dir = tmp_path / "job"
    job = ChannelJob(
        job_dir,
        enumerator=lambda url: make_info(["a"]),
        transcriber_factory=make_factory(),
        pdf_builder=make_pdf_builder(job_dir),
        published_after="20260321",
    )
    manifest = job.run("https://www.youtube.com/@test")
    assert manifest.published_after == "20260321"
    assert load_channel_manifest(job_dir).published_after == "20260321"


def test_channel_job_persists_title_contains(tmp_path):
    job_dir = tmp_path / "job"
    job = ChannelJob(
        job_dir,
        enumerator=lambda url: make_info(["a"]),
        transcriber_factory=make_factory(),
        pdf_builder=make_pdf_builder(job_dir),
        title_contains="wading through ai",
    )
    manifest = job.run("https://www.youtube.com/@m")
    assert manifest.title_contains == "wading through ai"
    assert load_channel_manifest(job_dir).title_contains == "wading through ai"


def test_enumeration_carries_upload_date(tmp_path):
    job_dir = tmp_path / "job"
    info = ChannelInfo(
        channel_id=None,
        channel_title="C",
        channel_handle="@c",
        source_url="s",
        videos=[ChannelVideo(video_id="a", title="A", url="u/a", upload_date="20260401")],
    )
    job = build_job(job_dir, enumerator=lambda url: info)
    manifest = job.run("https://www.youtube.com/@c")
    entry = next(v for v in manifest.videos if v.video_id == "a")
    assert entry.upload_date == "20260401"


def test_paragraphize_groups_sentences():
    text = "One. Two. Three. Four. Five. Six."
    paras = _paragraphize(text, sentences_per_para=2)
    assert paras == ["One. Two.", "Three. Four.", "Five. Six."]
    assert _paragraphize("") == []


def test_manifest_roundtrip_and_tolerates_extra_keys():
    manifest = ChannelManifest(
        job_id="j",
        url="u",
        videos=[VideoEntry(video_id="a", title="A", url="u/a", status=STATUS_COMPLETE)],
        pdf_files=["x.pdf"],
    )
    restored = ChannelManifest.from_dict(manifest.to_dict())
    assert restored == manifest

    # Unknown keys (e.g. from a newer schema) are ignored, not fatal.
    data = manifest.to_dict()
    data["future_field"] = "ignored"
    data["videos"][0]["future_video_field"] = "ignored"
    restored2 = ChannelManifest.from_dict(data)
    assert restored2.videos[0].video_id == "a"


def test_load_channel_manifest_missing_and_corrupt(tmp_path):
    assert load_channel_manifest(tmp_path / "nope") is None
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{not json", encoding="utf-8")
    assert load_channel_manifest(bad) is None
