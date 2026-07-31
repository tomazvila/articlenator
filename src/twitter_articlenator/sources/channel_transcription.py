"""Resumable channel-wide transcription → PDF(s) batch job.

A channel job is a directory on disk:

    <job_dir>/
        manifest.json              # batch state; updated atomically
        videos/<video_id>/...      # one resumable TranscriptionJob per video
        <channel>_<date>.pdf       # rendered output(s)

The batch persists per-video state to ``manifest.json`` after every video, so a
job interrupted by a network drop, a crash, or a full server restart resumes
from the first unfinished video. Enumeration runs once. A per-video failure is
tolerated (recorded and skipped) rather than sinking the whole batch, mirroring
the playlist-download policy. Enumeration / per-video transcription / PDF
rendering are all injected so the batch logic is testable without network, the
whisper binary, or WeasyPrint.
"""

from __future__ import annotations

import html
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import structlog

from ..pdf.generator import PACKAGING_COMBINED
from .base import Article
from .transcription import (
    MANIFEST_NAME,
    STATUS_COMPLETE,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_TRANSCRIBING,
    TranscriptionJob,
    _atomic_write_text,
)
from .youtube_channel import ChannelInfo

log = structlog.get_logger()

# Channel-job status progression (per-video states reuse the transcription ones).
STATUS_ENUMERATING = "enumerating"
STATUS_RENDERING = "rendering"

# Collaborators are injected so the batch logic is unit-testable.
Enumerator = Callable[[str], ChannelInfo]
TranscriberFactory = Callable[[Path], TranscriptionJob]
PdfBuilder = Callable[[list[Article], str], list[Path]]
ProgressCallback = Callable[[dict], None]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class VideoEntry:
    """Persisted state for one video within a channel job."""

    video_id: str
    title: str
    url: str
    status: str = STATUS_PENDING
    error: str | None = None
    upload_date: str | None = None  # YYYYMMDD when known


