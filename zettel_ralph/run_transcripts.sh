#!/usr/bin/env bash
# Video-transcript corpus → Obsidian zettelkasten, reusing the SAME (parameterized)
# synthesis + review Ralph loops as the Twitter corpus, fully isolated:
#   * its own staging dir (staging_transcripts/) — separate queue + concept-index
#   * its own vault folder ("Video Transcripts Zettelkasten")
#   * the transcript-tuned agent entry point (AGENTS_transcript.md)
#
# Phase A here is local-only (transcripts already on disk); Phases B/C are the shared loops.
#
#   nix develop
#   bash zettel_ralph/run_transcripts.sh                 # full run
#   bash zettel_ralph/run_transcripts.sh --limit 3       # ingest a few (pilot)
#   MAX_ITERS=5 bash zettel_ralph/run_transcripts.sh      # cap synthesis iterations
#
# Ingest runs a transcript quality check. A degraded transcript (whisper loop, missing
# speech) gets queue stage 'held' and is not synthesized. To queue it anyway, add
# --allow-degraded (or set ALLOW_DEGRADED=1). See staging/ingest_report.json.
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

echo "== Phase A: ingest local video transcripts -> $ZR_STAGING =="
python3 "$HERE/ingest_transcripts.py" "$@" || { echo "ingest failed (rc=$?) - stopping before synthesis" >&2; exit 1; }

STARTED="$(date +%s)"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"
export ZR_CONTENT=video

echo "== Phase B: synthesis -> $ZK_FOLDER =="
bash "$HERE/loop.sh"
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
