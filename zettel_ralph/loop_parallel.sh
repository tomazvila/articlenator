#!/usr/bin/env bash
# Phase B (parallel): launch N synthesis workers over disjoint shards of the queue, wait for
# all, then run a final whole-vault validation. MOCs/Home are built afterward by review_loop.sh.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING="${ZR_STAGING:-$HERE/staging}"
case "$STAGING" in
  /*) ;;
  *) STAGING="$(pwd)/$STAGING" ;;
esac
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
NWORKERS="${NWORKERS:-4}"
export VAULT ZK_FOLDER ZK_DIR HERE STAGING
export ZR_STAGING="$STAGING"

[ -f "$STAGING/queue.json" ] || { echo "no queue.json - run ingest first"; exit 1; }
mkdir -p "$ZK_DIR/00 Maps" "$ZK_DIR/01 Permanent Notes" "$ZK_DIR/02 Examples" "$ZK_DIR/03 Reviews"
[ -f "$STAGING/concept-index.json" ] || printf '{"version":1,"concepts":[],"mocs":[]}\n' >"$STAGING/concept-index.json"
[ -f "$STAGING/provenance.json" ] || printf '{}\n' >"$STAGING/provenance.json"

# Refresh dedup index from disk once before launching (crash recovery from any prior run).
python3 "$HERE/index_rebuild.py" || true

echo "launching $NWORKERS workers over $(python3 -c "import json;print(sum(1 for it in json.load(open('$STAGING/queue.json'))['items'] if it['stage'] in ('extracted','claimed')))") ready items"
pids=()
for w in $(seq 0 $((NWORKERS - 1))); do
  WORKER="$w" NWORKERS="$NWORKERS" bash "$HERE/worker.sh" >"$STAGING/worker-$w.log" 2>&1 &
  pids+=("$!")
  echo "  worker $w pid ${pids[-1]} -> staging/worker-$w.log"
done

rc=0
for p in "${pids[@]}"; do wait "$p" || rc=1; done
echo "all workers exited (rc=$rc)"

echo "=== final validation ==="
python3 "$HERE/validate.py" --vault "$ZK_DIR" --staging "$STAGING" || true
python3 "$HERE/progress.py" || true
