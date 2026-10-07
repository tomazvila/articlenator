"""Resumable YouTube transcription pipeline (Tier-2 local ASR).

A transcription job is a directory on disk:

    <job_dir>/
        manifest.json          # full job state; updated atomically
        audio.wav              # 16 kHz mono source audio
        chunks/chunk_0000.wav  # fixed-length audio chunks
        chunks/chunk_0000.json # per-chunk transcript (segments, global time)
        transcript.txt|.vtt|.json
        asr.json               # model really used, prompt, quality check result

Because every step writes its state to ``manifest.json`` with an atomic
replace, a job interrupted by a network drop, a crash, or a full server
restart can be resumed: completed chunks are skipped and work continues from
the first unfinished one. The actual download / chunking / ASR work is injected
(``audio_provider`` / ``chunker`` / ``transcriber``) so the resilience logic is
testable without network access or the whisper.cpp binary.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import structlog

from .asr_tools import (
    Seg,
    assess_transcript,
    build_sidecar,
    model_label,
    write_json_atomic,
)

log = structlog.get_logger()

MANIFEST_NAME = "manifest.json"

# Job status progression.
STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_CHUNKING = "chunking"
STATUS_TRANSCRIBING = "transcribing"
STATUS_STITCHING = "stitching"
STATUS_COMPLETE = "complete"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class Segment:
    """A transcribed span. Times are in seconds."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class ChunkSpec:
    """An audio chunk's index and time bounds (seconds, source-relative)."""

    index: int
    start: float
    end: float

    @property
    def audio_name(self) -> str:
        return f"chunk_{self.index:04d}.wav"

    @property
    def output_name(self) -> str:
        return f"chunk_{self.index:04d}.json"


@dataclass(frozen=True)
class AudioInfo:
    """Result of preparing source audio for a URL."""

    path: Path
    duration: float
    video_id: str | None = None
    title: str | None = None


@runtime_checkable
class Transcriber(Protocol):
    """Transcribe one audio chunk. Segment times are LOCAL to the chunk."""

    def transcribe(self, audio_path: Path, *, language: str | None = None) -> list[Segment]: ...


# An audio provider downloads/prepares 16 kHz mono audio for a URL.
AudioProvider = Callable[[str, Path], AudioInfo]
# A chunker writes [chunk.start, chunk.end] of ``audio_path`` to ``dest_path``.
Chunker = Callable[[Path, ChunkSpec, Path], None]
ProgressCallback = Callable[[dict], None]


def compute_chunks(duration: float, chunk_seconds: float) -> list[ChunkSpec]:
    """Split ``duration`` seconds into consecutive fixed-length chunks.

    The final chunk is shorter when the duration is not an exact multiple.
    """
    if duration <= 0:
        raise ValueError(f"duration must be positive, got {duration}")
    if chunk_seconds <= 0:
        raise ValueError(f"chunk_seconds must be positive, got {chunk_seconds}")

    chunks: list[ChunkSpec] = []
    start = 0.0
    index = 0
    while start < duration - 1e-6:
        end = min(start + chunk_seconds, duration)
        chunks.append(ChunkSpec(index=index, start=start, end=end))
        start = end
        index += 1
    return chunks


@dataclass
class ChunkState:
    """Persisted state for a single chunk."""

    index: int
    start: float
    end: float
    status: str = STATUS_PENDING
    output: str | None = None  # relative path to the chunk transcript JSON
    model: str | None = None  # model label that transcribed this chunk


