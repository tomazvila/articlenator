#!/usr/bin/env bash
# Phase C: the review Ralph loop — item-batched (WIP=1), not one agent over the whole vault.
#
# Per-note passes (atomicity, linking, source-free) iterate ONE note per fresh agent,
# tracked in staging/review_queue.json, each gated by validate.py and committed. The
# clustering pass is the one genuinely cross-note step; it is driven by a DETERMINISTIC
# squeeze table (validate.py --squeeze) rather than the agent eyeballing every topic. A
# final strict validation closes the run. Mirrors loop.sh's gate/bail discipline.
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
# MODEL remains supported as a compatibility alias, but DeepSeek is now the runner.
# OpenRouter config (reuse pi's key if not explicitly set)
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f /home/deploy/dotfiles/open-router-deepkseek-api-key.txt ]; then
  export DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
fi
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek/deepseek-v4-flash}}"
REVIEW_MAX_ITERS="${REVIEW_MAX_ITERS:-3000}"
# Sandboxed by deepseek_agent.py: only allowed roots and Ralph Python helpers.
AGENT=(python3 "$HERE/deepseek_agent.py")
export VAULT ZK_FOLDER ZK_DIR HERE STAGING
export ZR_STAGING="$STAGING"

PER_NOTE_PASSES=("atomicity" "linking" "source-free")
GIT=0; git -C "$ZK_DIR" rev-parse >/dev/null 2>&1 && GIT=1

python3 "$HERE/review_queue.py" build --vault "$ZK_DIR"

for pass in "${PER_NOTE_PASSES[@]}"; do
  echo "=== review pass (per-note): $pass ==="
  i=0
  while :; do
    i=$((i + 1))
    if [ "$i" -gt "$REVIEW_MAX_ITERS" ]; then echo "review iter cap hit"; exit 2; fi
    note="$(python3 "$HERE/review_queue.py" next --pass "$pass")"
    [ -z "$note" ] && { echo "pass '$pass' complete"; break; }

    RPROMPT="Read $HERE/prompts/review.md. Run ONLY review pass '$pass' on this SINGLE note: \
\"$ZK_DIR/$note\". Use $HERE/index_query.py for any dedup check. Apply fixes for this pass \
only, then stop."
    ( cd "$HERE" && printf '%s' "$RPROMPT" | timeout "${AGENT_TIMEOUT:-1800}" "${AGENT[@]}" )

    if ! python3 "$HERE/validate.py" --vault "$ZK_DIR"; then
      echo "VALIDATE FAILED at $pass / $note - stopping for human review."
      exit 4
    fi
    python3 "$HERE/review_queue.py" "done" --pass "$pass" --file "$note"
    if [ "$GIT" -eq 1 ]; then
      git -C "$ZK_DIR" add -A
      git -C "$ZK_DIR" commit -q -m "review $pass: $note" || true
    fi
  done
done

echo "=== clustering pass (squeeze-driven, deterministic trigger) ==="
python3 "$HERE/validate.py" --vault "$ZK_DIR" --squeeze >"$STAGING/squeeze.json"
CPROMPT="Read $HERE/prompts/review.md and run the 'clustering' pass. Authoritative topic \
counts (from disk) are in $STAGING/squeeze.json: build or refresh an MOC for every topic \
where at_squeeze is true, link its notes with context, and link the MOC from Home.md. Then stop."
( cd "$HERE" && printf '%s' "$CPROMPT" | timeout "${AGENT_TIMEOUT:-1800}" "${AGENT[@]}" )
[ "$GIT" -eq 1 ] && { git -C "$ZK_DIR" add -A; git -C "$ZK_DIR" commit -q -m "review clustering: MOCs" || true; }

echo "=== final strict validation ==="
python3 "$HERE/validate.py" --vault "$ZK_DIR" --strict
