#!/usr/bin/env bash
# Compatibility entry point: parallel per-note review plus the clustering pass.
# It runs parallel_review.sh (passes source-check, atomicity, linking) and then
# review_loop.sh, which finds no open per-note work and runs only the clustering pass
# and the final validation. Every agent run goes through run_lib.sh.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
export ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
bash "$HERE/parallel_review.sh" "${NREVIEW_WORKERS:-${NWORKERS:-16}}"
rc=$?
[ "$rc" -ne 0 ] && echo "parallel review exited with rc=$rc" >&2
bash "$HERE/review_loop.sh"
exit $?
