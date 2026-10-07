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
    ZR_STAGING="$PWD/zettel_ralph/staging_transcripts" \\
        python zettel_ralph/ingest_transcripts.py

Literature note contract (``lit/vid-<id>.md``): see the section "Transcription and
ingest" in ``README.md`` for the full field list. In short:

- Frontmatter: ``id``, ``video_id``, ``source_url``, ``title``, ``title_trusted``,
  ``channel``, ``speakers`` (main speaker first), ``speakers_source``,
  ``speakers_found_by``, ``multi_speaker``, ``published_at``, ``asr_model`` (the model
  that really ran, or ``unknown``), ``asr_quality`` (``ok`` | ``partial`` |
  ``degraded`` | ``unknown``), ``asr_issues``, ``asr_damaged_spans``, ``timestamps``,
  ``asr_source``, ``extracted_at``, ``canonical``.
- Body: ``# <title>`` and the transcript. If timed segments exist, each line starts
  with ``[MM:SS]`` or ``[H:MM:SS]`` (the segment start), so notes can cite
  ``vid-<id> @ MM:SS``.

Rules:

- Only videos with manifest status ``complete`` AND a transcript on disk are staged.
  A per-video job manifest that is not ``complete`` (a partial transcription) is
  skipped. Every skipped video is listed with its reason.
- A ``degraded`` transcript or an untrusted title gives queue stage ``held``, so no
  worker claims the item. ``--allow-degraded`` and ``--allow-untrusted-titles``
  override this; the lit file still records the problem. ``partial`` and ``unknown``
  transcripts are queued as ``extracted``.
- The title comes from ``meta.json`` (yt-dlp entry of the same id) when it exists,
  else from the manifest (see ``title_trust``).
- ``--asr-dir DIR`` reads newer transcripts from ``DIR/<video_id>/audio.*``.
  ``--replace-transcripts`` also replaces them for processed items (stage kept, old
  file in ``lit/_superseded/``). ``--upgrade-frontmatter`` rewrites the frontmatter of
  processed items in place.
- ``<staging>/transcripts.json`` names the canonical lit file per video.

Idempotent + resumable: re-running merges new videos into the existing queue and never
duplicates an item. Atomic writes throughout. Each run writes ``ingest_report.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging_transcripts"))

# Channel jobs that are click-test fixtures, not real content (the first-ever YouTube
# video used to validate the pipeline end to end). Excluded unless --include-tests.
_TEST_CHANNEL_TITLES = {"jawed"}
_TEST_VIDEO_IDS = {"jNQXAC9IVRw"}

STAGE_EXTRACTED = "extracted"
STAGE_HELD = "held"  # failed the transcript quality check; not claimed by workers
_REFRESHABLE_STAGES = {STAGE_EXTRACTED, STAGE_HELD}


def _load_asr_tools():
    """Load the standard-library-only ``asr_tools`` module by file path.

    A normal package import would also import the browser and PDF modules of the app.
    """
    import importlib.util

    name = "twitter_articlenator_asr_tools"
    if name in sys.modules:
        return sys.modules[name]
    path = PROJECT / "src" / "twitter_articlenator" / "sources" / "asr_tools.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


asr = _load_asr_tools()


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


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


_OWNER_SPLIT = re.compile(r"\s*[⎮|•–—]\s*")


def channel_owner(manifest: dict) -> str:
    """Main speaker of a channel: ``channel_owner`` from the manifest, else the part of
    the channel title before a separator ("Radoslav Radev ⎮ Calisthenics Mastery" gives
    "Radoslav Radev")."""
    owner = (manifest.get("channel_owner") or "").strip()
    if owner:
        return owner
    title = (manifest.get("channel_title") or "").strip() or "YouTube"
    return _OWNER_SPLIT.split(title)[0].strip() or title


@dataclass
class VideoRecord:
    """One video entry of a channel manifest, with the reason it is skipped (if any)."""

    job_dir: Path
    manifest: dict
    video: dict
    video_dir: Path
    transcript: Path | None
    skip_reason: str | None = None
    meta: dict = field(default_factory=dict)
    asr_dir: Path | None = None  # folder of a newer transcription (--asr-dir)
    speakers_file: dict = field(default_factory=dict)  # speakers.yaml of the job
    title_evidence: dict = field(default_factory=dict)  # id -> titles from run logs

    @property
    def asr_source_dir(self) -> Path:
        """Folder that holds the transcript files used for this record."""
        return self.asr_dir or self.video_dir

    @property
    def channel_title(self) -> str:
        return self.manifest.get("channel_title") or "YouTube"


def _job_manifests(channels_dir: Path):
    if not channels_dir.exists():
        return
    for job_dir in sorted(p for p in channels_dir.iterdir() if p.is_dir()):
        data = _read_json(job_dir / "manifest.json")
        if data is not None:
            yield job_dir, data


def _overlay_dir(asr_dir: Path | None, vid: str) -> Path | None:
    if asr_dir is None:
        return None
    d = asr_dir / vid
    js = d / "audio.json"
    if js.is_file() and js.stat().st_size > 0 and (d / "audio.txt").is_file():
        return d
    return None


_LOG_LINE = re.compile(r"^\s*\[\d+/\d+\]\s+\S+\s+([\w-]{11})\s{2}(.*?)\s{2}\(\d")


def title_evidence(job_dir: Path) -> dict[str, list[str]]:
    """Titles that ``batch_channel.py`` wrote per video id into its run files.

    Sources: ``failed.txt`` (``id<TAB>full title<TAB>...``) and ``pipeline.log``
    (``[007/10] ✓ <id>  <first 50 characters of the title>  (time)``). Both come
    from the yt-dlp entry of that id, so they can verify or refute a manifest
    title without network access.
    """
    out: dict[str, list[str]] = {}
    failed = job_dir / "failed.txt"
    if failed.is_file():
        for line in failed.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].strip():
                out.setdefault(parts[0].strip(), []).append(parts[1].strip())
    log = job_dir / "pipeline.log"
    if log.is_file():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _LOG_LINE.match(line)
            if m:
                out.setdefault(m.group(1), []).append(m.group(2).strip())
    return out


