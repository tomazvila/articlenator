#!/usr/bin/env bash
# Parallel synthesis: N worker loops in one process group, each claims any ready unit.
# Usage: parallel_synth.sh <num_workers>
#
# Each unit ends with run_integrity.py finalize: per-note verification, quarantine of
# failed notes, queue stage update. The old whole-vault "0 errors" test is gone: it marked
# a unit `synthesized` even when the agent failed, and one bad note blocked all others.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT:-/srv/obsidian/vaults/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Video Transcripts Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
STAGING="${ZR_STAGING:-$HERE/staging}"
case "$STAGING" in
  /*) ;;
  *) STAGING="$(pwd)/$STAGING" ;;
esac

if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f /home/deploy/dotfiles/open-router-deepkseek-api-key.txt ]; then
  export DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
fi
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek/deepseek-v4-flash}"
export DEEPSEEK_MAX_TOKENS="${DEEPSEEK_MAX_TOKENS:-32000}"
export DEEPSEEK_TEMPERATURE="${DEEPSEEK_TEMPERATURE:-0.2}"
export VAULT ZK_FOLDER ZK_DIR STAGING HERE
export ZR_STAGING="$STAGING"
export AGENTS_FILE="${AGENTS_FILE:-$HERE/AGENTS_transcript.md}"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"

WORKERS="${1:-4}"
echo "=== Parallel synthesis: $WORKERS workers ==="
echo "Model: $DEEPSEEK_MODEL  Vault: $ZK_DIR  Staging: $STAGING"
zr_run_start synth-parallel
trap 'zr_run_summary' EXIT
# Notes that waited for a source-check review in an earlier run.
zr_pending_reviews

worker_loop() {
  local WID=$1
  local MAX_ITERS=30
  for ((i=1; i<=MAX_ITERS; i++)); do
    UNIT_JSON=$(python3 "$HERE/parallel_claim.py" --worker "$WID")
    if [ "$UNIT_JSON" = "{}" ] || [ -z "$UNIT_JSON" ]; then
      echo "[W$WID] no more units"
      return 0
    fi
    IDS=$(printf '%s' "$UNIT_JSON" | python3 -c "import json,sys; print(','.join(json.load(sys.stdin).get('ids',[])))")
    echo "[W$WID] claimed $IDS"
    PROMPT="Read $AGENTS_FILE and follow it exactly. Your pre-selected work unit is: $UNIT_JSON . Synthesize ONLY this unit, then stop. Write all notes under this folder: $ZK_DIR"
    zr_synth_unit "$IDS" "$PROMPT" "w$WID"
    case $? in
      "$ZR_UNIT_OK") echo "[W$WID] $IDS finalized" ;;
      "$ZR_UNIT_QUARANTINED") echo "[W$WID] $IDS finalized; some notes quarantined" ;;
      "$ZR_UNIT_INCOMPLETE") echo "[W$WID] $IDS incomplete; backoff"; sleep "${SYNTH_BACKOFF:-120}" ;;
      *) echo "[W$WID] $IDS finalize error - see $RUN_DIR" ;;
    esac
  done
  echo "[W$WID] max iters"
}

PIDS=()
for ((w=0; w<WORKERS; w++)); do
  worker_loop "$w" >>"$LOG_DIR/worker-$w.log" 2>&1 &
  PIDS+=($!)
  echo "  worker $w -> $LOG_DIR/worker-$w.log"
done
for pid in "${PIDS[@]}"; do
  wait "$pid"
done

echo "=== All workers finished ==="
python3 "$HERE/validate.py" --vault "$ZK_DIR" >>"$LOG_DIR/final-validate.log" 2>&1
tail -1 "$LOG_DIR/final-validate.log"
