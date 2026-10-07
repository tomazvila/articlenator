# Shared run-integrity functions for the Ralph drivers. Source this file; do not run it.
#
# Needs: HERE, STAGING, ZK_DIR. Sets: RUN_DIR, RUN_ID, LOG_DIR.
#
# Every agent run in zettel_ralph goes through zr_agent below; no other script calls
# deepseek_agent.py. Rules that every driver gets from these functions:
# - The agent never writes into the vault. It writes into its unit's shadow folder
#   ($RUN_DIR/units/<unit>/out/).
# - After the agent: the mechanical check (`run_integrity.py check`). A failing note
#   gives the agent up to ZR_FIX_TURNS (2) more turns with the checker's report.
# - Video notes then get the LLM source-check review: ONE chat-completion call per note
#   (review_call.py), built and read by the harness, with no tools and no file access.
#   Each call has its own folder $RUN_DIR/units/<unit>/review-N/ (request, raw reply,
#   verdict, usage, review.json). `finalize` publishes a note only when the mechanical
#   check passes and a recorded review of exactly that text gives `supported` for every
#   item; a review that timed out or stayed invalid leaves the note in
#   $STAGING/pending-review/ (never quarantine for that), and the pending step of the next
#   run (every driver runs it) tries again, at most ZR_MAX_PENDING_CYCLES (5) times.
# - Budget: ZR_MAX_COST_USD (default 5) per run. When the run cost passes it, the driver
#   starts no new unit or review, writes the summary (budget.stopped) and exits 4.
# - One driver per staging: zr_run_start takes $STAGING/.driver.lock (flock); a second
#   driver on the same staging stops with exit code 75.
# - With ZR_GIT_COMMIT=1 (set when the vault is a git repo), finalize commits exactly the
#   published paths.
# - Logs: $STAGING/logs/<run_id>/, opened with >> only. At exit, the top-level driver
#   writes $RUN_DIR/summary.json.
# - The unit owner is the long-lived driver or worker process ($$), recorded with its
#   start time; `run_integrity.py start` recovers only units whose owner is gone.

# Defaults for every driver (override by env). A reasoning model spends most of its
# output tokens on reasoning: a 12k limit stopped flash in its first turn (pilot).
export DEEPSEEK_MAX_TOKENS="${DEEPSEEK_MAX_TOKENS:-32000}"
export AGENT_TIMEOUT="${AGENT_TIMEOUT:-1800}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-${MODEL:-deepseek/deepseek-v4-flash}}"
# The effective endpoint for every agent turn and review call (run.json records it).
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://openrouter.ai/api/v1}"
export ZR_REVIEW_MODEL="${ZR_REVIEW_MODEL:-anthropic/claude-sonnet-5.5}"
export ZR_SYNTH_MODE="${ZR_SYNTH_MODE:-single}"
export ZR_SYNTH_MODEL="${ZR_SYNTH_MODEL:-anthropic/claude-sonnet-5.5}"
export ZR_MAX_COST_USD="${ZR_MAX_COST_USD:-5}"
ZR_BUDGET_STOP=4
# Round 22 (X13): ZR_LLM_BACKEND is normalized once (spaces removed) and must be one of the
# known values; any other value stops every driver (exit 1).
ZR_LLM_BACKEND="$(printf '%s' "${ZR_LLM_BACKEND:-openrouter}" | tr -d '[:space:]')"
case "$ZR_LLM_BACKEND" in
  openrouter|files) export ZR_LLM_BACKEND ;;
  *) echo "ZR_LLM_BACKEND='$ZR_LLM_BACKEND' is not one of: openrouter files" >&2; exit 1 ;;
esac
# Exit codes of loop.sh and repair_loop.sh (round 22; README "Driver exit codes"):
#   0 nothing left for workers and no operator work   5 requests wait for workers
#   6 stuck folder(s) at the cycle limit (when nothing else waits)
#   7 finished, but operator work remains (needs_operator.json, needs-repair notes, refused writes)
#   4 budget or call limit   1 error   2/3 iteration cap / stall (loop.sh)   75 another driver holds the lock
ZR_OPERATOR_WORK=7

ZR_UNIT_OK=0
ZR_UNIT_QUARANTINED=10
ZR_UNIT_INCOMPLETE=11

