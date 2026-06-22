#!/usr/bin/env bash
# One parallel synthesis worker. Claims units from its shard, runs a fresh agent per unit,
# verifies the unit was marked done, retries transient failures. Workers defer MOC/Home and
# whole-vault validation to the Phase-C review (avoids concurrent-write races).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
MODEL="${MODEL:-}"
MAX_ITERS="${MAX_ITERS:-3000}"
WORKER="${WORKER:?set WORKER}"
NWORKERS="${NWORKERS:?set NWORKERS}"
# Sandboxed agent: only file edits + python3 helpers. No bash/sh/claude, so an agent CANNOT
# run worker.sh/loop.sh or spawn nested agents (prevents recursion).
AGENT=(claude -p --add-dir "$VAULT" --allowedTools Read Edit Write "Bash(python3:*)" "Bash(python:*)")
[ -n "$MODEL" ] && AGENT+=(--model "$MODEL")

# Reclaim any units this worker left 'claimed' in a previous (crashed) run.
python3 "$HERE/queue_claim.py" --worker "$WORKER" --of "$NWORKERS" --reclaim >/dev/null 2>&1 || true

i=0
while :; do
  i=$((i + 1)); [ "$i" -gt "$MAX_ITERS" ] && { echo "[w$WORKER] iter cap"; exit 2; }
  unit="$(python3 "$HERE/queue_claim.py" --worker "$WORKER" --of "$NWORKERS")"
  [ "$unit" = "{}" ] && { echo "[w$WORKER] shard done at iter $i"; exit 0; }
  ids="$(printf '%s' "$unit" | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["ids"]))')"
  echo "[w$WORKER] iter $i ids=$ids"

  PROMPT="Read $HERE/AGENTS.md and follow it. PARALLEL MODE OVERRIDES: your pre-selected unit \
is: $unit . Synthesize ONLY this unit. Dedup with index_query.py, record with index_add.py, \
mark done with queue_mark.py. Do NOT rewrite STATE.md. Do NOT run any whole-vault validate. \
Do NOT create or edit MOCs or Home.md (those are built later in review). Write permanent notes \
under: $ZK_DIR . Then stop."
  out="$(mktemp)"
  ( cd "$HERE" && printf '%s' "$PROMPT" | timeout "${AGENT_TIMEOUT:-1800}" "${AGENT[@]}" ) >"$out" 2>&1 || true
  tail -3 "$out"

  # Did the agent mark every id synthesized?
  notdone="$(python3 - "$HERE/staging/queue.json" "$ids" <<'PY'
import json, sys
ids = set(sys.argv[2].split(","))
q = json.load(open(sys.argv[1]))
print(sum(1 for it in q["items"] if it["id"] in ids and it["stage"] != "synthesized"))
PY
)"
  if [ "$notdone" -gt 0 ]; then
    # Rate-limit/overload is transient: retry WITHOUT spending the item's attempt budget.
    if grep -qiE "rate limit|overloaded|api error|529|connection closed|usage limit" "$out"; then
      echo "[w$WORKER] transient API issue; backing off ${SYNTH_BACKOFF:-120}s then retrying"
      python3 "$HERE/queue_claim.py" --release-soft "$ids" >/dev/null 2>&1 || true
      sleep "${SYNTH_BACKOFF:-120}"
    else
      echo "[w$WORKER] unit not completed; releasing for retry (attempt counted)"
      python3 "$HERE/queue_claim.py" --release "$ids" >/dev/null 2>&1 || true
    fi
    rm -f "$out"
    continue
  fi
  rm -f "$out"
done
