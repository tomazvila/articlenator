"""HTTP routes for the YouTube transcription service.

URL in -> transcription out. Jobs run in background daemon threads but persist
all state to disk (``manifest.json`` per job), so progress survives client
disconnects and full server restarts: the status endpoint reads the manifest
from disk, and ``/resume`` continues an interrupted job from its last completed
chunk.
"""

from __future__ import annotations

import re
import threading
import uuid

import structlog
from flask import Blueprint, current_app, jsonify, render_template, request, send_from_directory

from ..config import get_config
from ..auth import current_user
from ..security import is_valid_csrf_request
from ..resource_limits import ResourceLease, acquire_for_current_user
from ..sources.transcription import STATUS_COMPLETE, load_manifest
from ..sources.transcription_service import build_job
from ..sources.youtube_downloader import is_supported_youtube_url
from ..sources.youtube_cookies import YouTubeCookieStore
from ..user_data import current_user_paths

log = structlog.get_logger()

transcription_bp = Blueprint("transcription", __name__)

_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()

_TRANSCRIPT_FORMATS = {
    "txt": ("transcript.txt", "text/plain"),
    "vtt": ("transcript.vtt", "text/vtt"),
    "json": ("transcript.json", "application/json"),
}


def _csrf_error():
    return jsonify({"error": "CSRF token missing or invalid"}), 403


def _job_dir(job_id: str):
    if current_user() is None and not current_app.config.get("AUTH_REQUIRED", True):
        return get_config().transcription_dir / job_id
    return current_user_paths().transcription_dir / job_id


def _is_running(job_id: str) -> bool:
    with _threads_lock:
        thread = _threads.get(str(_job_dir(job_id)))
        return thread is not None and thread.is_alive()


def _start_thread(
    job_id: str,
    url: str | None,
    *,
    job_dir=None,
    cookie_store: YouTubeCookieStore | None = None,
    lease: ResourceLease | None = None,
) -> None:
    """Spawn (or restart) the background worker for a job."""
    job_dir = job_dir or _job_dir(job_id)

    def worker() -> None:
        try:
            build_job(job_dir, cookie_store=cookie_store).run(url)
        except Exception as exc:  # noqa: BLE001 - failure is persisted in the manifest
            log.warning("transcription_job_thread_failed", job_id=job_id, error=str(exc))
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
        thread = threading.Thread(target=worker, name=f"transcribe-{job_id}", daemon=True)
        _threads[thread_key] = thread
        try:
            thread.start()
        except Exception:
            _threads.pop(thread_key, None)
            if lease is not None:
                lease.release()
            raise


def _manifest_payload(job_id: str) -> dict | None:
    manifest = load_manifest(_job_dir(job_id))
    if manifest is None:
        return None
    total = len(manifest.chunks)
    done = manifest.done_chunks
    data = manifest.to_dict()
    data["done_chunks"] = done
    data["total_chunks"] = total
    data["progress"] = round(done / total, 4) if total else 0.0
    data["running"] = _is_running(job_id)
    return data


@transcription_bp.route("/transcription")
def page():
    """GET /transcription - transcription UI."""
    return render_template("transcription.html")


@transcription_bp.route("/api/transcription", methods=["POST"])
def start():
    """POST /api/transcription - start a transcription job for a YouTube URL."""
    if not is_valid_csrf_request():
        return _csrf_error()

    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not url or not is_supported_youtube_url(url):
        return jsonify({"error": "A valid YouTube video URL is required"}), 400

    job_id = uuid.uuid4().hex
    log.info("transcription_job_starting", job_id=job_id, url=url)
    if current_user() is None:
        _start_thread(job_id, url)
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
            job_dir=_job_dir(job_id),
            cookie_store=cookie_store,
            lease=lease,
        )
    return jsonify({"job_id": job_id, "status": "started"}), 202


@transcription_bp.route("/api/transcriptions", methods=["GET"])
def list_jobs():
    """GET /api/transcriptions - list known transcription jobs (most recent state)."""
    base = (
        get_config().transcription_dir
        if current_user() is None and not current_app.config.get("AUTH_REQUIRED", True)
        else current_user_paths().transcription_dir
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


@transcription_bp.route("/api/transcription/<job_id>", methods=["GET"])
def status(job_id: str):
    """GET /api/transcription/<job_id> - current job state (read from disk)."""
    if not _JOB_ID_RE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    payload = _manifest_payload(job_id)
    if payload is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(payload)


@transcription_bp.route("/api/transcription/<job_id>/resume", methods=["POST"])
def resume(job_id: str):
    """POST /api/transcription/<job_id>/resume - continue an interrupted job."""
    if not is_valid_csrf_request():
        return _csrf_error()
    if not _JOB_ID_RE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400

    manifest = load_manifest(_job_dir(job_id))
    if manifest is None:
        return jsonify({"error": "Job not found"}), 404
    if manifest.status == STATUS_COMPLETE:
        return jsonify({"job_id": job_id, "status": STATUS_COMPLETE})
    if _is_running(job_id):
        return jsonify({"job_id": job_id, "status": "running"})

    log.info("transcription_job_resuming", job_id=job_id)
    if current_user() is None:
        _start_thread(job_id, None)
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
            job_dir=_job_dir(job_id),
            cookie_store=cookie_store,
            lease=lease,
        )
    return jsonify({"job_id": job_id, "status": "resumed"}), 202


@transcription_bp.route("/api/transcription/<job_id>/transcript.<fmt>", methods=["GET"])
def transcript(job_id: str, fmt: str):
    """GET /api/transcription/<job_id>/transcript.<fmt> - download a transcript."""
    if not _JOB_ID_RE.match(job_id):
        return jsonify({"error": "Invalid job id"}), 400
    if fmt not in _TRANSCRIPT_FORMATS:
        return jsonify({"error": "Unsupported transcript format"}), 400

    manifest = load_manifest(_job_dir(job_id))
    if manifest is None:
        return jsonify({"error": "Job not found"}), 404
    if manifest.status != STATUS_COMPLETE:
        return jsonify({"error": "Transcript not ready", "status": manifest.status}), 409

    filename, mimetype = _TRANSCRIPT_FORMATS[fmt]
    job_dir = _job_dir(job_id)
    if not (job_dir / filename).exists():
        return jsonify({"error": "Transcript file missing"}), 404
    download_name = f"{manifest.video_id or job_id}.{fmt}"
    return send_from_directory(job_dir, filename, mimetype=mimetype, download_name=download_name)
