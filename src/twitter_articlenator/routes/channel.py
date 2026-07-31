"""HTTP routes for channel-wide transcription → PDF(s).

Channel URL in -> a single combined PDF or one PDF per video (selectable). Jobs
run in background daemon threads but persist all state to disk
(``manifest.json`` per job), so progress survives client disconnects and full
server restarts: the status endpoint reads the manifest from disk, and
``/resume`` continues an interrupted batch from its last completed video.
"""

from __future__ import annotations

import io
import re
import threading
import uuid
import zipfile
from datetime import date
from pathlib import Path

import structlog
from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)

from ..auth import current_user
from ..config import get_config
from ..pdf.generator import PACKAGING_COMBINED, PACKAGING_PER_ITEM
from ..resource_limits import ResourceLease, ResourceLimiter, acquire_for_current_user
from ..security import is_valid_csrf_request
from ..sources.channel_transcription import STATUS_COMPLETE, STATUS_ERROR, load_channel_manifest
from ..sources.channel_transcription_service import build_channel_job
from ..sources.youtube_channel import is_youtube_channel_url, months_ago_yyyymmdd
from ..sources.youtube_cookies import YouTubeCookieStore
from ..user_data import current_user_paths

log = structlog.get_logger()

channel_bp = Blueprint("channel", __name__)

_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_VALID_PACKAGING = {PACKAGING_COMBINED, PACKAGING_PER_ITEM}
_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


def _csrf_error():
    return jsonify({"error": "CSRF token missing or invalid"}), 403


def _job_dir(job_id: str) -> Path:
    if current_user() is None and not current_app.config.get("AUTH_REQUIRED", True):
        return get_config().channel_dir / job_id
    return current_user_paths().channel_dir / job_id


def _is_running(job_id: str) -> bool:
    with _threads_lock:
        thread = _threads.get(str(_job_dir(job_id)))
        return thread is not None and thread.is_alive()


def _start_thread(
    job_id: str,
    url: str | None,
    packaging: str,
    published_after: str | None = None,
    title_contains: str | None = None,
    *,
    job_dir: Path | None = None,
    cookie_store: YouTubeCookieStore | None = None,
    lease: ResourceLease | None = None,
) -> None:
    """Spawn (or restart) the background worker for a channel job."""
    job_dir = job_dir or _job_dir(job_id)

    def worker() -> None:
        try:
            build_channel_job(
                job_dir,
                packaging=packaging,
                published_after=published_after,
                title_contains=title_contains,
                cookie_store=cookie_store,
            ).run(url)
        except Exception as exc:  # noqa: BLE001 - failure is persisted in the manifest
            log.warning("channel_job_thread_failed", job_id=job_id, error=str(exc))
        finally:
            if lease is not None:
                lease.release()

    with _threads_lock:
        thread_key = str(job_dir)
        existing = _threads.get(thread_key)
        if existing is not None and existing.is_alive():
            if lease is not None:
                lease.release()
            return
        thread = threading.Thread(target=worker, name=f"channel-{job_id}", daemon=True)
        _threads[thread_key] = thread
        try:
            thread.start()
        except Exception:
            _threads.pop(thread_key, None)
            if lease is not None:
                lease.release()
            raise


def resume_inflight_channel_jobs(
    targets: list[tuple[Path, YouTubeCookieStore | None, str]] | None = None,
    *,
    limiter: ResourceLimiter | None = None,
) -> list[str]:
    """Re-spawn workers for channel jobs left mid-flight by a crash/restart.

    Called on server startup so a job survives the process dying (sleep, crash,
    or the controlling session being killed): the manifest is on disk, and any
    job whose status is not terminal (complete/error) is resumed from its last
    checkpoint. Returns the list of resumed job ids.
    """
    resumed: list[str] = []
    if targets is None:
        targets = [(get_config().channel_dir, None, "legacy-test-mode")]
    for base, cookie_store, user_id in targets:
        if not base.exists():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or not _JOB_ID_RE.match(entry.name):
                continue
            manifest = load_channel_manifest(entry)
            if manifest is None or manifest.status in (STATUS_COMPLETE, STATUS_ERROR):
                continue
            lease = limiter.try_acquire("transcription", user_id) if limiter is not None else None
            if limiter is not None and lease is None:
                log.warning(
                    "channel_job_resume_deferred_by_limit",
                    job_id=entry.name,
                    user_id=user_id,
                )
                continue
            if cookie_store is None:
                if lease is None:
                    _start_thread(
                        entry.name,
                        None,
                        manifest.packaging,
                        manifest.published_after,
                        manifest.title_contains,
                    )
                else:
                    _start_thread(
                        entry.name,
                        None,
                        manifest.packaging,
                        manifest.published_after,
                        manifest.title_contains,
                        lease=lease,
                    )
            else:
                _start_thread(
                    entry.name,
                    None,
                    manifest.packaging,
                    manifest.published_after,
                    manifest.title_contains,
                    job_dir=entry,
                    cookie_store=cookie_store,
                    lease=lease,
                )
            resumed.append(entry.name)
    if resumed:
        log.info("channel_jobs_resumed_on_startup", jobs=resumed)
    return resumed