def _title_matches(title: str, evidence: str) -> bool:
    """True if ``evidence`` (a full title or its first 50 characters) fits ``title``.

    The comparison ignores case, accents, spaces, and punctuation, because
    YouTube shows a handle as "@SasaVenos" or "@saša venos".
    """
    t, e = asr.speaker_key(title), asr.speaker_key(evidence)
    if not e:
        return False
    # A handle can be written two ways; compare without the @mention part too.
    strip = re.compile(r"@\S+(?:\s+\S+)?")
    t2 = asr.speaker_key(strip.sub("", title))
    e2 = asr.speaker_key(strip.sub("", evidence))
    return (
        t.startswith(e)
        or e.startswith(t)
        or (bool(e2) and (t2.startswith(e2) or e2.startswith(t2)))
    )


def title_trust(rec: "VideoRecord") -> tuple[bool, str]:
    """Return ``(trusted, reason)`` for the title that ingest uses.

    Trusted: a ``meta.json`` title (same yt-dlp entry as the id), a manifest
    entry that ``batch_channel.py`` wrote with a ``title_source``, a manifest from
    the app channel job (it records titles by id), or a manifest title that
    matches the id-keyed titles in ``failed.txt`` / ``pipeline.log``.
    Untrusted: every other manifest title, for example a hand-written one.
    """
    vid = rec.video["video_id"]
    title = rec.video.get("title") or ""
    if rec.meta.get("title"):
        return True, "meta.json"
    evidence = rec.title_evidence.get(vid, [])
    if evidence and title:
        if all(not _title_matches(title, e) for e in evidence):
            return False, f"title conflicts with the run log of this id: {evidence[0]!r}"
        return True, "matches run log"
    if rec.video.get("title_source") in ("listing", "meta.json", "video-page"):
        return True, f"batch_channel.py ({rec.video['title_source']})"
    if "packaging" in rec.manifest:
        return True, "app channel job (titles by id)"
    return False, "no meta.json and no id-keyed run log for this video"


def scan_channels(
    channels_dir: Path, *, include_tests: bool, asr_dir: Path | None = None
) -> list[VideoRecord]:
    """Return a record for every video entry in every channel job, staged or not."""
    records: list[VideoRecord] = []
    for job_dir, data in _job_manifests(channels_dir) or []:
        channel_title = data.get("channel_title") or "YouTube"
        if not include_tests and channel_title.strip().casefold() in _TEST_CHANNEL_TITLES:
            continue
        speakers_file = asr.load_speakers_file(job_dir)
        evidence = title_evidence(job_dir)
        for video in data.get("videos", []):
            if not isinstance(video, dict):
                continue
            vid = video.get("video_id")
            if not vid or (not include_tests and vid in _TEST_VIDEO_IDS):
                continue
            vdir = job_dir / "videos" / vid
            tx = vdir / "transcript.txt"
            rec = VideoRecord(job_dir, data, video, vdir, tx if tx.exists() else None)
            rec.meta = _read_json(vdir / "meta.json") or {}
            rec.speakers_file = speakers_file
            rec.title_evidence = evidence
            overlay = _overlay_dir(asr_dir, vid)
            status = video.get("status")
            if overlay is not None:
                # A finished newer transcription replaces the old state of this video.
                rec.asr_dir = overlay
                rec.transcript = overlay / "audio.txt"
            elif status != "complete":
                err = video.get("error")
                rec.skip_reason = f"manifest status {status!r}" + (f": {err}" if err else "")
            elif rec.transcript is None:
                rec.skip_reason = "manifest says complete, but transcript.txt is missing"
            else:
                job = _read_json(vdir / "manifest.json")  # per-video job (app pipeline)
                if job is not None and job.get("status") not in (None, "complete"):
                    rec.skip_reason = (
                        f"partial transcription: per-video job status {job.get('status')!r}"
                        + (f": {job.get('error')}" if job.get("error") else "")
                    )
            records.append(rec)
    return records


def iter_transcribed_videos(channels_dir: Path, *, include_tests: bool):
    """Yield ``(channel_title, video_dict, transcript_path)`` for every completed video
    that has a transcript on disk, across all channel jobs."""
    for rec in scan_channels(channels_dir, include_tests=include_tests):
        if rec.skip_reason is None:
            yield rec.channel_title, rec.video, rec.transcript


def speaker_aliases(channels_dir: Path) -> dict[str, str]:
    """Alias map from all channel jobs: channel title, handle, and owner -> owner name.

    With it, a guest mention "@saša venos" in a sthenics_ title maps to the
    "SasaVenos" channel owner. ``speaker_aliases`` in a manifest and ``aliases``
    in ``speakers.yaml`` win.
    """
    aliases: dict[str, str] = {}
    explicit: dict[str, str] = {}
    for job_dir, data in _job_manifests(channels_dir) or []:
        sf = asr.load_speakers_file(job_dir)
        owner = sf.get("channel_owner") or channel_owner(data)
        for name in (data.get("channel_title"), data.get("channel_handle"), owner):
            if name:
                aliases.setdefault(asr.speaker_key(str(name).lstrip("@")), owner)
        for k, v in (data.get("speaker_aliases") or {}).items():
            explicit[asr.speaker_key(k)] = v
        for k, v in (sf.get("aliases") or {}).items():
            explicit[asr.speaker_key(k)] = v
    aliases.update(explicit)
    return aliases