@dataclass
class ChannelManifest:
    """The full, on-disk state of a channel transcription job."""

    job_id: str
    url: str
    channel_title: str = ""
    channel_handle: str | None = None
    channel_id: str | None = None
    status: str = STATUS_PENDING
    packaging: str = PACKAGING_COMBINED
    published_after: str | None = None  # YYYYMMDD date-window filter, if any
    title_contains: str | None = None  # case-insensitive title filter, if any
    videos: list[VideoEntry] = field(default_factory=list)
    pdf_files: list[str] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelManifest":
        entry_fields = set(VideoEntry.__dataclass_fields__)
        videos = [
            VideoEntry(**{k: v for k, v in vd.items() if k in entry_fields})
            for vd in data.get("videos", [])
            if isinstance(vd, dict)
        ]
        pdf_files = list(data.get("pdf_files", []))
        known = {f for f in cls.__dataclass_fields__ if f not in ("videos", "pdf_files")}
        return cls(
            videos=videos,
            pdf_files=pdf_files,
            **{k: v for k, v in data.items() if k in known},
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def done_videos(self) -> int:
        return sum(1 for v in self.videos if v.status == STATUS_COMPLETE)

    @property
    def failed_videos(self) -> int:
        return sum(1 for v in self.videos if v.status == STATUS_ERROR)


def load_channel_manifest(job_dir: Path) -> ChannelManifest | None:
    """Read a channel job's manifest from disk, or None if absent/corrupt."""
    import json

    path = Path(job_dir) / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        return ChannelManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("channel_manifest_corrupt", job_dir=str(job_dir), error=str(exc))
        return None


def _paragraphize(text: str, sentences_per_para: int = 4) -> list[str]:
    """Group a flat transcript into readable paragraphs of a few sentences."""
    text = text.strip()
    if not text:
        return []
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if not sentences:
        return [text]
    return [
        " ".join(sentences[i : i + sentences_per_para])
        for i in range(0, len(sentences), sentences_per_para)
    ]


def _parse_upload_date(value: str | None) -> datetime | None:
    """Parse a YYYYMMDD upload date into a datetime, or None if absent/invalid."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d")
    except (ValueError, TypeError):
        return None


def _transcript_to_article(transcript_text: str, entry: VideoEntry, channel_title: str) -> Article:
    """Wrap a plain-text transcript as a PDF-ready Article (HTML-escaped)."""
    paragraphs = _paragraphize(transcript_text)
    body = "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs) or "<p>(no transcript)</p>"
    return Article(
        title=entry.title,
        author=channel_title or "YouTube",
        content=body,
        published_at=_parse_upload_date(entry.upload_date),
        source_url=entry.url,
        source_type="youtube_channel",
    )


class ChannelJob:
    """Drive a resumable channel transcription → PDF(s) job rooted at ``job_dir``."""

    def __init__(
        self,
        job_dir: Path,
        *,
        enumerator: Enumerator,
        transcriber_factory: TranscriberFactory,
        pdf_builder: PdfBuilder,
        packaging: str = PACKAGING_COMBINED,
        published_after: str | None = None,
        title_contains: str | None = None,
    ) -> None:
        self.dir = Path(job_dir)
        self.enumerator = enumerator
        self.transcriber_factory = transcriber_factory
        self.pdf_builder = pdf_builder
        self.packaging = packaging
        self.published_after = published_after
        self.title_contains = title_contains

    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST_NAME

    def load_manifest(self) -> ChannelManifest | None:
        return load_channel_manifest(self.dir)

    def _save(self, manifest: ChannelManifest) -> None:
        import json

        _atomic_write_text(self.manifest_path, json.dumps(manifest.to_dict(), indent=2))

    def _video_dir(self, video_id: str) -> Path:
        return self.dir / "videos" / video_id

    def run(
        self, url: str | None = None, *, on_progress: ProgressCallback | None = None
    ) -> ChannelManifest:
        """Run (or resume) the channel job to completion and return the manifest."""
        self.dir.mkdir(parents=True, exist_ok=True)
        manifest = self.load_manifest()
        if manifest is None:
            if not url:
                raise ValueError("url is required to start a new channel job")
            manifest = ChannelManifest(
                job_id=self.dir.name,
                url=url,
                packaging=self.packaging,
                published_after=self.published_after,
                title_contains=self.title_contains,
            )
            self._save(manifest)

        # A finished job whose PDFs are still present is a no-op (idempotent re-run).
        if (
            manifest.status == STATUS_COMPLETE
            and manifest.pdf_files
            and all((self.dir / name).exists() for name in manifest.pdf_files)
        ):
            return manifest

        def progress(event: str, **extra: object) -> None:
            if on_progress is None:
                return
            payload = {
                "event": event,
                "status": manifest.status,
                "done_videos": manifest.done_videos,
                "total_videos": len(manifest.videos),
            }
            payload.update(extra)
            on_progress(payload)

        try:
            self._ensure_enumerated(manifest, progress)
            self._transcribe_videos(manifest, progress)
            self._render_pdfs(manifest, progress)
        except Exception as exc:  # noqa: BLE001 - persist failure for resume/inspection
            manifest.status = STATUS_ERROR
            manifest.error = str(exc)
            self._save(manifest)
            log.error("channel_job_failed", job_id=manifest.job_id, error=str(exc))
            raise

        return manifest

    def _ensure_enumerated(self, manifest: ChannelManifest, progress: Callable[..., None]) -> None:
        if manifest.videos:
            return
        manifest.status = STATUS_ENUMERATING
        self._save(manifest)
        progress("enumeration_started")
        info = self.enumerator(manifest.url)
        manifest.channel_title = info.channel_title
        manifest.channel_handle = info.channel_handle
        manifest.channel_id = info.channel_id
        manifest.videos = [
            VideoEntry(video_id=v.video_id, title=v.title, url=v.url, upload_date=v.upload_date)
            for v in info.videos
        ]
        self._save(manifest)
        progress("enumeration_complete", total_videos=len(manifest.videos))

    def _transcribe_videos(self, manifest: ChannelManifest, progress: Callable[..., None]) -> None:
        manifest.status = STATUS_TRANSCRIBING
        self._save(manifest)

        for entry in manifest.videos:
            # Skip videos already transcribed; retry pending and previously-failed ones.
            if (
                entry.status == STATUS_COMPLETE
                and (self._video_dir(entry.video_id) / "transcript.txt").exists()
            ):
                continue

            entry.status = STATUS_TRANSCRIBING
            entry.error = None
            self._save(manifest)
            progress("video_started", video_id=entry.video_id, title=entry.title)

            try:
                job = self.transcriber_factory(self._video_dir(entry.video_id))
                job.run(entry.url)
                entry.status = STATUS_COMPLETE
                entry.error = None
                progress("video_complete", video_id=entry.video_id)
            except Exception as exc:  # noqa: BLE001 - one bad video must not sink the batch
                entry.status = STATUS_ERROR
                entry.error = str(exc)
                log.warning(
                    "channel_video_failed",
                    job_id=manifest.job_id,
                    video_id=entry.video_id,
                    error=str(exc),
                )
                progress("video_error", video_id=entry.video_id, error=str(exc))
            # Checkpoint after every video so a restart resumes from here.
            self._save(manifest)

    def _render_pdfs(self, manifest: ChannelManifest, progress: Callable[..., None]) -> None:
        manifest.status = STATUS_RENDERING
        self._save(manifest)
        progress("rendering_started")

        articles: list[Article] = []
        for entry in manifest.videos:
            if entry.status != STATUS_COMPLETE:
                continue
            transcript_path = self._video_dir(entry.video_id) / "transcript.txt"
            text = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
            articles.append(_transcript_to_article(text, entry, manifest.channel_title))

        if not articles:
            raise RuntimeError("No videos were transcribed successfully; nothing to render")

        pdf_paths = self.pdf_builder(articles, manifest.packaging)
        manifest.pdf_files = [os.path.relpath(Path(p), self.dir) for p in pdf_paths]
        manifest.status = STATUS_COMPLETE
        manifest.error = None
        self._save(manifest)
        progress("complete", pdf_count=len(manifest.pdf_files))