zr_git_mode() {
  if git -C "$ZK_DIR" rev-parse >/dev/null 2>&1; then ZR_GIT_COMMIT="${ZR_GIT_COMMIT:-1}"; else ZR_GIT_COMMIT=0; fi
  export ZR_GIT_COMMIT
}

# zr_run_start <kind>
# A child driver (a worker that a parent launched) reuses the parent's run folder.
zr_run_start() {
  if [ -n "${ZR_PARENT_RUN_DIR:-}" ] && [ -d "$ZR_PARENT_RUN_DIR" ]; then
    RUN_DIR="$ZR_PARENT_RUN_DIR"
    ZR_RUN_OWNER=0
  else
    zr_driver_lock
    RUN_DIR="$(python3 "$HERE/run_integrity.py" start --staging "$STAGING" --kind "$1" --vault "$ZK_DIR")" || {
      echo "run_integrity start failed - stopping before any agent run" >&2
      exit 1
    }
    ZR_RUN_OWNER=1
  fi
  RUN_ID="$(basename "$RUN_DIR")"
  case "$1" in repair*) ZR_DRIVER_KIND=repair ;; synth*) ZR_DRIVER_KIND=synth ;; *) ZR_DRIVER_KIND="" ;; esac  # X12
  LOG_DIR="$STAGING/logs/$RUN_ID"
  mkdir -p "$LOG_DIR"
  export ZR_PARENT_RUN_DIR="$RUN_DIR" ZR_RUN_ID="$RUN_ID"
  zr_git_mode
  echo "run $RUN_ID ($1): run folder $RUN_DIR, logs $LOG_DIR"
}

# zr_driver_lock: one top-level driver per staging. The lock is held until this shell
# and its children exit. A second driver exits 75 with a message.
zr_driver_lock() {
  mkdir -p "$STAGING"
  exec {ZR_DRIVER_LOCK_FD}>>"$STAGING/.driver.lock"
  if ! flock -n "$ZR_DRIVER_LOCK_FD"; then
    echo "another driver runs on staging $STAGING (lock $STAGING/.driver.lock is held)." >&2
    echo "Wait for it to end, or stop it, then start again. Workers of one driver share its run." >&2
    exit 75
  fi
  echo "$$ $(date -u +%FT%TZ) $0" >"$STAGING/.driver.owner"
}

# zr_budget_ok: 0 while the run cost is below ZR_MAX_COST_USD.
zr_budget_ok() {
  python3 "$HERE/run_integrity.py" budget --run-dir "$RUN_DIR" >/dev/null
}

# zr_budget_check: stop the driver (exit 4, summary written by the EXIT trap) when the
# run cost passed the budget. Call it before each new unit or review.
zr_budget_check() {
  zr_budget_ok && return 0
  echo "budget: the run cost passed ZR_MAX_COST_USD=$ZR_MAX_COST_USD - no new unit or review; stopping" >&2
  exit "$ZR_BUDGET_STOP"
}

zr_run_summary() {
  local driver_status=$?  # S10: the driver's own exit code (2, 3, 4 stay)
  [ "${ZR_RUN_OWNER:-0}" -eq 1 ] || return 0
  # A claim that no finalize took (budget stop, error, a worker that stopped) goes back
  # to its old stage. This driver holds the staging lock, so every claim is its own.
  if [ -f "$STAGING/queue.json" ]; then
    ZR_STAGING="$STAGING" python3 "$HERE/queue_next.py" --release-all >/dev/null || echo "queue_next.py --release-all failed" >&2
  fi
  # End of run: notes that waited for a sibling note of this run (link-waiting) get
  # their check again now that the other units are finalized.
  if zr_budget_ok; then zr_pending_reviews link-waiting; fi
  echo "=== run summary: $RUN_DIR/summary.json ==="
  python3 "$HERE/run_integrity.py" summary --run-dir "$RUN_DIR" || echo "run summary failed" >&2
  # Z12 (round 23): the operator-work list is printed after EVERY run, whatever the exit code.
  # Precedence of the exit code: the driver's own code (1, 2, 3, 4, 75) > 5 (requests wait) >
  # 6 (stuck folders) > 7 (operator work) > 0.
  ZR_OPERATOR_ITEMS=0
  zr_operator_print
  [ "$driver_status" -eq 0 ] && zr_exchange_exit
  if [ "$driver_status" -eq 0 ] && [ "$ZR_OPERATOR_ITEMS" != "0" ]; then
    exit "$ZR_OPERATOR_WORK"
  fi
  return 0
}