def _duration(rec: VideoRecord) -> float | None:
    for value in (
        rec.meta.get("duration"),
        rec.video.get("duration"),
        (_read_json(rec.video_dir / "manifest.json") or {}).get("duration"),
    ):
        try:
            if value and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            continue
    audio = rec.video_dir / "audio.wav"
    return asr.wav_duration(audio) if audio.is_file() else None


def _asr_model(rec: VideoRecord, sidecar: dict | None) -> tuple[str, str | None]:
    """Return ``(asr_model, claimed_label)``.

    Only a recorded model file counts as proof. A label without a model file (old
    per-video manifests say "large-v3" as a constant) is reported as ``claimed``.
    """
    from_output = asr.model_path_from_dir(rec.asr_source_dir)
    if from_output:
        return asr.model_label(from_output), None
    if rec.asr_dir is not None:
        return "unknown", None
    if sidecar and sidecar.get("model_file"):
        return sidecar.get("model") or "unknown", None
    job = _read_json(rec.video_dir / "manifest.json") or {}
    if job.get("model_file"):
        return job.get("model") or "unknown", None
    claimed = job.get("model") or (sidecar or {}).get("model")
    if claimed in (None, "", "unknown"):
        claimed = None
    return "unknown", claimed


def span_fields(report, segments) -> dict:
    """Frontmatter fields for the damaged spans of ``report``.

    - ``asr_span_unit``: ``timestamp`` if the body has timed lines, else
      ``lit-body-line``.
    - ``asr_damaged_spans``: ``[["MM:SS", "MM:SS"], ...]`` for ``timestamp``, else
      ``["lines A-B", ...]`` in lit body line numbers.
    - ``asr_damaged_spans_detail``: always in lit body line numbers. Lit body line 1
      is the first line after the closing ``---`` of the frontmatter, counting blank
      lines and the ``# title`` line. ``start_char`` is the 0-based offset of the
      first damaged character in ``start_line``; ``end_char`` is the offset after
      the last damaged character in ``end_line`` (both as the lines are written,
      including a ``[MM:SS] `` prefix). Text before ``start_char`` and after
      ``end_char`` on those lines is clean.
    """
    timed = [sg for sg in (segments or []) if sg.text.strip()]
    detail = []
    for sp in report.spans:
        start_char, end_char = sp["start_char"], sp["end_char"]
        if timed:
            first = timed[sp["first_line"] - asr.LIT_BODY_LINE_OFFSET - 1]
            last = timed[sp["last_line"] - asr.LIT_BODY_LINE_OFFSET - 1]
            start_char += len(f"[{asr.format_marker(first.start)}] ")
            end_char += len(f"[{asr.format_marker(last.start)}] ")
        detail.append(
            {
                "start_line": sp["first_line"],
                "start_char": start_char,
                "end_line": sp["last_line"],
                "end_char": end_char,
                "start": asr.format_marker(sp["start"]) if sp.get("start") is not None else None,
                "end": asr.format_marker(sp["end"]) if sp.get("end") is not None else None,
                "words": sp["words"],
                "kind": sp["kind"],
            }
        )
    return {
        "asr_span_unit": "timestamp" if timed else "lit-body-line",
        "asr_damaged_spans": report.span_markers()
        if timed
        else [f"lines {sp['first_line']}-{sp['last_line']}" for sp in report.spans],
        "asr_damaged_spans_detail": detail,
    }


def _segments_from_body(lines: list[str]) -> list:
    """Timed segments from body lines that start with ``[MM:SS]`` or ``[H:MM:SS]``."""
    out = []
    starts = []
    for line in lines:
        m = re.match(r"^\[(?:(\d+):)?(\d{1,2}):(\d{2})\] (.*)$", line)
        if not m:
            return []
        starts.append(int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)))
        out.append(m.group(4))
    ends = starts[1:] + [starts[-1] + 5 if starts else 0]
    return [asr.Seg(float(a), float(b), t) for a, b, t in zip(starts, ends, out)]


