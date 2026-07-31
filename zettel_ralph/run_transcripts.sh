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
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export ZR_STAGING="$HERE/staging_transcripts"
export ZK_FOLDER="${ZK_FOLDER:-Video Transcripts Zettelkasten}"
export AGENTS_FILE="$HERE/AGENTS_transcript.md"

echo "== Phase A: ingest local video transcripts -> $ZR_STAGING =="
python3 "$HERE/ingest_transcripts.py" "$@"

echo "== Phase B: synthesis Ralph loop -> $ZK_FOLDER =="
bash "$HERE/loop.sh"

echo "== Phase C: review Ralph loop =="
bash "$HERE/review_loop.sh"

echo "== done =="