# zr_operator_print: prints the operator-work list (run_integrity.py operator-work) and sets
# ZR_OPERATOR_ITEMS to its count (round 22 X11, round 23 Z12).
zr_operator_print() {
  local n
  n="$(python3 "$HERE/run_integrity.py" operator-work --staging "$STAGING" --vault "$ZK_DIR")" || {
    echo "operator-work failed" >&2; ZR_OPERATOR_ITEMS=1; return 0; }
  ZR_OPERATOR_ITEMS="${n%%$'\n'*}"
  if [ "$ZR_OPERATOR_ITEMS" != "0" ]; then
    echo "operator work remains ($ZR_OPERATOR_ITEMS item(s); exit $ZR_OPERATOR_WORK when nothing else comes first):" >&2
    printf '%s\n' "$n" | tail -n +2 >&2
  fi
}

# zr_exchange_exit: files backend (round 13). When requests wait for worker replies,
# print their count and end the driver with exit 5 (nothing more can progress in this pass).
ZR_EXCHANGE_WAIT=5
ZR_EXCHANGE_STUCK=6
zr_exchange_exit() {
  [ "${ZR_LLM_BACKEND:-openrouter}" = "files" ] || return 0
  local counts open unconsumed
  if ! counts="$(python3 "$HERE/synth_call.py" exchange-status --staging "$STAGING" --count-open --kind "${ZR_DRIVER_KIND:-}")"; then
    echo "exchange: exchange-status failed; the open requests are unknown" >&2
    exit 1
  fi
  open="${counts%% *}"; unconsumed="${counts##* }"  # unconsumed = folders at the cycle limit
  if [ "${open:-0}" -gt 0 ]; then
    echo "exchange: $open request(s) wait for a worker reply in $STAGING/exchange/requests - answer them, then run again" >&2
    exit "$ZR_EXCHANGE_WAIT"
  fi
  if [ "${unconsumed:-0}" -gt 0 ]; then
    # Round 15 (item 1): notes stuck at the cycle limit: never exit 0 with them
    echo "exchange: $unconsumed pending folder(s) reached the cycle limit (stuck notes: summary stuck_notes;" \
         "re-arm with: python3 run_integrity.py pending --staging $STAGING --rearm '<note>')" >&2
    exit "$ZR_EXCHANGE_STUCK"
  fi
}

# zr_new_unit <kind> <ids> <review note or ""> <label> [review pass]  -> prints the unit folder
# The owner is $$: the pid of this long-lived script, not of the $(...) subshell.
# ZR_ALLOW_OTHER_SOURCES=1 (repair): accept Evidence from videos that are not known sources.
zr_new_unit() {
  local args=(unit-start --run-dir "$RUN_DIR" --kind "$1" --ids "$2" --owner-pid "$$" --label "$4")
  [ -n "$3" ] && args+=(--review-note "$3")
  [ -n "${5:-}" ] && args+=(--review-pass "$5")
  [ "${ZR_ALLOW_OTHER_SOURCES:-0}" = "1" ] && args+=(--allow-other-sources)
  python3 "$HERE/run_integrity.py" "${args[@]}"
}

