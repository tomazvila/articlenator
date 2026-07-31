#!/usr/bin/env python3
"""Phase A (video): stage locally-produced video transcripts as literature notes.

The channel transcription pipeline already wrote one transcript per video to
``<channel_dir>/<job_id>/videos/<video_id>/transcript.txt`` with title / watch-URL /
channel / upload-date in each job's ``manifest.json``. This turns that corpus into the
SAME staging contract the synthesis Ralph loop (Phase B) consumes — one markdown
literature note per video plus a ``queue.json`` of ``extracted`` items — so the existing
``loop.sh`` / ``review_loop.sh`` machinery synthesizes them with no changes.

Unlike ``ingest.py`` (which re-fetches Twitter URLs over the network), this is **local
only**: the transcript text already exists, so items are staged directly at
``stage="extracted"`` (no ``pending`` fetch step).

Run inside the project's nix env, writing to a transcript-specific staging dir so it never
collides with the Twitter corpus:

    nix develop
    ZR_STAGING="$PWD/zettel_ralph/staging_transcripts" \
        python zettel_ralph/ingest_transcripts.py

Idempotent + resumable: re-running merges new videos into the existing queue and never
duplicates or overwrites a note already staged. Atomic writes throughout.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging_transcripts"))

# Channel jobs that are click-test fixtures, not real content (the first-ever YouTube
# video used to validate the pipeline end to end). Excluded unless --include-tests.
_TEST_CHANNEL_TITLES = {"jawed"}
_TEST_VIDEO_IDS = {"jNQXAC9IVRw"}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX — a crash mid-write can't corrupt the target


def _default_channels_dir() -> Path:
    """The app's channel-jobs directory, with a no-import fallback."""
    try:
        from twitter_articlenator.config import get_config

        return Path(get_config().channel_dir)
    except Exception:  # noqa: BLE001 - keep the ingest runnable without the app importable
        return Path.home() / "Downloads" / "twitter-articles" / "channels"


def _published_iso(upload_date: str | None) -> str | None:
    """Normalise a YYYYMMDD upload date to an ISO date string (None if absent/bad)."""
    if not upload_date:
        return None
    try:
        return datetime.strptime(upload_date, "%Y%m%d").date().isoformat()
    except (ValueError, TypeError):
        return None


def iter_transcribed_videos(channels_dir: Path, *, include_tests: bool):
    """Yield ``(channel_title, video_dict, transcript_path)`` for every completed video
    that has a transcript on disk, across all channel jobs."""
    if not channels_dir.exists():
        return
    for job_dir in sorted(p for p in channels_dir.iterdir() if p.is_dir()):
        manifest = job_dir / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        channel_title = data.get("channel_title") or "YouTube"
        if not include_tests and channel_title.strip().casefold() in _TEST_CHANNEL_TITLES:
            continue
        for video in data.get("videos", []):
            if video.get("status") != "complete":
                continue
            vid = video.get("video_id")
            if not vid or (not include_tests and vid in _TEST_VIDEO_IDS):
                continue
            tx = job_dir / "videos" / vid / "transcript.txt"
            if tx.exists():
                yield channel_title, video, tx


def write_lit_note(staging: Path, channel_title: str, video: dict, transcript: str) -> Path:
    lit_dir = staging / "lit"
    lit_dir.mkdir(parents=True, exist_ok=True)
    vid = video["video_id"]
    title = video.get("title") or vid
    fm = {
        "id": f"vid-{vid}",
        "kind": "video",
        "author": channel_title,
        "title": title,
        "source_url": video.get("url") or f"https://www.youtube.com/watch?v={vid}",
        "published_at": _published_iso(video.get("upload_date")),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    fm_yaml = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items())
    dest = lit_dir / f"vid-{vid}.md"
    _atomic_write(dest, f"---\n{fm_yaml}\n---\n\n# {title}\n\n{transcript.strip()}\n")
    return dest


def _lit_ref(lit_path: Path) -> str:
    """Path stored in the queue. The synthesis agent reads it relative to the harness dir
    (its cwd), so prefer a HERE-relative path; fall back to absolute for off-tree staging."""
    try:
        return str(lit_path.relative_to(HERE))
    except ValueError:
        return str(lit_path)


def _queue_item(staging: Path, video: dict, lit_path: Path) -> dict:
    return {
        "id": f"vid-{video['video_id']}",
        "url": video.get("url") or f"https://www.youtube.com/watch?v={video['video_id']}",
        "kind": "video",
        "stage": "extracted",  # transcript already on disk — no network fetch step
        "lit_note": _lit_ref(lit_path),
        "cluster": None,
        "notes_emitted": [],
        "attempts": 0,
        "error": None,
    }


def _seed_state(staging: Path) -> None:
    """Seed the Phase-B state files so the synthesis loop can run standalone (mirrors
    ingest.py's tail)."""
    cidx = staging / "concept-index.json"
    if not cidx.exists():
        _atomic_write(cidx, json.dumps({"version": 1, "concepts": [], "mocs": []}, indent=2))
    prov = staging / "provenance.json"
    if not prov.exists():
        _atomic_write(prov, "{}\n")
    for name in ("STATE.md", "DECISIONS.md"):
        dst = staging / name
        if not dst.exists():
            tmpl = HERE / "state" / f"{name.split('.')[0]}.template.md"
            _atomic_write(dst, tmpl.read_text() if tmpl.exists() else f"# {name}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channels-dir", type=Path, default=None, help="channel-jobs dir")
    ap.add_argument("--staging", type=Path, default=None, help="staging dir (else $ZR_STAGING)")
    ap.add_argument("--limit", type=int, default=0, help="stage at most N new videos (0 = all)")
    ap.add_argument("--include-tests", action="store_true", help="include jawed click-test clips")
    args = ap.parse_args()

    channels_dir = args.channels_dir or _default_channels_dir()
    staging = args.staging or STAGING
    staging.mkdir(parents=True, exist_ok=True)
    queue_path = staging / "queue.json"

    # Merge into any existing queue (idempotent / resumable): keep done work, add new videos.
    if queue_path.exists():
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
    else:
        queue = {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "items": []}
    by_id = {it["id"]: it for it in queue["items"]}

    added = skipped = 0
    for channel_title, video, tx_path in iter_transcribed_videos(
        channels_dir, include_tests=args.include_tests
    ):
        item_id = f"vid-{video['video_id']}"
        if item_id in by_id:
            skipped += 1
            continue
        if args.limit and added >= args.limit:
            break
        transcript = tx_path.read_text(encoding="utf-8")
        lit_path = write_lit_note(staging, channel_title, video, transcript)
        item = _queue_item(staging, video, lit_path)
        queue["items"].append(item)
        by_id[item_id] = item
        added += 1
        print(f"OK   {item_id}  <- {channel_title}: {(video.get('title') or '')[:50]}", flush=True)

    _atomic_write(queue_path, json.dumps(queue, indent=2, ensure_ascii=False))
    _seed_state(staging)

    total = len(queue["items"])
    print(
        f"DONE staged={added} already_present={skipped} queue_total={total} "
        f"staging={staging}",
        flush=True,
    )


if __name__ == "__main__":
    main()