def _manifest_payload(job_id: str) -> dict | None:
    manifest = load_channel_manifest(_job_dir(job_id))
    if manifest is None:
        return None
    total = len(manifest.videos)
    done = manifest.done_videos
    data = manifest.to_dict()
    data["job_id"] = job_id
    data["total_videos"] = total
    data["done_videos"] = done
    data["failed_videos"] = manifest.failed_videos
    data["progress"] = round(done / total, 4) if total else 0.0
    data["running"] = _is_running(job_id)
    return data


@channel_bp.route("/channel")
def page():
    """GET /channel - channel transcription UI."""
    return render_template("channel.html")


@channel_bp.route("/api/channel", methods=["POST"])
def start():
    """POST /api/channel - start a channel transcription job."""
    if not is_valid_csrf_request():
        return _csrf_error()

    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    packaging = str(data.get("packaging", PACKAGING_COMBINED)).strip() or PACKAGING_COMBINED
    if not url or not is_youtube_channel_url(url):
        return jsonify({"error": "A valid YouTube channel URL is required"}), 400
    if packaging not in _VALID_PACKAGING:
        return jsonify({"error": "Invalid packaging mode"}), 400

    published_after = None
    raw_months = data.get("since_months")
    if raw_months not in (None, "", 0, "0"):
        try:
            months = int(raw_months)
        except (TypeError, ValueError):
            return jsonify({"error": "since_months must be a whole number"}), 400
        if months < 1 or months > 600:
            return jsonify({"error": "since_months must be between 1 and 600"}), 400
        published_after = months_ago_yyyymmdd(date.today(), months)

    title_contains = str(data.get("title_contains", "")).strip() or None
    if title_contains and len(title_contains) > 200:
        return jsonify({"error": "title_contains is too long"}), 400

    job_id = uuid.uuid4().hex
    job_dir = _job_dir(job_id)
    log.info(
        "channel_job_starting",
        job_id=job_id,
        url=url,
        packaging=packaging,
        published_after=published_after,
        title_contains=title_contains,
    )
    if current_user() is None:
        _start_thread(job_id, url, packaging, published_after, title_contains)
    else:
        lease = acquire_for_current_user("transcription")
        if lease is None:
            return jsonify({"error": "Transcription job limit reached"}), 429
        config = get_config()
        paths = current_user_paths()
        cookie_store = YouTubeCookieStore(
            cookie_path=paths.youtube_cookie_path,
            encryption_key=current_app.config.get("COOKIE_ENCRYPTION_KEY"),
            require_encryption=current_app.config.get("REQUIRE_COOKIE_ENCRYPTION", True),
            max_bytes=getattr(config, "youtube_cookie_max_bytes", 262144),
        )
        _start_thread(
            job_id,
            url,
            packaging,
            published_after,
            title_contains,
            job_dir=job_dir,
            cookie_store=cookie_store,
            lease=lease,
        )
    return jsonify({"job_id": job_id, "status": "started"}), 202


@channel_bp.route("/api/channels", methods=["GET"])
def list_jobs():
    """GET /api/channels - list known channel jobs (most recent state)."""
    base = (
        get_config().channel_dir
        if current_user() is None and not current_app.config.get("AUTH_REQUIRED", True)
        else current_user_paths().channel_dir
    )
    jobs = []
    if base.exists():
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or not _JOB_ID_RE.match(entry.name):
                continue
            payload = _manifest_payload(entry.name)
            if payload is not None:
                jobs.append(payload)
    return jsonify({"jobs": jobs})


