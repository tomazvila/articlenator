#!/usr/bin/env bash
# Crawl @thdxr's last six months of profile posts, then run the existing Ralph
# ingestion/synthesis/review pipeline in an isolated staging folder.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HANDLE="${HANDLE:-thdxr}"
SINCE="${SINCE:-2025-12-26}"
UNTIL="${UNTIL:-2026-06-26}"
BOOKMARKS="${BOOKMARKS:-$HERE/data/${HANDLE}_${SINCE}_${UNTIL}.json}"
STAGING="${ZR_STAGING:-$HERE/staging_${HANDLE}_${SINCE}_${UNTIL}}"
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter ${HANDLE} ${SINCE} to ${UNTIL} Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
NWORKERS="${NWORKERS:-4}"

export ZR_STAGING="$STAGING" VAULT ZK_FOLDER ZK_DIR NWORKERS
export ZR_CONTENT=text
STARTED="$(date +%s)"

if [ -z "${X_COOKIES:-}" ] && [ -f "$HERE/staging/cookies.sh" ]; then
  # Local sensitive file, if present. The script never prints its contents.
  # shellcheck source=/dev/null
  . "$HERE/staging/cookies.sh"
fi

if [ -z "${X_COOKIES:-}" ]; then
  echo "ERROR: X_COOKIES is not set. Export it or provide $HERE/staging/cookies.sh." >&2
  exit 1
fi

mkdir -p "$STAGING"

if [ ! -s "$BOOKMARKS" ] || [ "${REFRESH_PROFILE:-0}" = "1" ]; then
  python3 "$HERE/profile_crawl.py" \
    --handle "$HANDLE" \
    --since "$SINCE" \
    --until "$UNTIL" \
    --out "$BOOKMARKS"
fi

if [ ! -f "$STAGING/queue.json" ] || [ "${REBUILD_QUEUE:-0}" = "1" ]; then
  python3 "$HERE/ingest_profile_json.py" --input "$BOOKMARKS" --rebuild
else
  echo "using existing queue: $STAGING/queue.json"
fi

python3 "$HERE/cluster.py" || { echo "cluster.py failed (rc=$?)" >&2; exit 1; }

for pass in $(seq 1 "${SYNTH_PASSES:-60}"); do
  ready="$(python3 - "$STAGING/queue.json" <<'PY'
import json, sys
q = json.load(open(sys.argv[1]))
print(sum(1 for it in q["items"] if it["stage"] in ("extracted", "claimed")))
PY
)"
  [ "$ready" -eq 0 ] && break
  echo "synthesis pass $pass (${ready} ready)"
  bash "$HERE/loop_parallel.sh"
  prc=$?
  if [ "$prc" -ne 0 ]; then
    echo "synthesis pass $pass exited with rc=$prc - stopping before the review" >&2
    exit "$prc"
  fi
done

bash "$HERE/review_loop.sh"
rrc=$?
[ "$rrc" -ne 0 ] && echo "review loop exited with rc=$rrc" >&2
python3 "$HERE/progress.py" || echo "progress.py failed" >&2
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"
zr_report_runs "$STAGING" "$STARTED"
src=$?
[ "$rrc" -eq 0 ] && [ "$src" -eq 0 ]