def build_lit(rec: VideoRecord, aliases: dict[str, str]) -> tuple[dict, str]:
    """Return ``(frontmatter, body)`` for one video. No file is written."""
    vid = rec.video["video_id"]
    manifest_title = rec.video.get("title")
    title = rec.meta.get("title") or manifest_title or vid
    title_source = (
        "meta.json" if rec.meta.get("title") else (rec.video.get("title_source") or "manifest")
    )
    title_mismatch = bool(
        rec.meta.get("title") and manifest_title and manifest_title != rec.meta["title"]
    )

    trusted, trust_reason = title_trust(rec)
    # If the manifest title conflicts with the id-keyed run log, the log title is the
    # better source for guest names ("w. @saša venos" in the real title).
    log_titles = rec.title_evidence.get(vid, [])
    speaker_title = log_titles[0] if (not trusted and log_titles) else title

    owner = rec.speakers_file.get("channel_owner") or channel_owner(rec.manifest)
    file_override = (rec.speakers_file.get("videos") or {}).get(vid)
    if file_override:
        override, override_source = file_override, "speakers-file"
    else:
        override, override_source = rec.video.get("speakers"), "manifest"
    spk = asr.detect_speakers(
        channel_owner=owner,
        title=speaker_title,
        override=override,
        override_source=override_source,
        aliases=aliases,
        channel_kind=rec.speakers_file.get("channel_kind") or rec.manifest.get("channel_kind"),
    )

    text = rec.transcript.read_text(encoding="utf-8", errors="replace") if rec.transcript else ""
    segments = asr.load_segments(rec.asr_source_dir)
    duration = _duration(rec)
    # Assess exactly the text that goes into the body, so span line numbers are lit
    # body line numbers (see asr_tools.LIT_BODY_LINE_OFFSET).
    report = asr.assess_transcript(
        None if segments else text.strip(),
        segments=segments,
        duration=duration,
        line_offset=asr.LIT_BODY_LINE_OFFSET,
    )
    sidecar = asr.read_sidecar(rec.asr_source_dir)
    issues = list(report.issues)
    quality = report.quality
    # Issues that only the transcription step knows (for example mixed models).
    for extra in (sidecar or {}).get("issues", []) if sidecar else []:
        if extra not in issues and extra.startswith("chunks used different models"):
            issues.insert(0, extra)
            quality = asr.QUALITY_DEGRADED
    model, claimed = _asr_model(rec, sidecar)

    fm = {
        "id": f"vid-{vid}",
        "kind": "video",
        "video_id": vid,
        "source_url": rec.meta.get("url")
        or rec.video.get("url")
        or f"https://www.youtube.com/watch?v={vid}",
        "title": title,
        "title_source": title_source,
        "channel": rec.channel_title,
        "author": rec.channel_title,
        "title_trusted": trusted,
        "speakers": spk.speakers,
        "speakers_source": spk.source,
        "speakers_found_by": spk.found_by,
        "multi_speaker": {"true": True, "false": False}.get(spk.multi_speaker, "unknown"),
        "published_at": _published_iso(rec.meta.get("upload_date") or rec.video.get("upload_date")),
        "duration_seconds": round(duration, 1) if duration else None,
        "asr_model": model,
        "asr_quality": quality,
        "asr_issues": issues,
        **span_fields(report, segments),
        "timestamps": bool(segments),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    if not trusted:
        fm["title_trust_reason"] = trust_reason
        if log_titles:
            fm["title_run_log"] = log_titles[0]
    if claimed:
        fm["asr_model_claimed"] = claimed
    if title_mismatch:
        fm["title_manifest"] = manifest_title
    if sidecar and sidecar.get("prompt"):
        fm["asr_prompt"] = sidecar["prompt"]
    # Folder of the transcript files this lit file comes from (every lit file has it).
    fm["asr_source"] = str(rec.asr_source_dir)

    body = asr.render_timestamped(segments) if segments else text.strip()
    return fm, body


def _render_lit(fm: dict, body: str) -> str:
    fm_yaml = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items())
    return f"---\n{fm_yaml}\n---\n\n# {fm['title']}\n\n{body}\n"


def write_lit_note(staging: Path, channel_title: str, video: dict, transcript: str) -> Path:
    """Write a lit note from plain text only (no video folder). Kept for older callers.

    The quality check still runs on the text. Speakers default to the channel owner.
    """
    vid = video["video_id"]
    rec = VideoRecord(
        job_dir=staging,
        manifest={"channel_title": channel_title},
        video=video,
        video_dir=staging / ".no-video-dir" / vid,
        transcript=None,
    )
    fm, _ = build_lit(rec, {})
    report = asr.assess_transcript(transcript, duration=video.get("duration"))
    fm["asr_quality"], fm["asr_issues"] = report.quality, report.issues
    dest = staging / "lit" / f"vid-{vid}.md"
    _atomic_write(dest, _render_lit(fm, transcript.strip()))
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
        "stage": STAGE_EXTRACTED,  # transcript already on disk — no network fetch step
        "lit_note": _lit_ref(lit_path),
        "cluster": None,
        "notes_emitted": [],
        "attempts": 0,
        "error": None,
    }


def _hold_reasons(fm: dict, *, allow_degraded: bool, allow_untrusted_titles: bool) -> list[str]:
    """Reasons to keep an item out of synthesis (stage ``held``)."""
    reasons = []
    if fm["asr_quality"] == asr.QUALITY_DEGRADED and not allow_degraded:
        major = [
            i for i in fm["asr_issues"] if not i.startswith(("minor:", "not checked", "loop:"))
        ]
        reasons.append("asr_quality degraded: " + "; ".join(major)[:400])
    if not fm.get("title_trusted", True) and not allow_untrusted_titles:
        reasons.append("title not verified: " + str(fm.get("title_trust_reason", "")))
    return reasons


def _is_hold_error(error: object) -> bool:
    return str(error or "").startswith(("asr_quality degraded", "title not verified"))


def _apply_quality(
    item: dict, fm: dict, *, allow_degraded: bool, allow_untrusted_titles: bool = False
) -> str:
    """Set stage and quality fields on a queue item that is still ``extracted``/``held``.

    Returns the new stage.
    """
    _record_quality(item, fm)
    reasons = _hold_reasons(
        fm, allow_degraded=allow_degraded, allow_untrusted_titles=allow_untrusted_titles
    )
    if reasons:
        item["stage"] = STAGE_HELD
        item["hold_reasons"] = reasons
        item["error"] = "; ".join(reasons)[:600]
    else:
        item["stage"] = STAGE_EXTRACTED
        item.pop("hold_reasons", None)
        if _is_hold_error(item.get("error")):
            item["error"] = None
        if fm["asr_quality"] == asr.QUALITY_DEGRADED:
            item["allowed_degraded"] = True
        else:
            item.pop("allowed_degraded", None)
    return item["stage"]


def _record_quality(item: dict, fm: dict) -> None:
    """Informational fields; never changes the stage."""
    item["asr_quality"] = fm["asr_quality"]
    item["asr_issues"] = fm["asr_issues"]
    item["asr_damaged_spans"] = fm.get("asr_damaged_spans", [])
    item["asr_model"] = fm["asr_model"]
    item["title_trusted"] = fm.get("title_trusted", True)


