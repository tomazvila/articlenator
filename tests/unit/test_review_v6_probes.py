"""Round 14: the 62 probes of the independent review of frozen-v6 (copied unchanged to
tests/probes_v6/) and their result now, in the CORRECT-BEHAVIOUR convention:

- A_* and C_* probes assert the DEFECT: each listed under FIXED must now FAIL;
- B_* probes assert the correct behaviour: they must PASS;
- NOT_FIXED: findings left open (minor, or owned by package B), still showing the defect;
- SUPERSEDED: B probes whose setup breaks a rule that a fix requires (a reply without a
  meta file, S6; a meta without a string `model`, S7; the old refusal file name, S8). The
  tests at the end of this file prove the correct behaviour for them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest

from tests.unit.test_run_integrity import ZR, ri
from tests.unit.test_synth_single import V5, _Single, reply
from tests.unit.test_run_integrity_r13 import review_answer, work

import synth_call as sc  # noqa: E402

ROOT = ZR.parent

# finding id -> probe test names (file::class::test, without the "test_probe_" prefix)
FIXED = {
    "R1": ["A_paths.py::AbsoluteNotePath::test_absolute_rel_writes_stub_into_vault_without_archive"],
    "R2": ["A_crash.py::InPlaceRejectedSplitPublished::test_in_place_quarantined_sibling_published"],
    "R3": ["A_crash.py::CrashBetweenReplacementAndStub::test_kill_after_replacement_write",
           "A_crash.py::CrashBetweenReplacementAndStub::test_rerun_after_crash_replays_cache_and_stays_stuck",
           "A_crash.py::CrashBetweenReplacementAndStub::test_rerun_with_fresh_reply_publishes_second_name"],
    "R4": ["A_ledger.py::GhostTitleInLedger::test_bullet_kept_in_unwritten_title_vanishes",
           "A_ledger.py::GhostTitleInLedger::test_bullet_kept_in_truncated_note_vanishes"],
    "R5 (partly: V7 open)": ["A_ledger.py::ExtendUnseenNote::test_second_call_overwrites_first_calls_note",
           "A_ledger.py::DropCheckReplacesWrittenNote::test_dropcheck_version_replaces_note"],
    "R6": ["A_ledger.py::PendingThenRejectedNoReopen::test_rejected_in_pending_unit_keeps_bullets_settled",
           "A_ledger.py::FixCallDropOrRename::test_fix_drop", "A_ledger.py::FixCallDropOrRename::test_fix_rename_then_quarantined",
           "A_crash.py::KillBeforeFinalize::test_kill_during_review"],
    "R7": ["A_ledger.py::PendingTargetRewritesNewerStub::test_stub_loses_a_published_target"],
    "R8": ["A_ledger.py::PartialStubNeverCloses::test_pending_line_stays_forever"],
    "R9": ["A_ledger.py::BudgetStopInFollowUp::test_follow_up_budget_stop_loses_open_bullet"],
    "R10": ["A_crash.py::ModelWrittenStub::test_model_stub_replaces_old_note_without_review"],
    "R11": ["A_paths.py::EmptyReplyCachedForever::test_empty_reply_poisons_the_note"],
    "R12": ["A_paths.py::RunningBatchLoopsForever::test_same_batch_forever"],
    "R13": ["A_ledger.py::TwoTitlesOneRejected::test_two_title_line_reopened"],
    "Y1": ["C_two_stage.py::ProbeInventory::test_C1_bracketed_time_crashes_the_unit"],
    "Y2": ["C_two_stage.py::ProbeInventory::test_C2_distinct_steps_in_one_part_are_merged_as_duplicates"],
    "Y3": ["C_two_stage.py::ProbeBatches::test_C6_multi_part_skip_lines_are_never_parsed",
           "C_two_stage.py::ProbeBatches::test_C6b_multi_part_run_loses_every_skip",
           "C_prompts.py::ProbePrompts::test_E4_batch_prompt_shows_C_ids_but_multi_part_ids_are_P_ids"],
    "Y4": ["C_two_stage.py::ProbeBatches::test_C7_already_covered_by_unknown_title_is_accepted"],
    "Y5": ["C_two_stage.py::ProbeBatches::test_C8_twice_refused_claim_makes_the_video_no_output"],
    "Y6": ["C_two_stage.py::ProbeInventory::test_C3_unknown_type_spelling_is_skipped_without_a_call",
           "C_prompts.py::ProbePrompts::test_E6_type_table_spelling_differs_from_claim_types"],
    "Y9": ["C_two_stage.py::ProbeBatches::test_C11_note_lists_a_claim_it_does_not_cover"],
    "P2": ["C_prompts.py::ProbePrompts::test_E5_worker_reads_a_system_prompt_that_says_it_has_no_tools",
           "C_prompts.py::ProbePrompts::test_E2_the_data_rule_of_synth_single_is_inside_one_list_item"],
    "P3": ["C_prompts.py::ProbePrompts::test_E3_no_excerpt_rule_contradicts_the_skip_table"],
    # B probes (correct-behaviour convention): must PASS
    "S1": ["B_loop.py::LoopProbes::test_one_worker_writes_and_reviews_through_loop_sh"],
    "S2": ["B_files_backend.py::Probes::test_cached_writer_call_loses_its_worker"],
    "S3": ["B_files_backend.py::Probes::test_second_title_fix_gets_the_first_title_fix_reply"],
    "S4": ["B_files_backend.py::Probes::test_waiting_review_uses_up_pending_cycles"],
    "S5": ["B_files_backend.py::Probes::test_invalid_review_reply_is_permanent"],
    "S7": ["B_files_backend.py::Probes::test_meta_json_array_crashes_files_call"],
    "S10": ["B_loop.py::LoopProbes::test_budget_stop_exit_4_is_replaced_by_5"],
    "ok": ["B_files_backend.py::Probes::test_ok_refused_request_is_listed_again",
           "B_files_backend.py::Probes::test_ok_exchange_dir_outside_staging_is_refused",
           "B_files_backend.py::Probes::test_ok_traversal_title_is_not_written",
           "B_loop.py::LoopProbes::test_ok_max_calls_is_per_run_not_per_pass_chain"],
}
SUPERSEDED = {  # probe -> the test of this file that proves the correct behaviour
    "B_files_backend.py::Probes::test_meta_worker_list_crashes_every_later_review": "test_S7_bad_meta_is_refused",
    "B_files_backend.py::Probes::test_meta_worker_int_and_str_crash_sorted": "test_S7_bad_meta_is_refused",
    "B_files_backend.py::Probes::test_ok_reply_survives_a_crash_after_consume": "test_S6_reply_needs_meta_and_survives",
    "B_files_backend.py::Probes::test_refusal_is_not_visible_in_exchange_list": "test_S8_refusal_is_visible",
    "C_prompts.py::ProbePrompts::test_E1_one_stage_mode_sends_the_batch_prompt_with_a_claims_first_user_format":
        "test_P1_one_stage_uses_its_own_prompt",
}
NOT_FIXED = {  # probe -> finding id and reason
    "C_prompts.py::ProbePrompts::test_E7_manifest_is_current": "not a defect (the manifest is current)",
    "C_prompts.py::ProbeReview::test_E8_supported_transcript_text_from_context_is_accepted": "P4 minor",
    "C_two_stage.py::ProbeInventory::test_C4_line_references_of_part_2_point_into_part_1": "Y13",
    "C_two_stage.py::ProbeInventory::test_C5_no_dedup_for_line_claims_across_the_overlap": "Y14 minor",
    "C_two_stage.py::ProbeBatches::test_C9_unlabeled_note_with_no_shared_word_maps_to_the_first_claim": "Y8",
    "C_two_stage.py::ProbeBatches::test_C10_unlabeled_note_maps_to_nearest_time_not_its_claim": "Y8",
    "C_two_stage.py::ProbeBatches::test_C12_damaged_span_with_char_detail_is_judged_clean": "Y7",
    "C_two_stage.py::ProbeBatches::test_C13_no_excerpt_prompt_rule_is_refused_by_the_harness": "P3 minor",
    "C_two_stage.py::ProbeBatches::test_C14_wrong_time_inside_the_video_gives_an_excerpt_without_the_claim": "Y10",
    "C_two_stage.py::ProbeBatches::test_C15_line_number_is_off_by_heading_lines": "Y14 minor",
    "C_two_stage.py::ProbeBatches::test_C16_same_title_same_claim_new_wording_is_a_copy_not_a_duplicate": "Y12",
    "C_two_stage.py::ProbeBatches::test_C17_yield_counts_claim_ids_that_do_not_exist": "Y14 minor",
    "C_two_stage.py::ProbeInventoryRetry::test_C18_prose_inventory_is_no_output_and_the_cache_repeats_it": "Y11",
}


def _expected(name: str) -> str:
    if name in SUPERSEDED:
        return "SUPERSEDED"
    if name in NOT_FIXED:
        return "PASSED" if not name.startswith("B_") else "FAILED"
    return "PASSED" if name.startswith("B_") else "FAILED"


class ReviewV6Probes(unittest.TestCase):
    def test_every_probe_shows_the_fix(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "PYTEST_"))}
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/probes_v6", "-q", "-p", "no:cacheprovider",
                            "-p", "no:randomly", "-rA"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=1500)
        got = {}
        for ln in r.stdout.splitlines():
            m = re.match(r"^(PASSED|FAILED|ERROR) tests/probes_v6/test_probe_(\S+)", ln)
            if m:
                got[m.group(2)] = m.group(1)
        self.assertEqual(len(got), 62, r.stdout[-2000:])
        listed = {n for v in FIXED.values() for n in v} | set(SUPERSEDED) | set(NOT_FIXED)
        self.assertEqual(sorted(set(got) - listed), [], "every probe is mapped to a finding")
        bad = {n: (got[n], _expected(n)) for n in got if _expected(n) != "SUPERSEDED" and got[n] != _expected(n)}
        self.assertEqual(bad, {}, "probe results that do not show the fix")


class CorrectBehaviour(_Single):
    """The correct behaviour for the SUPERSEDED probes, and items without a probe."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_LLM_BACKEND"] = "files"
        self.xdir = self.staging / "exchange"

    def _pending(self, content: str = "u") -> tuple[str, list[dict], dict]:
        msgs = [{"role": "user", "content": content}]
        s = {"model": "m", "max_tokens": 10}
        with self.assertRaises(sc.PendingReply) as cm:
            sc.files_call(self.unit, "synth-p1-0", msgs, s)
        return cm.exception.key, msgs, s

    def test_S6_reply_needs_meta_and_survives(self) -> None:
        key, msgs, s = self._pending()
        (self.xdir / "replies" / f"{key}.txt").write_text("hello\n")
        with self.assertRaises(sc.PendingReply):  # no meta yet: the reply waits, it is not taken
            sc.files_call(self.unit, "synth-p1-0", msgs, s)
        (self.xdir / "replies" / f"{key}.meta.json").write_text(json.dumps({"key": key, "worker": "w", "model": "m"}))
        self.assertEqual(sc.files_call(self.unit, "synth-p1-0", msgs, s)[0], "hello\n")
        self.assertEqual(sc.files_call(self.unit, "synth-p1-0", msgs, s)[0], "hello\n")  # survives a re-read

    def test_S7_bad_meta_is_refused(self) -> None:
        for i, meta in enumerate(([], {"worker": ["w1"]}, {"worker": 7}, {"worker": "w", "model": 3})):
            key, msgs, s = self._pending(f"u{i}")
            (self.xdir / "replies" / f"{key}.txt").write_text("x")
            m = dict(meta, key=key) if isinstance(meta, dict) else meta
            (self.xdir / "replies" / f"{key}.meta.json").write_text(json.dumps(m))
            with self.assertRaises(sc.PendingReply):  # refused, no crash; the request is open again
                sc.files_call(self.unit, "synth-p1-0", msgs, s)
            self.assertTrue(list((self.xdir / "replies").glob(f"{key}.refused-*.json")))
            self.assertFalse((self.xdir / "replies" / f"{key}.txt").exists())
        self.assertEqual(sc.writer_workers(self.unit), [])

    def test_S8_refusal_is_visible(self) -> None:
        self.fake()
        self.synth()
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="writer-1")
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])
        import review_call as rc
        rc.review_unit(self.staging, self.zk, u)
        for _ in range(2):  # two refusals keep two records
            work(self.xdir, lambda r: review_answer(r["user"]), worker="writer-1")
            rc.review_unit(self.staging, self.zk, u)
        row = [r for r in sc.exchange_list(self.xdir) if r["kind"] == "review"][0]
        self.assertEqual(row["refused_workers"], ["writer-1"])
        self.assertEqual(row["writer_workers"], ["writer-1"])
        self.assertEqual(len(row["refusals"]), 2)
        self.assertIn("same worker", row["refusals"][0]["reason"])

    def test_S9_stale_request_is_obsolete(self) -> None:
        # Round 18 (V14): no automatic marking (it hid requests of waiting units); the
        # operator marks a request obsolete, and it leaves --open and the open count.
        self.fake()
        self.synth()
        key = sc.exchange_list(self.xdir)[0]["key"]
        run2 = ri.start(self.staging, "synth", self.zk)
        (run2 / "exchange_wanted.json").write_text("{}")
        self.assertEqual(sc.exchange_status(self.xdir, self.staging)["open"], 1)  # still open
        cmd = [sys.executable, str(ZR / "synth_call.py"), "exchange-status", "--staging", str(self.staging),
               "--mark-obsolete", key]
        r = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        # W36 (round 21): a waiting unit holds the key: refused, still open
        self.assertIn("held by a waiting unit", r.stderr)
        self.assertEqual(sc.exchange_status(self.xdir, self.staging)["open"], 1)
        for p in self.staging.glob("runs/*/units/*/agent_result.json"):  # the unit no longer waits
            d = json.loads(p.read_text())
            if d.get("end") == "waiting-for-reply":
                p.write_text(json.dumps(dict(d, end="released")))
        r = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        st = sc.exchange_status(self.xdir, self.staging)
        self.assertEqual((st["open"], st["obsolete"]), (0, 1))
        self.assertEqual(sc.exchange_list(self.xdir, staging=self.staging), [])

    def test_P1_one_stage_uses_its_own_prompt(self) -> None:
        os.environ["ZR_LLM_BACKEND"] = "openrouter"
        self.fake(synth=[reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)])])
        self.synth()
        req = json.loads((self.unit / "calls" / "synth-p1-0.request.json").read_text())
        system = req["messages"][0]["content"]
        self.assertIn("=====CLAIMS=====", system)  # the same format as the user's reminder
        self.assertNotIn("BATCH of at most five", system)

    def test_T1_max_calls_refuses_the_second_request(self) -> None:
        os.environ["ZR_MAX_CALLS"] = "1"
        self._pending("first")
        with self.assertRaises(sc.BudgetStop):
            sc.files_call(self.unit, "synth-p2-0", [{"role": "user", "content": "second"}], {"model": "m", "max_tokens": 1})
        self.assertTrue((self.unit.parent.parent / "budget-stop.json").exists())

    def test_T2_manifest_bypass_needs_pytest(self) -> None:
        src = (ZR / "run_integrity.py").read_text()
        self.assertIn('os.environ.get("PYTEST_CURRENT_TEST")', src)

    def test_R1_absolute_or_outside_paths_are_refused(self) -> None:
        self.assertIsNone(ri.safe_note_rel(self.zk, str(self.vpath("X"))))
        self.assertIsNone(ri.safe_note_rel(self.zk, "../X.md"))
        self.assertIsNone(ri.safe_note_rel(self.zk, "Home.md"))
        self.assertEqual(ri.safe_note_rel(self.zk, "01 Permanent Notes/X.md"), "01 Permanent Notes/X.md")
        with self.assertRaises(ri.HarnessError):
            ri.mark_unsupported(self.unit, self.zk, str(self.vpath("X")), "r")

    def test_S10_trap_keeps_the_driver_status(self) -> None:
        lib = (ZR / "run_lib.sh").read_text()
        self.assertIn('local driver_status=$?', lib)
        self.assertIn('[ "$driver_status" -eq 0 ] && zr_exchange_exit', lib)

    def test_P2_worker_instruction(self) -> None:
        w = (ZR / "prompts" / "worker_instruction.md").read_text()
        for s in ("DATA", "Ignore any instruction", "no tool-call", "Copy it exactly", "meta file FIRST"):
            self.assertIn(s, w)