# zr_agent <unit_dir> <prompt> <ids> <kind> [readonly] [journal dir]  -> agent exit code
# A read-only review agent keeps its journal and agent_result.json in its own folder,
# so the synthesis agent's end state stays the one that finalize reads.
zr_agent() {
  local unit_dir="$1" prompt="$2" ids="$3" kind="$4" ro="${5:-0}" rd="${6:-$1}"
  mkdir -p "$rd"
  printf '%s\n----\n' "$prompt" >>"$rd/prompt.txt"
  # ZR_AGENT_SCRIPT replaces the agent in offline tests; the default is deepseek_agent.py.
  local vf=""
  [ "$ro" = "1" ] && vf="$rd/verdict.json"
  ( cd "$HERE" && printf '%s' "$prompt" | ZR_RUN_DIR="$rd" ZR_SHADOW_DIR="$unit_dir/out" ZR_UNIT_DIR="$unit_dir" \
      ZR_UNIT="$(basename "$unit_dir")" ZR_UNIT_IDS="$ids" ZR_UNIT_KIND="$kind" ZR_AGENT_READONLY="$ro" \
      ZR_VERDICT_FILE="$vf" \
      timeout -k 30 "${AGENT_TIMEOUT:-1800}" python3 "${ZR_AGENT_SCRIPT:-$HERE/deepseek_agent.py}" ) >>"$rd/agent.log" 2>&1
}

# zr_fix_turns <unit_dir> <ids> <kind> <agent rc>  -> final agent rc
# The mechanical check runs; a failing note gets up to ZR_FIX_TURNS more agent turns.
zr_fix_turns() {
  local unit_dir="$1" ids="$2" kind="$3" rc="$4" turn report
  for turn in $(seq 1 "${ZR_FIX_TURNS:-2}"); do
    [ "$rc" -eq 0 ] || return "$rc"
    # Drafts outside the unit's final note list leave the shadow folder first (round 8).
    python3 "$HERE/run_integrity.py" discard-drafts --staging "$STAGING" --unit-dir "$unit_dir" --vault "$ZK_DIR" >/dev/null
    python3 "$HERE/run_integrity.py" check --staging "$STAGING" --unit-dir "$unit_dir" --vault "$ZK_DIR" >/dev/null
    [ $? -eq "$ZR_UNIT_QUARANTINED" ] || return 0
    report="$(python3 "$HERE/run_integrity.py" fix-report --staging "$STAGING" --unit-dir "$unit_dir" --vault "$ZK_DIR")"
    [ -n "$report" ] || return 0
    echo "[fix $turn] mechanical check failed for $(basename "$unit_dir"); one more agent turn"
    zr_limited_turn "$unit_dir" "FIX TURN $turn of ${ZR_FIX_TURNS:-2}. The mechanical check failed for the notes below \
(only notes of your final note list). Fix each note in place (same vault path; your writes go to your shadow \
folder): copy the exact words, numbers, units and periods from the transcript, or delete the claim, or delete the \
note with delete_draft (a new note) and record the passage with queue_mark.py --skip-passage. The transcript file \
and the cited passages of each note are below: read nothing else. delete_draft and move_file update your note list; \
do not run queue_mark.py --notes again. Change nothing else. Then stop.

$report" "$ids" "$kind"
    rc=$?
  done
  return "$rc"
}

# zr_limited_turn <unit_dir> <prompt> <ids> <kind>: one fix or review-repair turn with its own
# output limit (ZR_FIX_MAX_TOKENS, default 20000). A turn that hits that limit ends cleanly:
# the unit stays finished (fix-turn-limit.json) and the notes are judged as they are.
zr_limited_turn() {
  local rc
  DEEPSEEK_MAX_TOKENS="${ZR_FIX_MAX_TOKENS:-20000}" zr_agent "$1" "$2" "$3" "$4"
  rc=$?
  if [ "$rc" -eq 3 ]; then
    echo "{\"at\": \"$(date -u +%FT%TZ)\", \"max_tokens\": ${ZR_FIX_MAX_TOKENS:-20000}}" >"$1/fix-turn-limit.json"
    echo "[fix] $(basename "$1"): the turn hit its token limit (ZR_FIX_MAX_TOKENS); the notes are judged as they are"
    return 0
  fi
  return "$rc"
}

