#!/usr/bin/env bash
# Compatibility entry point: one sharded synthesis worker. It runs parallel_worker.sh,
# which sends every agent run through run_lib.sh (shadow folder + finalize).
# Env: WORKER, NWORKERS (required); AGENTS_FILE defaults to AGENTS.md as before.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AGENTS_FILE="${AGENTS_FILE:-$HERE/AGENTS.md}"
export VAULT="${VAULT:-$HOME/Documents/Themis 2.0}"
export ZK_FOLDER="${ZK_FOLDER:-Twitter Bookmarks Zettelkasten}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek/deepseek-v4-flash}}"
exec bash "$HERE/parallel_worker.sh" "${WORKER:?set WORKER}" "${NWORKERS:?set NWORKERS}"
