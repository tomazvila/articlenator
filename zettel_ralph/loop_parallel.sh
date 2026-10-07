#!/usr/bin/env bash
# Phase B (parallel): launch N synthesis workers over disjoint shards of the queue, wait for
# all, then run a final whole-vault validation report. MOCs/Home are built afterward by
# review_loop.sh.
#
# Each worker is parallel_worker.sh. All workers share one run folder
# ($STAGING/runs/<run_id>/) and write logs to $STAGING/logs/<run_id>/worker-<w>.log
# (append only; a new run never overwrites an earlier log). At exit this script writes
# $STAGING/runs/<run_id>/summary.json.
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
# Same default model as the former worker.sh; run.json records the real value.
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek/deepseek-v4-flash}}"
export VAULT ZK_FOLDER ZK_DIR HERE STAGING
export ZR_STAGING="$STAGING"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"

[ -f "$STAGING/queue.json" ] || { echo "no queue.json - run ingest first"; exit 1; }
mkdir -p "$ZK_DIR/00 Maps" "$ZK_DIR/01 Permanent Notes" "$ZK_DIR/02 Examples" "$ZK_DIR/03 Reviews"
[ -f "$STAGING/concept-index.json" ] || printf '{"version":1,"concepts":[],"mocs":[]}\n' >"$STAGING/concept-index.json"

zr_run_start synth-parallel
trap 'zr_run_summary' EXIT
# Notes that waited for a source-check review in an earlier run.
zr_pending_reviews

# Refresh dedup index from disk once before launching (crash recovery from any prior run).
python3 "$HERE/index_rebuild.py" || echo "index_rebuild.py failed (index may be stale)" >&2

echo "launching $NWORKERS workers over $(python3 -c "import json;print(sum(1 for it in json.load(open('$STAGING/queue.json'))['items'] if it['stage'] in ('extracted','claimed','incomplete')))") ready items"
pids=()
for w in $(seq 0 $((NWORKERS - 1))); do
  WORKER="$w" NWORKERS="$NWORKERS" bash "$HERE/parallel_worker.sh" >>"$LOG_DIR/worker-$w.log" 2>&1 &
  pids+=("$!")
  echo "  worker $w pid ${pids[-1]} -> $LOG_DIR/worker-$w.log"
done

rc=0
for p in "${pids[@]}"; do wait "$p" || rc=1; done
echo "all workers exited (rc=$rc)"

# Whole-vault form report. It does not decide publication: finalize already checked and,
# when needed, quarantined each note. The report goes to the run log folder.
echo "=== final validation (report only; per-note checks ran in finalize) ==="
python3 "$HERE/validate.py" --vault "$ZK_DIR" >>"$LOG_DIR/final-validate.log" 2>&1
vrc=$?
tail -1 "$LOG_DIR/final-validate.log"
[ "$vrc" -ne 0 ] && echo "whole-vault validate reported errors (rc=$vrc); see $LOG_DIR/final-validate.log"
python3 "$HERE/progress.py" || echo "progress.py failed" >&2
exit "$rc"