# zr_review_repair <unit_dir> <ids> <kind>: ONE targeted repair turn for the notes that the
# review rejected (round 8). The agent gets the rejected items, the transcript words and the
# marked passages; then the mechanical check and ONE new review run for the rewritten notes
# only. Notes that passed are never done again; a note rejected twice is quarantined.
zr_review_repair() {
  local unit_dir="$1" ids="$2" kind="$3" msg rc
  [ "${ZR_REVIEW_REPAIR:-1}" = "1" ] || return 0
  msg="$(python3 "$HERE/run_integrity.py" review-repair-message --staging "$STAGING" --unit-dir "$unit_dir" --vault "$ZK_DIR")"
  [ -n "$msg" ] || return 0
  zr_budget_ok || { echo "[review-repair] budget reached - no repair turn"; return 0; }
  echo "[review-repair] $(basename "$unit_dir"): one repair turn for the rejected notes"
  zr_limited_turn "$unit_dir" "$msg" "$ids" "$kind"
  rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  python3 "$HERE/run_integrity.py" check --staging "$STAGING" --unit-dir "$unit_dir" --vault "$ZK_DIR" >/dev/null
  zr_source_check "$unit_dir"
  return 0
}

# zr_fix_single <unit_dir>: single mode. Up to ZR_FIX_TURNS rounds; in each round one fix
# call per listed note that fails the mechanical check (at most ZR_FIX_TURNS calls per note).
zr_fix_single() {
  local unit_dir="$1" turn
  for turn in $(seq 1 "${ZR_FIX_TURNS:-2}"); do
    python3 "$HERE/run_integrity.py" check --staging "$STAGING" --unit-dir "$unit_dir" --vault "$ZK_DIR" >/dev/null
    [ $? -eq "$ZR_UNIT_QUARANTINED" ] || return 0
    # Over the budget, synth_call.py makes no call: it records the failing notes in
    # fix-pending.json, and finalize keeps them pending (reason budget-stop).
    echo "[fix $turn] $(basename "$unit_dir"): one fix call per failing note"
    python3 "$HERE/synth_call.py" fix --mode mech --staging "$STAGING" --vault "$ZK_DIR" --unit-dir "$unit_dir" \
      >>"$unit_dir/agent.log" 2>&1
  done
  return 0
}

# zr_review_repair_single <unit_dir>: single mode. One fix call per note that the review
# rejected, then the mechanical check and ONE new review of the rewritten notes.
zr_review_repair_single() {
  local unit_dir="$1" out
  [ "${ZR_REVIEW_REPAIR:-1}" = "1" ] || return 0
  out="$(python3 "$HERE/synth_call.py" fix --mode review --staging "$STAGING" --vault "$ZK_DIR" --unit-dir "$unit_dir")"
  printf '%s\n' "$out" >>"$unit_dir/agent.log"
  [ "$out" = "[]" ] && return 0
  python3 "$HERE/run_integrity.py" check --staging "$STAGING" --unit-dir "$unit_dir" --vault "$ZK_DIR" >/dev/null
  zr_source_check "$unit_dir"
  return 0
}

