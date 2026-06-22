#!/usr/bin/env bash
# Phase B: the synthesis Ralph loop.
#
# Outer driver. Each iteration spawns a FRESH-context agent that reads AGENTS.md,
# does exactly ONE work unit (one article, or one small tweet cluster) from the
# staged literature notes, writes/updates permanent notes in the vault, advances
# queue.json, then exits. The driver gates on validate.py and bails on no progress.
#
# Nothing here runs the agent until you invoke it. Requires the `claude` CLI.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING="$HERE/staging"
QUEUE="$STAGING/queue.json"

# ---- config (override via env) -------------------------------------------- #
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
MAX_ITERS="${MAX_ITERS:-400}"
MODEL="${MODEL:-claude-opus-4-8}"
# The agent command. AGENTS.md is the real instruction set; the -p prompt just routes.
AGENT_CMD=${AGENT_CMD:-"claude -p --model $MODEL --permission-mode acceptEdits"}

export VAULT ZK_FOLDER ZK_DIR STAGING QUEUE HERE

pending_count() {
  python3 - "$QUEUE" <<'PY'
import json, sys
q = json.load(open(sys.argv[1]))
print(sum(1 for it in q["items"] if it["stage"] in ("pending", "extracted")))
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
while :; do
  i=$((i + 1))
  if [ "$i" -gt "$MAX_ITERS" ]; then echo "iter cap ($MAX_ITERS) hit"; exit 2; fi

  remaining="$(pending_count)"
  if [ "$remaining" -eq 0 ]; then echo "ALL ITEMS PROCESSED - phase B done"; exit 0; fi
  echo "=== iter $i | remaining=$remaining synthesized=$(synthesized_count) ==="

  # Fresh-context agent. cd so AGENTS.md relative paths resolve; pass the resolved vault
  # path explicitly (markdown isn't shell-expanded, so the agent needs the literal path).
  ( cd "$HERE" && $AGENT_CMD "Read $HERE/AGENTS.md and follow it exactly. Do ONE work \
unit of the synthesis phase from $QUEUE, then stop. Write all notes under this folder: \
$ZK_DIR" )

  # Verification gate (worker != checker). --staging enables the own-words overlap check.
  if ! python3 "$HERE/validate.py" --vault "$ZK_DIR" --staging "$STAGING"; then
    echo "VALIDATE FAILED after iter $i - stopping for human review."
    exit 4
  fi

  # Progress = the unit left pending/extracted (synthesized OR skipped both count).
  after="$(pending_count)"
  if [ "$after" -ge "$remaining" ]; then
    echo "NO PROGRESS this iter (remaining $remaining -> $after) - stopping for review."
    exit 3
  fi

  # Durable checkpoint of the source-of-truth state + the vault.
  cp "$QUEUE" "$STAGING/queue.bak.json"
  if [ "$GIT" -eq 1 ]; then
    git -C "$ZK_DIR" add -A
    git -C "$ZK_DIR" commit -q -m "zettel: synthesis iter $i ($(synthesized_count) done)" || true
  fi
done
