#!/usr/bin/env python3
"""
Batch-transcribe a YouTube channel and synthesize zettelkasten notes — in parallel.

Usage (inside nix develop):

    # Preview only:
    python zettel_ralph/batch_channel.py --channel @SasaVenos --preview

    # Full parallel run (uses all cores for transcription):
    python zettel_ralph/batch_channel.py --channel @SasaVenos

    # Run with specific parallelism:
    python zettel_ralph/batch_channel.py --channel @SasaVenos --workers 8

    # Pilot: process only 3 videos:
    python zettel_ralph/batch_channel.py --channel @SasaVenos --limit 3 --workers 3

    # Skip transcription (just ingest + synthesize existing transcripts):
    python zettel_ralph/batch_channel.py --channel @SasaVenos --synthesize-only

    # Transcribe only (no Phase A/B/C). The manifest is still written:
    python zettel_ralph/batch_channel.py --channel @SasaVenos --transcribe-only

    # Rebuild manifest.json from disk only (no network, no transcription):
    python zettel_ralph/batch_channel.py --channel @sthenics_ --manifest-only

    # Re-read title and upload date for every video id from YouTube (network),
    # then rebuild the manifest. Use it to repair wrong titles:
    python zettel_ralph/batch_channel.py --channel @sthenics_ --manifest-only --refresh-metadata

Resumable: re-running picks up where it stopped (skips videos with existing transcripts).

Transcription rules:
- The whisper model is an explicit setting (--whisper-model, else $WHISPER_MODEL,
  else $TWITTER_ARTICLENATOR_WHISPER_MODEL, else the large-v3 default). If the file
  does not exist, the script stops with an error. It never uses another model.
- whisper writes text AND JSON segments. The video folder gets transcript.txt,
  transcript.json (timed segments), transcript.vtt, whisper.log, and asr.json (model
  really used, prompt, quality check result).
- whisper gets an initial prompt with domain words (--vocabulary, default: the
  calisthenics glossary). The transcript text is never changed after whisper.
- Each video folder gets meta.json with the id, title, URL, and upload date from the
  same yt-dlp entry. The manifest takes titles from there, keyed by video id.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent


def _load_asr_tools():
    """Load the standard-library-only ``asr_tools`` module by file path.

    A normal package import would also import the browser and PDF modules of
    the app, which this script does not need.
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

# ── config (from app defaults / environment) ──────────────────────────────────

DEFAULT_CHANNELS_DIR = Path.home() / "Downloads" / "twitter-articles" / "channels"
WHISPER_BIN = os.environ.get("WHISPER_BIN", "whisper-cli")
# Model setting, in this order: --whisper-model, $WHISPER_MODEL,
# $TWITTER_ARTICLENATOR_WHISPER_MODEL (set by `nix develop`), large-v3 default.
# A missing file is an error (see check_whisper_model). There is no fallback.
WHISPER_MODEL = (
    os.environ.get("WHISPER_MODEL")
    or os.environ.get("TWITTER_ARTICLENATOR_WHISPER_MODEL")
    or "/nix/store/27cj9jgv586w5ix4kgf8yhb41jd4vpf4-ggml-large-v3.bin"
)
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en")
YTDLP_BIN = os.environ.get("YTDLP_BIN", "yt-dlp")
COOKIES_FILE = os.environ.get(
    "COOKIES_FILE", "/home/deploy/dev/articlenator/cookies.txt"
)
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "14400"))  # 4h per video
CPU_COUNT = multiprocessing.cpu_count()
POLL_SLEEP = 5

FAILED_FILE = "failed.txt"
META_FILE = "meta.json"
WHISPER_PREFIX = "whisper"  # whisper-cli writes whisper.txt and whisper.json


def check_whisper_model(model_path: str) -> Path:
    """Return the model path, or raise WhisperModelMissingError with a clear message."""
    return asr.require_model_file(model_path, setting="--whisper-model or $WHISPER_MODEL")