@channel_bp.route("/api/channel/<job_id>", methods=["GET"])
def status(job_id: str):
    """GET /api/channel/<job_id> - current job state (read from disk)."""
    if not _JOB_ID_RE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    payload = _manifest_payload(job_id)
    if payload is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(payload)


@channel_bp.route("/api/channel/<job_id>/resume", methods=["POST"])
def resume(job_id: str):
    """POST /api/channel/<job_id>/resume - continue an interrupted job."""
    if not is_valid_csrf_request():
        return _csrf_error()
    if not _JOB_ID_RE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400

    manifest = load_channel_manifest(_job_dir(job_id))
    if manifest is None:
        return jsonify({"error": "Job not found"}), 404
    if manifest.status == STATUS_COMPLETE and manifest.pdf_files:
        return jsonify({"job_id": job_id, "status": STATUS_COMPLETE})
    if _is_running(job_id):
        return jsonify({"job_id": job_id, "status": "running"})

    log.info("channel_job_resuming", job_id=job_id)
    if current_user() is None:
        _start_thread(
            job_id,
            None,
            manifest.packaging,
            manifest.published_after,
            manifest.title_contains,
        )
    else:
        lease = acquire_for_current_user("transcription")
        if lease is None:
            return jsonify({"error": "Transcription job limit reached"}), 429
        config = get_config()
        paths = current_user_paths()
        cookie_store = YouTubeCookieStore(
            cookie_path=paths.youtube_cookie_path,
            encryption_key=current_app.config.get("COOKIE_ENCRYPTION_KEY"),
            require_encryption=current_app.config.get("REQUIRE_COOKIE_ENCRYPTION", True),
            max_bytes=getattr(config, "youtube_cookie_max_bytes", 262144),
        )
        _start_thread(
            job_id,
            None,
            manifest.packaging,
            manifest.published_after,
            manifest.title_contains,
            job_dir=_job_dir(job_id),
            cookie_store=cookie_store,
            lease=lease,
        )
    return jsonify({"job_id": job_id, "status": "resumed"}), 202


def _resolve_pdf(job_id: str, index: int) -> Path | None:
    """Return the on-disk path of generated PDF ``index`` if valid, else None."""
    manifest = load_channel_manifest(_job_dir(job_id))
    if manifest is None or manifest.status != STATUS_COMPLETE:
        return None
    if index < 0 or index >= len(manifest.pdf_files):
        return None
    job_dir = _job_dir(job_id).resolve()
    candidate = (job_dir / manifest.pdf_files[index]).resolve()
    # Defence in depth: keep the served file inside the job directory.
    if job_dir not in candidate.parents or not candidate.exists():
        return None
    return candidate


@channel_bp.route("/api/channel/<job_id>/pdf/<int:index>", methods=["GET"])
def pdf(job_id: str, index: int):
    """GET /api/channel/<job_id>/pdf/<index> - download one generated PDF."""
    if not _JOB_ID_RE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    manifest = load_channel_manifest(_job_dir(job_id))
    if manifest is None:
        return jsonify({"error": "Job not found"}), 404
    if manifest.status != STATUS_COMPLETE:
        return jsonify({"error": "PDFs not ready", "status": manifest.status}), 409
    resolved = _resolve_pdf(job_id, index)
    if resolved is None:
        return jsonify({"error": "PDF not found"}), 404
    return send_from_directory(
        resolved.parent,
        resolved.name,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=resolved.name,
    )


@channel_bp.route("/api/channel/<job_id>/pdfs.zip", methods=["GET"])
def pdfs_zip(job_id: str):
    """GET /api/channel/<job_id>/pdfs.zip - download all generated PDFs as a zip."""
    if not _JOB_ID_RE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    manifest = load_channel_manifest(_job_dir(job_id))
    if manifest is None:
        return jsonify({"error": "Job not found"}), 404
    if manifest.status != STATUS_COMPLETE:
        return jsonify({"error": "PDFs not ready", "status": manifest.status}), 409
    if not manifest.pdf_files:
        return jsonify({"error": "No PDFs available"}), 404

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(len(manifest.pdf_files)):
            resolved = _resolve_pdf(job_id, index)
            if resolved is not None:
                archive.write(resolved, arcname=resolved.name)
    buffer.seek(0)

    stem = (manifest.channel_handle or manifest.channel_title or job_id).lstrip("@") or job_id
    download_name = f"{re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('-') or job_id}.zip"
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
    )
