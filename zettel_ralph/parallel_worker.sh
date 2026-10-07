#!/usr/bin/env bash
# One sharded synthesis worker (loop_parallel.sh launches N of these).
#
# Usage: parallel_worker.sh [<worker_id> <total_workers>]   (or env WORKER / NWORKERS)
#
# For each unit of its shard: claim, skip a degraded transcript, run a fresh agent,
# then run_integrity.py finalize. finalize verifies each note the agent wrote,
# quarantines failures, and sets the queue stage (`incomplete` after a limit,
# timeout or error). The worker never marks a unit `synthesized` itself.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT:-/srv/obsidian/vaults/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Video Transcripts Zettelkasten}"
ZK_DIR="${ZK_DIR:-$VAULT/$ZK_FOLDER}"
STAGING="${ZR_STAGING:-$HERE/staging}"
case "$STAGING" in
  /*) ;;
  *) STAGING="$(pwd)/$STAGING" ;;
esac

if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f /home/deploy/dotfiles/open-router-deepkseek-api-key.txt ]; then
  export DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
fi
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
# Env wins over the default. run.json records the model of each run.
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek/deepseek-v4-flash}"
export DEEPSEEK_MAX_TOKENS="${DEEPSEEK_MAX_TOKENS:-32000}"
export DEEPSEEK_TEMPERATURE="${DEEPSEEK_TEMPERATURE:-0.2}"
export VAULT ZK_FOLDER ZK_DIR STAGING HERE
export ZR_STAGING="$STAGING"
export AGENTS_FILE="${AGENTS_FILE:-$HERE/AGENTS_transcript.md}"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"

WID="${1:-${WORKER:?set WORKER or pass <worker_id>}}"
TOTAL="${2:-${NWORKERS:?set NWORKERS or pass <total_workers>}}"
export WORKER="$WID" NWORKERS="$TOTAL"
MAX_UNITS="${MAX_ITERS:-50}"

zr_run_start "synth-worker-$WID"
trap 'zr_run_summary' EXIT

# Reclaim any units this worker left 'claimed' in a previous (crashed) run.
python3 "$HERE/queue_claim.py" --worker "$WID" --of "$TOTAL" --reclaim >/dev/null || echo "[W$WID] reclaim failed" >&2

for ((i=1; i<=MAX_UNITS; i++)); do
  UNIT_JSON=$(python3 "$HERE/queue_claim.py" --worker "$WID" --of "$TOTAL")
  if [ "$UNIT_JSON" = "{}" ] || [ -z "$UNIT_JSON" ]; then
    echo "[W$WID] no more units"
    exit 0
  fi
  IDS=$(printf '%s' "$UNIT_JSON" | python3 -c "import json,sys; print(','.join(json.load(sys.stdin).get('ids',[])))")
  echo "[W$WID] claimed $IDS (model $DEEPSEEK_MODEL)"

  PROMPT="Read $AGENTS_FILE and follow it. PARALLEL MODE OVERRIDES: your pre-selected unit \
is: $UNIT_JSON . Synthesize ONLY this unit. Dedup with index_query.py, record with index_add.py, \
mark done with queue_mark.py. Do NOT rewrite STATE.md. Do NOT run any whole-vault validate. \
Do NOT create or edit MOCs or Home.md (those are built later in review). NEVER run any .sh \
script, bash, sh, claude, codex, or deepseek_agent.py, and never spawn agents, workers, or loops \
(you are ONE unit of work). Write permanent notes under: $ZK_DIR . Then stop."

  zr_synth_unit "$IDS" "$PROMPT" "w$WID"
  frc=$?
  if [ "$frc" -eq "$ZR_UNIT_INCOMPLETE" ]; then
    echo "[W$WID] $IDS incomplete (limit, timeout or error); backoff ${SYNTH_BACKOFF:-120}s"
    sleep "${SYNTH_BACKOFF:-120}"
  fi
done
echo "[W$WID] max units"
