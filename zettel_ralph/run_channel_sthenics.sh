#!/usr/bin/env bash
# Batch-process @sthenics_ YouTube channel → zettelkasten notes.
#
# Uses PARALLEL synthesis (loop_parallel.sh) for Phase B to maximize throughput.
# Runs alongside existing SasaVenos pipeline — conservative worker counts.
#
# Pipeline phases (each resumable):
#   Phase 0: Download + transcribe (parallel via ProcessPoolExecutor)
#   Phase A: Ingest transcripts → staging (fast, sequential)
#   Phase B: Parallel synthesis via loop_parallel.sh (N agents via DeepSeek API)
#   Phase C: Review pass via review_loop.sh
#
# Usage:
#   nix develop
#   bash zettel_ralph/run_channel_sthenics.sh                  # full run
#   bash zettel_ralph/run_channel_sthenics.sh --transcribe-only  # stop after transcribing
#   bash zettel_ralph/run_channel_sthenics.sh --synthesize-only  # skip download/transcribe
#   bash zettel_ralph/run_channel_sthenics.sh --preview       # list videos only
#   bash zettel_ralph/run_channel_sthenics.sh --limit 5       # process first 5
#   bash zettel_ralph/run_channel_sthenics.sh --allow-degraded # also queue transcripts
#                                                              # that fail the quality check
#
# Whisper settings (passed through to batch_channel.py):
#   WHISPER_MODEL=/path/ggml-large-v3.bin   model file; a missing file stops the run
#   --vocabulary "planche,front lever,..."  domain words for the whisper prompt
#                                           (default: built-in calisthenics list)
#
# Resumable: safe to Ctrl+C and re-run. Already-transcribed videos are skipped.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── OpenRouter / DeepSeek config ─────────────────────────────────────────────
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f /home/deploy/dotfiles/open-router-deepkseek-api-key.txt ]; then
  export DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
elif [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "ERROR: DEEPSEEK_API_KEY not set and key file not found."
  exit 1
fi
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek/deepseek-v4-flash}"

# ── Vault / Zettelkasten config ──────────────────────────────────────────────
export VAULT="${VAULT:-/srv/obsidian/vaults/Themis 2.0}"
export ZK_FOLDER="${ZK_FOLDER:-Video Transcripts Zettelkasten}"
export AGENTS_FILE="$HERE/AGENTS_transcript.md"

# ── Channel config ───────────────────────────────────────────────────────────
CHANNEL="@sthenics_"
CHANNELS_DIR="${CHANNELS_DIR:-/home/deploy/Downloads/twitter-articles/channels}"
# ZR_STAGING overrides the staging folder (tests use a temp folder, never the code tree).
STAGING_DIR="${ZR_STAGING:-$HERE/staging_transcripts}"
COOKIES_FILE="${COOKIES_FILE:-/home/deploy/dev/articlenator/cookies.txt}"

# ── Parallelism config (conservative — 17 whisper processes already running) ──
# 2 transcription workers + 2 parallel synthesis agents = 4 concurrent heavy processes
TRANSCODE_WORKERS="${TRANSCODE_WORKERS:-2}"
SYNTH_WORKERS="${SYNTH_WORKERS:-2}"

# ── Check vault exists ───────────────────────────────────────────────────────
if [ ! -d "$VAULT" ]; then
  echo "ERROR: Vault directory not found: $VAULT"
  exit 1
fi
mkdir -p "$CHANNELS_DIR" "$STAGING_DIR"

# ── Parse shared args ────────────────────────────────────────────────────────
BATCH_ARGS=("--channel" "$CHANNEL" "--channels-dir" "$CHANNELS_DIR")
PASSTHROUGH=()

for arg in "$@"; do
  case "$arg" in
    --allow-degraded)
      # Ingest reads ALLOW_DEGRADED=1: queue transcripts that fail the quality check.
      export ALLOW_DEGRADED=1
      ;;
    *)
      PASSTHROUGH+=("$arg")
      ;;
  esac
done

