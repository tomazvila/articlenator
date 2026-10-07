"""Probe 10 C (reviewer C, fifth review, frozen-v10): README "Driver exit codes" (round 22)
against REAL loop.sh / repair_loop.sh runs with the files backend and fake workers (no
network, no LLM).

CONVENTION OF THIS FILE:
- `test_documented_*` asserts the exit code that the README table DOCUMENTS for the case.
  PASS = the driver behaves as documented.
- `test_defect_*` asserts the DEFECT (the observed code that differs from the README, or a
  0 while operator work remains). PASS = the defect is present; FAIL = fixed.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.unit.test_run_integrity import OLD_NOTE, PN, ZR, ri
from tests.unit.test_synth_single import V5
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv
from tests.probes_v9.test_probe9_D_exitcodes import _crash_env, _rep, _run
from tests.probes_v6 import test_probe_B_loop as _bl
from tests.unit.test_run_integrity_r13 import work

import synth_call as sc  # noqa: E402

OPERATOR_ENTRY = [{"note": f"{PN}/Some Target.md", "unit": "u", "why": "edited since the pipeline's last write "
                   "(user edit?): not overwritten", "at": "2026-10-04T00:00:00Z"}]


def _stuck(stg: Path) -> None:
    pdir = stg / "pending-review" / "20261004T000000Z-1--u"
    (pdir / "out").mkdir(parents=True)
    (pdir / "meta.json").write_text(json.dumps({"pending_cycles": ri.MAX_PENDING_CYCLES, "notes": [f"{PN}/X.md"],
                                                "reasons": {f"{PN}/X.md": "review-rejected"}}))


class _Lock:
    def __init__(self, stg: Path) -> None:
        stg.mkdir(parents=True, exist_ok=True)
        self.fh = open(stg / ".driver.lock", "a")  # noqa: SIM115
        fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


# --------------------------------------------------------------------------- #
# repair_loop.sh
# --------------------------------------------------------------------------- #


class RepairLoop(_DriverEnv):
    def _nolit(self, title: str = "Old Nolit") -> str:
        """An old note whose source has no transcript: no call; it becomes `unsupported`."""
        text = OLD_NOTE.format(t=title).replace("created:", "sources:\n  - id: vid-NOTRANSCR1\n    speaker: X\ncreated:")
        self.vpath(title).write_text(text)
        return f"{PN}/{title}.md"

    def _old(self, title: str) -> str:
        text = OLD_NOTE.format(t=title).replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath(title).write_text(text)
        return f"{PN}/{title}.md"

    def _ops(self) -> None:
        (self.staging / "needs_operator.json").write_text(json.dumps(OPERATOR_ENTRY))

    # ---- rows that hold ---------------------------------------------------- #

    def test_documented_0_nothing_left(self) -> None:
        r = _run("repair_loop.sh", self._env(), self._nolit())
        self.assertEqual(r.returncode, 0, r.stderr[-800:])

    def test_documented_7_operator_work(self) -> None:
        self._ops()
        r = _run("repair_loop.sh", self._env(), self._nolit())
        self.assertIn("operator work remains", r.stderr)
        self.assertEqual(r.returncode, 7, r.stderr[-800:])

    def test_documented_5_waiting(self) -> None:
        r = _run("repair_loop.sh", self._env(), self._old("Old Wait"))
        self.assertEqual(r.returncode, 5, r.stderr[-800:])

    def test_documented_6_stuck(self) -> None:
        _stuck(self.staging)
        r = _run("repair_loop.sh", self._env(), self._nolit())
        self.assertEqual(r.returncode, 6, r.stderr[-800:])

    def test_documented_1_bad_backend(self) -> None:
        r = _run("repair_loop.sh", dict(self._env(), ZR_LLM_BACKEND="filez"), self._nolit())
        self.assertEqual(r.returncode, 1, r.stderr[-800:])

    def test_documented_4_call_limit(self) -> None:
        a, b = self._old("Old One"), self._old("Old Two")
        r = _run("repair_loop.sh", dict(self._env(), ZR_MAX_CALLS="1", ZR_REPAIR_MAX_NOTES="1"), a, b)
        self.assertEqual(r.returncode, 4, r.stderr[-800:])

    # ---- 75 ------------------------------------------------------------------ #

    def test_defect_lock_held_gives_1_not_75(self) -> None:
        """README row 75: 'Another driver holds the staging lock.' repair_loop.sh runs the
        preflight (repair_loop.sh:61-63) BEFORE zr_run_start takes the lock; the preflight
        finds the held lock ('lock: another driver holds ...') and the driver exits 1. A
        dispatcher that waits and retries on 75 stops instead."""
        rel = self._nolit()
        lk = _Lock(self.staging)
        try:
            r = _run("repair_loop.sh", self._env(), rel)
        finally:
            lk.release()
        self.assertIn("lock: another driver holds", r.stdout)
        self.assertEqual(r.returncode, 1, r.stderr[-800:])

    def test_documented_lock_with_preflight_force_gives_75(self) -> None:
        rel = self._nolit()
        lk = _Lock(self.staging)
        try:
            r = _run("repair_loop.sh", dict(self._env(), ZR_PREFLIGHT_FORCE="1"), rel)
        finally:
            lk.release()
        self.assertEqual(r.returncode, 75, r.stderr[-800:])

    # ---- 0 while operator work remains ------------------------------------- #

    def test_defect_no_candidates_exits_0_with_operator_work_and_stuck_folder(self) -> None:
        """repair_loop.sh:57 `exit 0` ('no notes to repair') runs before zr_run_start and the
        EXIT trap: no zr_exchange_exit, no zr_operator_exit. With --from-candidates and no
        candidate (every old note is a stub, or no transcript was replaced), the driver
        exits 0 although needs_operator.json holds an entry AND a pending folder is stuck
        at the cycle limit (README rows 7 and 6). Exit 0 = 'The run is complete.'"""
        self._ops()
        _stuck(self.staging)
        r = _run("repair_loop.sh", self._env(), "--from-candidates")
        self.assertIn("no notes to repair", r.stdout)
        out = subprocess.run([sys.executable, str(ZR / "run_integrity.py"), "operator-work", "--staging",
                              str(self.staging), "--vault", str(self.zk)], capture_output=True, text=True)
        self.assertNotEqual(out.stdout.splitlines()[0], "0")  # operator-work itself sees the work
        self.assertEqual(r.returncode, 0, r.stderr[-800:])

    # ---- precedence: 7 masked ------------------------------------------------ #

    def test_defect_7_masked_by_5_and_list_not_printed(self) -> None:
        """zr_run_summary (run_lib.sh:127-128) runs zr_exchange_exit first; exit 5 ends the
        shell before zr_operator_exit. The README gives no precedence, and the operator list
        (a refused write: checklist step 6 says stop at that pass) is not printed."""
        self._ops()
        r = _run("repair_loop.sh", self._env(), self._old("Old Wait"))
        self.assertNotIn("operator work remains", r.stderr)
        self.assertEqual(r.returncode, 5, r.stderr[-800:])

    def test_defect_7_masked_by_6(self) -> None:
        self._ops()
        _stuck(self.staging)
        r = _run("repair_loop.sh", self._env(), self._nolit())
        self.assertNotIn("operator work remains", r.stderr)
        self.assertEqual(r.returncode, 6, r.stderr[-800:])

    def test_defect_7_masked_by_1(self) -> None:
        """One failed repair call (check_secrets refuses the request) plus operator work: exit 1
        (`[ "$driver_status" -eq 0 ] && zr_operator_exit`), no operator list."""
        self._ops()
        text = OLD_NOTE.format(t="Old Key").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath("Old Key").write_text(text.replace("- legacy bullet", "- legacy bullet sk-proj-abcdefghij0123456789abcd", 1))
        r = _run("repair_loop.sh", self._env(), f"{PN}/Old Key.md")
        self.assertNotIn("operator work remains", r.stderr)
        self.assertEqual(r.returncode, 1, r.stdout[-800:])

    def test_defect_7_masked_by_4(self) -> None:
        self._ops()
        a, b = self._old("Old One"), self._old("Old Two")
        r = _run("repair_loop.sh", dict(self._env(), ZR_MAX_CALLS="1", ZR_REPAIR_MAX_NOTES="1"), a, b)
        self.assertNotIn("operator work remains", r.stderr)
        self.assertEqual(r.returncode, 4, r.stderr[-800:])

    # ---- exchange-status --labels vs the driver ------------------------------ #

    def test_defect_labels_disagree_with_the_driver_count(self) -> None:
        """README: `exchange-status --count-open --labels` prints requests_open=N ... . The
        labeled form prints the RAW open request count (synth_call.py:3874), with no kind
        filter and no 'a unit still waits' rule; the unlabeled form (what the driver reads)
        applies both. With one synthesis request open, `--kind repair` gives '0 0' (the
        repair driver exits 0) and `--kind repair --labels` gives requests_open=1."""
        self.fake()
        self.assertEqual(self.synth()["outcome"], "waiting-for-reply")
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)

        def co(*extra: str) -> str:
            return subprocess.run([sys.executable, str(ZR / "synth_call.py"), "exchange-status", "--staging",
                                   str(self.staging), "--count-open", *extra], capture_output=True, text=True,
                                  env=dict(os.environ)).stdout.strip()
        self.assertEqual(co("--kind", "repair"), "0 0")
        self.assertTrue(co("--kind", "repair", "--labels").startswith("requests_open=1 "))
        # and a request that no unit waits for any more is still "open" in the labeled form
        q = json.loads((self.staging / "queue.json").read_text())
        q["items"][0]["stage"] = "extracted"
        (self.staging / "queue.json").write_text(json.dumps(q))
        self.assertEqual(co(), "0 0")
        self.assertTrue(co("--labels").startswith("requests_open=1 "))


# --------------------------------------------------------------------------- #
# loop.sh
# --------------------------------------------------------------------------- #


class Loop(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probe10C-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stg, self.zk, self.env, self.replies = _bl.LoopProbes._setup(self, self.tmp)

    def test_documented_75_lock(self) -> None:
        lk = _Lock(self.stg)
        try:
            r = _run("loop.sh", self.env)
        finally:
            lk.release()
        self.assertEqual(r.returncode, 75, r.stderr[-800:])

    def test_documented_1_bad_backend(self) -> None:
        r = _run("loop.sh", dict(self.env, ZR_LLM_BACKEND="FILES"))
        self.assertEqual(r.returncode, 1, r.stderr[-800:])

    def test_documented_7_operator_work(self) -> None:
        q = json.loads((self.stg / "queue.json").read_text())
        for it in q["items"]:
            it["stage"] = "synthesized"
        (self.stg / "queue.json").write_text(json.dumps(q))
        (self.stg / "needs_operator.json").write_text(json.dumps(OPERATOR_ENTRY))
        r = _run("loop.sh", self.env)
        self.assertEqual(r.returncode, 7, r.stderr[-800:])

    def test_defect_attempt_limit_fails_every_video_and_exits_0(self) -> None:
        """X14 is fixed (the item is `failed` after ZR_MAX_UNIT_ATTEMPTS). But a video that
        failed is not operator work for `operator-work` (run_integrity.py:2996-3018 reads
        needs_operator.json, pending folders, needs-repair and validate only). Every unit
        crashes (OSError, disk full), every video ends `failed`, and loop.sh says 'ALL
        EXTRACTED ITEMS PROCESSED' and exits 0, which README row 0 calls 'The run is
        complete'; row 1 says a failed call is exit 1."""
        r = _run("loop.sh", _crash_env(dict(self.env, MAX_ITERS="20", MAX_STALL="3"), self.tmp))
        q = json.loads((self.stg / "queue.json").read_text())["items"]
        self.assertEqual(sorted(i["stage"] for i in q), ["failed", "failed"], r.stdout[-1500:])
        self.assertIn("ALL EXTRACTED ITEMS PROCESSED", r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])

    def test_documented_chain_ends_0(self) -> None:
        from tests.unit.test_run_integrity_r13 import review_answer

        def ans(rq: dict) -> str | None:
            if rq["kind"] == "synth":
                return next(v for k, v in self.replies.items() if k in rq["user"])
            if rq["kind"] == "review":
                return review_answer(rq["user"])
            return None
        codes = []
        for _ in range(8):
            r = _run("loop.sh", self.env)
            codes.append(r.returncode)
            if r.returncode != 5:
                break
            work(self.stg / "exchange", ans, worker=lambda q: "w-" + q["kind"])
        self.assertEqual(codes[-1], 0, (codes, r.stderr[-800:]))



