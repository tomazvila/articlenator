"""Build a configured, resumable whisper.cpp transcription job.

This bridges the pure pipeline (``transcription.TranscriptionJob``) with the
real collaborators (``whisper_transcriber``), wiring in the app config: model
path, whisper binary, yt-dlp downloader, cookies, and chunk length. Both the
HTTP routes and the WER integration suite construct jobs through here.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config, get_config
from .transcription import TranscriptionJob
from .whisper_transcriber import WhisperCppTranscriber, extract_audio, ffmpeg_chunk
from .youtube_cookies import YouTubeCookieStore


def resolve_cookie_file(config: Config) -> Path | None:
    """Return the server-side cookie file if present, else None."""
    path = config.youtube_cookie_path
    return path if path and Path(path).exists() else None


def build_job(
    job_dir: Path,
    *,
    config: Config | None = None,
    chunk_seconds: int | None = None,
    language: str | None = None,
    cookie_store: YouTubeCookieStore | None = None,
) -> TranscriptionJob:
    """Construct a TranscriptionJob backed by whisper.cpp + yt-dlp + ffmpeg."""
    config = config or get_config()
    legacy_cookie_file = resolve_cookie_file(config) if cookie_store is None else None
    downloader_bin = config.youtube_downloader_bin
    timeout = config.youtube_download_timeout

    transcriber = WhisperCppTranscriber(
        model_path=config.whisper_model_path,
        whisper_bin=config.whisper_bin,
        threads=config.whisper_threads,
        timeout_seconds=timeout,
    )

    def audio_provider(url: str, dest_dir: Path):
        if cookie_store is not None and cookie_store.is_configured():
            with cookie_store.temporary_cookie_file() as cookie_file:
                return extract_audio(
                    url,
                    dest_dir,
                    cookie_file=cookie_file,
                    downloader_bin=downloader_bin,
                    timeout_seconds=timeout,
                )
        return extract_audio(
            url,
            dest_dir,
            cookie_file=legacy_cookie_file,
            downloader_bin=downloader_bin,
            timeout_seconds=timeout,
        )

    def chunker(audio_path, chunk, dest_path):
        ffmpeg_chunk(audio_path, chunk, dest_path)

    return TranscriptionJob(
        Path(job_dir),
        transcriber=transcriber,
        audio_provider=audio_provider,
        chunker=chunker,
        chunk_seconds=chunk_seconds or config.transcription_chunk_seconds,
        language=language,
    )
