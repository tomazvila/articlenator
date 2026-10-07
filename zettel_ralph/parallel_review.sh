#!/usr/bin/env bash
# Parallel review worker.
# Usage: parallel_review.sh <num_workers>
#
# Passes: source-check, atomicity, linking (the old source-free pass removed sources and
# no longer exists). Each note runs through run_integrity.py finalize; see review_loop.sh.
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
export VAULT ZK_FOLDER ZK_DIR STAGING HERE
export ZR_STAGING="$STAGING"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"

WORKERS="${1:-10}"
PASSES=("source-check" "atomicity" "linking")

zr_run_start review-parallel
trap 'zr_run_summary' EXIT
# Notes that wait in pending-review (an earlier review timed out or was invalid).
zr_pending_reviews

worker_loop() {
  local WID=$1
  local MAX_ITERS=100
  for ((i=1; i<=MAX_ITERS; i++)); do
    # Find current pass
    local CURR_PASS=""
    for pass in "${PASSES[@]}"; do
      local NEXT_NOTE
      NEXT_NOTE=$(python3 "$HERE/review_queue.py" next --pass "$pass")
      if [ -n "$NEXT_NOTE" ]; then
        CURR_PASS="$pass"
        break
      fi
    done
    [ -z "$CURR_PASS" ] && { echo "[W$WID] all passes complete"; return 0; }

    NOTE=$(python3 "$HERE/review_queue.py" claim --pass "$CURR_PASS" --worker "$WID")
    [ -z "$NOTE" ] && { echo "[W$WID] no notes left in pass $CURR_PASS"; continue; }
    echo "[W$WID] claimed $NOTE (pass: $CURR_PASS)"

    PASSAGES="$(python3 "$HERE/run_integrity.py" passages --staging "$STAGING" --vault "$ZK_DIR" --note "$NOTE")"
    PROMPT="Read $HERE/prompts/review.md and $HERE/NOTE_CONTRACT.md. Run ONLY review pass '$CURR_PASS' \
on this SINGLE note: \"$ZK_DIR/$NOTE\". Edit only this note and any new note created by splitting it. \
Use $HERE/index_query.py for any dedup check. If index_add.py exits with code 3, the fold is refused: \
create a new note instead and link the two. Do NOT run whole-vault validate. Then stop.
Cited transcript passages for this note:
$PASSAGES"
    zr_review_unit "$NOTE" "$CURR_PASS" "$PROMPT" "rw$WID-$CURR_PASS"
  done
  echo "[W$WID] max iters"
}

echo "=== Parallel review: $WORKERS workers (run $RUN_ID) ==="
PIDS=()
for ((w=0; w<WORKERS; w++)); do
  worker_loop "$w" >>"$LOG_DIR/review-worker-$w.log" 2>&1 &
  PIDS+=($!)
  echo "  review worker $w -> $LOG_DIR/review-worker-$w.log"
done
for pid in "${PIDS[@]}"; do wait "$pid"; done
echo "=== All review workers finished ==="
