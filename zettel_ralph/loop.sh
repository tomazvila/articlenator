#!/usr/bin/env bash
# Phase B: the synthesis Ralph loop.
#
# Outer driver. Each iteration spawns a FRESH-context agent that reads AGENTS.md,
# does exactly ONE work unit (one article, or one small tweet cluster) from the
# staged literature notes, writes/updates permanent notes in the vault, then exits.
#
# The agent writes only into its unit's shadow folder. After each agent run,
# run_integrity.py finalize verifies every shadow note (verify_claims.py + validate.py)
# and publishes only the notes that pass; a failing note goes to
# $STAGING/quarantine/<run_id>/ and the vault does not change. The loop then continues
# with the next unit. See README "Verification and run integrity".
#
# Nothing here runs the agent until you invoke it. Requires a DeepSeek API key.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING="${ZR_STAGING:-$HERE/staging}"
case "$STAGING" in
  /*) ;;
  *) STAGING="$(pwd)/$STAGING" ;;
esac
QUEUE="$STAGING/queue.json"

# ---- config (override via env) -------------------------------------------- #
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
AGENTS_FILE="${AGENTS_FILE:-$HERE/AGENTS.md}"
MAX_ITERS="${MAX_ITERS:-400}"
MAX_STALL="${MAX_STALL:-5}"
# OpenRouter config (reuse pi's key if not explicitly set)
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f /home/deploy/dotfiles/open-router-deepkseek-api-key.txt ]; then
  export DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
fi
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek/deepseek-v4-flash}}"

# Contract checks apply to video notes; the Twitter corpus keeps the old form rules.
case "$AGENTS_FILE" in *transcript*) ZR_CONTENT="${ZR_CONTENT:-video}" ;; *) ZR_CONTENT="${ZR_CONTENT:-text}" ;; esac
export VAULT ZK_FOLDER ZK_DIR STAGING QUEUE HERE AGENTS_FILE ZR_CONTENT
export ZR_STAGING="$STAGING"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"

# Count items READY for synthesis (extracted, or incomplete = an earlier run ended by a
# limit, a timeout or an error). `pending` items are not ingested yet.
pending_count() {
  python3 - "$QUEUE" <<'PY'
import json, sys
q = json.load(open(sys.argv[1]))
print(sum(1 for it in q["items"] if it["stage"] in ("extracted", "incomplete")))
PY
}

# Queue signature: stage and attempt count of every item. A unit that changes it (a
# published note, a quarantine that counts an attempt, a held video) is progress.
queue_signature() {
  python3 - "$QUEUE" <<'PY'
import json, sys
q = json.load(open(sys.argv[1]))
print(";".join(f"{it['id']}={it['stage']}/{it.get('synth_attempts', 0)}" for it in q["items"]))
PY
}

synthesized_count() {
  python3 - "$QUEUE" <<'PY'
import json, sys
q = json.load(open(sys.argv[1]))
print(sum(1 for it in q["items"] if it["stage"] == "synthesized"))
PY
}

[ -f "$QUEUE" ] || { echo "No queue.json - run ingest.py (Phase A) first."; exit 1; }
mkdir -p "$ZK_DIR/00 Maps" "$ZK_DIR/01 Permanent Notes" "$ZK_DIR/02 Examples" "$ZK_DIR/03 Reviews"

# Seed Phase-B state files if missing (idempotent; ingest.py also seeds these).
[ -f "$STAGING/concept-index.json" ] || printf '{"version":1,"concepts":[],"mocs":[]}\n' >"$STAGING/concept-index.json"
[ -f "$STAGING/STATE.md" ] || cp "$HERE/state/STATE.template.md" "$STAGING/STATE.md"
[ -f "$STAGING/DECISIONS.md" ] || cp "$HERE/state/DECISIONS.template.md" "$STAGING/DECISIONS.md"

zr_run_start synth
trap 'zr_run_summary' EXIT
# A claim that a crashed serial run left behind goes back to its old stage.
python3 "$HERE/queue_next.py" --release >/dev/null || echo "queue_next.py --release failed" >&2
# Notes that waited for a source-check review in an earlier run.
zr_pending_reviews

i=0
stall=0
while :; do
  i=$((i + 1))
  if [ "$i" -gt "$MAX_ITERS" ]; then echo "iter cap ($MAX_ITERS) hit"; exit 2; fi

  zr_budget_check
  remaining="$(pending_count)"
  sig_before="$(queue_signature)"
  if [ "$remaining" -eq 0 ]; then echo "ALL EXTRACTED ITEMS PROCESSED - phase B done"; exit 0; fi
  # Rebuild the dedup index from disk so a prior crashed iteration's notes are seen
  # (re-processing folds instead of duplicating).
  python3 "$HERE/index_rebuild.py" >/dev/null 2>&1 || echo "  index_rebuild.py failed (index may be stale)" >&2
  unit="$(python3 "$HERE/queue_next.py")"
  if [ "$unit" = "{}" ]; then echo "no extractable unit left - done"; exit 0; fi
  ids="$(printf '%s' "$unit" | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["ids"]))')"
  echo "=== iter $i | remaining=$remaining synthesized=$(synthesized_count) ==="
  echo "  unit: $unit"

  # Fresh-context agent. cd so AGENTS.md relative paths resolve; the work unit is pre-selected
  # (agent never scans the big queue). Prompt via stdin.
  PROMPT="Read $AGENTS_FILE and follow it exactly. Your pre-selected work unit is: $unit . \
Synthesize ONLY this unit, then stop. Write all notes under this folder: $ZK_DIR"
  zr_synth_unit "$ids" "$PROMPT" "iter$i"
  frc=$?
  # finalize moves a claimed item on; a unit stopped before finalize (precheck) releases it.
  python3 "$HERE/queue_next.py" --release >/dev/null || echo "  queue_next.py --release failed" >&2
  [ "$frc" -eq "$ZR_UNIT_QUARANTINED" ] && echo "  some notes of $ids were quarantined - continuing"

  # Progress = the unit left the ready stages (synthesized, skipped, failed). finalize sets
  # `incomplete` after a limit, timeout or error and fails the item after
  # ZR_MAX_UNIT_ATTEMPTS counted attempts, so a retry loop always ends.
  if [ "$(queue_signature)" = "$sig_before" ]; then
    stall=$((stall + 1))
    echo "  no progress (stall $stall/$MAX_STALL) on this unit - retrying."
    if [ "$stall" -ge "$MAX_STALL" ]; then echo "NO PROGRESS after $MAX_STALL attempts - stopping for review."; exit 3; fi
    sleep "${SYNTH_BACKOFF:-0}"
    continue
  fi
  stall=0

  # Durable checkpoint of the queue. finalize already committed the published notes
  # (only those paths) when the vault is a git repo.
  cp "$QUEUE" "$STAGING/queue.bak.json"
done