def _seed_state(staging: Path) -> None:
    """Seed the Phase-B state files so the synthesis loop can run standalone.

    ``provenance.json`` is not seeded: the harness owns provenance.
    """
    cidx = staging / "concept-index.json"
    if not cidx.exists():
        _atomic_write(cidx, json.dumps({"version": 1, "concepts": [], "mocs": []}, indent=2))
    for name in ("STATE.md", "DECISIONS.md"):
        dst = staging / name
        if not dst.exists():
            tmpl = HERE / "state" / f"{name.split('.')[0]}.template.md"
            _atomic_write(dst, tmpl.read_text() if tmpl.exists() else f"# {name}\n")


def _state_lock(staging: Path):
    """The staging lock that workers use for queue.json, if available."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        from _lock import state_lock

        return state_lock(staging)
    except Exception:  # noqa: BLE001 - the lock is optional for a standalone run
        import contextlib

        return contextlib.nullcontext()


# ── lit files: read, supersede, index ───────────────────────────────────────

SUPERSEDED_DIR = "_superseded"
INDEX_NAME = "transcripts.json"
_MARKER_LINE = re.compile(r"^\[\d+:\d{2}(?::\d{2})?\] ", re.M)


def read_lit(path: Path) -> tuple[dict, str]:
    """Return ``(frontmatter, body)`` of a lit file (values are JSON per line)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    fm: dict = {}
    for line in head.splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            continue
        try:
            fm[key] = json.loads(value)
        except json.JSONDecodeError:
            fm[key] = value
    return fm, body.lstrip("\n")


def _supersede(staging: Path, lit_path: Path, new_lit: str) -> str:
    """Move the old lit file to ``lit/_superseded/vid-<id>.<asr_model>.md``.

    The moved file gets ``canonical: false`` and ``superseded_by``. Returns its
    path relative to the staging dir.
    """
    fm, body = read_lit(lit_path)
    model = re.sub(r"[^\w.-]", "_", str(fm.get("asr_model") or "unknown"))
    dest_dir = staging / "lit" / SUPERSEDED_DIR
    dest = dest_dir / f"{lit_path.stem}.{model}.md"
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = dest_dir / f"{lit_path.stem}.{model}.{stamp}.md"
    fm["canonical"] = False
    fm["superseded_by"] = new_lit
    fm["superseded_at"] = datetime.now(timezone.utc).isoformat()
    title = fm.get("title") or lit_path.stem
    fm_yaml = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items())
    if not body.lstrip().startswith("#"):
        body = f"# {title}\n\n{body}"
    _atomic_write(dest, f"---\n{fm_yaml}\n---\n\n{body.rstrip()}\n")
    lit_path.unlink()
    return str(dest.relative_to(staging))


PERMANENT_DIR = "01 Permanent Notes"

_EPISODE = re.compile(r"\bEp\.?\s*\d+|\bEpisode\s*\d+|\bPodcast\b|\bInterview\b", re.I)


def _q(value: str) -> str:
    """A double-quoted YAML scalar for the strict parser (no escapes needed)."""
    return '"' + value.replace('"', "'") + '"'


def speakers_template(job_dir: Path, aliases: dict[str, str]) -> str:
    """A ready-to-edit ``speakers.yaml`` for one channel job.

    It lists every video id with its title and the speakers that ingest detects
    now, so the owner only corrects names. ``channel_kind: podcast`` is proposed
    when at least a third of the titles look like episodes or interviews.
    """
    data = _read_json(job_dir / "manifest.json") or {}
    sf = asr.load_speakers_file(job_dir)
    owner = sf.get("channel_owner") or channel_owner(data)
    videos = [v for v in data.get("videos", []) if isinstance(v, dict) and v.get("video_id")]
    titles = [str(v.get("title") or "") for v in videos]
    episodic = sum(1 for t in titles if _EPISODE.search(t))
    kind = sf.get("channel_kind") or (
        "podcast" if videos and episodic * 3 >= len(videos) else "solo"
    )
    evidence = title_evidence(job_dir)
    lines = [
        f"# speakers.yaml for channel job {job_dir.name!r}.",
        "# Generated by: ingest_transcripts.py --speakers-template. Check every entry.",
        "# Per video: speakers in order, main speaker first. An active line is taken as",
        "# fact (speakers_source: manual). Lines that start with '# <id>:' are not active.",
        "# Remove a video line to use the automatic detection from the title.",
        "# Format: README, section Speakers.",
        f"channel_owner: {_q(owner)}",
        f"channel_kind: {kind}  # podcast | interview | solo ({episodic} of {len(videos)} "
        "titles look like episodes or interviews)",
    ]
    if videos:
        lines.append("videos:")
    for v in videos:
        vid = v["video_id"]
        meta = _read_json(job_dir / "videos" / vid / "meta.json") or {}
        title = meta.get("title") or v.get("title") or ""
        rec = VideoRecord(job_dir, data, v, job_dir / "videos" / vid, None, meta=meta)
        rec.speakers_file, rec.title_evidence = sf, evidence
        trusted, reason = title_trust(rec)
        speaker_title = (evidence.get(vid) or [title])[0] if not trusted else title
        override = (sf.get("videos") or {}).get(vid) or v.get("speakers")
        spk = asr.detect_speakers(
            channel_owner=owner,
            title=speaker_title,
            override=override,
            aliases=aliases,
            channel_kind=kind,
        )
        lines.append(f"  # {title}")
        if not trusted:
            lines.append(f"  #   title not verified: {reason}")
        lines.append(
            f"  #   detected: {', '.join(spk.found_by)}; multi_speaker: {spk.multi_speaker}"
        )
        entry = f"{vid}: [{', '.join(_q(n) for n in spk.speakers)}]"
        if spk.multi_speaker == "unknown":
            # Not active until the owner edits it: the file states facts.
            lines.append(f"  # {entry}  # UNKNOWN: add the guest, or remove '# ' to confirm")
        else:
            lines.append(f"  {entry}")
    return "\n".join(lines) + "\n"


