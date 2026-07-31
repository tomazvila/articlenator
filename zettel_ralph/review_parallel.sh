#!/usr/bin/env bash
# Parallel Phase C: run per-note review passes with multiple DeepSeek workers,
# then run the vault-level MOC clustering pass once.
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
NREVIEW_WORKERS="${NREVIEW_WORKERS:-${NWORKERS:-16}}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek-v4-flash}}"
export VAULT ZK_FOLDER ZK_DIR HERE STAGING
export ZR_STAGING="$STAGING"
AGENT=(python3 "$HERE/deepseek_agent.py")

python3 "$HERE/review_queue.py" build --vault "$ZK_DIR"

for pass in atomicity linking source-free; do
  echo "=== parallel review pass: $pass ($NREVIEW_WORKERS workers) ==="
  pids=()
  for w in $(seq 0 $((NREVIEW_WORKERS - 1))); do
    REVIEW_PASS="$pass" WORKER="$pass-$w" bash "$HERE/review_worker.sh" >"$STAGING/review-$pass-$w.log" 2>&1 &
    pids+=("$!")
    echo "  review worker $pass/$w pid ${pids[-1]} -> $STAGING/review-$pass-$w.log"
  done
  rc=0
  for p in "${pids[@]}"; do wait "$p" || rc=1; done
  echo "review pass $pass exited (rc=$rc)"
done

echo "=== clustering pass (single worker) ==="
python3 "$HERE/validate.py" --vault "$ZK_DIR" --squeeze >"$STAGING/squeeze.json"
CPROMPT="Read $HERE/prompts/review.md and run the 'clustering' pass. Authoritative topic \
counts are in $STAGING/squeeze.json: build or refresh an MOC for every topic where at_squeeze \
is true, link its notes with context, and link the MOC from Home.md. Then stop."
( cd "$HERE" && printf '%s' "$CPROMPT" | timeout "${AGENT_TIMEOUT:-1800}" "${AGENT[@]}" )

echo "=== final strict validation ==="
python3 "$HERE/validate.py" --vault "$ZK_DIR" --strict
