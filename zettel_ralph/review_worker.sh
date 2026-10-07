#!/usr/bin/env bash
# Compatibility entry point: one per-note review worker for one pass.
# Every agent run goes through run_lib.sh (zr_review_unit: shadow folder + finalize).
# Env: REVIEW_PASS (source-check | atomicity | linking; the old source-free pass runs
# source-check), WORKER.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
STAGING="${ZR_STAGING:-$HERE/staging}"
case "$STAGING" in
  /*) ;;
  *) STAGING="$(pwd)/$STAGING" ;;
esac
REVIEW_PASS="${REVIEW_PASS:?set REVIEW_PASS}"
[ "$REVIEW_PASS" = "source-free" ] && REVIEW_PASS="source-check"
WORKER="${WORKER:?set WORKER}"
MAX_ITERS="${REVIEW_WORKER_MAX_ITERS:-2000}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek/deepseek-v4-flash}}"
export VAULT ZK_FOLDER ZK_DIR HERE STAGING
export ZR_STAGING="$STAGING"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"
zr_run_start "review-worker-$WORKER"
trap 'zr_run_summary' EXIT
# Notes that wait in pending-review (only when this worker owns its run).
zr_pending_reviews

python3 "$HERE/review_queue.py" reclaim --worker "$WORKER" >/dev/null || echo "[rw$WORKER] reclaim failed" >&2
for ((i=1; i<=MAX_ITERS; i++)); do
  note="$(python3 "$HERE/review_queue.py" claim --pass "$REVIEW_PASS" --worker "$WORKER")"
  [ -z "$note" ] && { echo "[rw$WORKER/$REVIEW_PASS] pass done"; exit 0; }
  PASSAGES="$(python3 "$HERE/run_integrity.py" passages --staging "$STAGING" --vault "$ZK_DIR" --note "$note")"
  RPROMPT="Read $HERE/prompts/review.md and $HERE/NOTE_CONTRACT.md. Run ONLY review pass '$REVIEW_PASS' \
on this SINGLE note: \"$ZK_DIR/$note\". Edit only this note and any new note created by splitting it. \
Then stop.
Cited transcript passages for this note:
$PASSAGES"
  zr_review_unit "$note" "$REVIEW_PASS" "$RPROMPT" "rw$WORKER-$REVIEW_PASS"
  [ $? -eq "$ZR_UNIT_INCOMPLETE" ] && sleep "${REVIEW_BACKOFF:-30}"
done
echo "[rw$WORKER/$REVIEW_PASS] iter cap"