class RepairChainGitVault(_DriverEnv):
    """A full repair chain on a vault that is a git repo (README checklist step 2 commits
    before each pass): pass 1 waits (5), a worker answers, pass 2 publishes. The preflight
    of each pass needs a clean git state."""

    _old = RepairLoop._old

    def _git(self, *a: str) -> str:
        return subprocess.run(["git", "-C", str(self.zk), *a], capture_output=True, text=True).stdout

    def test_documented_chain_5_then_done(self) -> None:
        from tests.unit.test_run_integrity_r13 import review_answer
        rel = self._old("Old Chain")
        for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                    ["add", "-A"], ["commit", "-qm", "init"]):
            self._git(*cmd)
        rep = _rep("Old Chain", self.a, self.ta, f"Old Chain.md#b1 | kept in {self.a}\n")
        codes, dirty = [], []
        for _ in range(6):
            r = _run("repair_loop.sh", self._env(), rel)
            codes.append(r.returncode)
            dirty.append(self._git("status", "--porcelain").strip())
            if r.returncode != 5:
                break
            work(self.xdir, lambda q: rep if q["kind"] == "repair" else (review_answer(q["user"]) if q["kind"] == "review" else None),
                 worker=lambda q: "w-" + q["kind"])
        if os.environ.get("PROBE_DEBUG"):
            print(codes, dirty, r.stdout[-1500:], r.stderr[-1500:])
        self.assertNotIn(1, codes, (codes, dirty, r.stdout[-1200:]))
        self.assertIn(codes[-1], (0, 7), (codes, dirty))


