"""Round 23: the probes of the fifth review (frozen-v10, REVIEW.md), copied to tests/probes_v10/
(import paths changed only; the shared driver helpers commit the zettelkasten folder before
`repair_loop.sh`, a round-23 adaptation marked in tests/probes_v8 and tests/probes_v9: the
preflight now needs a committed git vault, checklist step 2). Each probe is judged by the
convention of its file (REVIEW.md table). A setup ERROR never counts as fixed. OPEN lists
the probes that still show their defect; SUPERSEDED the documented results that a round-23
decision replaces, with the reason."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

from tests.unit.test_run_integrity import ZR

ROOT = ZR.parent
OPEN = {
    "A_misc.py::RenameChain::test_defect_rename_back_loop_resolves_to_a_title_that_is_gone": "Z24: not fixed",
    "A_misc.py::RenameChain::test_defect_reused_title_resolves_to_the_old_rename": "Z24: not fixed",
    "A_regress.py::X6ArchiveLookup::test_defect_without_planned_sha_a_respeak_or_heal_archive_counts_as_newest":
        "Z26: not fixed (plan notes are safe: the planned sha wins, and Z4 binds the archive to the note)",
    "B_twostate.py::VersionSuffixArchiveCollision::test_other_note_is_taken_as_archive": "Z26 (as above)",
    "A_regress.py::W40W27RespeakAdapted::test_defect_N6_channel_with_the_word_video_breaks_the_new_title": "W40: not fixed",
    "A_regress.py::W40W27RespeakAdapted::test_defect_N7_respeak_leaves_case_path_and_md_links_dead": "W27: not fixed",
    "B_preflight.py::PreflightForceLeavesNoRecord::test_forced_findings_are_not_recorded":
        "Z28: partly: a `preflight-forced` provenance record is written; the probe asks for an operator-work entry",
}
SUPERSEDED = {  # round 24: the owner's "mark" is a defined, checked state (state 3 of contract section 16)
    "A_misc.py::NoTranscriptNoteSingleMode::test_defect_no_transcript_old_note_is_rewritten_in_place_with_status_lines":
        "round 24: the mark is the owner's chosen outcome: only verification/unsupported_reason/unsupported_marked, "
        "body byte-identical (test_r24.MarkWrite); validate rejects any other change",
    "C_exitcodes.py::AllBulletsDroppedExit0::test_defect_dropped_note_exit_0_but_repair_status_unlisted":
        "round 24: a known-source note whose every video dropped every bullet is marked (state 3) and listed in "
        "notes_marked_unsupported and repair-status (`marked-unsupported`); not operator work, so exit 0",
}


def _conv(name: str) -> str:
    f = name.split(".py")[0]
    cls = name.split("::")[1] if "::" in name else ""
    test = name.split("::")[-1]
    if test.startswith(("test_ok_", "test_documented_")):
        return "correct"
    if test.startswith("test_defect_"):
        return "defect"
    if f == "B_title":
        return "correct" if cls.startswith("Holds") else "defect"
    return "defect"


class ReviewV10Probes(unittest.TestCase):
    def test_probe_results(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "PYTEST_"))}
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/probes_v10", "-q", "-p", "no:cacheprovider",
                            "-p", "no:randomly", "-rA"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=2400)
        got = {m.group(2): m.group(1) for m in re.finditer(r"(?m)^(PASSED|FAILED|ERROR) tests/probes_v10/test_probe10_(\S+)",
                                                           r.stdout)}
        self.assertGreaterEqual(len(got), 80, r.stdout[-1500:])
        bad = {}
        for name, res in got.items():
            if name in SUPERSEDED:
                continue
            fixed = (res == "PASSED") if _conv(name) == "correct" else (res == "FAILED")
            if fixed == (name in OPEN):
                bad[name] = (res, "listed OPEN but fixed" if fixed else "should be fixed")
        self.maxDiff = None
        self.assertEqual(bad, {})
