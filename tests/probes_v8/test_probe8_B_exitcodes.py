"""Reviewer B, round 3 (frozen-v8): driver exit codes with the files backend, from real
loop.sh / repair_loop.sh runs with fake workers.

CONVENTION OF THIS FILE: the assertion states the DESIGNED exit code (run_lib.sh header,
S10: 2, 3, 4 stay; 5 = requests wait; 6 = stuck folders). PASS = as designed. The defects
(exit 0 with work undone) are in test_probe8_B_attacks.py. No network, no LLM."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_synth_single import V5
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv
from tests.probes_v6 import test_probe_B_loop as _bl

import synth_call as sc  # noqa: E402


class LoopSh(unittest.TestCase):
    def _run(self, **extra: str) -> tuple[subprocess.CompletedProcess, Path]:
        self.tmp = Path(tempfile.mkdtemp(prefix="probe8B-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stg, _zk, env, _ = _bl.LoopProbes._setup(self, self.tmp)
        env.update(extra)
        return subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300), stg

    def test_waiting_exits_5(self) -> None:
        r, stg = self._run()
        self.assertEqual(r.returncode, 5, r.stderr[-800:])

    def test_iteration_cap_exits_2_and_keeps_2(self) -> None:
        r, stg = self._run(MAX_ITERS="1")
        self.assertEqual(r.returncode, 2, r.stdout[-800:])
        self.assertIn("iter cap", r.stdout)

    def test_max_calls_exits_4(self) -> None:
        r, stg = self._run(ZR_MAX_CALLS="1")
        self.assertEqual(r.returncode, 4, r.stderr[-800:])

    def test_stuck_folder_exits_6(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="probe8B-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stg, _zk, env, _ = _bl.LoopProbes._setup(self, tmp)
        q = json.loads((stg / "queue.json").read_text())
        for it in q["items"]:
            it["stage"] = "synthesized"
        (stg / "queue.json").write_text(json.dumps(q))
        pdir = stg / "pending-review" / "20261004T000000Z-1--u"
        (pdir / "out").mkdir(parents=True)
        (pdir / "meta.json").write_text(json.dumps({"pending_cycles": ri.MAX_PENDING_CYCLES, "notes": [f"{PN}/X.md"],
                                                    "reasons": {f"{PN}/X.md": "review-rejected"}}))
        r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 6, r.stderr[-800:])


class RepairLoopSh(_DriverEnv):
    def _old(self, title: str) -> str:
        from tests.unit.test_run_integrity import OLD_NOTE
        text = OLD_NOTE.format(t=title).replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath(title).write_text(text)
        return f"{PN}/{title}.md"

    def test_max_calls_exits_4(self) -> None:
        a, b = self._old("Old One"), self._old("Old Two")
        env = dict(self._env(), ZR_MAX_CALLS="1", ZR_REPAIR_MAX_NOTES="1")
        r = subprocess.run(["bash", str(ZR / "repair_loop.sh"), a, b], env=env, capture_output=True, text=True,
                           timeout=300)
        st = json.loads((self.staging / "repair_state.json").read_text())
        self.assertIn("waiting", [x["status"] for x in st["batches"].values()])
        self.assertIn("budget:", r.stderr)  # zr_budget_check stops before the next batch
        self.assertEqual(r.returncode, 4, r.stderr[-800:])
