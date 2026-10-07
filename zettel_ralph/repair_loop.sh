#!/usr/bin/env bash
# Repair old notes into contract form (NOTE_CONTRACT.md section 13, prompts/repair_note.md).
#
#   bash repair_loop.sh "01 Permanent Notes/Old Title.md" [...]   # these notes
#   bash repair_loop.sh --from-candidates                           # old-format and needs-repair notes
#   bash repair_loop.sh --allow-other-sources <note> [...]          # accept Evidence from other videos
#   bash repair_loop.sh --force <note> [...]                        # also notes that are done already
#
# Known sources of an old note: its `sources`, legacy provenance, and
# $STAGING/known_sources.json. Build that file once from the vault git history:
#   python3 run_integrity.py known-sources --staging "$STAGING" --vault "$ZK_DIR"
# When no known source of a note has a transcript, the note gets
# `verification: unsupported` with `unsupported_reason: source transcript missing`
# (no agent run), unless --allow-other-sources is given.
#
# Each note runs as one repair unit through run_lib.sh: the agent writes into its shadow
# folder; finalize (repair mode) accepts a converted note, a `superseded` stub, or
# `verification: unsupported` with `unsupported_reason`, and publishes only what passes.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT:-/srv/obsidian/vaults/Themis 2.0}"
ZK_FOLDER="${ZK_FOLDER:-Video Transcripts Zettelkasten}"
ZK_DIR="$VAULT/$ZK_FOLDER"
STAGING="${ZR_STAGING:-$HERE/staging_transcripts}"
case "$STAGING" in
  /*) ;;
  *) STAGING="$(pwd)/$STAGING" ;;
esac
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f /home/deploy/dotfiles/open-router-deepkseek-api-key.txt ]; then
  export DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
fi
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek/deepseek-v4-flash}"
export VAULT ZK_FOLDER ZK_DIR STAGING HERE ZR_CONTENT=video
export ZR_STAGING="$STAGING"
# shellcheck source=run_lib.sh
. "$HERE/run_lib.sh"

export ZR_ALLOW_OTHER_SOURCES=0
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-other-sources) ZR_ALLOW_OTHER_SOURCES=1; shift ;;
    --force) FORCE=1; shift ;;  # repair a note even when it is done (source-checked, unsupported, stub)
    *) break ;;
  esac
done
notes=()
if [ "${1:-}" = "--from-candidates" ]; then
  while IFS= read -r n; do [ -n "$n" ] && notes+=("$n"); done < <(
    python3 "$HERE/run_integrity.py" repair-candidates --staging "$STAGING" --vault "$ZK_DIR" |
      python3 -c 'import json,sys; [print(n) for it in json.load(sys.stdin) for n in it["notes"]]')
else
  notes=("$@")
fi
# Z1 (round 23): repair runs in single mode only (agent mode can write a note over the old
# note). Override only with ZR_REPAIR_AGENT_MODE_I_KNOW=1.
if [ "${ZR_REPAIR_AGENT_MODE_I_KNOW:-0}" != "1" ]; then
  if [ "$ZR_ALLOW_OTHER_SOURCES" = "1" ] || [ "${ZR_SYNTH_MODE:-single}" != "single" ]; then
    echo "repair_loop.sh: repair runs in single mode only: do not set ZR_SYNTH_MODE (now '${ZR_SYNTH_MODE:-single}')" \
         "and do not pass --allow-other-sources (set ZR_REPAIR_AGENT_MODE_I_KNOW=1 to override)" >&2
    exit 2
  fi
fi
if [ "${#notes[@]}" -eq 0 ]; then
  echo "no notes to repair"
  # Z7: --from-candidates with no candidate still reports operator work (exit 7)
  ow="$(python3 "$HERE/run_integrity.py" operator-work --staging "$STAGING" --vault "$ZK_DIR")" || exit 1
  if [ "${ow%%$'\n'*}" != "0" ]; then
    echo "operator work remains (exit 7):" >&2; printf '%s\n' "$ow" | tail -n +2 >&2; exit 7
  fi
  exit 0
fi

# Round 22: the preflight (legacy stubs, unsafe names, backend value, dirty vault, other
# drivers' locks and waits). Any finding stops the run (exit 1) unless ZR_PREFLIGHT_FORCE=1.
pf=(repair-preflight --staging "$STAGING" --vault "$ZK_DIR")
[ "${ZR_PREFLIGHT_FORCE:-0}" = "1" ] && pf+=(--force)
python3 "$HERE/synth_call.py" "${pf[@]}"
pf_rc=$?
if [ "$pf_rc" -eq 75 ]; then
  echo "another driver holds the staging lock; stopping" >&2; exit 75  # Z14: as documented
elif [ "$pf_rc" -ne 0 ]; then
  echo "repair-preflight found problems (see above); stopping" >&2; exit 1
fi
# Z6 (round 23): the known sources of the old notes (vault git history, offline) are built
# once, when the staging has none; without them every old note would go to the operator.
if [ ! -f "$STAGING/known_sources.json" ]; then
  echo "[repair] no known_sources.json: building it from the vault git history (run_integrity.py known-sources)"
  python3 "$HERE/run_integrity.py" known-sources --staging "$STAGING" --vault "$ZK_DIR" || {
    echo "known-sources failed; stopping" >&2; exit 1; }
fi

zr_run_start repair
trap 'zr_run_summary' EXIT
if [ "${ZR_SYNTH_MODE:-single}" = "single" ] && [ "$ZR_ALLOW_OTHER_SOURCES" != "1" ]; then
  # Repair waiting requests before pending reviews can change their prompt keys.
  mkdir -p "$STAGING/repair"
  zr_repair_single "$FORCE" "${notes[@]}"
  repair_rc=$?
  # Earlier repair reviews can now proceed; preserve the repair command's result.
  zr_pending_reviews
  exit "$repair_rc"
fi
# Notes of earlier repair runs that wait for a review (they keep the repair rules).
zr_pending_reviews
mkdir -p "$STAGING/repair"
rc_all=0
for note in "${notes[@]}"; do
  zr_budget_check
  plan_args=(repair-plan --staging "$STAGING" --vault "$ZK_DIR" --note "$note")
  [ "$ZR_ALLOW_OTHER_SOURCES" = "1" ] && plan_args+=(--allow-other-sources)
  plan="$(python3 "$HERE/run_integrity.py" "${plan_args[@]}")" || { echo "[repair] $note: repair-plan failed" >&2; rc_all=1; continue; }
  state="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')"
  case "$state" in
    done|stub|operator|missing)
      if [ "$FORCE" != "1" ]; then
        echo "[repair] $note: skipped (state $state; use --force to repair it again)"
        continue
      fi ;;
  esac
  srcs="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)["sources"]))')"
  unsupported="$(printf '%s' "$plan" | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["mark_unsupported"]))')"
  unit_dir="$(zr_new_unit repair "" "" repair)" || exit 1
  if [ "$unsupported" = "1" ]; then
    # No known source has a transcript: no agent run, no Evidence from other videos.
    echo "[repair] $note: no transcript for $srcs - verification: unsupported"
    python3 "$HERE/run_integrity.py" mark-unsupported --unit-dir "$unit_dir" --vault "$ZK_DIR" --note "$note" \
      --reason "source transcript missing" >/dev/null
    rc=$?
  else
    PROMPT="Read $HERE/prompts/repair_note.md and $HERE/NOTE_CONTRACT.md. Repair ONE old note: \"$ZK_DIR/$note\". \
Candidate source videos: ${srcs:-none recorded}. Write the output record to $STAGING/repair/$(basename "$note" .md).json. Then stop."
    zr_agent "$unit_dir" "$PROMPT" "" repair
    rc=$?
    zr_fix_turns "$unit_dir" "" repair "$rc"
    rc=$?
    [ "$rc" -eq 0 ] && zr_source_check "$unit_dir"
    [ "$rc" -eq 0 ] && { zr_review_repair "$unit_dir" "" repair; rc=$?; }
  fi
  zr_finalize "$unit_dir" "$rc" "" "" "repair: $note (run $RUN_ID)"
  frc=$?
  echo "[repair] $note: agent rc=$rc, finalize rc=$frc"
  [ "$frc" -ne 0 ] && rc_all=1
done
exit "$rc_all"
