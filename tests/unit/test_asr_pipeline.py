"""Package A tests for the app transcription path: model truth, quality, preflight, prompt."""

from __future__ import annotations

import json
import subprocess

import pytest

from twitter_articlenator.sources.asr_tools import WhisperModelMissingError
from twitter_articlenator.sources.channel_transcription import (
    STATUS_ERROR,
    ChannelJob,
    load_channel_manifest,
)
from twitter_articlenator.sources.transcription import (
    AudioInfo,
    Segment,
    TranscriptionJob,
    load_manifest,
)
from twitter_articlenator.sources.whisper_transcriber import WhisperCppTranscriber
from twitter_articlenator.sources.youtube_channel import ChannelInfo, ChannelVideo


class ModelTranscriber:
    """Fake transcriber that exposes a model file path like WhisperCppTranscriber."""

    def __init__(self, model_path, lines=None):
        self.model_path = model_path
        self.prompt = "Glossary: planche."
        self.lines = lines

    def transcribe(self, audio_path, *, language=None):
        lines = self.lines or [
            f"we hold the planche lean for set number {i} today" for i in range(20)
        ]
        return [Segment(i * 3.0, i * 3.0 + 2.5, t) for i, t in enumerate(lines)]


def _provider(duration):
    def provider(url, dest_dir):
        p = dest_dir / "audio.wav"
        p.write_bytes(b"RIFF")
        return AudioInfo(path=p, duration=duration, video_id="vid", title="T")

    return provider


