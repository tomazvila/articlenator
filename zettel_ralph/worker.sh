#!/usr/bin/env bash
# One parallel synthesis worker. Claims units from its shard, runs a fresh agent per unit,
# verifies the unit was marked done, retries transient failures. Workers defer MOC/Home and
# whole-vault validation to the Phase-C review (avoids concurrent-write races).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING="${ZR_STAGING:-$HERE/staging}"
case "$STAGING" in
  /*) ;;
  *) STAGING="$(pwd)/$STAGING" ;;
esac
AGENTS_FILE="${AGENTS_FILE:-$HERE/AGENTS.md}"
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
MODEL="${MODEL:-}"
MAX_ITERS="${MAX_ITERS:-3000}"
WORKER="${WORKER:?set WORKER}"
NWORKERS="${NWORKERS:?set NWORKERS}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek-v4-flash}}"
# DeepSeek-backed local tool runner. File edits are restricted to the harness/staging/vault,
# and command execution is restricted to the Ralph Python helpers.
AGENT=(python3 "$HERE/deepseek_agent.py")

# Run the current $PROMPT through DeepSeek, output to file $1.
run_engine() {
  ( cd "$HERE" && printf '%s' "$PROMPT" | timeout "${AGENT_TIMEOUT:-1800}" "${AGENT[@]}" ) >"$1" 2>&1 || true
}
notdone_count() {  # how many of comma-list $1 are not yet 'synthesized'
  python3 - "$STAGING/queue.json" "$1" <<'PY'
import json, sys
ids = set(sys.argv[2].split(","))
q = json.load(open(sys.argv[1]))
print(sum(1 for it in q["items"] if it["id"] in ids and it["stage"] != "synthesized"))
PY
}
is_account_limit() {  # empty/short output OR an API/account-wide limit signal in file $1
  [ "$(wc -c <"$1" 2>/dev/null || echo 0)" -lt 400 ] || \
    grep -qiE "usage limit|session limit|quota|exhausted|rate limit|429|529|overloaded" "$1"
}

# Reclaim any units this worker left 'claimed' in a previous (crashed) run.
python3 "$HERE/queue_claim.py" --worker "$WORKER" --of "$NWORKERS" --reclaim >/dev/null 2>&1 || true

i=0
while :; do
  i=$((i + 1)); [ "$i" -gt "$MAX_ITERS" ] && { echo "[w$WORKER] iter cap"; exit 2; }

  unit="$(python3 "$HERE/queue_claim.py" --worker "$WORKER" --of "$NWORKERS")"
  [ "$unit" = "{}" ] && { echo "[w$WORKER] shard done at iter $i"; exit 0; }
  ids="$(printf '%s' "$unit" | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["ids"]))')"
  echo "[w$WORKER] iter $i ids=$ids"

  PROMPT="Read $AGENTS_FILE and follow it. PARALLEL MODE OVERRIDES: your pre-selected unit \
is: $unit . Synthesize ONLY this unit. Dedup with index_query.py, record with index_add.py, \
mark done with queue_mark.py. Do NOT rewrite STATE.md. Do NOT run any whole-vault validate. \
Do NOT create or edit MOCs or Home.md (those are built later in review). NEVER run any .sh \
script, bash, sh, claude, codex, or deepseek_agent.py, and never spawn agents, workers, or loops (you are ONE \
unit of work). Write permanent notes under: $ZK_DIR . Then stop."
  engine=deepseek
  echo "[w$WORKER] engine=$engine model=$DEEPSEEK_MODEL"
  out="$(mktemp)"
  run_engine "$out"
  tail -3 "$out"
  notdone="$(notdone_count "$ids")"

  if [ "$notdone" -gt 0 ]; then
    bytes="$(wc -c <"$out" 2>/dev/null || echo 0)"
    if is_account_limit "$out"; then
      # DeepSeek/API unavailable. Soft-release (no penalty) + short backoff.
      echo "[w$WORKER] $engine unavailable (out=${bytes}B); soft-release + ${SYNTH_BACKOFF:-120}s"
      python3 "$HERE/queue_claim.py" --release-soft "$ids" >/dev/null 2>&1 || true
      sleep "${SYNTH_BACKOFF:-120}"
    elif grep -qiE "connection|timed out|timeout|killed|terminated|stream error|network|aborted|api error" "$out"; then
      echo "[w$WORKER] transient on $engine (out=${bytes}B); soft-release + ${SYNTH_BACKOFF:-120}s"
      python3 "$HERE/queue_claim.py" --release-soft "$ids" >/dev/null 2>&1 || true
      sleep "${SYNTH_BACKOFF:-120}"
    else
      # Genuine synthesis failure (substantial output, unit not completed): count an attempt.
      echo "[w$WORKER] not completed on $engine; release (attempt counted)"
      python3 "$HERE/queue_claim.py" --release "$ids" >/dev/null 2>&1 || true
    fi
    rm -f "$out"
    continue
  fi
  rm -f "$out"
done
