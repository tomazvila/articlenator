#!/usr/bin/env bash
# Video-transcript corpus → zettelkasten, PARALLEL synthesis: N sharded workers run
# concurrent fresh agents (queue_claim.py hands each worker a disjoint shard; the state
# lock serializes only the short index/queue writes). MOCs/Home/whole-vault validation are
# deferred to the review loop, so workers never race on shared vault files.
#
#   nix develop
#   NWORKERS=6 bash zettel_ralph/run_transcripts_parallel.sh
#
# Ingest runs a transcript quality check. A degraded transcript (whisper loop, missing
# speech) gets queue stage 'held' and is not synthesized. To queue it anyway, add
# --allow-degraded (or set ALLOW_DEGRADED=1). See staging/ingest_report.json.
#
# Resumable: re-running reclaims any shard a crashed worker left 'claimed' and continues.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# OpenRouter config (reuse pi's key if not explicitly set)
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f /home/deploy/dotfiles/open-router-deepkseek-api-key.txt ]; then
  export DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
fi
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek/deepseek-v4-flash}"

export ZR_STAGING="$HERE/staging_transcripts"
export ZK_FOLDER="${ZK_FOLDER:-Video Transcripts Zettelkasten}"
export AGENTS_FILE="$HERE/AGENTS_transcript.md"
export NWORKERS="${NWORKERS:-6}"

echo "== Phase A: ingest local video transcripts -> $ZR_STAGING =="
python3 "$HERE/ingest_transcripts.py" "$@" || { echo "ingest failed (rc=$?) - stopping before synthesis" >&2; exit 1; }

STARTED="$(date +%s)"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"
export ZR_CONTENT=video

echo "== Phase B: synthesis -> $ZK_FOLDER =="
bash "$HERE/loop_parallel.sh"
brc=$?
# 0 = all units done; 2 = iteration cap; 3 = no progress (API down?). Any other value
# than 0 stops the run before the review.
if [ "$brc" -ne 0 ]; then
  echo "synthesis loop exited with rc=$brc - no review on a failed synthesis run" >&2
  zr_report_runs "$ZR_STAGING" "$STARTED"
  exit "$brc"
fi

echo "== Phase C: review Ralph loop =="
bash "$HERE/review_loop.sh"
rrc=$?
[ "$rrc" -ne 0 ] && echo "review loop exited with rc=$rrc (final strict validation failed or iteration cap)" >&2

echo "== run summaries =="
zr_report_runs "$ZR_STAGING" "$STARTED"
src=$?
echo "== done (synthesis rc=$brc, review rc=$rrc, summaries rc=$src) =="
[ "$brc" -eq 0 ] && [ "$rrc" -eq 0 ] && [ "$src" -eq 0 ]
