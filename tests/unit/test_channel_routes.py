"""HTTP contract tests for the channel routes (no real work performed)."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

import twitter_articlenator.routes.channel as ch
from twitter_articlenator.resource_limits import ResourceLimiter
from twitter_articlenator.sources.channel_transcription import ChannelManifest, VideoEntry

VALID_URL = "https://www.youtube.com/@testchannel"


@pytest.fixture
def ch_config(tmp_path, monkeypatch):
    class FakeConfig:
        channel_dir = tmp_path / "channels"

    cfg = FakeConfig()
    cfg.channel_dir.mkdir(parents=True)
    monkeypatch.setattr(ch, "get_config", lambda: cfg)
    return cfg


def _write_manifest(base, job_id, *, status="complete", total=2, done=2, pdf_files=None):
    job_dir = base / job_id
    job_dir.mkdir(parents=True)
    videos = [
        VideoEntry(
            video_id=f"vid{i}",
            title=f"Video {i}",
            url=f"https://www.youtube.com/watch?v=vid{i}",
            status="complete" if i < done else "pending",
        )
        for i in range(total)
    ]
    manifest = ChannelManifest(
        job_id=job_id,
        url=VALID_URL,
        channel_title="Test Channel",
        channel_handle="@testchannel",
        status=status,
        packaging="combined",
        videos=videos,
        pdf_files=pdf_files or [],
    )
    (job_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()))
    return job_dir


def test_page_renders(client):
    resp = client.get("/channel")
    assert resp.status_code == 200
    assert b"Channel" in resp.data


def test_start_requires_csrf(client, ch_config, monkeypatch):
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: False)
    resp = client.post("/api/channel", json={"url": VALID_URL})
    assert resp.status_code == 403


def test_start_rejects_non_channel_url(client, ch_config, monkeypatch):
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: True)
    monkeypatch.setattr(ch, "_start_thread", lambda *a, **k: None)
    resp = client.post(
        "/api/channel", json={"url": "https://www.youtube.com/watch?v=abcdEFGHijk"}
    )
    assert resp.status_code == 400


def test_start_rejects_bad_packaging(client, ch_config, monkeypatch):
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: True)
    monkeypatch.setattr(ch, "_start_thread", lambda *a, **k: None)
    resp = client.post("/api/channel", json={"url": VALID_URL, "packaging": "weird"})
    assert resp.status_code == 400


def test_start_accepts_valid(client, ch_config, monkeypatch):
    started = {}
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: True)
    monkeypatch.setattr(
        ch,
        "_start_thread",
        lambda job_id, url, packaging, published_after=None, title_contains=None: started.update(
            job_id=job_id,
            url=url,
            packaging=packaging,
            published_after=published_after,
            title_contains=title_contains,
        ),
    )
    resp = client.post("/api/channel", json={"url": VALID_URL, "packaging": "per_item"})
    assert resp.status_code == 202
    body = resp.get_json()
    assert len(body["job_id"]) == 32
    assert started["url"] == VALID_URL
    assert started["packaging"] == "per_item"
    assert started["published_after"] is None  # no window by default


def test_start_with_since_months(client, ch_config, monkeypatch):
    from datetime import date

    from twitter_articlenator.sources.youtube_channel import months_ago_yyyymmdd

    started = {}
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: True)
    monkeypatch.setattr(
        ch,
        "_start_thread",
        lambda job_id, url, packaging, published_after=None, title_contains=None: started.update(
            published_after=published_after
        ),
    )
    resp = client.post("/api/channel", json={"url": VALID_URL, "since_months": 3})
    assert resp.status_code == 202
    assert started["published_after"] == months_ago_yyyymmdd(date.today(), 3)


def test_start_with_title_contains(client, ch_config, monkeypatch):
    started = {}
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: True)
    monkeypatch.setattr(
        ch,
        "_start_thread",
        lambda job_id, url, packaging, published_after=None, title_contains=None: started.update(
            title_contains=title_contains
        ),
    )
    resp = client.post(
        "/api/channel", json={"url": VALID_URL, "title_contains": "Wading Through AI"}
    )
    assert resp.status_code == 202
    assert started["title_contains"] == "Wading Through AI"


def test_start_rejects_bad_since_months(client, ch_config, monkeypatch):
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: True)
    monkeypatch.setattr(ch, "_start_thread", lambda *a, **k: None)
    assert (
        client.post("/api/channel", json={"url": VALID_URL, "since_months": "abc"}).status_code
        == 400
    )
    assert (
        client.post("/api/channel", json={"url": VALID_URL, "since_months": -3}).status_code == 400
    )
    # 0 / blank means "no window" and is accepted.
    assert (
        client.post("/api/channel", json={"url": VALID_URL, "since_months": 0}).status_code == 202
    )


def test_status_rejects_bad_id(client, ch_config):
    assert client.get("/api/channel/not-a-valid-id").status_code == 400


def test_status_not_found(client, ch_config):
    assert client.get("/api/channel/" + "0" * 32).status_code == 404


def test_status_reports_progress(client, ch_config):
    job_id = "a" * 32
    _write_manifest(ch_config.channel_dir, job_id, status="transcribing", total=4, done=1)
    resp = client.get(f"/api/channel/{job_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_videos"] == 4
    assert data["done_videos"] == 1
    assert data["progress"] == 0.25
    assert len(data["videos"]) == 4


def test_pdf_not_ready_returns_409(client, ch_config):
    job_id = "b" * 32
    _write_manifest(ch_config.channel_dir, job_id, status="transcribing", total=2, done=1)
    assert client.get(f"/api/channel/{job_id}/pdf/0").status_code == 409


def test_pdf_index_out_of_range(client, ch_config):
    job_id = "c" * 32
    job_dir = _write_manifest(
        ch_config.channel_dir, job_id, status="complete", pdf_files=["a.pdf"]
    )
    (job_dir / "a.pdf").write_bytes(b"%PDF-1.4 fake")
    assert client.get(f"/api/channel/{job_id}/pdf/5").status_code == 404


def test_pdf_download_when_complete(client, ch_config):
    job_id = "d" * 32
    job_dir = _write_manifest(
        ch_config.channel_dir, job_id, status="complete", pdf_files=["a.pdf"]
    )
    (job_dir / "a.pdf").write_bytes(b"%PDF-1.4 fake content")
    resp = client.get(f"/api/channel/{job_id}/pdf/0")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert b"%PDF-1.4" in resp.data


def test_pdfs_zip_when_complete(client, ch_config):
    job_id = "e" * 32
    job_dir = _write_manifest(
        ch_config.channel_dir,
        job_id,
        status="complete",
        pdf_files=["a.pdf", "b.pdf"],
    )
    (job_dir / "a.pdf").write_bytes(b"%PDF-1.4 a")
    (job_dir / "b.pdf").write_bytes(b"%PDF-1.4 b")
    resp = client.get(f"/api/channel/{job_id}/pdfs.zip")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.data)) as archive:
        assert sorted(archive.namelist()) == ["a.pdf", "b.pdf"]


def test_list_jobs(client, ch_config):
    _write_manifest(ch_config.channel_dir, "f" * 32, status="complete", pdf_files=["a.pdf"])
    resp = client.get("/api/channels")
    assert resp.status_code == 200
    jobs = resp.get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "f" * 32
    assert jobs[0]["channel_title"] == "Test Channel"


def test_resume_not_found(client, ch_config, monkeypatch):
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: True)
    assert client.post("/api/channel/" + "1" * 32 + "/resume").status_code == 404


def test_resume_completed_job_is_noop(client, ch_config, monkeypatch):
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: True)
    job_id = "9" * 32
    _write_manifest(ch_config.channel_dir, job_id, status="complete", pdf_files=["a.pdf"])
    resp = client.post(f"/api/channel/{job_id}/resume")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "complete"


def test_resume_requires_csrf(client, ch_config, monkeypatch):
    monkeypatch.setattr(ch, "is_valid_csrf_request", lambda: False)
    assert client.post("/api/channel/" + "9" * 32 + "/resume").status_code == 403


def test_resume_inflight_channel_jobs_on_startup(client, ch_config, monkeypatch):
    started = []
    monkeypatch.setattr(
        ch,
        "_start_thread",
        lambda job_id, url, packaging, published_after=None, title_contains=None: started.append(
            job_id
        ),
    )
    _write_manifest(ch_config.channel_dir, "a" * 32, status="complete", pdf_files=["x.pdf"])
    _write_manifest(ch_config.channel_dir, "b" * 32, status="error")
    _write_manifest(ch_config.channel_dir, "c" * 32, status="transcribing", total=3, done=1)
    _write_manifest(ch_config.channel_dir, "d" * 32, status="enumerating", total=0, done=0)

    resumed = ch.resume_inflight_channel_jobs()

    # Only the non-terminal (in-flight) jobs are resumed; complete/error are left alone.
    assert set(resumed) == {"c" * 32, "d" * 32}
    assert set(started) == {"c" * 32, "d" * 32}


def test_startup_resume_obeys_global_transcription_limit(tmp_path, monkeypatch):
    alice_dir = tmp_path / "alice"
    bob_dir = tmp_path / "bob"
    _write_manifest(alice_dir, "a" * 32, status="transcribing", total=1, done=0)
    _write_manifest(bob_dir, "b" * 32, status="transcribing", total=1, done=0)
    started = []

    def capture_start(job_id, *args, **kwargs):
        started.append((job_id, kwargs["lease"]))

    monkeypatch.setattr(ch, "_start_thread", capture_start)
    limiter = ResourceLimiter({"transcription": (1, 1)})

    resumed = ch.resume_inflight_channel_jobs(
        [(alice_dir, None, "alice"), (bob_dir, None, "bob")],
        limiter=limiter,
    )

    assert resumed == ["a" * 32]
    assert [job_id for job_id, _lease in started] == ["a" * 32]
    assert limiter.active("transcription") == {"global": 1, "users": {"alice": 1}}
    started[0][1].release()