def write_speakers_templates(channels_dirs: list[Path], out_dir: Path) -> list[Path]:
    """Write ``<out_dir>/<job>/speakers.yaml`` for every channel job. Returns the paths."""
    aliases: dict[str, str] = {}
    for d in channels_dirs:
        for k, v in speaker_aliases(d).items():
            aliases.setdefault(k, v)
    written = []
    for d in channels_dirs:
        for job_dir, _data in _job_manifests(d) or []:
            text = speakers_template(job_dir, aliases)
            asr.parse_simple_yaml(text, f"template for {job_dir.name}")  # must parse
            dest = out_dir / job_dir.name / "speakers.yaml"
            _atomic_write(dest, text)
            written.append(dest)
    return written


def note_status(item: dict, vault: Path) -> list[dict]:
    """Which notes of a queue item exist in the vault folder ``vault``.

    Names come from ``notes_emitted`` (titles) and ``published_notes`` (paths
    relative to the vault folder). A title ``T`` maps to
    ``01 Permanent Notes/T.md``. Returns ``[{"note", "exists"}]``.
    """
    names: list[str] = []
    for rel in item.get("published_notes") or []:
        if rel not in names:
            names.append(str(rel))
    for title in item.get("notes_emitted") or []:
        rel = f"{PERMANENT_DIR}/{title}.md"
        if rel not in names:
            names.append(rel)
    return [{"note": rel, "exists": (vault / rel).is_file()} for rel in names]


def _record_note_status(item: dict, vault: Path | None) -> None:
    if vault is None:
        return
    status = note_status(item, vault)
    item["notes_on_disk"] = status
    item["notes_checked_at"] = datetime.now(timezone.utc).isoformat()
    item["notes_checked_vault"] = str(vault)


def write_index(staging: Path) -> dict:
    """Write ``<staging>/transcripts.json``: the canonical lit file per video id.

    Quotes and repairs must use ``items[<id>].lit_note`` (relative to the
    staging dir). Older transcripts of the same video are listed in
    ``superseded``; never quote from them.
    """
    lit_dir = staging / "lit"
    items: dict[str, dict] = {}
    for path in sorted(lit_dir.glob("vid-*.md")):
        fm, _ = read_lit(path)
        items[path.stem] = {
            "lit_note": str(path.relative_to(staging)),
            "asr_model": fm.get("asr_model"),
            "asr_quality": fm.get("asr_quality"),
            "asr_source": fm.get("asr_source"),
            "timestamps": fm.get("timestamps"),
            "title_trusted": fm.get("title_trusted"),
            "superseded": [],
        }
    sup = lit_dir / SUPERSEDED_DIR
    if sup.is_dir():
        for path in sorted(sup.glob("vid-*.md")):
            fm, _ = read_lit(path)
            vid = fm.get("id") or path.name.split(".")[0]
            items.setdefault(vid, {"lit_note": None, "superseded": []})["superseded"].append(
                {"path": str(path.relative_to(staging)), "asr_model": fm.get("asr_model")}
            )
    index = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule": "quote only from items[<id>].lit_note; superseded files are history",
        "items": items,
    }
    _atomic_write(staging / INDEX_NAME, json.dumps(index, indent=2, ensure_ascii=False))
    return index


def _upgrade_in_place(lit_path: Path, fm_new: dict) -> bool:
    """Replace the frontmatter of an existing lit file; keep its body as is.

    ``timestamps`` follows the kept body. Returns True if the file changed.
    """
    fm_old, body = read_lit(lit_path)
    fm = dict(fm_new)
    for key in ("extracted_at", "asr_source"):
        if key in fm_old:
            fm[key] = fm_old[key]
    # Quality and spans must describe the kept body, not the new source files.
    lines = body.split("\n")
    head = 2 if lines and lines[0].startswith("# ") else 0  # "# title", blank line
    transcript = [ln for ln in lines[head:]]
    while transcript and not transcript[-1].strip():
        transcript.pop()
    segs = _segments_from_body([ln for ln in transcript if ln.strip()])
    duration = fm.get("duration_seconds")
    report = asr.assess_transcript(
        None if segs else "\n".join(transcript),
        segments=segs or None,
        duration=duration,
        line_offset=asr.LIT_BODY_LINE_OFFSET if head else 1,
    )
    fm["timestamps"] = bool(segs)
    fm["asr_quality"] = report.quality
    fm["asr_issues"] = report.issues
    fm.update(span_fields(report, segs))
    fm["frontmatter_upgraded_at"] = datetime.now(timezone.utc).isoformat()
    if all(fm_old.get(k) == v for k, v in fm.items() if k != "frontmatter_upgraded_at"):
        return False
    fm_yaml = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items())
    _atomic_write(lit_path, f"---\n{fm_yaml}\n---\n\n{body.rstrip()}\n")
    return True