@dataclass
class Manifest:
    """The full, on-disk state of a transcription job."""

    job_id: str
    url: str
    status: str = STATUS_PENDING
    # The model label is set from the model file that the transcriber really
    # loads (see ``asr_tools.model_label``). Old manifests can hold a constant
    # label here; ``model_file`` is None for those, so readers must not trust them.
    model: str = ""
    model_file: str | None = None
    prompt: str | None = None
    asr_quality: str | None = None
    asr_issues: list[str] = field(default_factory=list)
    language: str | None = None
    chunk_seconds: float = 600.0
    duration: float | None = None
    audio: str | None = None  # relative path to prepared audio
    video_id: str | None = None
    title: str | None = None
    chunks: list[ChunkState] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        chunk_fields = set(ChunkState.__dataclass_fields__)
        chunks = [
            ChunkState(**{k: v for k, v in c.items() if k in chunk_fields})
            for c in data.get("chunks", [])
        ]
        known = {f for f in cls.__dataclass_fields__ if f != "chunks"}
        return cls(chunks=chunks, **{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def done_chunks(self) -> int:
        return sum(1 for c in self.chunks if c.status == STATUS_COMPLETE)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_manifest(job_dir: Path) -> Manifest | None:
    """Read a job's manifest from disk, or None if absent/corrupt."""
    path = Path(job_dir) / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("transcription_manifest_corrupt", job_dir=str(job_dir), error=str(exc))
        return None


def stitch_segments(chunk_segment_lists: list[list[Segment]]) -> list[Segment]:
    """Flatten per-chunk (already global-time) segments into one ordered list."""
    segments: list[Segment] = [seg for chunk in chunk_segment_lists for seg in chunk]
    segments.sort(key=lambda s: (s.start, s.end))
    return segments


def segments_to_text(segments: list[Segment]) -> str:
    """Join segment text into a single normalized-spacing transcript."""
    parts = [seg.text.strip() for seg in segments if seg.text.strip()]
    return " ".join(parts).strip()


def _format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def segments_to_vtt(segments: list[Segment]) -> str:
    """Render segments as a WebVTT document."""
    lines = ["WEBVTT", ""]
    for seg in segments:
        if not seg.text.strip():
            continue
        lines.append(f"{_format_timestamp(seg.start)} --> {_format_timestamp(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _transcriber_attr(transcriber: object, name: str) -> object:
    return getattr(transcriber, name, None)


class TranscriptionJob:
    """Drive a resumable transcription job rooted at ``job_dir``.

    The model label comes from the transcriber (``model_path`` or
    ``model_name`` attribute). If the transcriber does not expose one and the
    caller gives no ``model``, the label is ``unknown``. The job never writes
    a constant model name.
    """

    def __init__(
        self,
        job_dir: Path,
        *,
        transcriber: Transcriber,
        audio_provider: AudioProvider,
        chunker: Chunker,
        chunk_seconds: float = 600.0,
        language: str | None = None,
        model: str | None = None,
    ) -> None:
        self.dir = Path(job_dir)
        self.transcriber = transcriber
        self.audio_provider = audio_provider
        self.chunker = chunker
        self.chunk_seconds = chunk_seconds
        self.language = language
        model_path = _transcriber_attr(transcriber, "model_path")
        self.model_file = Path(str(model_path)).name if model_path else None
        self.model = (
            model
            or _transcriber_attr(transcriber, "model_name")
            or (model_label(str(model_path)) if model_path else None)
            or "unknown"
        )
        self.prompt = _transcriber_attr(transcriber, "prompt")

    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST_NAME

    def load_manifest(self) -> Manifest | None:
        """Load the persisted manifest, or None if absent/corrupt."""
        return load_manifest(self.dir)

    def _save(self, manifest: Manifest) -> None:
        _atomic_write_text(self.manifest_path, json.dumps(manifest.to_dict(), indent=2))

    def run(
        self, url: str | None = None, *, on_progress: ProgressCallback | None = None
    ) -> Manifest:
        """Run (or resume) the job to completion and return the final manifest."""
        self.dir.mkdir(parents=True, exist_ok=True)
        manifest = self.load_manifest()
        if manifest is None:
            if not url:
                raise ValueError("url is required to start a new transcription job")
            manifest = Manifest(
                job_id=self.dir.name,
                url=url,
                model=self.model,
                model_file=self.model_file,
                prompt=self.prompt if isinstance(self.prompt, str) else None,
                language=self.language,
                chunk_seconds=self.chunk_seconds,
            )
            self._save(manifest)

        # A finished job is a no-op (idempotent re-run).
        if manifest.status == STATUS_COMPLETE and (self.dir / "transcript.txt").exists():
            return manifest

        def progress(event: str, **extra: object) -> None:
            if on_progress is None:
                return
            payload = {
                "event": event,
                "status": manifest.status,
                "done_chunks": manifest.done_chunks,
                "total_chunks": len(manifest.chunks),
            }
            payload.update(extra)
            on_progress(payload)

        try:
            # Fail before any download if the model file is missing. There is
            # no fallback to another model.
            check = _transcriber_attr(self.transcriber, "check")
            if callable(check):
                check()
            self._ensure_audio(manifest, progress)
            self._ensure_chunk_plan(manifest)
            self._transcribe_chunks(manifest, progress)
            self._stitch(manifest, progress)
        except Exception as exc:  # noqa: BLE001 - persist failure for resume/inspection
            manifest.status = STATUS_ERROR
            manifest.error = str(exc)
            self._save(manifest)
            log.error("transcription_job_failed", job_id=manifest.job_id, error=str(exc))
            raise

        return manifest

    def _ensure_audio(self, manifest: Manifest, progress: Callable[..., None]) -> None:
        if manifest.audio and (self.dir / manifest.audio).exists() and manifest.duration:
            return
        manifest.status = STATUS_DOWNLOADING
        self._save(manifest)
        progress("download_started")
        info = self.audio_provider(manifest.url, self.dir)
        if info.duration <= 0:
            raise ValueError(f"prepared audio has non-positive duration: {info.duration}")
        manifest.audio = os.path.relpath(info.path, self.dir)
        manifest.duration = info.duration
        manifest.video_id = info.video_id
        manifest.title = info.title
        self._save(manifest)
        progress("download_complete", duration=info.duration)

    def _ensure_chunk_plan(self, manifest: Manifest) -> None:
        if manifest.chunks:
            return
        manifest.status = STATUS_CHUNKING
        assert manifest.duration is not None
        specs = compute_chunks(manifest.duration, manifest.chunk_seconds)
        manifest.chunks = [ChunkState(index=s.index, start=s.start, end=s.end) for s in specs]
        self._save(manifest)

    def _transcribe_chunks(self, manifest: Manifest, progress: Callable[..., None]) -> None:
        manifest.status = STATUS_TRANSCRIBING
        self._save(manifest)
        chunks_dir = self.dir / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        assert manifest.audio is not None
        audio_path = self.dir / manifest.audio

        for state in manifest.chunks:
            if (
                state.status == STATUS_COMPLETE
                and state.output
                and (self.dir / state.output).exists()
            ):
                continue  # already done; resume past it

            spec = ChunkSpec(index=state.index, start=state.start, end=state.end)
            chunk_audio = chunks_dir / spec.audio_name
            if not chunk_audio.exists():
                self.chunker(audio_path, spec, chunk_audio)

            local_segments = self.transcriber.transcribe(chunk_audio, language=manifest.language)
            global_segments = [
                Segment(start=s.start + spec.start, end=s.end + spec.start, text=s.text)
                for s in local_segments
            ]
            output_rel = f"chunks/{spec.output_name}"
            _atomic_write_text(
                self.dir / output_rel,
                json.dumps([asdict(s) for s in global_segments]),
            )
            # Checkpoint: mark done only after the output is safely on disk.
            state.output = output_rel
            state.model = self.model
            state.status = STATUS_COMPLETE
            self._save(manifest)
            progress("chunk_complete", chunk_index=state.index)

    def _stitch(self, manifest: Manifest, progress: Callable[..., None]) -> None:
        manifest.status = STATUS_STITCHING
        self._save(manifest)
        chunk_lists: list[list[Segment]] = []
        for state in manifest.chunks:
            assert state.output is not None
            raw = json.loads((self.dir / state.output).read_text(encoding="utf-8"))
            chunk_lists.append([Segment(**s) for s in raw])

        segments = stitch_segments(chunk_lists)

        # Record the model that really transcribed the chunks. Chunks from an
        # older run have no label; a resume with another model gives a mix.
        chunk_models = sorted({c.model or "unknown" for c in manifest.chunks})
        manifest.model = chunk_models[0] if len(chunk_models) == 1 else "mixed"
        if manifest.model != self.model:
            manifest.model_file = None

        report = assess_transcript(
            None,
            segments=[Seg(s.start, s.end, s.text) for s in segments],
            duration=manifest.duration,
        )
        if len(chunk_models) > 1:
            report.issues.insert(0, "chunks used different models: " + ", ".join(chunk_models))
            report.quality = "degraded"
        manifest.asr_quality = report.quality
        manifest.asr_issues = list(report.issues)

        _atomic_write_text(self.dir / "transcript.txt", segments_to_text(segments) + "\n")
        _atomic_write_text(self.dir / "transcript.vtt", segments_to_vtt(segments))
        _atomic_write_text(
            self.dir / "transcript.json",
            json.dumps(
                {
                    "job_id": manifest.job_id,
                    "url": manifest.url,
                    "video_id": manifest.video_id,
                    "title": manifest.title,
                    "duration": manifest.duration,
                    "model": manifest.model,
                    "model_file": manifest.model_file,
                    "prompt": manifest.prompt,
                    "asr_quality": manifest.asr_quality,
                    "asr_issues": manifest.asr_issues,
                    "segments": [asdict(s) for s in segments],
                },
                indent=2,
            ),
        )
        model_path = _transcriber_attr(self.transcriber, "model_path")
        write_json_atomic(
            self.dir / "asr.json",
            build_sidecar(
                model_path=str(model_path) if model_path and manifest.model_file else None,
                prompt=manifest.prompt,
                language=manifest.language,
                report=report,
                timestamps=True,
                extra={"model": manifest.model, "chunk_models": chunk_models},
            ),
        )
        manifest.status = STATUS_COMPLETE
        manifest.error = None
        self._save(manifest)
        progress("complete", asr_quality=report.quality)
