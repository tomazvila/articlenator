#!/usr/bin/env bash
# @radoslav__radev YouTube channel -> transcripts -> DeepSeek Ralph synthesis.
#
# Orchestrates the full pipeline with PARALLEL synthesis and review:
#   Phase 0: Channel transcription (yt-dlp download + whisper.cpp transcription)
#   Phase A: Ingest completed transcripts as literature notes
#   Phase B: Parallel synthesis via OpenRouter deepseek/deepseek-v4-flash (N workers)
#   Phase C: Parallel review pass (N workers)
#
# Output goes to /srv/obsidian/vaults/Themis 2.0/Video Transcripts Zettelkasten
#
# Usage (inside nix develop):
#   bash zettel_ralph/run_radoslav_radev.sh              # full parallel run
#   bash zettel_ralph/run_radoslav_radev.sh --preview    # list videos only
#   bash zettel_ralph/run_radoslav_radev.sh --limit 5    # process first 5
#   bash zettel_ralph/run_radoslav_radev.sh --published-after 20250101
#   NWORKERS=4 bash zettel_ralph/run_radoslav_radev.sh   # override worker count
#   bash zettel_ralph/run_radoslav_radev.sh --allow-degraded  # also queue transcripts
#                                                             # that fail the quality check
#
# Whisper settings:
#   WHISPER_MODEL   model file. Default: $TWITTER_ARTICLENATOR_WHISPER_MODEL (large-v3
#                   from `nix develop`). If the file does not exist, the script stops.
#                   It never falls back to another model. To use small.en on purpose,
#                   set WHISPER_MODEL to that file; the manifest then records small.en.
#   WHISPER_PROMPT  initial prompt. Default "calisthenics" = built-in glossary
#                   (planche, front lever, maltese, ...). "" = no prompt.
#   WHISPER_LANGUAGE  spoken language ("en"). Default "en".
#
# History: the first run used ggml-small.en.bin as a silent default ("large-v3 is
# broken on this system"), while each manifest said "large-v3". That label was a
# constant in the code; it is now taken from the model file.
# Resumable: re-running continues from where it stopped.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Whisper model (explicit; no fallback) ─────────────────────────────────────
WHISPER_MODEL="${WHISPER_MODEL:-${TWITTER_ARTICLENATOR_WHISPER_MODEL:-}}"
if [ -z "$WHISPER_MODEL" ]; then
  echo "ERROR: no whisper model set. Set WHISPER_MODEL to a ggml model file" >&2
  echo "       (or run inside 'nix develop', which sets TWITTER_ARTICLENATOR_WHISPER_MODEL)." >&2
  exit 2
fi
export TWITTER_ARTICLENATOR_WHISPER_MODEL="$WHISPER_MODEL"
export TWITTER_ARTICLENATOR_WHISPER_PROMPT="${WHISPER_PROMPT-calisthenics}"
export TWITTER_ARTICLENATOR_WHISPER_LANGUAGE="${WHISPER_LANGUAGE:-en}"

# ── OpenRouter / DeepSeek config ──────────────────────────────────────────────
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f /home/deploy/dotfiles/open-router-deepkseek-api-key.txt ]; then
  export DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
fi
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek/deepseek-v4-flash}"

# ── Channel-specific config ───────────────────────────────────────────────────
export COOKIES_FILE="${COOKIES_FILE:-/home/deploy/dev/articlenator/cookies.txt}"
export TWITTER_ARTICLENATOR_YOUTUBE_COOKIE_PATH="${TWITTER_ARTICLENATOR_YOUTUBE_COOKIE_PATH:-$COOKIES_FILE}"
export CHANNEL_URL="${CHANNEL_URL:-https://www.youtube.com/@radoslav__radev/videos}"
export CHANNEL_JOBS_DIR="${CHANNEL_JOBS_DIR:-$HERE/channel_jobs_radoslav_radev}"
export JOB_DIR="${JOB_DIR:-$CHANNEL_JOBS_DIR/radoslav-radev}"