class AllBulletsDroppedExit0(_DriverEnv):
    """Every bullet of an old note is dropped ('not in this transcript') by its only source
    video. The note closes `done-unsupported`; the vault note stays UNCHANGED (no stub, no
    mark) with claims that the transcript does not support. `operator-work` lists nothing
    (no needs_operator entry, run_integrity.py:2996-3018), so repair_loop.sh exits 0 ('The
    run is complete'). `repair-status` on the same files flags the note UNLISTED ('give each
    such note a decision'): the two read-only answers disagree, and the exit code hides it."""

    _old = RepairLoop._old

    def test_defect_dropped_note_exit_0_but_repair_status_unlisted(self) -> None:
        rel = self._old("Old Drop")
        rep = "=====BULLETS=====\nOld Drop.md#b1 | dropped: not in this transcript\n"
        codes = []
        for _ in range(4):
            r = _run("repair_loop.sh", self._env(), rel)
            codes.append(r.returncode)
            if r.returncode != 5:
                break
            work(self.xdir, lambda q: rep if q["kind"] == "repair" else None, worker="w1")
        st = json.loads((self.staging / "repair_state.json").read_text())["notes"][rel]
        self.assertEqual(st["status"], "done-unsupported", (codes, st))
        self.assertNotIn("old_bullets:", (self.zk / rel).read_text())
        rows = {x["note"]: x for x in sc.repair_status(self.staging, self.zk)}
        self.assertTrue(rows[rel]["unlisted"], rows)
        self.assertEqual(codes[-1], 0, (codes, r.stderr[-800:]))
