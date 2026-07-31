#!/usr/bin/env bash
# Phase B: the synthesis Ralph loop.
#
# Outer driver. Each iteration spawns a FRESH-context agent that reads AGENTS.md,
# does exactly ONE work unit (one article, or one small tweet cluster) from the
# staged literature notes, writes/updates permanent notes in the vault, advances
# queue.json, then exits. The driver gates on validate.py and bails on no progress.
#
# Nothing here runs the agent until you invoke it. Requires a DeepSeek API key.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING="${ZR_STAGING:-$HERE/staging}"
QUEUE="$STAGING/queue.json"

# ---- config (override via env) -------------------------------------------- #
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
AGENTS_FILE="${AGENTS_FILE:-$HERE/AGENTS.md}"
MAX_ITERS="${MAX_ITERS:-400}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek-v4-flash}}"
# Sandboxed by deepseek_agent.py: file edits are restricted to the harness/staging/vault,
# and commands are restricted to the Ralph Python helpers.
AGENT=(python3 "$HERE/deepseek_agent.py")

export VAULT ZK_FOLDER ZK_DIR STAGING QUEUE HERE AGENTS_FILE
export ZR_STAGING="$STAGING"

# Count items READY for synthesis (extracted). `pending` items aren't ingested yet, so they
# are not the synthesis loop's concern; ingestion (Phase A) turns them into extracted/failed.
pending_count() {
  python3 - "$QUEUE" <<'PY'
import json, sys
q = json.load(open(sys.argv[1]))
print(sum(1 for it in q["items"] if it["stage"] == "extracted"))
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
[ -f "$STAGING/provenance.json" ] || printf '{}\n' >"$STAGING/provenance.json"
[ -f "$STAGING/STATE.md" ] || cp "$HERE/state/STATE.template.md" "$STAGING/STATE.md"
[ -f "$STAGING/DECISIONS.md" ] || cp "$HERE/state/DECISIONS.template.md" "$STAGING/DECISIONS.md"

GIT=0; git -C "$ZK_DIR" rev-parse >/dev/null 2>&1 && GIT=1

i=0
stall=0
while :; do
  i=$((i + 1))
  if [ "$i" -gt "$MAX_ITERS" ]; then echo "iter cap ($MAX_ITERS) hit"; exit 2; fi

  remaining="$(pending_count)"
  if [ "$remaining" -eq 0 ]; then echo "ALL EXTRACTED ITEMS PROCESSED - phase B done"; exit 0; fi
  # Rebuild the dedup index from disk so a prior crashed iteration's notes are seen
  # (re-processing folds instead of duplicating).
  python3 "$HERE/index_rebuild.py" >/dev/null 2>&1 || true
  unit="$(python3 "$HERE/queue_next.py")"
  if [ "$unit" = "{}" ]; then echo "no extractable unit left - done"; exit 0; fi
  echo "=== iter $i | remaining=$remaining synthesized=$(synthesized_count) ==="
  echo "  unit: $unit"

  # Fresh-context agent. cd so AGENTS.md relative paths resolve; the work unit is pre-selected
  # (agent never scans the big queue). Prompt via stdin (--add-dir is variadic and would eat a
  # positional prompt arg).
  PROMPT="Read $AGENTS_FILE and follow it exactly. Your pre-selected work unit is: $unit . \
Synthesize ONLY this unit, then stop. Write all notes under this folder: $ZK_DIR"
  ( cd "$HERE" && printf '%s' "$PROMPT" | timeout "${AGENT_TIMEOUT:-1800}" "${AGENT[@]}" )

  # Verification gate (worker != checker). --staging enables the own-words overlap check.
  if ! python3 "$HERE/validate.py" --vault "$ZK_DIR" --staging "$STAGING"; then
    echo "VALIDATE FAILED after iter $i - stopping for human review."
    exit 4
  fi

  # Progress = the unit left extracted (synthesized OR skipped). Transient API errors can
  # interrupt an iteration; retry the unit a few times before giving up (index was rebuilt
  # from disk above, so retries fold rather than duplicate).
  after="$(pending_count)"
  if [ "$after" -ge "$remaining" ]; then
    stall=$((stall + 1))
    echo "  no progress (stall $stall/3) on this unit - retrying."
    if [ "$stall" -ge 3 ]; then echo "NO PROGRESS after 3 attempts - stopping for review."; exit 3; fi
    continue
  fi
  stall=0

  # Durable checkpoint of the source-of-truth state + the vault.
  cp "$QUEUE" "$STAGING/queue.bak.json"
  if [ "$GIT" -eq 1 ]; then
    git -C "$ZK_DIR" add -A
    git -C "$ZK_DIR" commit -q -m "zettel: synthesis iter $i ($(synthesized_count) done)" || true
  fi
done