def resolve_prompt(
    *,
    prompt: str | None,
    vocabulary: str | None,
    vocabulary_file: Path | None,
    previous: str | None,
) -> str | None:
    """Return the whisper initial prompt for this channel job.

    Order: --prompt text, --vocabulary-file, --vocabulary, the prompt saved in
    the existing manifest, then the default calisthenics glossary. The value
    "none" for --vocabulary or --prompt turns the prompt off.
    """
    if prompt is not None:
        return None if prompt.strip().casefold() == "none" else prompt.strip() or None
    if vocabulary_file is not None:
        return asr.vocabulary_prompt(asr.parse_vocabulary(vocabulary_file.read_text(encoding="utf-8")))
    if vocabulary is not None:
        v = vocabulary.strip()
        if v.casefold() == "none":
            return None
        if v.casefold() == "calisthenics":
            return asr.vocabulary_prompt(asr.DEFAULT_CALISTHENICS_VOCABULARY)
        return asr.vocabulary_prompt(asr.parse_vocabulary(v))
    if previous is not None:
        return previous or None
    return asr.vocabulary_prompt(asr.DEFAULT_CALISTHENICS_VOCABULARY)


def _channel_handle(url: str) -> str:
    m = re.search(r"@([\w-]+)", url)
    return m.group(1) if m else url


def _elapsed(start: float) -> str:
    d = time.time() - start
    h, r = divmod(int(d), 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


# ── stage 1: enumerate channel videos ─────────────────────────────────────────

def _video_from_entry(entry: dict) -> dict | None:
    """Turn one yt-dlp JSON entry into our video dict (all fields keyed by its id)."""
    vid = entry.get("id", "")
    if not vid:
        return None
    return {
        "video_id": vid,
        "title": entry.get("title") or None,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "duration": float(entry["duration"]) if entry.get("duration") else 0.0,
        "upload_date": entry.get("upload_date"),
        "channel": entry.get("channel") or entry.get("playlist_channel")
        or entry.get("playlist_uploader") or entry.get("uploader"),
    }


def enumerate_channel(channel_url: str) -> list[dict]:
    """List the channel videos with yt-dlp.

    Raises RuntimeError if yt-dlp fails and gives no entries. An empty list
    must never look like "the channel has no videos".
    """
    print(f"  Enumerating {channel_url} ...", flush=True)
    cmd = [
        YTDLP_BIN,
        "--cookies", COOKIES_FILE,
        "--js-runtimes", "node",
        "--flat-playlist",
        "--dump-json",
        channel_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    videos = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        v = _video_from_entry(entry)
        if v:
            if not v["title"]:
                v["title"] = v["video_id"]
                v["title_missing"] = True
            videos.append(v)
    if not videos and result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp channel listing failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()[-400:]}"
        )
    return videos


