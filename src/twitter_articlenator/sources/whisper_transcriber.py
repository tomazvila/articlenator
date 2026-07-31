"""whisper.cpp transcriber plus yt-dlp/ffmpeg audio preparation.

These are the real (network + subprocess) implementations of the injected
``Transcriber`` / ``AudioProvider`` / ``Chunker`` collaborators used by
``transcription.TranscriptionJob``. They are exercised end-to-end by the WER
integration suite rather than the fast unit tests.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import structlog

from .transcription import AudioInfo, ChunkSpec, Segment

log = structlog.get_logger()


def probe_duration(path: Path, *, ffprobe_bin: str = "ffprobe") -> float:
    """Return the duration of a media file in seconds via ffprobe."""
    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return float(result.stdout.strip())


def extract_audio(
    url: str,
    dest_dir: Path,
    *,
    cookie_file: Path | None = None,
    downloader_bin: str = "yt-dlp",
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    timeout_seconds: int = 14400,
) -> AudioInfo:
    """Download best audio for ``url`` and convert it to 16 kHz mono WAV.

    The download uses ``--continue`` so a partially fetched file resumes rather
    than restarting after a network drop. The final WAV is what whisper.cpp
    consumes; its true duration (ffprobe) drives chunk boundaries.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    wav_path = dest_dir / "audio.wav"
    if wav_path.exists():
        return AudioInfo(path=wav_path, duration=probe_duration(wav_path, ffprobe_bin=ffprobe_bin))

    metadata = _fetch_metadata(url, cookie_file=cookie_file, downloader_bin=downloader_bin)

    source_template = str(dest_dir / "source.%(ext)s")
    cmd = [
        downloader_bin,
        "--no-warnings",
        "--no-playlist",
        "--continue",
        "--force-overwrites",
        "--js-runtimes",
        "node",
        "--socket-timeout",
        "30",
        "--retries",
        "10",
        "--fragment-retries",
        "20",
        "--retry-sleep",
        "http:exp=1:30",
        "-f",
        "bestaudio/b",
        "-o",
        source_template,
    ]
    if cookie_file is not None:
        cmd.extend(["--cookies", str(cookie_file)])
    cmd.append(url)

    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout_seconds)
    if proc.returncode != 0:
        raise RuntimeError(
            f"yt-dlp audio download failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )

    sources = sorted(p for p in dest_dir.glob("source.*") if p.suffix != ".part")
    if not sources:
        raise RuntimeError("yt-dlp completed but no audio file was produced")
    source = sources[0]

    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(source),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    source.unlink(missing_ok=True)

    return AudioInfo(
        path=wav_path,
        duration=probe_duration(wav_path, ffprobe_bin=ffprobe_bin),
        video_id=metadata.get("id"),
        title=metadata.get("title"),
    )


def _fetch_metadata(url: str, *, cookie_file: Path | None, downloader_bin: str) -> dict:
    cmd = [
        downloader_bin,
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--js-runtimes",
        "node",
        "--socket-timeout",
        "30",
    ]
    if cookie_file is not None:
        cmd.extend(["--cookies", str(cookie_file)])
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=300, check=True)
        return json.loads(proc.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log.warning("transcription_metadata_failed", url=url, error=str(exc))
        return {}


def ffmpeg_chunk(
    audio_path: Path,
    chunk: ChunkSpec,
    dest_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Write the [chunk.start, chunk.end] span of ``audio_path`` to ``dest_path``."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-ss",
            f"{chunk.start:.3f}",
            "-to",
            f"{chunk.end:.3f}",
            "-i",
            str(audio_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(dest_path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )


def parse_whisper_json(json_path: Path) -> list[Segment]:
    """Parse whisper-cli JSON output into segments.

    Reads with ``errors="replace"`` because whisper-cli can emit invalid UTF-8
    bytes for music / no-speech audio; the U+FFFD replacement char is valid
    inside a JSON string, so the document still parses.
    """
    data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    segments: list[Segment] = []
    for entry in data.get("transcription", []):
        offsets = entry.get("offsets", {})
        text = entry.get("text", "").strip()
        if not text:
            continue
        segments.append(
            Segment(
                start=float(offsets.get("from", 0)) / 1000.0,
                end=float(offsets.get("to", 0)) / 1000.0,
                text=text,
            )
        )
    return segments


class WhisperCppTranscriber:
    """Transcribe audio chunks by shelling out to whisper.cpp's ``whisper-cli``."""

    def __init__(
        self,
        *,
        model_path: Path,
        whisper_bin: str = "whisper-cli",
        threads: int | None = None,
        timeout_seconds: int = 14400,
    ) -> None:
        self.model_path = Path(model_path)
        self.whisper_bin = whisper_bin
        self.threads = threads
        self.timeout_seconds = timeout_seconds

    def transcribe(self, audio_path: Path, *, language: str | None = None) -> list[Segment]:
        if not self.model_path.exists():
            raise FileNotFoundError(f"whisper model not found: {self.model_path}")

        output_prefix = audio_path.with_suffix("")
        cmd = [
            self.whisper_bin,
            "-m",
            str(self.model_path),
            "-f",
            str(audio_path),
            "-l",
            language or "auto",
            "--output-json",
            "--output-file",
            str(output_prefix),
            "--no-prints",
        ]
        if self.threads:
            cmd.extend(["-t", str(self.threads)])

        # Decode tolerantly: whisper-cli can emit non-UTF-8 bytes for music /
        # no-speech audio, which would otherwise crash a strict decode.
        proc = subprocess.run(
            cmd,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=self.timeout_seconds,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"whisper-cli failed: {proc.stderr.strip() or proc.stdout.strip()}")

        json_path = output_prefix.with_suffix(".json")
        if not json_path.exists():
            raise RuntimeError(f"whisper-cli produced no JSON output at {json_path}")

        return parse_whisper_json(json_path)