# zr_repair_single <force 0|1> <note>...: repair old notes, one call per batch (round 10).
# Notes without a transcript get `unsupported` without a call. Known notes go in batches
# per video (ZR_REPAIR_MAX_NOTES, ZR_REPAIR_MAX_INPUT_CHARS). A note with several source
# videos goes on to its next video with the bullets the last one did not hold. A note of
# unknown source is sent to its best candidate video only; the next candidate gets it only
# after a KEEP-NEEDS-REPAIR. Notes that no video takes are listed for the operator.
zr_repair_single() {
  local force="$1" plan state batch unit_dir rc frc rc_all=0 args note label
  shift
  plan="$RUN_DIR/repair-plan.json"
  state="$STAGING/repair_state.json"  # round 11 (E): persists across runs; a rerun resumes
  args=(repair-groups --staging "$STAGING" --vault "$ZK_DIR" --state "$state")
  [ "$force" = "1" ] && args+=(--force)
  python3 "$HERE/synth_call.py" "${args[@]}" "$@" >"$plan" || { echo "[repair] repair-groups failed" >&2; return 1; }
  while IFS= read -r note; do
    [ -n "$note" ] || continue
    zr_budget_check
    # Round 24: the owner's "mark": only the frontmatter keys verification: unsupported,
    # unsupported_reason, unsupported_marked (body unchanged; a refusal goes to the operator)
    echo "[repair] $note: no transcript for its source - marked unsupported (frontmatter only)"
    python3 "$HERE/run_integrity.py" note-unsupported --staging "$STAGING" --vault "$ZK_DIR" --note "$note" \
      --reason "source transcript missing" >/dev/null || rc_all=1
  done < <(python3 -c 'import json,sys; [print(n) for n in json.load(open(sys.argv[1]))["unsupported"]]' "$plan")
  python3 -c 'import json,sys; [print("[repair] skipped", s["note"], "(state", s["state"] + ")") for s in json.load(open(sys.argv[1]))["skipped"]]' "$plan"
  python3 -c 'import json,sys; [print("[repair] planned again (state needs-repair):", s["note"]) for s in json.load(open(sys.argv[1])).get("replanned", [])]' "$plan"
  while :; do
    zr_budget_check
    batch="$(python3 "$HERE/synth_call.py" next-batch --staging "$STAGING" --vault "$ZK_DIR" --state "$state")" || break
    [ -n "$batch" ] || break
    label="$(printf '%s' "$batch" | python3 -c 'import json,sys; b=json.load(sys.stdin); print(b["mode"] + "-" + b["vid"])')"
    unit_dir="$(zr_new_unit repair "" "" "repair-$label")" || return 1
    printf '%s' "$batch" >"$unit_dir/batch.json"
    python3 "$HERE/synth_call.py" repair --staging "$STAGING" --vault "$ZK_DIR" --unit-dir "$unit_dir" \
      --batch "$unit_dir/batch.json" --state "$state" >>"$unit_dir/agent.log" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] && zr_fix_single "$unit_dir"
    [ "$rc" -eq 0 ] && zr_source_check "$unit_dir"
    [ "$rc" -eq 0 ] && zr_review_repair_single "$unit_dir"
    zr_finalize "$unit_dir" "$rc" "" "" "repair: $label (run $RUN_ID)"
    frc=$?
    echo "[repair] $label: call rc=$rc, finalize rc=$frc"
    # V2: a unit that waits for a worker reply is not a failure (finalize 11, end waiting)
    # round 20: quarantined (10) or incomplete (11) units are recorded in the summary; they do
    # not stop the pass loop (the driver still exits 5 while requests wait)
    # W20 (round 21): a repair call that failed (call rc not 0, or agent end `error`) makes the
    # driver exit nonzero. Finalize 10 (quarantined) and 11 (waiting, or nothing to publish
    # for this candidate) are recorded in the summary and do not.
    if [ "$rc" -ne 0 ] || grep -q '"end": "error"' "$unit_dir/agent_result.json" 2>/dev/null \
        || { [ "$frc" -ne 0 ] && [ "$frc" -ne "$ZR_UNIT_QUARANTINED" ] && [ "$frc" -ne 11 ]; }; then
      rc_all=1
    fi
  done
  # Z-A2 (round 25): a note whose every bullet every planned video dropped is marked at the
  # end of its repair (every recorded source asked), or listed for the operator
  python3 "$HERE/run_integrity.py" mark-finished --staging "$STAGING" --vault "$ZK_DIR" >/dev/null || rc_all=1
  python3 -c 'import json,sys; [print("[repair] OPERATOR:", o["note"], "-", o["reason"]) for o in json.load(open(sys.argv[1]))["operator"]]' "$plan"
  # Z6 (round 23): every such note is also a needs_operator.json entry (exit 7)
  python3 "$HERE/run_integrity.py" note-operator --staging "$STAGING" --plan "$plan" >/dev/null || rc_all=1
  return "$rc_all"
}

# zr_source_check <unit_dir>: the source-check review of every shadow note that passed
# the mechanical check (and, in review mode, of the unchanged assigned note): one
# chat-completion call per note (review_call.py), no agent, no tools.
zr_source_check() {
  python3 "$HERE/review_call.py" --staging "$STAGING" --vault "$ZK_DIR" --unit-dir "$1" ||
    echo "[source-check] review_call.py failed for $(basename "$1")" >&2
}

# zr_finalize <unit_dir> <agent rc> <ids> <review note or ""> <commit message>
zr_finalize() {
  local args=(finalize --staging "$STAGING" --run-dir "$RUN_DIR" --unit-dir "$1" --agent-rc "$2"
              --ids "$3" --vault "$ZK_DIR")
  [ -n "$4" ] && args+=(--review-note "$4")
  [ "${ZR_GIT_COMMIT:-0}" = "1" ] && args+=(--commit "$5")
  python3 "$HERE/run_integrity.py" "${args[@]}"
}