def fetch_video_metadata(video_id: str) -> dict | None:
    """Read title and upload date for one video id from YouTube (network)."""
    cmd = [
        YTDLP_BIN, "--cookies", COOKIES_FILE, "--js-runtimes", "node",
        "--no-warnings", "--no-playlist", "--skip-download", "--dump-single-json",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        entry = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None
    v = _video_from_entry(entry)
    if v is None or v["video_id"] != video_id:
        return None
    return v


def write_video_meta(video_dir: Path, video: dict, *, source: str) -> None:
    """Write meta.json: the enumeration data of this one video id."""
    meta = {
        "video_id": video["video_id"],
        "title": video.get("title"),
        "url": video.get("url") or f"https://www.youtube.com/watch?v={video['video_id']}",
        "upload_date": video.get("upload_date"),
        "duration": video.get("duration") or None,
        "channel": video.get("channel"),
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    asr.write_json_atomic(Path(video_dir) / META_FILE, meta)


def read_video_meta(video_dir: Path) -> dict | None:
    path = Path(video_dir) / META_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


# ── stage 2: download + transcribe (one video — runs in a worker process) ─────

def _result(video: dict, status: str, error: str | None, **extra) -> dict:
    out = {"video_id": video["video_id"], "status": status, "error": error,
           "title": video.get("title")}
    out.update(extra)
    return out


def _cleanup_partial(video_dir: Path) -> None:
    """Remove whisper outputs of an unfinished run (the audio stays)."""
    for name in (f"{WHISPER_PREFIX}.txt", f"{WHISPER_PREFIX}.json",
                 "transcript.json", "transcript.vtt", asr.ASR_SIDECAR_NAME):
        try:
            (video_dir / name).unlink()
        except FileNotFoundError:
            pass


def _log_tail(path: Path, limit: int = 400) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[-limit:]
    except OSError:
        return ""


def _whisper_warnings(log_path: Path) -> list[str]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines()
            if "warning" in ln.casefold() or "not multilingual" in ln.casefold()][:20]


def finalize_transcript(video_dir: Path, *, model_path: str | None, prompt: str | None,
                        language: str | None, duration: float | None,
                        video: dict, warnings: list[str] | None = None) -> dict:
    """Turn whisper.txt + whisper.json into the final files of one video.

    Writes transcript.json (timed segments), transcript.vtt, asr.json, and last
    transcript.txt. The text file is the completion marker, so it is renamed
    into place only after the other files exist. Returns the asr.json data.
    """
    wtxt = video_dir / f"{WHISPER_PREFIX}.txt"
    wjson = video_dir / f"{WHISPER_PREFIX}.json"
    if not wjson.is_file() or not wtxt.is_file():
        raise RuntimeError(f"whisper produced no output files in {video_dir} "
                           f"(expected {wtxt.name} and {wjson.name})")
    data = json.loads(wjson.read_text(encoding="utf-8", errors="replace"))
    # The model path that whisper itself wrote into its JSON output wins over the
    # configured path: it is the file that really ran.
    model_path = asr.model_path_from_whisper_json(data) or model_path
    segments = asr.segments_from_whisper_json(data)
    text = wtxt.read_text(encoding="utf-8", errors="replace")
    report = asr.assess_transcript(text, segments=segments or None, duration=duration)
    asr.write_json_atomic(video_dir / "transcript.json", {
        "video_id": video["video_id"],
        "title": video.get("title"),
        "url": video.get("url"),
        "duration": duration,
        "model": asr.model_label(model_path),
        "model_file": Path(model_path).name if model_path else None,
        "prompt": prompt,
        "language": language,
        "asr_quality": report.quality,
        "asr_issues": report.issues,
        "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in segments],
    })
    (video_dir / "transcript.vtt").write_text(asr.render_vtt(segments), encoding="utf-8")
    sidecar = asr.build_sidecar(model_path=model_path, prompt=prompt, language=language,
                                report=report, timestamps=bool(segments), warnings=warnings)
    asr.write_json_atomic(video_dir / asr.ASR_SIDECAR_NAME, sidecar)
    os.replace(wtxt, video_dir / "transcript.txt")
    return sidecar


def legacy_sidecar(video_dir: Path, duration: float | None) -> dict:
    """asr.json data for a transcript from an older run: the model is unknown."""
    sidecar = asr.read_sidecar(video_dir)
    if sidecar is not None:
        return sidecar
    text = (video_dir / "transcript.txt").read_text(encoding="utf-8", errors="replace")
    segments = asr.load_segments(video_dir)
    report = asr.assess_transcript(text, segments=segments, duration=duration)
    return asr.build_sidecar(model_path=None, prompt=None, language=None, report=report,
                             timestamps=bool(segments),
                             extra={"note": "older run; the model was not recorded"})


def process_one_video(args: dict) -> dict:
    """Process a single video: download audio → transcribe.
    Runs in a worker subprocess (no shared state). Returns result dict.
    """
    video = args["video"]
    job_dir = Path(args["job_dir"])
    video_id = video["video_id"]
    video_dir = job_dir / "videos" / video_id
    video_dir.mkdir(parents=True, exist_ok=True)

    # Config from parent (pickled across the process boundary)
    cookies = args.get("cookies", COOKIES_FILE)
    whisper_bin = args.get("whisper_bin", WHISPER_BIN)
    whisper_model = args.get("whisper_model", WHISPER_MODEL)
    language = args.get("language", WHISPER_LANGUAGE)
    prompt = args.get("prompt")
    ytdlp = args.get("ytdlp", YTDLP_BIN)
    timeout = args.get("timeout", YTDLP_TIMEOUT)

    # Title and date of THIS id, from the same yt-dlp entry (see manifest step).
    if video.get("title") and not video.get("title_missing"):
        write_video_meta(video_dir, video, source="channel-listing")

    transcript_path = video_dir / "transcript.txt"
    if transcript_path.exists():
        sidecar = legacy_sidecar(video_dir, video.get("duration") or None)
        return _result(video, "already_done", None,
                       chars=transcript_path.stat().st_size,
                       asr_model=sidecar.get("model"), asr_quality=sidecar.get("quality"),
                       asr_issues=sidecar.get("issues", []))

    # ── download audio ──
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        audio_path = video_dir / "audio.wav"

        if not audio_path.exists():
            subprocess.run(
                [ytdlp, "--cookies", cookies,
                 "--js-runtimes", "node",
                 "-x", "--audio-format", "wav",
                 "--output", str(video_dir / "audio.%(ext)s"),
                 "--no-keep-video",
                 url],
                check=True, timeout=timeout,
                capture_output=True, text=True,
            )
            # yt-dlp may write audio.wav or audio_<name>.wav — find it
            if not audio_path.exists():
                found = list(video_dir.glob("audio*.wav"))
                if found:
                    audio_path = found[0]
                else:
                    raise FileNotFoundError(f"No audio.wav in {video_dir}")

        # ── transcribe ──
        # Fail here, not after a silent fallback, if the model file is missing.
        check_whisper_model(whisper_model)
        _cleanup_partial(video_dir)
        # -otxt and -oj: text plus timed segments. -of sets the output prefix.
        cmd = [whisper_bin, "-m", str(whisper_model), "-f", str(audio_path),
               "-otxt", "-oj", "-of", str(video_dir / WHISPER_PREFIX)]
        if language:
            cmd += ["-l", language]
        if prompt:
            cmd += ["--prompt", prompt]
        log_path = video_dir / "whisper.log"
        with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
            proc = subprocess.run(cmd, timeout=timeout * 2,
                                  stdout=subprocess.DEVNULL, stderr=log_file)
        if proc.returncode != 0:
            raise RuntimeError(
                f"whisper-cli exit code {proc.returncode}: {_log_tail(log_path)}")

        duration = asr.wav_duration(audio_path) or video.get("duration") or None
        sidecar = finalize_transcript(
            video_dir, model_path=str(whisper_model), prompt=prompt, language=language,
            duration=duration, video=video, warnings=_whisper_warnings(log_path))

        return _result(video, "ok", None, chars=transcript_path.stat().st_size,
                       asr_model=sidecar["model"], asr_quality=sidecar["quality"],
                       asr_issues=sidecar["issues"])

    except subprocess.TimeoutExpired:
        _cleanup_partial(video_dir)
        return _result(video, "timeout", "timeout")
    except subprocess.CalledProcessError as e:
        _cleanup_partial(video_dir)
        return _result(video, "error", e.stderr[-400:] if e.stderr else str(e))
    except Exception as e:
        _cleanup_partial(video_dir)
        return _result(video, "error", str(e)[:400])


# ── stage 3: write manifest ───────────────────────────────────────────────────

_RESULT_STATUS = {"ok": "complete", "already_done": "complete",
                  "timeout": "error", "error": "error"}


def _load_manifest(job_dir: Path) -> dict:
    path = job_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_manifest(job_dir: Path, channel_url: str, videos: list[dict],
                   results: list[dict], *, asr_prompt: str | None = None,
                   asr_model: str | None = None, channel_owner: str | None = None,
                   channel_title: str | None = None) -> dict:
    """Write manifest.json (the ingest_transcripts.py input), merged with the old one.

    Rules:
    - Titles, URLs, and dates come from meta.json in the video folder (written from
      the yt-dlp entry of that same id), else from this run's listing, else from the
      old manifest. They are never typed by hand or matched by position.
    - A video is "complete" only if transcript.txt exists. A failed or timed-out
      video is "error" with its message. A video with no attempt is "pending".
    - Operator fields survive a rewrite: per-video "speakers", and channel-level
      "channel_owner", "speaker_aliases", "asr_prompt".
    - Each video entry carries asr_model, asr_quality, and asr_issues from asr.json.
    - The manifest status is "complete" only if every video is complete, else "partial".
    """
    manifest_path = job_dir / "manifest.json"
    handle = _channel_handle(channel_url)
    old = _load_manifest(job_dir)
    old_by_id = {v.get("video_id"): v for v in old.get("videos", []) if isinstance(v, dict)}
    result_by_id = {r["video_id"]: r for r in results}

    # Order: this run's listing, then old entries, then folders on disk.
    order: list[str] = []
    listed = {}
    for v in videos:
        if v["video_id"] not in listed:
            listed[v["video_id"]] = v
            order.append(v["video_id"])
    for vid in old_by_id:
        if vid and vid not in listed:
            order.append(vid)
    vids_dir = job_dir / "videos"
    if vids_dir.is_dir():
        for d in sorted(p for p in vids_dir.iterdir() if p.is_dir()):
            if d.name not in order:
                order.append(d.name)

    if channel_title is None:
        channel_title = next((v.get("channel") for v in videos if v.get("channel")), None)
    channel_title = channel_title or old.get("channel_title") or handle.lstrip("@")

    video_entries = []
    for vid in order:
        prev = old_by_id.get(vid, {})
        lv = listed.get(vid, {})
        vdir = vids_dir / vid
        meta = read_video_meta(vdir) or {}
        title = meta.get("title") or (None if lv.get("title_missing") else lv.get("title"))
        title_source = "meta.json" if meta.get("title") else ("listing" if title else None)
        if not title and prev.get("title") and prev.get("title") != vid:
            title, title_source = prev["title"], "old-manifest"
        if prev.get("title") and title and prev["title"] != title:
            print(f"  WARN title of {vid} changes: {prev['title']!r} -> {title!r} "
                  f"(source: {title_source})", flush=True)

        r = result_by_id.get(vid)
        has_tx = (vdir / "transcript.txt").is_file()
        if r is not None:
            status = _RESULT_STATUS.get(r["status"], "error")
            error = r.get("error")
            if status == "complete" and not has_tx:
                status, error = "error", "transcript.txt missing after transcription"
        elif has_tx:
            status, error = "complete", None
        elif prev.get("status") == "error":
            status, error = "error", prev.get("error")
        else:
            status, error = "pending", None

        sidecar = asr.read_sidecar(vdir) if has_tx else None
        entry = {
            "video_id": vid,
            "title": title or vid,
            "title_source": title_source or "missing",
            "url": meta.get("url") or lv.get("url") or prev.get("url")
            or f"https://www.youtube.com/watch?v={vid}",
            "status": status,
            "error": error,
            "upload_date": meta.get("upload_date") or lv.get("upload_date")
            or prev.get("upload_date"),
            "duration": meta.get("duration") or lv.get("duration") or prev.get("duration"),
            "asr_model": (sidecar or {}).get("model") or (r or {}).get("asr_model")
            or ("unknown" if has_tx else None),
            "asr_quality": (sidecar or {}).get("quality") or (r or {}).get("asr_quality"),
            "asr_issues": (sidecar or {}).get("issues") or (r or {}).get("asr_issues") or [],
        }
        if prev.get("speakers"):
            entry["speakers"] = prev["speakers"]
        video_entries.append(entry)

    done = sum(1 for v in video_entries if v["status"] == "complete")
    failed = sum(1 for v in video_entries if v["status"] == "error")
    manifest = {
        "job_id": handle.lstrip("@"),
        "url": channel_url,
        "channel_title": channel_title,
        "channel_handle": handle,
        "channel_owner": channel_owner or old.get("channel_owner"),
        "speaker_aliases": old.get("speaker_aliases") or {},
        "asr_model": asr_model or old.get("asr_model"),
        "asr_prompt": asr_prompt if asr_prompt is not None else old.get("asr_prompt"),
        "status": "complete" if video_entries and done == len(video_entries) else "partial",
        "done_videos": done,
        "failed_videos": failed,
        "pending_videos": len(video_entries) - done - failed,
        "videos": video_entries,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }

    asr.write_json_atomic(manifest_path, manifest)
    return manifest


def refresh_metadata(job_dir: Path) -> int:
    """Fetch title and date by video id for every video folder (network). Returns the count."""
    count = 0
    vids_dir = job_dir / "videos"
    if not vids_dir.is_dir():
        return 0
    for d in sorted(p for p in vids_dir.iterdir() if p.is_dir()):
        v = fetch_video_metadata(d.name)
        if v is None:
            print(f"  [!] {d.name}  metadata fetch failed", flush=True)
            continue
        write_video_meta(d, v, source="video-page")
        count += 1
        print(f"  [✓] {d.name}  {v.get('title')}", flush=True)
    return count

# ── main ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", required=True,
                    help="YouTube channel URL or handle (e.g. @SasaVenos)")
    ap.add_argument("--channels-dir", type=Path, default=DEFAULT_CHANNELS_DIR,
                    help="parent directory for channel jobs")
    ap.add_argument("--limit", type=int, default=0,
                    help="max videos to process (0 = all)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel transcription workers (default: 4, optimal for whisper's 4 threads on 16 cores)")
    ap.add_argument("--preview", action="store_true",
                    help="just list videos, don't process")
    ap.add_argument("--synthesize-only", action="store_true",
                    help="skip download/transcribe, just ingest + synthesize")
    ap.add_argument("--transcribe-only", action="store_true",
                    help="stop after transcribing and writing manifest.json (no Phase A/B/C)")
    ap.add_argument("--manifest-only", action="store_true",
                    help="rebuild manifest.json from the files on disk; no listing, "
                         "no transcription, no Phase A/B/C")
    ap.add_argument("--refresh-metadata", action="store_true",
                    help="with --manifest-only: read title and upload date for each "
                         "video id from YouTube first (network)")
    ap.add_argument("--whisper-model", default=None,
                    help="path of the ggml model file (default: $WHISPER_MODEL, else "
                         "$TWITTER_ARTICLENATOR_WHISPER_MODEL, else large-v3). "
                         "A missing file stops the run.")
    ap.add_argument("--language", default=None,
                    help=f"spoken language for whisper (default: $WHISPER_LANGUAGE or "
                         f"{WHISPER_LANGUAGE!r})")
    ap.add_argument("--vocabulary", default=None,
                    help='comma-separated domain words for the whisper prompt; '
                         '"calisthenics" (default) = built-in list; "none" = no prompt')
    ap.add_argument("--vocabulary-file", type=Path, default=None,
                    help="file with one domain word or phrase per line")
    ap.add_argument("--prompt", default=None,
                    help='raw whisper initial prompt (overrides --vocabulary); "none" = no prompt')
    ap.add_argument("--channel-owner", default=None,
                    help="main speaker name for this channel (saved in manifest.json)")
    ap.add_argument("--allow-degraded", action="store_true",
                    help="pass --allow-degraded to ingest: queue transcripts that "
                         "failed the quality check")
    ap.add_argument("--kill-stale-whisper", action="store_true",
                    help="kill ALL whisper-cli processes on this host before the run "
                         "(old default; it also stops other jobs)")
    return ap


