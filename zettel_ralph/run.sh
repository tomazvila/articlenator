#!/usr/bin/env bash
# Robust end-to-end orchestrator: ingest -> cluster -> parallel synthesis -> review.
# Resumable, idempotent, single-instance, retries every phase with backoff. Safe to kill and
# re-run at any time — each phase continues from queue.json / review_queue.json.
#
# Run it DETACHED so it survives your terminal / this session ending (the robust way for a
# multi-day job):
#   cd /Users/lilvilla/Programming/articlenator
#   export X_COOKIES='auth_token=...; ct0=...'
#   nohup nix develop --command bash zettel_ralph/run.sh >> zettel_ralph/staging/run.log 2>&1 &
#
# Watch:   tail -f zettel_ralph/staging/run.log      Status: python zettel_ralph/progress.py
# Re-arm after a cookie refresh: re-export X_COOKIES and launch again (it resumes).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING="$HERE/staging"; mkdir -p "$STAGING"
VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
NWORKERS="${NWORKERS:-4}"
BOOKMARKS="${BOOKMARKS:-$HERE/data/bookmarks_ordered.json}"
INGEST_ATTEMPTS="${INGEST_ATTEMPTS:-40}"
SYNTH_PASSES="${SYNTH_PASSES:-60}"
BACKOFF="${BACKOFF:-30}"
export VAULT ZK_FOLDER ZK_DIR NWORKERS HERE STAGING

log() { echo "[$(date '+%F %T')] $*"; }

# --- single-instance guard (portable; no flock binary needed) ---------------- #
if [ -f "$STAGING/run.pid" ] && kill -0 "$(cat "$STAGING/run.pid" 2>/dev/null)" 2>/dev/null; then
  log "another run.sh is active (pid $(cat "$STAGING/run.pid")) - exiting"; exit 1
fi
echo $$ >"$STAGING/run.pid"
trap 'rm -f "$STAGING/run.pid"; log "run.sh exiting"' EXIT

count() { # count(field, value...) -> items with kind!=video and stage in values
  python3 - "$STAGING/queue.json" "$@" <<'PY'
import json, sys
q = json.load(open(sys.argv[1]))
want = set(sys.argv[2:])
print(sum(1 for it in q["items"] if it["kind"] != "video" and it["stage"] in want))
PY
}

log "=== zettel_ralph run start (nworkers=$NWORKERS, vault=$ZK_DIR) ==="
[ -f "$STAGING/queue.json" ] || { log "no queue.json - run ingest.py --rebuild once first"; exit 1; }

# --- Phase A: ingest, retry with backoff; stop early if cookies are clearly dead --- #
if [ -z "${SKIP_INGEST:-}" ]; then
  [ -n "${X_COOKIES:-}" ] || log "WARNING: X_COOKIES not set; ingestion will fail until you set it."
  stale=0
  for a in $(seq 1 "$INGEST_ATTEMPTS"); do
    before="$(count pending)"
    [ "$before" -eq 0 ] && { log "ingest: nothing pending"; break; }
    log "ingest attempt $a/$INGEST_ATTEMPTS ($before non-video pending)"
    python3 "$HERE/ingest.py" --bookmarks "$BOOKMARKS" || true
    after="$(count pending)"
    if [ "$after" -ge "$before" ]; then
      stale=$((stale + 1))
      log "ingest: no progress this attempt ($before -> $after); stale=$stale"
      [ "$stale" -ge 3 ] && { log "ingest: 3 stale attempts - cookies likely expired; continuing with $after still pending. Re-run with fresh X_COOKIES to finish."; break; }
      sleep "$BACKOFF"
    else
      stale=0
    fi
  done
else
  log "ingest skipped (SKIP_INGEST set)"
fi

# --- Phase A2: cluster (deterministic, idempotent) --- #
log "clustering tweets"; python3 "$HERE/cluster.py" || true

# --- Phase B: parallel synthesis; re-run until nothing is ready (crash recovery) --- #
for a in $(seq 1 "$SYNTH_PASSES"); do
  ready="$(count extracted claimed)"
  [ "$ready" -eq 0 ] && { log "synthesis: complete"; break; }
  log "synthesis pass $a/$SYNTH_PASSES ($ready items ready)"
  NWORKERS="$NWORKERS" bash "$HERE/loop_parallel.sh" || true
done

# --- Phase C: review (item-batched, resumable) --- #
log "review phase"; bash "$HERE/review_loop.sh" || true

log "=== run complete ==="
python3 "$HERE/progress.py" || true