# zr_synth_unit <ids> <prompt> <label> [kind]  -> finalize exit code (0 ok, 10 quarantined, 11 incomplete)
zr_synth_unit() {
  local ids="$1" prompt="$2" label="$3" kind="${4:-synth}" keep unit_dir rc frc
  zr_budget_check
  keep="$(python3 "$HERE/run_integrity.py" precheck --staging "$STAGING" --run-dir "$RUN_DIR" --ids "$ids")"
  rc=$?
  if [ "$rc" -eq "$ZR_UNIT_QUARANTINED" ]; then
    echo "[$label] held $ids: transcript asr_quality is degraded (no agent run)"
    return "$ZR_UNIT_OK"
  elif [ "$rc" -ne 0 ]; then
    echo "[$label] precheck failed for $ids (rc=$rc) - no agent run" >&2
    return "$rc"
  fi
  unit_dir="$(zr_new_unit "$kind" "$keep" "" "$label")" || return 1
  if [ "$(python3 "$HERE/synth_call.py" mode --staging "$STAGING" --ids "$keep")" = "single" ]; then
    # Single-call synthesis (round 9, default for video units): the harness makes the call,
    # parses the reply and writes the notes; fix and repair are single calls too.
    python3 "$HERE/synth_call.py" synth --staging "$STAGING" --vault "$ZK_DIR" --unit-dir "$unit_dir" \
      --ids "$keep" >>"$unit_dir/agent.log" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] && zr_fix_single "$unit_dir"
    [ "$rc" -eq 0 ] && zr_source_check "$unit_dir"
    [ "$rc" -eq 0 ] && zr_review_repair_single "$unit_dir"
  else
    zr_agent "$unit_dir" "$prompt" "$keep" "$kind"
    rc=$?
    zr_fix_turns "$unit_dir" "$keep" "$kind" "$rc"
    rc=$?
    [ "$rc" -eq 0 ] && zr_source_check "$unit_dir"
    [ "$rc" -eq 0 ] && { zr_review_repair "$unit_dir" "$keep" "$kind"; rc=$?; }
  fi
  tail -3 "$unit_dir/agent.log"
  zr_finalize "$unit_dir" "$rc" "$keep" "" "zettel: $kind $keep (run $RUN_ID)"
  frc=$?
  echo "[$label] $ids: agent rc=$rc, finalize rc=$frc (0 ok, 10 quarantined, 11 incomplete)"
  return "$frc"
}

# zr_pending_reviews [reason]: check, review and finalize notes that wait in
# $STAGING/pending-review/ (from an earlier run, or link-waiting notes of this run).
# Every driver runs it at start; zr_run_summary runs it for link-waiting at the end.
zr_pending_reviews() {
  [ "${ZR_RUN_OWNER:-0}" -eq 1 ] || return 0
  local unit_dir args=(pending-units --staging "$STAGING" --run-dir "$RUN_DIR" --owner-pid "$$")
  [ -n "${1:-}" ] && args+=(--reason "$1")
  zr_budget_ok || { echo "[pending] budget reached - pending notes wait for the next run" >&2; return 0; }
  while IFS= read -r unit_dir; do
    [ -n "$unit_dir" ] || continue
    if ! zr_budget_ok; then
      echo "[pending] budget reached - $(basename "$unit_dir") stays pending" >&2
      zr_finalize "$unit_dir" 0 "" "" "zettel: pending notes (run $RUN_ID)" >/dev/null
      continue
    fi
    echo "[pending] check and review of $(basename "$unit_dir")"
    if [ -f "$unit_dir/fix-pending.json" ]; then
      # A fix call that the budget stopped in an earlier run (P7): make it now.
      zr_fix_single "$unit_dir"
      zr_source_check "$unit_dir"
      zr_review_repair_single "$unit_dir"
    else
      zr_source_check "$unit_dir"
      # Round 11: a note that the pending review rejects gets its one review-fix call
      # (single mode), then one new review.
      if grep -q '"synth_mode": "single"' "$unit_dir/unit.json" 2>/dev/null; then
        zr_review_repair_single "$unit_dir"
      fi
    fi
    zr_finalize "$unit_dir" 0 "" "" "zettel: reviewed pending notes (run $RUN_ID)"
  done < <(python3 "$HERE/run_integrity.py" "${args[@]}")
}