# ── Run summary helpers ───────────────────────────────────────────────────────
# Each loop writes $STAGING_DIR/runs/<run_id>/summary.json at exit (run_lib.sh).
# phase_summary prints the newest one. phase_check stops this driver when a loop
# fails: a failed phase never continues into the next phase.
phase_summary() {
  python3 - "$1" "$STAGING_DIR" <<'PYSUM'
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

# ── Phase 0: Preview ─────────────────────────────────────────────────────────
for arg in "$@"; do
  if [ "$arg" = "--preview" ]; then
    echo "=== Preview mode: listing videos from $CHANNEL ==="
    exec python3 "$HERE/batch_channel.py" "${BATCH_ARGS[@]}" --preview
  fi
done

# ── Print config ─────────────────────────────────────────────────────────────
echo "=" >&2
echo "= sthenics_ channel batch (parallel)" >&2
echo "= Vault:       $VAULT" >&2
echo "= ZK Folder:   $ZK_FOLDER" >&2
echo "= Model:       $DEEPSEEK_MODEL via $DEEPSEEK_BASE_URL" >&2
echo "= Transcribe:  $TRANSCODE_WORKERS workers" >&2
echo "= Synthesis:   $SYNTH_WORKERS parallel agents" >&2
echo "= Job dir:     $CHANNELS_DIR/sthenics_" >&2
echo "=" >&2
echo "" >&2

# ── Phase 0: Download + Transcribe (parallel) ────────────────────────────────
# Uses batch_channel.py's ProcessPoolExecutor with --workers.
# --transcribe-only stops batch_channel.py before Phase A/B/C.
python3 "$HERE/batch_channel.py" \
  "${BATCH_ARGS[@]}" \
  --workers "$TRANSCODE_WORKERS" \
  --transcribe-only \
  "${PASSTHROUGH[@]}" 2>&1

TRANSCODE_RC=$?
if [ $TRANSCODE_RC -ne 0 ]; then
  echo "ERROR: Transcription phase failed (exit $TRANSCODE_RC). Check logs above." >&2
  exit $TRANSCODE_RC
fi

# Check if --transcribe-only was passed (user wanted to stop there)
for arg in "$@"; do
  if [ "$arg" = "--transcribe-only" ]; then
    echo "== Stopped after transcription (--transcribe-only) =="
    exit 0
  fi
done

# ── Rebuild manifest from disk (no network) ─────────────────────────────────
# batch_channel.py already wrote manifest.json in Phase 0. This step rebuilds it
# from the files on disk only, so a failed Phase 0 still gives a correct list.
# Titles come from videos/<id>/meta.json (the yt-dlp entry of that same id).
# The old inline builder called yt-dlp a second time with --cookies /dev/null;
# that call returned no videos, the manifest got 0 entries, and an operator then
# typed four titles by hand onto the wrong ids. Never edit titles by hand: run
#   python3 zettel_ralph/batch_channel.py --channel @sthenics_ --manifest-only --refresh-metadata
echo "== Rebuilding manifest.json from disk =="
python3 "$HERE/batch_channel.py" "${BATCH_ARGS[@]}" --manifest-only 2>&1
MANIFEST_RC=$?
if [ $MANIFEST_RC -ne 0 ]; then
  echo "ERROR: manifest rebuild failed (exit $MANIFEST_RC)." >&2
  exit $MANIFEST_RC
fi

# ── Phase A: Ingest transcripts → staging ────────────────────────────────────
echo "== Phase A: Ingest transcripts -> $STAGING_DIR =="
python3 "$HERE/ingest_transcripts.py" \
  --channels-dir "$CHANNELS_DIR" \
  --staging "$STAGING_DIR" 2>&1

INGEST_RC=$?
if [ $INGEST_RC -ne 0 ]; then
  echo "ERROR: Ingest phase failed (exit $INGEST_RC). Stopping before synthesis." >&2
  exit $INGEST_RC
fi

# Check if --synthesize-only was passed
for arg in "$@"; do
  if [ "$arg" = "--synthesize-only" ]; then
    # Already ran ingest, check if there's anything to synthesize
    QUEUE_FILE="$STAGING_DIR/queue.json"
    if [ -f "$QUEUE_FILE" ]; then
      PENDING=$(python3 -c "
import json
q = json.load(open('$QUEUE_FILE'))
print(sum(1 for it in q['items'] if it['stage'] == 'extracted'))
" 2>/dev/null || echo "0")
      echo "== synthesize-only: $PENDING items ready for synthesis =="
    fi
  fi
done

# ── Phase B: Parallel synthesis ──────────────────────────────────────────────
echo "== Phase B: Parallel synthesis ($SYNTH_WORKERS agents) =="
export NWORKERS="$SYNTH_WORKERS"
export ZR_STAGING="$STAGING_DIR"
export MAX_ITERS="${MAX_ITERS:-400}"

# Check how many items are ready
QUEUE_FILE="$STAGING_DIR/queue.json"
if [ ! -f "$QUEUE_FILE" ]; then
  echo "No queue.json found at $QUEUE_FILE — nothing to synthesize."
else
  PENDING=$(python3 -c "
import json
q = json.load(open('$QUEUE_FILE'))
print(sum(1 for it in q['items'] if it['stage'] == 'extracted'))
" 2>/dev/null || echo "0")

  if [ "$PENDING" -eq 0 ]; then
    echo "  No extracted items to synthesize. Already done or nothing ingested."
  else
    echo "  $PENDING items ready for $SYNTH_WORKERS parallel synthesis agents"
    SYNTH_RC=0
    bash "$HERE/loop_parallel.sh" 2>&1 || SYNTH_RC=$?
    phase_check "Phase B (synthesis)" "$SYNTH_RC"
  fi
fi

# ── Phase C: Review ──────────────────────────────────────────────────────────
echo "== Phase C: Review (parallel_review.sh, $SYNTH_WORKERS workers) =="
REVIEW_RC=0
bash "$HERE/parallel_review.sh" "$SYNTH_WORKERS" 2>&1 || REVIEW_RC=$?
phase_check "Phase C (review)" "$REVIEW_RC"

echo "== Pipeline complete =="
echo "  Vault: $VAULT/$ZK_FOLDER"
echo "  Staging: $STAGING_DIR"
echo ""
echo "You can check progress with: python3 $HERE/progress.py"