# ── Synthesis config ──────────────────────────────────────────────────────────
export VAULT="${VAULT:-/srv/obsidian/vaults/Themis 2.0}"
export ZK_FOLDER="${ZK_FOLDER:-Video Transcripts Zettelkasten}"
export ZR_STAGING="${ZR_STAGING:-$HERE/staging_radoslav_radev}"
export AGENTS_FILE="${AGENTS_FILE:-$HERE/AGENTS_transcript.md}"
export MAX_ITERS="${MAX_ITERS:-400}"
export NWORKERS="${NWORKERS:-6}"
export NREVIEW_WORKERS="${NREVIEW_WORKERS:-4}"

ZK_DIR="$VAULT/$ZK_FOLDER"
mkdir -p "$ZR_STAGING" "$CHANNEL_JOBS_DIR" "$ZK_DIR"

# ── Parse args ────────────────────────────────────────────────────────────────
PREVIEW=false
LIMIT=""
PUBLISHED_AFTER=""
INGEST_EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --preview) PREVIEW=true; shift ;;
    --allow-degraded) INGEST_EXTRA+=("--allow-degraded"); shift ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --published-after) PUBLISHED_AFTER="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Run summary helpers ───────────────────────────────────────────────────────
# Each loop writes $ZR_STAGING/runs/<run_id>/summary.json at exit (run_lib.sh).
# phase_summary prints the newest one. phase_check stops this driver when a loop
# fails: a failed phase never continues into the next phase.
phase_summary() {
  python3 - "$1" "$ZR_STAGING" <<'PYSUM'
import json, sys
from pathlib import Path
phase, staging = sys.argv[1], Path(sys.argv[2])
found = sorted((staging / "runs").glob("*/summary.json"), key=lambda p: p.stat().st_mtime)
if not found:
    print(f"[{phase}] no runs/<run_id>/summary.json found in {staging}")
    sys.exit(0)
s = json.loads(found[-1].read_text())
keys = ("run_id", "kind", "units_run", "videos_processed", "notes_created", "notes_merged",
        "notes_quarantined", "skipped_degraded")
print(f"[{phase}] summary {found[-1]}")
for k in keys:
    if k in s:
        print(f"  {k}: {s[k]}")
PYSUM
}

phase_check() {
  local phase="$1" rc="$2"
  phase_summary "$phase"
  if [ "$rc" -ne 0 ]; then
    echo "ERROR: $phase failed (exit $rc). Stopping here; see the summary and logs above." >&2
    exit "$rc"
  fi
}

# ── Phase 0: Channel transcription job ────────────────────────────────────────
echo ""
echo "======================================================================="
echo "RADOSLAV RADEV - CHANNEL TRANSCRIPTION"
echo "  channel: $CHANNEL_URL"
echo "  job_dir: $JOB_DIR"
echo "  published_after: ${PUBLISHED_AFTER:-all videos}"
echo "  limit: ${LIMIT:-all}"
echo "  model: $DEEPSEEK_MODEL (OpenRouter)"
echo "  synthesis workers: $NWORKERS"
echo "  review workers: $NREVIEW_WORKERS"
echo "  vault: $VAULT / $ZK_FOLDER"
echo "======================================================================="
echo ""

if [ "$PREVIEW" = true ]; then
  echo "== Preview mode: listing channel videos =="
  python3 - "$CHANNEL_URL" "$COOKIES_FILE" <<'PY'
import json, subprocess, sys

url = sys.argv[1]
cookies = sys.argv[2]
cmd = ["yt-dlp", "--cookies", cookies, "--flat-playlist", "--dump-json", url]
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
    vid = entry.get("id", "")
    if not vid:
        continue
    videos.append({
        "video_id": vid,
        "title": entry.get("title") or vid,
        "duration": float(entry["duration"]) if entry.get("duration") else 0.0,
        "upload_date": entry.get("upload_date"),
    })

