"""Unit tests for the resumable transcription pipeline.

These use injected fakes for the network/ffmpeg/whisper collaborators, so they
exercise chunking, atomic checkpointing, resume-after-failure and idempotency
deterministically and fast.
"""

from __future__ import annotations

import json

import pytest

from twitter_articlenator.sources.transcription import (
    STATUS_COMPLETE,
    STATUS_ERROR,
    STATUS_PENDING,
    AudioInfo,
    Segment,
    TranscriptionJob,
    compute_chunks,
    segments_to_text,
    segments_to_vtt,
    stitch_segments,
)

URL = "https://youtu.be/abcdEFGHijk"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeTranscriber:
    """Records calls; can simulate a one-time failure on specific chunk indices."""

    def __init__(self, fail_on: list[int] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = set(fail_on or [])
        self._failed: set[int] = set()

    def transcribe(self, audio_path, *, language=None):
        self.calls.append(audio_path.name)
        index = int(audio_path.stem.split("_")[1])
        if index in self.fail_on and index not in self._failed:
            self._failed.add(index)
            raise RuntimeError(f"simulated whisper failure on chunk {index}")
        return [Segment(start=0.0, end=1.0, text=f"chunk{index}")]

    def count_for(self, index: int) -> int:
        return self.calls.count(f"chunk_{index:04d}.wav")


def make_audio_provider(duration: float, *, fail_times: int = 0):
    state = {"calls": 0}

    def provider(url, dest_dir):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise RuntimeError("simulated network drop during download")
        path = dest_dir / "audio.wav"
        path.write_bytes(b"RIFFfake")
        return AudioInfo(path=path, duration=duration, video_id="abcdEFGHijk", title="Test Video")

    provider.state = state  # type: ignore[attr-defined]
    return provider


def fake_chunker(audio_path, chunk, dest_path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(b"chunkaudio")


def make_job(job_dir, transcriber, audio_provider, *, chunk_seconds=10.0):
    return TranscriptionJob(
        job_dir,
        transcriber=transcriber,
        audio_provider=audio_provider,
        chunker=fake_chunker,
        chunk_seconds=chunk_seconds,
    )


# --------------------------------------------------------------------------- #
# compute_chunks
# --------------------------------------------------------------------------- #


def test_compute_chunks_splits_with_short_final_chunk():
    chunks = compute_chunks(25.0, 10.0)
    assert [(c.start, c.end) for c in chunks] == [(0.0, 10.0), (10.0, 20.0), (20.0, 25.0)]
    assert [c.index for c in chunks] == [0, 1, 2]


def test_compute_chunks_exact_multiple():
    chunks = compute_chunks(20.0, 10.0)
    assert [(c.start, c.end) for c in chunks] == [(0.0, 10.0), (10.0, 20.0)]


def test_compute_chunks_shorter_than_chunk_size_yields_single_chunk():
    chunks = compute_chunks(5.0, 10.0)
    assert [(c.start, c.end) for c in chunks] == [(0.0, 5.0)]


@pytest.mark.parametrize("duration,chunk", [(0.0, 10.0), (-1.0, 10.0), (10.0, 0.0), (10.0, -5.0)])
def test_compute_chunks_rejects_non_positive(duration, chunk):
    with pytest.raises(ValueError):
        compute_chunks(duration, chunk)


# --------------------------------------------------------------------------- #
# stitching / rendering
# --------------------------------------------------------------------------- #


def test_stitch_orders_segments_by_start():
    a = [Segment(20.0, 21.0, "third")]
    b = [Segment(0.0, 1.0, "first")]
    c = [Segment(10.0, 11.0, "second")]
    stitched = stitch_segments([a, b, c])
    assert [s.text for s in stitched] == ["first", "second", "third"]


def test_segments_to_text_joins_and_trims():
    segs = [Segment(0, 1, "  hello "), Segment(1, 2, "world"), Segment(2, 3, "  ")]
    assert segments_to_text(segs) == "hello world"


def test_segments_to_vtt_formats_timestamps():
    vtt = segments_to_vtt([Segment(0.0, 1.5, "hello"), Segment(1.5, 3.0, "world")])
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in vtt
    assert "hello" in vtt and "world" in vtt


# --------------------------------------------------------------------------- #
# TranscriptionJob happy path + outputs
# --------------------------------------------------------------------------- #


def test_run_produces_complete_transcript(tmp_path):
    job = make_job(tmp_path / "job1", FakeTranscriber(), make_audio_provider(25.0))
    manifest = job.run(URL)

    assert manifest.status == STATUS_COMPLETE
    assert manifest.job_id == "job1"
    assert manifest.duration == 25.0
    assert manifest.title == "Test Video"
    assert all(c.status == STATUS_COMPLETE for c in manifest.chunks)
    assert (tmp_path / "job1" / "transcript.txt").read_text().strip() == "chunk0 chunk1 chunk2"
    assert (tmp_path / "job1" / "transcript.vtt").exists()
    assert (tmp_path / "job1" / "transcript.json").exists()


def test_run_leaves_no_temp_files(tmp_path):
    job = make_job(tmp_path / "job1", FakeTranscriber(), make_audio_provider(25.0))
    job.run(URL)
    assert not (tmp_path / "job1" / "manifest.json.tmp").exists()


def test_new_job_requires_url(tmp_path):
    job = make_job(tmp_path / "job1", FakeTranscriber(), make_audio_provider(25.0))
    with pytest.raises(ValueError):
        job.run()


def test_progress_callbacks_fire(tmp_path):
    events = []
    job = make_job(tmp_path / "job1", FakeTranscriber(), make_audio_provider(25.0))
    job.run(URL, on_progress=lambda e: events.append(e["event"]))
    assert "download_complete" in events
    assert events.count("chunk_complete") == 3
    assert events[-1] == "complete"


# --------------------------------------------------------------------------- #
# Resilience: resume after crash / network drop, idempotency
# --------------------------------------------------------------------------- #


def test_resume_after_transcribe_failure_skips_completed_chunks(tmp_path):
    job_dir = tmp_path / "job1"
    transcriber = FakeTranscriber(fail_on=[1])
    job = make_job(job_dir, transcriber, make_audio_provider(25.0))

    with pytest.raises(RuntimeError):
        job.run(URL)

    interrupted = job.load_manifest()
    assert interrupted.status == STATUS_ERROR
    assert interrupted.chunks[0].status == STATUS_COMPLETE
    assert interrupted.chunks[1].status == STATUS_PENDING

    # Resume with the same on-disk state (no url needed).
    resumed = make_job(job_dir, transcriber, make_audio_provider(25.0)).run()
    assert resumed.status == STATUS_COMPLETE
    assert transcriber.count_for(0) == 1  # not re-transcribed
    assert transcriber.count_for(1) == 2  # failed once, then succeeded
    assert (job_dir / "transcript.txt").read_text().strip() == "chunk0 chunk1 chunk2"


def test_resume_after_network_drop_redownloads(tmp_path):
    job_dir = tmp_path / "job1"
    provider = make_audio_provider(25.0, fail_times=1)
    job = make_job(job_dir, FakeTranscriber(), provider)

    with pytest.raises(RuntimeError):
        job.run(URL)
    assert job.load_manifest().status == STATUS_ERROR

    resumed = make_job(job_dir, FakeTranscriber(), provider).run()
    assert resumed.status == STATUS_COMPLETE
    assert provider.state["calls"] == 2  # retried the download


def test_completed_job_rerun_is_idempotent(tmp_path):
    job_dir = tmp_path / "job1"
    transcriber = FakeTranscriber()
    job = make_job(job_dir, transcriber, make_audio_provider(25.0))
    job.run(URL)
    calls_after_first = list(transcriber.calls)

    job.run()  # second run
    assert transcriber.calls == calls_after_first  # no extra transcription work


def test_chunk_marked_done_but_missing_output_is_retranscribed(tmp_path):
    job_dir = tmp_path / "job1"
    job = make_job(job_dir, FakeTranscriber(fail_on=[2]), make_audio_provider(25.0))
    with pytest.raises(RuntimeError):
        job.run(URL)  # chunks 0,1 done; chunk 2 fails -> status error

    # Lose chunk 1's output but leave it marked done in the manifest.
    (job_dir / "chunks" / "chunk_0001.json").unlink()

    fresh = FakeTranscriber()
    make_job(job_dir, fresh, make_audio_provider(25.0)).run()
    assert fresh.count_for(0) == 0  # output intact -> skipped
    assert fresh.count_for(1) == 1  # output missing -> redone
    assert fresh.count_for(2) == 1  # was pending -> done


def test_corrupt_manifest_is_recovered(tmp_path):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    (job_dir / "manifest.json").write_text("this is not json {{{")
    job = make_job(job_dir, FakeTranscriber(), make_audio_provider(25.0))
    assert job.load_manifest() is None
    manifest = job.run(URL)
    assert manifest.status == STATUS_COMPLETE


def test_checkpoint_written_after_each_chunk(tmp_path):
    """The manifest on disk reflects per-chunk progress as it happens."""
    job_dir = tmp_path / "job1"
    seen_done_counts = []

    class CheckpointingTranscriber(FakeTranscriber):
        def transcribe(self, audio_path, *, language=None):
            manifest = json.loads((job_dir / "manifest.json").read_text())
            seen_done_counts.append(
                sum(1 for c in manifest["chunks"] if c["status"] == STATUS_COMPLETE)
            )
            return super().transcribe(audio_path, language=language)

    make_job(job_dir, CheckpointingTranscriber(), make_audio_provider(25.0)).run(URL)
    # Before transcribing chunk N, chunks 0..N-1 are already persisted as done.
    assert seen_done_counts == [0, 1, 2]
