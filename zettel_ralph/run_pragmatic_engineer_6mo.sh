#!/usr/bin/env bash
# Pragmatic Engineer channel -> transcripts -> DeepSeek Ralph synthesis.
#
# Run inside nix develop. This is intentionally isolated from the generic
# transcript staging so it can be resumed, inspected, or stopped on its own.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export CHANNEL_URL="${CHANNEL_URL:-https://www.youtube.com/@pragmaticengineer/videos}"
export PUBLISHED_AFTER="${PUBLISHED_AFTER:-20251226}"
export CHANNEL_JOBS_DIR="${CHANNEL_JOBS_DIR:-$HERE/channel_jobs_pragmatic_engineer}"
export JOB_DIR="${JOB_DIR:-$CHANNEL_JOBS_DIR/pragmatic-engineer-$PUBLISHED_AFTER}"

export ZR_STAGING="${ZR_STAGING:-$HERE/staging_pragmatic_engineer}"
export ZK_FOLDER="${ZK_FOLDER:-Video Transcripts Zettelkasten}"
export AGENTS_FILE="${AGENTS_FILE:-$HERE/AGENTS_transcript.md}"
export NWORKERS="${NWORKERS:-6}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-v4-flash}"

mkdir -p "$ZR_STAGING" "$CHANNEL_JOBS_DIR"

echo "== Phase 0: channel transcription job =="
echo "channel: $CHANNEL_URL"
echo "published_after: $PUBLISHED_AFTER"
echo "job_dir: $JOB_DIR"
python3 - <<'PY'
import json
import os
from pathlib import Path

from twitter_articlenator.pdf.generator import PACKAGING_PER_ITEM
from twitter_articlenator.sources.channel_transcription_service import build_channel_job

channel_url = os.environ["CHANNEL_URL"]
published_after = os.environ["PUBLISHED_AFTER"]
job_dir = Path(os.environ["JOB_DIR"])


def progress(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


job = build_channel_job(
    job_dir,
    packaging=PACKAGING_PER_ITEM,
    published_after=published_after,
)
manifest = job.run(channel_url, on_progress=progress)
print(
    "CHANNEL_DONE",
    json.dumps(
        {
            "status": manifest.status,
            "done_videos": manifest.done_videos,
            "total_videos": len(manifest.videos),
        },
        sort_keys=True,
    ),
    flush=True,
)
PY

echo "== Phase A: ingest completed transcripts =="
python3 "$HERE/ingest_transcripts.py" \
  --channels-dir "$CHANNEL_JOBS_DIR" \
  --staging "$ZR_STAGING"

echo "== Phase B: synthesize with DeepSeek ($DEEPSEEK_MODEL, workers=$NWORKERS) =="
bash "$HERE/loop_parallel.sh"

echo "== done =="