print(f"\nFound {len(videos)} videos:\n")
for v in videos:
    mins = int(v["duration"] // 60)
    secs = int(v["duration"] % 60)
    title = (v["title"] or "")[:70]
    date = v.get("upload_date", "") or ""
    print(f"  {v['video_id']:12s}  {date:8s}  {mins:02d}:{secs:02d}  {title}")

total_min = sum(v["duration"] for v in videos) / 60
print(f"\nTotal: {len(videos)} videos ({total_min:.0f} min / {total_min/60:.1f} hours)")
PY
  exit 0
fi

echo "== Phase 0: channel transcription job (yt-dlp + whisper.cpp) =="
if [ ! -f "$WHISPER_MODEL" ]; then
  echo "ERROR: whisper model file not found: $WHISPER_MODEL" >&2
  echo "       Set WHISPER_MODEL to an existing ggml model file. There is no fallback model." >&2
  exit 2
fi
echo "  whisper model: $WHISPER_MODEL"
echo "  whisper prompt: ${TWITTER_ARTICLENATOR_WHISPER_PROMPT:-(none)}"
python3 - <<'PY'
import json
import os
from pathlib import Path

from twitter_articlenator.pdf.generator import PACKAGING_PER_ITEM
from twitter_articlenator.sources.channel_transcription_service import build_channel_job

channel_url = os.environ["CHANNEL_URL"]
job_dir = Path(os.environ["JOB_DIR"])
published_after = os.environ.get("PUBLISHED_AFTER") or None


def progress(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


job = build_channel_job(
    job_dir,
    packaging=PACKAGING_PER_ITEM,
    published_after=published_after,
)
manifest = job.run(channel_url, on_progress=progress)

degraded = [v.video_id for v in manifest.videos if v.asr_quality == "degraded"]
print(
    "CHANNEL_DONE",
    json.dumps(
        {
            "status": manifest.status,
            "done_videos": manifest.done_videos,
            "failed_videos": manifest.failed_videos,
            "degraded_videos": degraded,
            "asr_model": manifest.asr_model,
            "total_videos": len(manifest.videos),
            "job_dir": str(job_dir),
        },
        sort_keys=True,
    ),
    flush=True,
)
PY

# ── Phase A: ingest completed transcripts ─────────────────────────────────────
echo ""
echo "======================================================================="
echo "PHASE A: Ingest transcripts as literature notes"
echo "======================================================================="
python3 "$HERE/ingest_transcripts.py" \
  --channels-dir "$CHANNEL_JOBS_DIR" \
  --staging "$ZR_STAGING" \
  ${LIMIT:+--limit "$LIMIT"} \
  ${INGEST_EXTRA[@]+"${INGEST_EXTRA[@]}"}

# ── Phase B: PARALLEL synthesis Ralph loop ────────────────────────────────────
echo ""
echo "======================================================================="
echo "PHASE B: Parallel synthesis via OpenRouter ($DEEPSEEK_MODEL)"
echo "  workers: $NWORKERS"
echo "======================================================================="
export NWORKERS ZR_STAGING ZK_FOLDER AGENTS_FILE VAULT HERE
SYNTH_RC=0
bash "$HERE/loop_parallel.sh" || SYNTH_RC=$?
phase_check "Phase B (synthesis)" "$SYNTH_RC"

# ── Phase C: PARALLEL review ──────────────────────────────────────────────────
echo ""
echo "======================================================================="
echo "PHASE C: Parallel review pass"
echo "  workers: $NREVIEW_WORKERS"
echo "======================================================================="
export NREVIEW_WORKERS NWORKERS ZR_STAGING ZK_FOLDER AGENTS_FILE VAULT HERE
# parallel_review.sh (package C) runs the source-check, atomicity, and linking passes
# with run_integrity.py finalize. The old review_parallel.sh ran the removed
# source-free pass and had no finalize step.
REVIEW_RC=0
bash "$HERE/parallel_review.sh" "$NREVIEW_WORKERS" || REVIEW_RC=$?
phase_check "Phase C (review)" "$REVIEW_RC"

echo ""
echo "======================================================================="
echo "COMPLETE"
echo "  Channel:  $CHANNEL_URL"
echo "  Videos:   $(python3 -c "import json; m=json.load(open('$JOB_DIR/manifest.json')); print(f\"{m.get('done_videos',0)}/{len(m.get('videos',[]))} transcribed\")" 2>/dev/null || echo "?")"
echo "  Vault:    $VAULT / $ZK_FOLDER"
echo "  Job dir:  $JOB_DIR"
echo "  Staging:  $ZR_STAGING"
echo "  Model:    $DEEPSEEK_MODEL"
echo "  Workers:  $NWORKERS (synth) / $NREVIEW_WORKERS (review)"
echo "======================================================================="