def main() -> None:
    args = build_parser().parse_args()

    channel_url = args.channel
    if not channel_url.startswith("http"):
        channel_url = f"https://www.youtube.com/{channel_url}/videos"

    handle = _channel_handle(channel_url)
    job_dir = args.channels_dir / handle.lstrip("@")
    job_dir.mkdir(parents=True, exist_ok=True)
    start_time = time.time()
    whisper_model = args.whisper_model or WHISPER_MODEL
    language = args.language or WHISPER_LANGUAGE
    old_manifest = _load_manifest(job_dir)
    prompt = resolve_prompt(prompt=args.prompt, vocabulary=args.vocabulary,
                            vocabulary_file=args.vocabulary_file,
                            previous=old_manifest.get("asr_prompt") if old_manifest else None)

    # ── Manifest only (no network unless --refresh-metadata) ──
    if args.manifest_only:
        if args.refresh_metadata:
            n = refresh_metadata(job_dir)
            print(f"  metadata refreshed for {n} video(s)")
        manifest = write_manifest(job_dir, channel_url, [], [],
                                  channel_owner=args.channel_owner)
        print(f"  Manifest: {manifest['done_videos']}/{len(manifest['videos'])} videos complete, "
              f"{manifest['failed_videos']} failed -> {job_dir / 'manifest.json'}")
        for v in manifest["videos"]:
            if v["title_source"] in ("missing", "old-manifest"):
                print(f"  WARN {v['video_id']}: title not verified by id "
                      f"(source: {v['title_source']}); run with --refresh-metadata", flush=True)
        return

    # Check the model before any network or CPU work. No fallback model.
    if not args.preview and not args.synthesize_only:
        try:
            check_whisper_model(whisper_model)
        except asr.WhisperModelMissingError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)

    # ── Step 1: Enumerate ──
    print(f"\n{'='*60}")
    print(f"CHANNEL: {channel_url}")
    print(f"JOB DIR: {job_dir}")
    if not args.preview and not args.synthesize_only:
        print(f"MODEL:   {asr.model_label(whisper_model)} ({whisper_model})")
        print(f"PROMPT:  {prompt or '(none)'}")
    print(f"{'='*60}")

    videos = enumerate_channel(channel_url)
    print(f"  Found {len(videos)} videos")

    if args.limit:
        videos = videos[:args.limit]
        print(f"  Limiting to {args.limit} videos")

    if args.preview:
        total_dur = 0
        print(f"\n{'─'*60}")
        for v in videos:
            mins = int(v["duration"] // 60)
            secs = int(v["duration"] % 60)
            total_dur += v["duration"]
            print(f"  {v['video_id']:12s}  {mins:02d}:{secs:02d}  {(v['title'] or '')[:70]}")
        print(f"{'─'*60}")
        total_min = total_dur / 60
        print(f"  Videos: {len(videos)}  Duration: {total_min:.0f} min ({total_min/60:.1f} hours)")
        return

    # Sort by duration (shortest first) to maximize worker throughput
    videos.sort(key=lambda v: v["duration"])

    # ── Step 2: Parallel Download + Transcribe ──
    if args.synthesize_only:
        # Just count existing transcripts
        results = []
        for v in videos:
            tx = job_dir / "videos" / v["video_id"] / "transcript.txt"
            if tx.exists():
                results.append({
                    "video_id": v["video_id"], "status": "already_done",
                    "error": None, "title": v["title"], "chars": tx.stat().st_size,
                })
            else:
                print(f"  [ ] {v['video_id']}  no transcript, skipping", flush=True)
        print(f"\n  Existing: {len(results)}/{len(videos)}")
    else:
        if args.kill_stale_whisper:
            subprocess.run(["pkill", "-9", "-f", "whisper-cli"],
                           capture_output=True, timeout=5)
            time.sleep(2)
        # Prepare worker args (everything must be picklable)
        worker_args = {
            "job_dir": str(job_dir),
            "cookies": COOKIES_FILE,
            "whisper_bin": WHISPER_BIN,
            "whisper_model": str(whisper_model),
            "language": language,
            "prompt": prompt,
            "ytdlp": YTDLP_BIN,
            "timeout": YTDLP_TIMEOUT,
        }
        # Videos already transcribed: record meta.json and asr.json, no new work.
        todo = []
        results = []
        for v in videos:
            tx = job_dir / "videos" / v["video_id"] / "transcript.txt"
            if tx.exists():
                results.append(process_one_video({**worker_args, "video": v}))
                print(f"  [✓] {v['video_id']}  already done", flush=True)
            else:
                todo.append(v)

        if not todo:
            print(f"\n  All {len(videos)} videos already transcribed.")
        else:
            workers = min(args.workers, len(todo))
            print(f"\n{'─'*60}")
            print("STAGE 2: Parallel Download + Transcribe")
            print(f"  Workers: {workers}  Videos to process: {len(todo)}/{len(videos)}")
            print(f"{'─'*60}")

            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        process_one_video, {**worker_args, "video": v}
                    ): v
                    for v in todo
                }

                done_count = 0
                for future in as_completed(futures):
                    v = futures[future]
                    try:
                        r = future.result()
                    except Exception as e:
                        r = {"video_id": v["video_id"], "status": "error",
                             "error": str(e)[:400], "title": v["title"]}
                    results.append(r)

                    done_count += 1
                    vid = r["video_id"]
                    status_symbol = "✓" if r["status"] == "ok" else "✓" if r["status"] == "already_done" else ("⚠" if r["status"] == "timeout" else "✗")
                    title = (v["title"] or "")[:50]
                    elapsed = _elapsed(start_time)
                    quality = f"  asr={r.get('asr_quality')}" if r.get("asr_quality") else ""
                    print(f"  [{done_count:03d}/{len(todo)}] {status_symbol} {vid}  {title}  ({elapsed}){quality}", flush=True)
                    for issue in r.get("asr_issues") or []:
                        print(f"       asr issue: {issue}", flush=True)
                    if r["error"]:
                        print(f"       error: {r['error']}", flush=True)
                        with open(job_dir / FAILED_FILE, "a") as f:
                            f.write(f"{vid}\t{v['title']}\t{r['error']}\t{datetime.now().isoformat()}\n")

        ok = sum(1 for r in results if r["status"] in ("ok", "already_done"))
        fail = sum(1 for r in results if r["status"] in ("error", "timeout"))
        degraded = sum(1 for r in results if r.get("asr_quality") == "degraded")
        elapsed = _elapsed(start_time)
        print(f"\n  Transcription complete ({elapsed})")
        print(f"    OK: {ok}  Failed: {fail}  Degraded (quality check): {degraded}")

    # ── Step 3: Build manifest (also with --transcribe-only) ──
    print(f"\n{'─'*60}")
    print("STAGE 3: Build manifest.json")
    print(f"{'─'*60}")
    manifest = write_manifest(
        job_dir, channel_url, videos, results,
        asr_prompt=None if args.synthesize_only else (prompt or ""),
        asr_model=None if args.synthesize_only else asr.model_label(whisper_model),
        channel_owner=args.channel_owner)
    n_complete = manifest["done_videos"]
    print(f"  {n_complete}/{len(manifest['videos'])} videos complete, "
          f"{manifest['failed_videos']} failed, {manifest['pending_videos']} pending")

    if args.transcribe_only:
        elapsed = _elapsed(start_time)
        print(f"\n{'='*60}")
        print("DONE (--transcribe-only)")
        print(f"  Duration: {elapsed}")
        print(f"  Job dir:  {job_dir}")
        print(f"{'='*60}")
        return

    # ── Step 4: Phase A — Ingest ──
    print(f"\n{'─'*60}")
    print("STAGE 4: Phase A — ingest_transcripts.py")
    print(f"{'─'*60}")
    staging_dir = HERE / "staging_transcripts"
    ingest_cmd = [
        sys.executable, str(HERE / "ingest_transcripts.py"),
        "--channels-dir", str(args.channels_dir),
        "--staging", str(staging_dir),
    ]
    if args.allow_degraded:
        ingest_cmd.append("--allow-degraded")
    result = subprocess.run(ingest_cmd, capture_output=True, text=True)
    for line in result.stdout.split("\n"):
        if line.strip():
            print(f"  {line}")
    if result.returncode != 0:
        print(f"  ingest error (exit {result.returncode}): {result.stderr[-400:]}",
              file=sys.stderr)
        print("  Stopped before synthesis: fix the ingest error first.", file=sys.stderr)
        sys.exit(result.returncode)

    # ── Step 5: Phase B — Synthesis (serial — API-limited) ──
    print(f"\n{'─'*60}")
    print("STAGE 5: Phase B — loop.sh (synthesis)")
    print(f"{'─'*60}")
    vault = os.environ.get("VAULT", "/srv/obsidian/vaults/Themis 2.0")
    zk_folder = os.environ.get("ZK_FOLDER", "Video Transcripts Zettelkasten")
    loop_env = os.environ.copy()
    loop_env.update({
        "VAULT": vault,
        "ZK_FOLDER": zk_folder,
        "ZR_STAGING": str(staging_dir),
        "AGENTS_FILE": str(HERE / "AGENTS_transcript.md"),
        "MAX_ITERS": "400",
    })
    result = subprocess.run(
        ["bash", str(HERE / "loop.sh")], env=loop_env,
        capture_output=True, text=True, timeout=43200)

    for line in result.stdout.split("\n"):
        if line.strip():
            print(f"  {line}")
    if result.returncode not in (0, 4):
        print(f"  loop.sh error (code {result.returncode}): {result.stderr[:200]}",
              file=sys.stderr)

    # ── Step 6: Phase C — Review ──
    print(f"\n{'─'*60}")
    print("STAGE 6: Phase C — review_loop.sh")
    print(f"{'─'*60}")
    result = subprocess.run(
        ["bash", str(HERE / "review_loop.sh")], env=loop_env,
        capture_output=True, text=True, timeout=43200)
    for line in result.stdout.split("\n"):
        if line.strip():
            print(f"  {line}")
    if result.returncode != 0:
        print(f"  review_loop.sh error: {result.stderr[:200]}", file=sys.stderr)

    # ── Done ──
    elapsed = _elapsed(start_time)
    completed = sum(1 for v in manifest["videos"] if v["status"] == "complete")
    total = len(manifest["videos"])
    print(f"\n{'='*60}")
    print("BATCH COMPLETE")
    print(f"  Channel:   {channel_url}")
    print(f"  Videos:    {completed}/{total} transcribed")
    print(f"  Vault:     {vault}/{zk_folder}")
    print(f"  Duration:  {elapsed}")
    print(f"  Job dir:   {job_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()