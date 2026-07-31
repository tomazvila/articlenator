"""Build a configured, resumable channel transcription → PDF(s) job.

Wires the pure batch driver (``channel_transcription.ChannelJob``) to the real
collaborators using the app config: channel enumeration (yt-dlp), the existing
per-video whisper.cpp pipeline (``transcription_service.build_job`` — fully
reused, including cookies/model/chunking), and PDF rendering
(``pdf.generator.generate_pdfs``). Both the HTTP routes and tests construct
channel jobs through here.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config, get_config
from ..pdf.generator import PACKAGING_COMBINED, generate_pdfs
from .base import Article
from .channel_transcription import ChannelJob
from .transcription_service import build_job, resolve_cookie_file
from .youtube_channel import ChannelInfo, list_channel_videos
from .youtube_cookies import YouTubeCookieStore

# yt-dlp flat enumeration of a channel is metadata-only; cap it well under the
# (much larger) media-download timeout.
CHANNEL_ENUMERATION_TIMEOUT = 600


def build_channel_job(
    job_dir: Path,
    *,
    config: Config | None = None,
    packaging: str = PACKAGING_COMBINED,
    published_after: str | None = None,
    title_contains: str | None = None,
    cookie_store: YouTubeCookieStore | None = None,
) -> ChannelJob:
    """Construct a ChannelJob backed by yt-dlp + the per-video whisper pipeline.

    ``published_after`` (YYYYMMDD) limits enumeration to videos uploaded on/after
    that date; ``title_contains`` keeps only videos whose title contains the
    string (case-insensitive). Both are optional and combinable.
    """
    config = config or get_config()
    legacy_cookie_file = resolve_cookie_file(config) if cookie_store is None else None
    job_dir = Path(job_dir)

    def enumerator(url: str) -> ChannelInfo:
        if cookie_store is not None and cookie_store.is_configured():
            with cookie_store.temporary_cookie_file() as cookie_file:
                return list_channel_videos(
                    url,
                    after_date=published_after,
                    title_contains=title_contains,
                    cookie_file=cookie_file,
                    downloader_bin=config.youtube_downloader_bin,
                    timeout_seconds=CHANNEL_ENUMERATION_TIMEOUT,
                )
        return list_channel_videos(
            url,
            after_date=published_after,
            title_contains=title_contains,
            cookie_file=legacy_cookie_file,
            downloader_bin=config.youtube_downloader_bin,
            timeout_seconds=CHANNEL_ENUMERATION_TIMEOUT,
        )

    def transcriber_factory(video_dir: Path):
        return build_job(video_dir, config=config, cookie_store=cookie_store)

    def pdf_builder(articles: list[Article], pkg: str) -> list[Path]:
        return generate_pdfs(articles, output_dir=job_dir, packaging=pkg)

    return ChannelJob(
        job_dir,
        enumerator=enumerator,
        transcriber_factory=transcriber_factory,
        pdf_builder=pdf_builder,
        packaging=packaging,
        published_after=published_after,
        title_contains=title_contains,
    )