# zr_review_unit <note> <pass> <prompt> <label>
# The source-check pass runs no editing agent: only the per-note review agent of
# zr_source_check. finalize returns 11 (the pass stays open) when no recorded verdict exists.
zr_review_unit() {
  local note="$1" pass="$2" prompt="$3" label="$4" unit_dir rc frc
  zr_budget_check
  unit_dir="$(zr_new_unit review "" "$note" "$label" "$pass")" || return 1
  if [ "$pass" = "source-check" ]; then
    rc=0
  else
    zr_agent "$unit_dir" "$prompt" "" review
    rc=$?
    zr_fix_turns "$unit_dir" "" review "$rc"
    rc=$?
  fi
  [ "$rc" -eq 0 ] && zr_source_check "$unit_dir"
  tail -3 "$unit_dir/agent.log"
  zr_finalize "$unit_dir" "$rc" "" "$note" "review $pass: $note (run $RUN_ID)"
  frc=$?
  case "$frc" in
    "$ZR_UNIT_OK") python3 "$HERE/review_queue.py" "done" --pass "$pass" --file "$note" ;;
    "$ZR_UNIT_QUARANTINED")
      python3 "$HERE/review_queue.py" fail --pass "$pass" --file "$note" \
        --reason "verification failed in run $RUN_ID; see $STAGING/quarantine/$RUN_ID" ;;
    *) python3 "$HERE/review_queue.py" release --pass "$pass" --file "$note" ;;
  esac
  echo "[$label] $pass $note: agent rc=$rc, finalize rc=$frc"
  return "$frc"
}

# zr_clustering_unit <prompt>: MOCs and Home.md only (no source-check review).
zr_clustering_unit() {
  local unit_dir rc
  zr_budget_check
  unit_dir="$(zr_new_unit clustering "" "" clustering)" || return 1
  zr_agent "$unit_dir" "$1" "" clustering
  rc=$?
  zr_finalize "$unit_dir" "$rc" "" "" "review clustering: MOCs (run $RUN_ID)"
}

# zr_report_runs <staging> <since-epoch>: print each run summary written since the given
# time, and return 1 when a run quarantined notes, failed a commit, or ended units by
# limit, kill or error.
zr_report_runs() {
  python3 - "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
staging, since = Path(sys.argv[1]), float(sys.argv[2])
bad = 0
for p in sorted(staging.glob("runs/*/summary.json")):
    if p.stat().st_mtime < since:
        continue
    s = json.loads(p.read_text())
    keys = ("notes_created", "notes_merged", "notes_quarantined", "runs_ended_by_limit",
            "runs_ended_by_timeout", "runs_ended_by_kill_or_crash", "runs_ended_by_error")
    print(f"run {s['run_id']} ({s.get('kind')}): " + ", ".join(f"{k}={s.get(k, 0)}" for k in keys)
          + f", pending_review={len(s.get('notes_pending_review', []))}, commit_failed={len(s.get('commit_failed', []))}")
    print(f"  summary: {p}")
    if s.get("pending_for_operator"):
        print(f"  OPERATOR: {len(s['pending_for_operator'])} pending folder(s) waited 5 cycles; see "
              "pending_for_operator in the summary. Re-arm one note with: "
              "python3 run_integrity.py pending --staging <staging> --rearm '<note path>'")
        bad = 1
    if s.get("videos_without_notes"):
        print(f"  ATTENTION: {len(s['videos_without_notes'])} video(s) ended without a note: "
              + "; ".join(f"{v['ids']} {v['outcome']}" for v in s["videos_without_notes"])[:600])
        bad = 1
    if s.get("notes_for_operator"):
        print(f"  OPERATOR: {len(s['notes_for_operator'])} note(s) where repair found no supporting passage "
              "(verification: needs-repair, repair_note)")
        bad = 1
    if s.get("notes_quarantined") or s.get("runs_ended_by_kill_or_crash") or s.get("runs_ended_by_error") \
            or s.get("commit_failed"):
        bad = 1
sys.exit(bad)
PY
}
