#!/usr/bin/env bash
# One parallel per-note review worker. Claims notes from review_queue.py and
# applies exactly one review pass to one note at a time.
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
WORKER="${WORKER:?set WORKER}"
MAX_ITERS="${REVIEW_WORKER_MAX_ITERS:-2000}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek-v4-flash}}"
export VAULT ZK_FOLDER ZK_DIR HERE STAGING
export ZR_STAGING="$STAGING"
AGENT=(python3 "$HERE/deepseek_agent.py")

python3 "$HERE/review_queue.py" reclaim --worker "$WORKER" >/dev/null 2>&1 || true

i=0
while :; do
  i=$((i + 1)); [ "$i" -gt "$MAX_ITERS" ] && { echo "[rw$WORKER/$REVIEW_PASS] iter cap"; exit 2; }
  note="$(python3 "$HERE/review_queue.py" claim --pass "$REVIEW_PASS" --worker "$WORKER")"
  [ -z "$note" ] && { echo "[rw$WORKER/$REVIEW_PASS] pass done"; exit 0; }
  echo "[rw$WORKER/$REVIEW_PASS] $note"

  RPROMPT="Read $HERE/prompts/review.md. PARALLEL REVIEW OVERRIDES: run ONLY review pass \
'$REVIEW_PASS' on this SINGLE note: \"$ZK_DIR/$note\". Edit only this assigned note and any \
new note created by splitting it. Do NOT edit MOCs, Home.md, STATE.md, DECISIONS.md, or other \
existing permanent notes. Use $HERE/index_query.py for dedup checks and $HERE/index_add.py for \
index/provenance updates. Do NOT run whole-vault validate. Then stop."
  out="$(mktemp)"
  ( cd "$HERE" && printf '%s' "$RPROMPT" | timeout "${AGENT_TIMEOUT:-1800}" "${AGENT[@]}" ) >"$out" 2>&1
  rc=$?
  tail -4 "$out"
  if [ "$rc" -eq 0 ]; then
    python3 "$HERE/review_queue.py" "done" --pass "$REVIEW_PASS" --file "$note"
  else
    echo "[rw$WORKER/$REVIEW_PASS] agent rc=$rc; release $note"
    python3 "$HERE/review_queue.py" release --pass "$REVIEW_PASS" --file "$note"
    sleep "${REVIEW_BACKOFF:-30}"
  fi
  rm -f "$out"
done
