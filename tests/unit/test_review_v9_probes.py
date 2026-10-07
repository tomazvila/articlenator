"""Round 22: the probes of the fourth review (frozen-v9, REVIEW.md), copied to tests/probes_v9/
(import paths changed only). Each probe is judged by the convention of its file (REVIEW.md
table): a FIXED result is the correct behaviour. A setup ERROR never counts as fixed (X27).
OPEN lists the probes that still show their defect; SUPERSEDED the designed results that
the round-22 decisions replace, with the reason and the test that covers the new rule."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

from tests.unit.test_run_integrity import ZR

ROOT = ZR.parent
OPEN = {
    "A_shapes.py::LostTextPassesTheCheck::test_A10_frontmatter_is_not_carried":
        "X22: not carried in the stub (the archive keeps it); B documents the omission in contract section 16",
    "A_shapes.py::AlteredOrMovedText::test_A6_nested_bullets_are_joined_into_one_claim_with_one_status":
        "X18: latent Details shapes (0 of 476 real notes); the text is carried, the entry shape differs",
    "A_shapes.py::AlteredOrMovedText::test_A6b_star_marker_becomes_dash": "X18 (as A6)",
    "A_shapes.py::AlteredOrMovedText::test_A6c_plus_and_numbered_Details_items_get_no_status": "X18 (as A6)",
    "A_shapes.py::AlteredOrMovedText::test_A6d_line_separator_in_a_bullet_cuts_the_bullet": "X18 (as A6)",
    "C_regress.py::DefectReplayTtFb3R21::test_b3_points_to_a_note_without_it":
        "W1/X16 on the r21 replay data: b3 points to a note without its text; marked (unverified), text in the stub",
}
SUPERSEDED = {
    "C_six.py::W19OwnWaitsOnly::test_repair_loop_keeps_the_synthesis_wait_and_does_not_exit_0":
        "X12 (round 22): a driver's exit code counts only its own kind's waits; repair_loop.sh ends 0 (or 7) and "
        "keeps the synthesis wait (not released: W19 holds); the preflight lists it as information",
    "D_exitcodes.py::RepairLoopSh::test_designed_waiting_repair_hides_nothing_after_another_driver": "as W19OwnWaitsOnly",
}


def _conv(name: str) -> str:
    f = name.split(".py")[0]
    cls = name.split("::")[1] if "::" in name else ""
    if "test_ok_" in name or "test_designed_" in name or f in ("A_realnotes", "C_six"):
        return "correct"
    if f == "C_regress":
        return "correct" if cls.startswith("Ok") else "defect"
    return "defect"


class ReviewV9Probes(unittest.TestCase):
    def test_probe_results(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "PYTEST_"))}
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/probes_v9", "-q", "-p", "no:cacheprovider",
                            "-p", "no:randomly", "-rA"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=2400)
        got = {m.group(2): m.group(1) for m in re.finditer(r"(?m)^(PASSED|FAILED|ERROR) tests/probes_v9/test_probe9_(\S+)",
                                                           r.stdout)}
        self.assertGreaterEqual(len(got), 85, r.stdout[-1500:])
        bad = {}
        for name, res in got.items():
            if name in SUPERSEDED:
                continue
            fixed = (res == "PASSED") if _conv(name) == "correct" else (res == "FAILED")
            if fixed == (name in OPEN):
                bad[name] = (res, "listed OPEN but fixed" if fixed else "should be fixed")
        self.maxDiff = None
        self.assertEqual(bad, {})