def ingest(
    channels_dir: Path | list[Path],
    staging: Path,
    *,
    limit: int = 0,
    include_tests: bool = False,
    allow_degraded: bool = False,
    allow_untrusted_titles: bool = False,
    asr_dir: Path | None = None,
    replace_transcripts: bool = False,
    upgrade_frontmatter: bool = False,
    vault: Path | None = None,
) -> dict:
    """Stage transcripts and update ``queue.json``. Returns the run report.

    ``channels_dir`` is one channel-jobs folder or a list of them.

    ``replace_transcripts`` (needs ``asr_dir``): for every video with a newer
    transcript in ``asr_dir``, write it as the canonical lit file at any queue
    stage. The old lit file moves to ``lit/_superseded/``. The queue item keeps
    its stage and gets ``transcript_replaced_at`` and ``transcript_replaced``
    (old and new model). An item that is ``held`` or ``skipped`` only because
    of degraded ASR returns to ``extracted`` if the new transcript is usable.

    ``upgrade_frontmatter``: for items past ``extracted``/``held``, rewrite the
    frontmatter of the existing lit file in place (body kept, stage kept).

    ``vault`` (the zettelkasten folder): every item with a replaced transcript gets
    ``notes_on_disk`` (``[{"note", "exists"}]``) for its ``notes_emitted`` and
    ``published_notes``, so the repair work list skips notes that no longer exist.
    """
    if replace_transcripts and asr_dir is None:
        raise ValueError("--replace-transcripts needs --asr-dir")
    staging.mkdir(parents=True, exist_ok=True)
    queue_path = staging / "queue.json"
    dirs = [channels_dir] if isinstance(channels_dir, Path) else list(channels_dir)
    records = [
        r for d in dirs for r in scan_channels(d, include_tests=include_tests, asr_dir=asr_dir)
    ]
    aliases: dict[str, str] = {}
    for d in dirs:
        for k, v in speaker_aliases(d).items():
            aliases.setdefault(k, v)
    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channels_dir": [str(d) for d in dirs],
        "asr_dir": str(asr_dir) if asr_dir else None,
        "allow_degraded": allow_degraded,
        "allow_untrusted_titles": allow_untrusted_titles,
        "staged": [],
        "refreshed": [],
        "held": [],
        "skipped_incomplete": [],
        "degraded_already_processed": [],
        "untrusted_titles": [],
        "title_mismatch": [],
        "replaced": [],
        "released": [],
        "upgraded": [],
        "replace_available": [],
    }

    with _state_lock(staging):
        # Merge into any existing queue (idempotent / resumable): keep done work.
        if queue_path.exists():
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
        else:
            queue = {
                "version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "items": [],
            }
        by_id = {it["id"]: it for it in queue["items"]}

        added = 0
        for rec in records:
            vid = rec.video["video_id"]
            item_id = f"vid-{vid}"
            if rec.skip_reason is not None:
                report["skipped_incomplete"].append({"id": item_id, "reason": rec.skip_reason})
                print(f"SKIP {item_id}  {rec.skip_reason}", flush=True)
                continue
            existing = by_id.get(item_id)
            if existing is None and limit and added >= limit:
                continue
            fm, body = build_lit(rec, aliases)
            fm["canonical"] = True
            if "title_manifest" in fm:
                report["title_mismatch"].append(
                    {"id": item_id, "manifest": fm["title_manifest"], "meta.json": fm["title"]}
                )
                print(
                    f"WARN {item_id}  title from meta.json {fm['title']!r} replaces "
                    f"manifest title {fm['title_manifest']!r}",
                    flush=True,
                )
            if not fm["title_trusted"]:
                report["untrusted_titles"].append(
                    {"id": item_id, "title": fm["title"], "reason": fm["title_trust_reason"]}
                )
                print(
                    f"WARN {item_id}  title not verified ({fm['title_trust_reason']}): "
                    f"{fm['title']!r}. Repair: batch_channel.py --channel <handle> "
                    "--manifest-only --refresh-metadata",
                    flush=True,
                )

            lit_path = staging / "lit" / f"vid-{vid}.md"
            lit_rel = str(lit_path.relative_to(staging))
            processed = existing is not None and existing.get("stage") not in _REFRESHABLE_STAGES

            if processed and replace_transcripts and rec.asr_dir is not None:
                old_fm = read_lit(lit_path)[0] if lit_path.is_file() else {}
                if old_fm.get("asr_source") == fm.get("asr_source") and old_fm.get(
                    "asr_model"
                ) == fm.get("asr_model"):
                    _record_quality(existing, fm)
                    _record_note_status(existing, vault)
                    continue  # already replaced by an earlier run
                old_path = _supersede(staging, lit_path, lit_rel) if lit_path.is_file() else None
                fm["supersedes"] = [old_path] if old_path else []
                _atomic_write(lit_path, _render_lit(fm, body))
                existing["lit_note"] = _lit_ref(lit_path)
                existing["transcript_replaced_at"] = datetime.now(timezone.utc).isoformat()
                existing["transcript_replaced"] = {
                    "old_model": old_fm.get("asr_model", "unknown"),
                    "new_model": fm["asr_model"],
                    "old_lit": old_path,
                    "old_quality": old_fm.get("asr_quality"),
                    "new_quality": fm["asr_quality"],
                    "stage_at_replace": existing.get("stage"),
                }
                _record_quality(existing, fm)
                _record_note_status(existing, vault)
                report["replaced"].append(
                    {
                        "id": item_id,
                        "notes_on_disk": existing.get("notes_on_disk"),
                        "stage": existing.get("stage"),
                        "old_model": old_fm.get("asr_model", "unknown"),
                        "new_model": fm["asr_model"],
                        "asr_quality": fm["asr_quality"],
                    }
                )
                # Skipped only because of degraded ASR: back to synthesis if usable now.
                if existing.get("stage") == "skipped" and _is_hold_error(existing.get("error")):
                    if fm["asr_quality"] != asr.QUALITY_DEGRADED:
                        _apply_quality(
                            existing,
                            fm,
                            allow_degraded=allow_degraded,
                            allow_untrusted_titles=allow_untrusted_titles,
                        )
                        if existing["stage"] == STAGE_EXTRACTED:
                            report["released"].append(item_id)
                print(
                    f"REPL {item_id}  {old_fm.get('asr_model', 'unknown')} -> {fm['asr_model']}"
                    f"  stage={existing.get('stage')}  asr={fm['asr_quality']}",
                    flush=True,
                )
                continue

            if processed:
                # A worker claimed or finished it: never move it.
                _record_quality(existing, fm)
                if rec.asr_dir is not None:
                    report["replace_available"].append(item_id)
                if upgrade_frontmatter and lit_path.is_file():
                    if _upgrade_in_place(lit_path, fm):
                        report["upgraded"].append(item_id)
                if fm["asr_quality"] == asr.QUALITY_DEGRADED:
                    report["degraded_already_processed"].append(
                        {"id": item_id, "stage": existing.get("stage"), "issues": fm["asr_issues"]}
                    )
                    print(
                        f"WARN {item_id}  stage {existing.get('stage')!r} but transcript is "
                        f"degraded: {'; '.join(fm['asr_issues'])[:200]}",
                        flush=True,
                    )
                continue

            _atomic_write(lit_path, _render_lit(fm, body))
            if existing is None:
                item = _queue_item(staging, rec.video, lit_path)
                queue["items"].append(item)
                by_id[item_id] = item
                added += 1
                report["staged"].append(item_id)
            else:
                item = existing
                item["lit_note"] = _lit_ref(lit_path)
                report["refreshed"].append(item_id)
            stage = _apply_quality(
                item,
                fm,
                allow_degraded=allow_degraded,
                allow_untrusted_titles=allow_untrusted_titles,
            )
            label = "HELD" if stage == STAGE_HELD else ("OK  " if existing is None else "UPD ")
            print(
                f"{label} {item_id}  <- {', '.join(fm['speakers'])}: {fm['title'][:50]}"
                f"  [asr={fm['asr_quality']} model={fm['asr_model']}]",
                flush=True,
            )
            if stage == STAGE_HELD:
                report["held"].append({"id": item_id, "reasons": item["hold_reasons"]})
                for reason in item["hold_reasons"]:
                    print(f"       {reason[:200]}", flush=True)

        _atomic_write(queue_path, json.dumps(queue, indent=2, ensure_ascii=False))
    _seed_state(staging)
    write_index(staging)
    if report["replace_available"] and not replace_transcripts:
        print(
            f"NOTE {len(report['replace_available'])} processed item(s) have a newer transcript "
            "in --asr-dir; add --replace-transcripts to use it",
            flush=True,
        )
    _atomic_write(staging / "ingest_report.json", json.dumps(report, indent=2, ensure_ascii=False))
    report["queue_total"] = len(queue["items"])
    return report


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--channels-dir",
        type=Path,
        action="append",
        default=None,
        help="channel-jobs dir (repeat the flag for more than one)",
    )
    ap.add_argument(
        "--asr-dir",
        type=Path,
        default=None,
        help="folder with <video_id>/audio.{txt,json,vtt} from a newer whisper "
        "run; these transcripts replace the ones in the channel jobs",
    )
    ap.add_argument(
        "--replace-transcripts",
        action="store_true",
        help="with --asr-dir: also replace the lit file of items that are already "
        "synthesized/skipped/claimed (old file kept in lit/_superseded/, stage kept)",
    )
    ap.add_argument(
        "--upgrade-frontmatter",
        action="store_true",
        help="rewrite the frontmatter of lit files of processed items in place "
        "(body and stage kept)",
    )
    ap.add_argument(
        "--speakers-template",
        type=Path,
        default=None,
        metavar="OUT_DIR",
        help="only write <OUT_DIR>/<job>/speakers.yaml templates (every video with its "
        "detected speakers) for the owner to edit; no staging change",
    )
    ap.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="zettelkasten folder (VAULT/ZK_FOLDER); with it, replaced items record which "
        "of their notes exist on disk (notes_on_disk)",
    )
    ap.add_argument("--staging", type=Path, default=None, help="staging dir (else $ZR_STAGING)")
    ap.add_argument("--limit", type=int, default=0, help="stage at most N new videos (0 = all)")
    ap.add_argument("--include-tests", action="store_true", help="include jawed click-test clips")
    ap.add_argument(
        "--allow-degraded",
        action="store_true",
        default=os.environ.get("ALLOW_DEGRADED") == "1",
        help="queue transcripts that fail the quality check as 'extracted' "
        "(default: stage 'held'; env ALLOW_DEGRADED=1 does the same)",
    )
    ap.add_argument(
        "--allow-untrusted-titles",
        action="store_true",
        default=os.environ.get("ALLOW_UNTRUSTED_TITLES") == "1",
        help="queue videos whose title is not verified by id (default: stage 'held')",
    )
    args = ap.parse_args()
    if args.replace_transcripts and args.asr_dir is None:
        ap.error("--replace-transcripts needs --asr-dir")

    channels_dirs = args.channels_dir or [_default_channels_dir()]
    staging = args.staging or STAGING
    try:
        if args.speakers_template is not None:
            for path in write_speakers_templates(channels_dirs, args.speakers_template):
                print(f"WROTE {path}", flush=True)
            return
        report = _run_ingest(args, channels_dirs, staging)
    except asr.SpeakersFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    _print_done(report, staging)


def _run_ingest(args, channels_dirs, staging) -> dict:
    return ingest(
        channels_dirs,
        staging,
        limit=args.limit,
        include_tests=args.include_tests,
        allow_degraded=args.allow_degraded,
        allow_untrusted_titles=args.allow_untrusted_titles,
        asr_dir=args.asr_dir,
        replace_transcripts=args.replace_transcripts,
        upgrade_frontmatter=args.upgrade_frontmatter,
        vault=args.vault,
    )


def _print_done(report: dict, staging: Path) -> None:
    print(
        f"DONE staged={len(report['staged'])} refreshed={len(report['refreshed'])} "
        f"held={len(report['held'])} replaced={len(report['replaced'])} "
        f"released={len(report['released'])} upgraded={len(report['upgraded'])} "
        f"untrusted_titles={len(report['untrusted_titles'])} "
        f"skipped_incomplete={len(report['skipped_incomplete'])} "
        f"queue_total={report['queue_total']} staging={staging}",
        flush=True,
    )


if __name__ == "__main__":
    main()
