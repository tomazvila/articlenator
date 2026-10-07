#!/usr/bin/env bash
# Phase C: the review Ralph loop — item-batched (WIP=1), not one agent over the whole vault.
#
# Per-note passes (source-check, atomicity, linking) iterate ONE note per fresh agent,
# tracked in staging/review_queue.json. The prompt carries the cited transcript passages
# (run_integrity.py passages). After each agent run, run_integrity.py finalize checks every
# note the agent wrote and the assigned note. The source-check pass runs no editing agent:
# one read-only review agent per note fills the harness skeleton (run_lib.sh
# zr_source_check), and the pass is done only when a recorded verdict exists.
# A contract note that fails is quarantined or marked needs-repair.
# A failed note does not stop the loop: the pass is marked failed for that note and the
# loop goes on. The clustering pass is driven by a DETERMINISTIC squeeze table.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# W31 (round 21): the files backend has no wait handling in this loop (a waiting review is
# taken again at once, without end). Refuse it with a clear message.
ZR_LLM_BACKEND="$(printf '%s' "${ZR_LLM_BACKEND:-openrouter}" | tr -d '[:space:]')"  # X13: "files " is files
if [ "$ZR_LLM_BACKEND" = "files" ]; then
  echo "review_loop.sh: ZR_LLM_BACKEND=files is not supported here; use ZR_LLM_BACKEND=openrouter," \
       "or let loop.sh / repair_loop.sh do the source-check reviews with the files backend" >&2
  exit 2
fi
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
STAGING="${ZR_STAGING:-$HERE/staging}"
case "$STAGING" in
  /*) ;;
  *) STAGING="$(pwd)/$STAGING" ;;
esac
# MODEL remains supported as a compatibility alias, but DeepSeek is now the runner.
# OpenRouter config (reuse pi's key if not explicitly set)
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f /home/deploy/dotfiles/open-router-deepkseek-api-key.txt ]; then
  export DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
fi
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek/deepseek-v4-flash}}"
REVIEW_MAX_ITERS="${REVIEW_MAX_ITERS:-3000}"
export VAULT ZK_FOLDER ZK_DIR HERE STAGING
export ZR_STAGING="$STAGING"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"

PER_NOTE_PASSES=("source-check" "atomicity" "linking")
case "$ZK_FOLDER" in *Twitter*) ZR_CONTENT="${ZR_CONTENT:-text}" ;; *) ZR_CONTENT="${ZR_CONTENT:-video}" ;; esac
export ZR_CONTENT

zr_run_start review
trap 'zr_run_summary' EXIT
# Notes that wait in pending-review (an earlier review timed out or was invalid).
zr_pending_reviews

python3 "$HERE/review_queue.py" build --vault "$ZK_DIR"

for pass in "${PER_NOTE_PASSES[@]}"; do
  echo "=== review pass (per-note): $pass ==="
  i=0
  while :; do
    i=$((i + 1))
    if [ "$i" -gt "$REVIEW_MAX_ITERS" ]; then echo "review iter cap hit"; exit 2; fi
    note="$(python3 "$HERE/review_queue.py" next --pass "$pass")"
    [ -z "$note" ] && { echo "pass '$pass' complete"; break; }
    python3 "$HERE/review_queue.py" claim --pass "$pass" --worker "review-loop" >/dev/null

    PASSAGES="$(python3 "$HERE/run_integrity.py" passages --staging "$STAGING" --vault "$ZK_DIR" --note "$note")"
    RPROMPT="Read $HERE/prompts/review.md and $HERE/NOTE_CONTRACT.md. Run ONLY review pass '$pass' on \
this SINGLE note: \"$ZK_DIR/$note\". Use $HERE/index_query.py for any dedup check. If index_add.py \
exits with code 3, the fold is refused: create a new note instead and link the two. Apply fixes for \
this pass only, then stop.
Cited transcript passages for this note:
$PASSAGES"
    zr_review_unit "$note" "$pass" "$RPROMPT" "review-$pass"
  done
done

echo "=== clustering pass (squeeze-driven, deterministic trigger) ==="
python3 "$HERE/validate.py" --vault "$ZK_DIR" --squeeze >"$STAGING/squeeze.json"
CPROMPT="Read $HERE/prompts/review.md and run the 'clustering' pass. Authoritative topic \
counts (from disk) are in $STAGING/squeeze.json: build or refresh an MOC for every topic \
where at_squeeze is true, link its notes with context, and link the MOC from Home.md. Then stop."
zr_clustering_unit "$CPROMPT"

echo "=== final validation ==="
# No strict mode: warnings (near-duplicate titles of different speakers, old notes that wait
# for repair) are reported, not a failed run. Errors still give a non-zero exit code.
python3 "$HERE/validate.py" --vault "$ZK_DIR" --staging "$STAGING" >>"$LOG_DIR/final-validate.log" 2>&1
vrc=$?
tail -1 "$LOG_DIR/final-validate.log"
exit "$vrc"