def _chunker(audio_path, chunk, dest_path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(b"x")


def test_manifest_model_comes_from_the_model_file(tmp_path):
    job = TranscriptionJob(
        tmp_path / "job",
        transcriber=ModelTranscriber("/models/ggml-small.en.bin"),
        audio_provider=_provider(60.0),
        chunker=_chunker,
        chunk_seconds=600,
    )
    m = job.run("https://youtu.be/vid")
    assert m.model == "ggml-small.en"  # never the old constant "large-v3"
    assert m.model_file == "ggml-small.en.bin"
    assert m.prompt == "Glossary: planche."
    assert m.asr_quality == "ok"
    sidecar = json.loads((tmp_path / "job" / "asr.json").read_text())
    assert sidecar["model"] == "ggml-small.en" and sidecar["timestamps"] is True
    tj = json.loads((tmp_path / "job" / "transcript.json").read_text())
    assert tj["model"] == "ggml-small.en" and tj["asr_quality"] == "ok"


def test_transcriber_without_model_gives_unknown_not_a_constant(tmp_path):
    class Bare:
        def transcribe(self, audio_path, *, language=None):
            return [Segment(0.0, 1.0, "hello there friend")]

    job = TranscriptionJob(
        tmp_path / "j", transcriber=Bare(), audio_provider=_provider(5.0), chunker=_chunker
    )
    assert job.run("https://youtu.be/x").model == "unknown"


def test_looped_chunk_output_marks_job_degraded(tmp_path):
    lines = ["intro words about the session plan"] + ["And then we look at the performance."] * 60
    job = TranscriptionJob(
        tmp_path / "job",
        transcriber=ModelTranscriber("/m/ggml-large-v3.bin", lines=lines),
        audio_provider=_provider(200.0),
        chunker=_chunker,
    )
    m = job.run("https://youtu.be/vid")
    assert m.status == "complete"  # the run finished ...
    assert m.asr_quality == "degraded"  # ... but the transcript is not usable as is
    assert any("damaged spans hold" in i for i in m.asr_issues)
    assert any(i.startswith("loop: [00:03]-") for i in m.asr_issues)


def test_resume_with_another_model_is_recorded_as_mixed(tmp_path):
    job_dir = tmp_path / "job"
    first = TranscriptionJob(
        job_dir,
        transcriber=ModelTranscriber("/m/ggml-small.en.bin"),
        audio_provider=_provider(20.0),
        chunker=_chunker,
        chunk_seconds=10,
    )
    m = first.run("https://youtu.be/vid")
    # Simulate an interrupted run: chunk 1 not done, then resume with large-v3.
    data = json.loads((job_dir / "manifest.json").read_text())
    data["status"] = "transcribing"
    data["chunks"][1]["status"] = "pending"
    (job_dir / "manifest.json").write_text(json.dumps(data))
    (job_dir / "transcript.txt").unlink()
    second = TranscriptionJob(
        job_dir,
        transcriber=ModelTranscriber("/m/ggml-large-v3.bin"),
        audio_provider=_provider(20.0),
        chunker=_chunker,
        chunk_seconds=10,
    )
    m = second.run()
    assert m.model == "mixed"
    assert m.asr_quality == "degraded"
    assert "chunks used different models" in m.asr_issues[0]


def test_missing_model_fails_before_download_and_is_saved(tmp_path):
    calls = []

    def provider(url, dest_dir):
        calls.append(url)
        raise AssertionError("must not download")

    t = WhisperCppTranscriber(model_path=tmp_path / "ggml-large-v3.bin")
    job = TranscriptionJob(
        tmp_path / "job", transcriber=t, audio_provider=provider, chunker=_chunker
    )
    with pytest.raises(WhisperModelMissingError):
        job.run("https://youtu.be/vid")
    assert calls == []
    m = load_manifest(tmp_path / "job")
    assert m.status == "error" and "not found" in m.error
    assert m.model == "ggml-large-v3"


def test_whisper_command_has_prompt(tmp_path, monkeypatch):
    model = tmp_path / "ggml-large-v3.bin"
    model.write_bytes(b"m")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        prefix = cmd[cmd.index("--output-file") + 1]
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            json.dump({"transcription": [{"offsets": {"from": 0, "to": 1000}, "text": "hi"}]}, f)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    t = WhisperCppTranscriber(model_path=model, prompt="Glossary: planche, maltese.")
    segs = t.transcribe(tmp_path / "chunk_0000.wav", language="en")
    assert segs[0].text == "hi"
    assert seen["cmd"][seen["cmd"].index("--prompt") + 1] == "Glossary: planche, maltese."
    assert t.model_name == "ggml-large-v3"


def test_channel_job_preflight_error_is_saved_in_manifest(tmp_path):
    def enumerator(url):
        raise AssertionError("must not enumerate")

    def preflight():
        raise WhisperModelMissingError("Whisper model file not found: /x.bin")

    job = ChannelJob(
        tmp_path / "ch",
        enumerator=enumerator,
        transcriber_factory=lambda d: None,
        pdf_builder=lambda a, p: [],
        preflight=preflight,
    )
    with pytest.raises(WhisperModelMissingError):
        job.run("https://www.youtube.com/@x/videos")
    m = load_channel_manifest(tmp_path / "ch")
    assert m.status == STATUS_ERROR and "not found" in m.error


def test_channel_job_copies_asr_fields_and_keeps_speakers(tmp_path):
    info = ChannelInfo(
        channel_title="C",
        channel_handle="@c",
        channel_id="1",
        source_url="https://www.youtube.com/@c/videos",
        videos=[
            ChannelVideo(video_id="a", title="A", url="https://www.youtube.com/watch?v=a"),
        ],
    )

    def factory(video_dir):
        return TranscriptionJob(
            video_dir,
            transcriber=ModelTranscriber("/m/ggml-large-v3.bin"),
            audio_provider=_provider(60.0),
            chunker=_chunker,
        )

    pdf = tmp_path / "ch" / "out.pdf"

    def builder(articles, pkg):
        pdf.write_bytes(b"%PDF")
        return [pdf]

    job = ChannelJob(
        tmp_path / "ch",
        enumerator=lambda url: info,
        transcriber_factory=factory,
        pdf_builder=builder,
        asr_model="ggml-large-v3",
        asr_prompt="P",
    )
    job.run("https://www.youtube.com/@c/videos")
    # An operator adds speakers; a reload and save keeps them.
    path = tmp_path / "ch" / "manifest.json"
    data = json.loads(path.read_text())
    data["videos"][0]["speakers"] = ["C", "Guest"]
    path.write_text(json.dumps(data))
    m = load_channel_manifest(tmp_path / "ch")
    assert m.videos[0].speakers == ["C", "Guest"]
    assert m.videos[0].asr_model == "ggml-large-v3"
    assert m.videos[0].asr_quality == "ok"
    assert m.asr_model == "ggml-large-v3" and m.asr_prompt == "P"
