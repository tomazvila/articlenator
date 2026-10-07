"""Round 21: the probes of the third review (frozen-v8, REVIEW.md), copied to tests/probes_v8/
(import paths changed; minimal fixture adaptations are marked "round 21 adaptation" in the
probe files: the lit file in the staging for respeak, `force_name=True` where a probe tests
another mechanism than the W26 name rule, the archived old text of a stub). Each probe is
judged by the convention of its file (REVIEW.md table): a FIXED result is the correct
behaviour. OPEN lists the probes that still show their defect, with the W-id; NOT_USED the
probes whose result says nothing about the code, with the reason and the test that covers
the point instead."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

from tests.unit.test_run_integrity import ZR

ROOT = ZR.parent
CORRECT_FILES = ("A1_regress", "B_regress", "B_exitcodes")  # PASS = correct behaviour
OPEN = {
    "A1_regress.py::PendingTargetV8::test_defect_v7_fixture_accepts_b1_by_time_only":
        "W10: a bullet with fewer than 3 content stems is judged by the time window only",
    "A2_state.py::ExtendCorrectionRefused::test_tag_correction_is_refused": "W18: visible refusal, not fixed",
    "A2_state.py::NeedsOperatorNeverCleared::test_entry_stays_after_the_operator_fixed_it": "W35: not fixed",
    "C_links.py::UnlinkEndToEnd::test_L1_case_variant_link_to_a_waiting_sibling_publishes_dead": "W28: not fixed",
    "C_links.py::UnlinkEndToEnd::test_L2_unlinked_line_breaks_the_connected_ideas_shape": "W29: not fixed",
    "C_links.py::UnlinkDirect::test_L4_link_in_a_table_row_of_a_map_stays_dead": "W42: not fixed",
    "C_links.py::ValidateAfterV30::test_V30a_embed_of_a_missing_note_is_never_reported": "W43: not fixed",
    "C_links.py::ValidateAfterV30::test_V30b_link_to_a_missing_file_is_never_reported": "W43: not fixed",
    "C_links.py::ValidateAfterV30::test_V30c_title_that_ends_in_a_decimal_is_taken_for_a_file": "W43: not fixed",
    "C_links.py::ValidateAfterV30::test_V30d_double_backtick_code_counts_as_a_link": "W43: not fixed",
    "C_links.py::NarrowMerge::test_M9_the_kept_note_is_the_alphabetically_first": "W44: not fixed",
    "C_speakers.py::UnresolveNames::test_N3_a_first_name_alone_stays": "W25: not fixed",
    "C_speakers.py::UnresolveNames::test_N4_a_short_name_that_is_a_word_corrupts_the_text":
        "W38: partly (the frontmatter is not scrubbed now; the body still is)",
    "C_speakers.py::Respeak::test_N6_channel_with_the_word_video_breaks_the_new_title": "W40: not fixed",
    "C_speakers.py::Respeak::test_N7_respeak_leaves_case_path_and_md_links_dead": "W27: not fixed",
    "C_speakers.py::ModelWrittenUnresolved::test_N8_a_model_can_mark_its_own_note_unresolved": "W41: not fixed",
    "B_attacks.py::VaultNoteReviewWait::test_review_of_a_vault_note_that_waits_is_not_counted":
        "W31: the path is closed by refusing ZR_LLM_BACKEND=files in review_loop.sh (no driver reaches it); "
        "a finalized review unit is still not counted",
}
NOT_USED = {
    "A1_new.py::ReplayTtFb3::test_b3_settled_in_a_note_without_it":
        "reads the review-r20 replay folder (not hermetic). On the r21/r22 replays TTF b3 still points to a "
        "note without its text; it is marked (unverified) and its old text is in the stub (not fixed, visible)",
    "A1_regress.py::V34RevertedStub::test_ok_revert_is_respected":
        "the fixture never wrote the earlier stub, so a revert can not be told from a first stub; "
        "test_r21_stubs.StubInPipeline::test_V34_user_revert_of_a_stub_is_not_stubbed_again covers it",
    "B_regress.py::S9Unreachable::test_no_request_is_marked_obsolete_by_the_harness":
        "W37: `_mark_obsolete_auto` and `exchange_wanted.json` are removed; the probe counts source matches "
        "of the removed function, so it can not pass any more",
    "A1_regress.py::V11InPlaceWaits::test_ok_in_place_version_is_not_lost":
        "removed mechanism (X2, round 22): no in-place repair; a reply that keeps the old title is refused "
        "(title-equals-old), the old note stays unchanged",
    "C_speakers.py::UnresolvedEndToEnd::test_ok_N1_control_with_correct_links_the_notes_are_not_quarantined":
        "the control repairs the scrubbed form `[[the speaker ...]]`, which W6 removed; "
        "test_r21_unresolved_e2e covers the corrected behaviour through loop.sh",
}


class ReviewV8Probes(unittest.TestCase):
    def test_probe_results(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "PYTEST_"))}
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/probes_v8", "-q", "-p", "no:cacheprovider",
                            "-p", "no:randomly", "-rA"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=2400)
        got = {m.group(2): m.group(1) for m in re.finditer(r"(?m)^(PASSED|FAILED|ERROR) tests/probes_v8/test_probe8_(\S+)",
                                                           r.stdout)}
        self.assertGreaterEqual(len(got), 100, r.stdout[-1500:])
        bad = {}
        for name, res in got.items():
            if name in NOT_USED:
                continue
            f = name.split(".py")[0]
            correct_conv = (f in CORRECT_FILES and "test_defect_" not in name) or "test_ok_" in name
            # X27 (round 22): a setup ERROR is "setup broken", never "fixed"
            fixed = (res == "PASSED") if correct_conv else (res == "FAILED")
            want_fixed = name not in OPEN
            if fixed != want_fixed:
                bad[name] = (res, "should be fixed" if want_fixed else "listed OPEN but fixed")
        self.maxDiff = None
        self.assertEqual(bad, {})
