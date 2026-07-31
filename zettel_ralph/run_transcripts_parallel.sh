#!/usr/bin/env bash
# Video-transcript corpus → zettelkasten, PARALLEL synthesis: N sharded workers run
# concurrent fresh agents (queue_claim.py hands each worker a disjoint shard; the state
# lock serializes only the short index/queue writes). MOCs/Home/whole-vault validation are
# deferred to the review loop, so workers never race on shared vault files.
#
#   nix develop
#   NWORKERS=6 bash zettel_ralph/run_transcripts_parallel.sh
#
# Resumable: re-running reclaims any shard a crashed worker left 'claimed' and continues.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ZR_STAGING="$HERE/staging_transcripts"
export ZK_FOLDER="${ZK_FOLDER:-Video Transcripts Zettelkasten}"
export AGENTS_FILE="$HERE/AGENTS_transcript.md"
export NWORKERS="${NWORKERS:-6}"

echo "== Phase A: ingest local video transcripts =="
python3 "$HERE/ingest_transcripts.py" "$@"

echo "== Phase B (parallel: $NWORKERS workers): synthesis -> $ZK_FOLDER =="
bash "$HERE/loop_parallel.sh"

echo "== Phase C: review Ralph loop (MOCs, Home, dedup cleanup, strict validate) =="
bash "$HERE/review_loop.sh"

echo "== done =="
