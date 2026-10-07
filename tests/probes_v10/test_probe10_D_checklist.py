"""Reviewer D, round 5 (frozen-v10): README "Operator checklist" walk-through probes.

Each `test_defect_*` asserts a DEFECT (PASS = the defect is present). No network, no LLM.
The full scripted walk-through lives in scratchpad/pipefix/rv10d/ (setup.py, worker.py,
pass.sh, check.sh, audit.py, log/).
"""
from __future__ import annotations

import json
import subprocess

from tests.unit.test_run_integrity import OLD_NOTE, PN, ZR, ri
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv

import synth_call as sc  # noqa: E402


class SilentOperatorSkip(_DriverEnv):
    def test_defect_unknown_source_note_is_skipped_and_driver_exits_0(self) -> None:
        """Z-D1: an old note without a known source (no frontmatter `sources`, no
        known_sources.json entry; the checklist never asks for `known-sources`) and no
        transcript above ZR_CANDIDATE_MIN_SCORE: repair_loop.sh prints `[repair] OPERATOR:`
        and exits 0. Nothing records it: no needs_operator.json, no repair_state entry,
        operator-work 0, repair-status prints no row (so no UNLISTED either)."""
        self.vpath("Old Orphan").write_text(OLD_NOTE.format(t="Old Orphan"))
        rel = f"{PN}/Old Orphan.md"
        r = subprocess.run(["bash", str(ZR / "repair_loop.sh"), rel], env=self._env(),
                           capture_output=True, text=True, timeout=300)
        self.assertIn("[repair] OPERATOR:", r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertFalse((self.staging / "needs_operator.json").exists())
        self.assertEqual(ri.operator_work(self.staging, self.zk), [])
        self.assertEqual(sc.repair_status(self.staging, self.zk), [])
        self.assertEqual(self.vpath("Old Orphan").read_text(), OLD_NOTE.format(t="Old Orphan"))


class ExitSevenNeverClears(_DriverEnv):
    def test_defect_needs_repair_state_survives_clearing_needs_operator(self) -> None:
        """Z-D4: the exit-7 action is "remove the entries that you resolved from
        needs_operator.json". operator_work also lists every repair_state note with status
        `needs-repair` that the file does not name, so after the file is emptied the list
        is still not empty (exit 7 again); no command clears the state."""
        st = {"notes": {f"{PN}/Old X.md": {"status": "needs-repair", "bullets": {}}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        (self.staging / "needs_operator.json").write_text("[]")
        self.assertEqual(ri.operator_work(self.staging, self.zk), ["needs-repair: Old X"])
