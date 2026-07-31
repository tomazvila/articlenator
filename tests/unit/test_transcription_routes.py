"""HTTP contract tests for the transcription routes (no real work performed)."""

from __future__ import annotations

import json

import pytest

import twitter_articlenator.routes.transcription as tx
from twitter_articlenator.sources.transcription import ChunkState, Manifest

VALID_URL = "https://www.youtube.com/watch?v=abcdEFGHijk"


@pytest.fixture
def tx_config(tmp_path, monkeypatch):
    class FakeConfig:
        transcription_dir = tmp_path / "transcriptions"

    cfg = FakeConfig()
    cfg.transcription_dir.mkdir(parents=True)
    monkeypatch.setattr(tx, "get_config", lambda: cfg)
    return cfg


def _write_manifest(base, job_id, *, status="complete", chunks=2, done=2):
    job_dir = base / job_id
    job_dir.mkdir(parents=True)
    manifest = Manifest(
        job_id=job_id,
        url=VALID_URL,
        status=status,
        video_id="abcdEFGHijk",
        title="Test Video",
        chunks=[
            ChunkState(
                index=i,
                start=i * 10.0,
                end=i * 10.0 + 10.0,
                status="complete" if i < done else "pending",
            )
            for i in range(chunks)
        ],
    )
    (job_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()))
    return job_dir


def test_page_renders(client):
    resp = client.get("/transcription")
    assert resp.status_code == 200
    assert b"YouTube Transcription" in resp.data


def test_start_requires_csrf(client, tx_config, monkeypatch):
    monkeypatch.setattr(tx, "is_valid_csrf_request", lambda: False)
    resp = client.post("/api/transcription", json={"url": VALID_URL})
    assert resp.status_code == 403


def test_start_rejects_non_youtube_url(client, tx_config, monkeypatch):
    monkeypatch.setattr(tx, "is_valid_csrf_request", lambda: True)
    monkeypatch.setattr(tx, "_start_thread", lambda *a, **k: None)
    resp = client.post("/api/transcription", json={"url": "https://example.com/video"})
    assert resp.status_code == 400


def test_start_accepts_valid_url(client, tx_config, monkeypatch):
    started = {}
    monkeypatch.setattr(tx, "is_valid_csrf_request", lambda: True)
    monkeypatch.setattr(
        tx, "_start_thread", lambda job_id, url: started.update(job_id=job_id, url=url)
    )
    resp = client.post("/api/transcription", json={"url": VALID_URL})
    assert resp.status_code == 202
    body = resp.get_json()
    assert len(body["job_id"]) == 32
    assert started["url"] == VALID_URL


def test_status_rejects_bad_id(client, tx_config):
    assert client.get("/api/transcription/not-a-valid-id").status_code == 400


def test_status_not_found(client, tx_config):
    assert client.get("/api/transcription/" + "0" * 32).status_code == 404


def test_status_reports_progress(client, tx_config):
    job_id = "a" * 32
    _write_manifest(tx_config.transcription_dir, job_id, status="transcribing", chunks=4, done=1)
    resp = client.get(f"/api/transcription/{job_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_chunks"] == 4
    assert data["done_chunks"] == 1
    assert data["progress"] == 0.25


def test_transcript_not_ready_returns_409(client, tx_config):
    job_id = "b" * 32
    _write_manifest(tx_config.transcription_dir, job_id, status="transcribing", chunks=2, done=1)
    assert client.get(f"/api/transcription/{job_id}/transcript.txt").status_code == 409


def test_transcript_download_when_complete(client, tx_config):
    job_id = "c" * 32
    job_dir = _write_manifest(tx_config.transcription_dir, job_id, status="complete")
    (job_dir / "transcript.txt").write_text("hello world\n")
    resp = client.get(f"/api/transcription/{job_id}/transcript.txt")
    assert resp.status_code == 200
    assert b"hello world" in resp.data


def test_transcript_rejects_unknown_format(client, tx_config):
    job_id = "c" * 32
    _write_manifest(tx_config.transcription_dir, job_id, status="complete")
    assert client.get(f"/api/transcription/{job_id}/transcript.exe").status_code == 400


def test_list_jobs(client, tx_config):
    _write_manifest(tx_config.transcription_dir, "d" * 32, status="complete")
    resp = client.get("/api/transcriptions")
    assert resp.status_code == 200
    jobs = resp.get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "d" * 32


def test_resume_not_found(client, tx_config, monkeypatch):
    monkeypatch.setattr(tx, "is_valid_csrf_request", lambda: True)
    assert client.post("/api/transcription/" + "e" * 32 + "/resume").status_code == 404


def test_resume_completed_job_is_noop(client, tx_config, monkeypatch):
    monkeypatch.setattr(tx, "is_valid_csrf_request", lambda: True)
    job_id = "f" * 32
    _write_manifest(tx_config.transcription_dir, job_id, status="complete")
    resp = client.post(f"/api/transcription/{job_id}/resume")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "complete"
