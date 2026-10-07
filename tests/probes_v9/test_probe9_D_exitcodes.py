"""Reviewer D, round 4 (frozen-v9): driver exit codes with the files backend, from REAL
loop.sh / repair_loop.sh / review_loop.sh runs with fake workers (no network, no LLM).

CONVENTION OF THIS FILE: each `test_designed_*` asserts the DESIGNED exit code (run_lib.sh
header: 0 done, 1 error, 2 iteration cap, 3 stall, 4 budget/ZR_MAX_CALLS, 5 requests wait,
6 stuck folders; review_loop.sh + files = 2). PASS = as designed.
Each `test_defect_*` asserts a DEFECT (PASS = the defect is present).
A non-HarnessError crash is injected with a `sitecustomize.py` on PYTHONPATH that makes the
first exchange request write of `synth_call.py` raise OSError(28) (disk full)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.unit.test_run_integrity import OLD_NOTE, PN, ZR, ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv
from tests.probes_v6 import test_probe_B_loop as _bl

import synth_call as sc  # noqa: E402

CRASH_SITE = '''
import os, sys, pathlib
if os.environ.get("PROBE_CRASH") and sys.argv and sys.argv[0].endswith("synth_call.py"):
    _real = pathlib.Path.write_text
    def _boom(self, *a, **k):
        if "/exchange/requests/" in str(self) and str(self).endswith(".system.md"):
            raise OSError(28, os.environ.get("PROBE_CRASH_MSG") or "No space left on device (probe)")
        return _real(self, *a, **k)
    pathlib.Path.write_text = _boom
'''


def _crash_env(env: dict, tmp: Path) -> dict:
    d = tmp / "site"
    d.mkdir(exist_ok=True)
    (d / "sitecustomize.py").write_text(CRASH_SITE)
    return dict(env, PROBE_CRASH="1", PYTHONPATH=str(d) + os.pathsep + env.get("PYTHONPATH", ""))


def _run(script: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    if script == "repair_loop.sh" and env.get("VAULT"):
        # round 23 adaptation (Z20): the preflight needs a committed git vault (checklist step 2)
        zk = str(Path(env["VAULT"]) / env.get("ZK_FOLDER", ""))
        if subprocess.run(["git", "-C", zk, "rev-parse"], capture_output=True).returncode != 0:
            for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
                subprocess.run(["git", "-C", zk, *cmd], capture_output=True)
        subprocess.run(["git", "-C", zk, "add", "-A", "--", "."], capture_output=True)
        subprocess.run(["git", "-C", zk, "commit", "-qm", "before repair pass", "--allow-empty"], capture_output=True)
    return subprocess.run(["bash", str(ZR / script), *args], env=env, capture_output=True, text=True, timeout=600)


# --------------------------------------------------------------------------- #
# loop.sh
# --------------------------------------------------------------------------- #


class LoopSh(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probe9D-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stg, self.zk, self.env, self.replies = _bl.LoopProbes._setup(self, self.tmp)

    def ans(self, r: dict) -> str | None:
        if r["kind"] == "synth":
            return next(v for k, v in self.replies.items() if k in r["user"])
        if r["kind"] == "review":
            return review_answer(r["user"])
        return None

    def stages(self) -> list[str]:
        return sorted(it["stage"] for it in json.loads((self.stg / "queue.json").read_text())["items"])

    def test_designed_waiting_exits_5(self) -> None:
        r = _run("loop.sh", self.env)
        self.assertEqual(r.returncode, 5, r.stderr[-800:])

    def test_designed_worker_answers_during_pass_exits_5(self) -> None:
        """W7: a worker pool answers every synthesis request as soon as it appears."""
        stop = threading.Event()

        def pool() -> None:
            while not stop.is_set():
                try:
                    if (self.stg / "exchange" / "requests").is_dir():
                        work(self.stg / "exchange", lambda r: self.ans(r) if r["kind"] == "synth" else None, "pool-1")
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.05)
        th = threading.Thread(target=pool, daemon=True)
        th.start()
        r = _run("loop.sh", self.env)
        stop.set()
        th.join(5)
        st = sc.exchange_status(self.stg / "exchange", self.stg)
        self.assertEqual(st["open"], 0)  # every request is answered
        self.assertTrue(st["unconsumed"])
        self.assertEqual(r.returncode, 5, r.stderr[-800:])

    def test_designed_max_calls_exits_4(self) -> None:
        r = _run("loop.sh", dict(self.env, ZR_MAX_CALLS="1"))
        self.assertEqual(r.returncode, 4, r.stderr[-800:])

    def test_designed_iteration_cap_exits_2(self) -> None:
        r = _run("loop.sh", dict(self.env, MAX_ITERS="1"))
        self.assertEqual(r.returncode, 2, r.stdout[-800:])

    def test_designed_stuck_folder_exits_6(self) -> None:
        q = json.loads((self.stg / "queue.json").read_text())
        for it in q["items"]:
            it["stage"] = "synthesized"
        (self.stg / "queue.json").write_text(json.dumps(q))
        pdir = self.stg / "pending-review" / "20261004T000000Z-1--u"
        (pdir / "out").mkdir(parents=True)
        (pdir / "meta.json").write_text(json.dumps({"pending_cycles": ri.MAX_PENDING_CYCLES, "notes": [f"{PN}/X.md"],
                                                    "reasons": {f"{PN}/X.md": "review-rejected"}}))
        r = _run("loop.sh", self.env)
        self.assertEqual(r.returncode, 6, r.stderr[-800:])

    def test_designed_error_at_start_exits_1(self) -> None:
        (self.stg / "runs").write_text("not a folder")  # run_integrity start fails
        r = _run("loop.sh", self.env)
        self.assertIn("run_integrity start failed", r.stderr)
        self.assertEqual(r.returncode, 1, r.stderr[-800:])

    def test_designed_full_chain_with_worker_between_passes_ends_0(self) -> None:
        codes = []
        for _ in range(8):
            r = _run("loop.sh", self.env)
            codes.append(r.returncode)
            if r.returncode != 5:
                break
            work(self.stg / "exchange", self.ans, worker=lambda q: "w-" + q["kind"])
        self.assertEqual(codes[-1], 0, (codes, r.stderr[-800:]))
        self.assertEqual(self.stages(), ["synthesized", "synthesized"], codes)

    def test_defect_crash_attempt_limit_never_enforced(self) -> None:
        """A non-HarnessError crash (OSError, disk full) of `synth_call.py synth` on every
        attempt. Each attempt is counted (`synth_attempts`), but nothing reads
        ZR_MAX_UNIT_ATTEMPTS (run_integrity.py:75 MAX_ATTEMPTS has no user; loop.sh:119-120
        says the item fails after it). The attempt count changes the queue signature, so
        the stall rule never fires either: loop.sh makes a new crashing unit per iteration
        until MAX_ITERS (default 400) and exits 2. Here MAX_ITERS=12: 12 crash units,
        6 counted attempts per video, no video `failed`."""
        r = _run("loop.sh", _crash_env(dict(self.env, MAX_ITERS="12", MAX_STALL="2"), self.tmp))
        q = json.loads((self.stg / "queue.json").read_text())["items"]
        if os.environ.get("PROBE_DEBUG"):
            print(r.returncode, [(i["stage"], i.get("synth_attempts")) for i in q])
        self.assertIn("iter cap (12) hit", r.stdout)
        self.assertEqual(r.returncode, 2)
        self.assertFalse([i for i in q if i["stage"] == "failed"])
        self.assertGreater(max(i.get("synth_attempts", 0) for i in q), ri.MAX_ATTEMPTS)

    def test_designed_transient_error_stalls_exits_3(self) -> None:
        """A transient error (the message matches TRANSIENT) is not an attempt: no progress."""
        env = _crash_env(dict(self.env, MAX_ITERS="12", MAX_STALL="2",
                              PROBE_CRASH_MSG="HTTP 503 temporarily unavailable"), self.tmp)
        r = _run("loop.sh", env)
        self.assertIn("NO PROGRESS", r.stdout)
        self.assertEqual(r.returncode, 3, r.stdout[-800:])

    def test_defect_backend_value_with_space_exits_0_with_requests_open(self) -> None:
        """ZR_LLM_BACKEND='files ' (trailing space, e.g. from an env file). synth_call.backend()
        and run_integrity._budget_decision strip the value (files backend: requests are
        written), but run_lib.sh zr_exchange_exit compares the raw value with 'files' and
        returns at once: loop.sh exits 0 with every request open."""
        r = _run("loop.sh", dict(self.env, ZR_LLM_BACKEND="files "))
        st = sc.exchange_status(self.stg / "exchange", self.stg)
        self.assertEqual(st["open"], 2)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])


# --------------------------------------------------------------------------- #
# repair_loop.sh
# --------------------------------------------------------------------------- #


def _rep(old: str, title: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: b1=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class RepairLoopSh(_DriverEnv):
    def _old(self, title: str, n: int = 1) -> str:
        text = OLD_NOTE.format(t=title).replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        text = text.replace("- legacy bullet\n", "".join(f"- legacy bullet {i}\n" for i in range(1, n + 1)))
        self.vpath(title).write_text(text)
        return f"{PN}/{title}.md"

    def state(self) -> dict:
        return json.loads((self.staging / "repair_state.json").read_text())

    def test_designed_waiting_exits_5(self) -> None:
        rel = self._old("Old Wait")
        r = _run("repair_loop.sh", self._env(), rel)
        self.assertEqual(r.returncode, 5, r.stderr[-800:])

    def test_designed_worker_answers_during_pass_exits_5(self) -> None:
        """W7 for repair: the repair reply arrives while the pass runs (after the unit waited).
        The batch is `waiting`; its reply is answered but not consumed."""
        rel = self._old("Old Conc")
        stop = threading.Event()
        rep = _rep("Old Conc", self.a, self.ta, f"Old Conc.md#b1 | kept in {self.a}\n")

        def pool() -> None:
            while not stop.is_set():
                try:
                    if (self.xdir / "requests").is_dir():
                        work(self.xdir, lambda q: rep if q["kind"] == "repair" else None, "pool-1")
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.05)
        th = threading.Thread(target=pool, daemon=True)
        th.start()
        r = _run("repair_loop.sh", self._env(), rel)
        stop.set()
        th.join(5)
        if os.environ.get("PROBE_DEBUG"):
            print(r.returncode, self.state()["batches"], r.stderr[-1000:])
        self.assertEqual(r.returncode, 5, r.stderr[-800:])

    def test_designed_max_calls_exits_4(self) -> None:
        a, b = self._old("Old One"), self._old("Old Two")
        r = _run("repair_loop.sh", dict(self._env(), ZR_MAX_CALLS="1", ZR_REPAIR_MAX_NOTES="1"), a, b)
        self.assertEqual(r.returncode, 4, r.stderr[-800:])

    def test_designed_stuck_folder_exits_6(self) -> None:
        pdir = self.staging / "pending-review" / "20261004T000000Z-1--u"
        (pdir / "out").mkdir(parents=True)
        (pdir / "meta.json").write_text(json.dumps({"pending_cycles": ri.MAX_PENDING_CYCLES, "notes": [f"{PN}/X.md"],
                                                    "reasons": {f"{PN}/X.md": "review-rejected"}}))
        text = OLD_NOTE.format(t="Old Nolit").replace("created:", "sources:\n  - id: vid-NOTRANSCR1\n    speaker: X\ncreated:")
        self.vpath("Old Nolit").write_text(text)
        r = _run("repair_loop.sh", self._env(), f"{PN}/Old Nolit.md")
        self.assertEqual(r.returncode, 6, r.stderr[-800:])

    def test_designed_failed_repair_call_exits_1(self) -> None:
        """W20: a request that check_secrets refuses (HarnessError, call rc 2), twice."""
        text = OLD_NOTE.format(t="Old Key").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath("Old Key").write_text(text.replace("- legacy bullet", "- legacy bullet sk-proj-abcdefghij0123456789abcd", 1))
        r = _run("repair_loop.sh", self._env(), f"{PN}/Old Key.md")
        self.assertEqual(r.returncode, 1, r.stdout[-800:])

    def test_designed_crash_not_harness_error_exits_1_and_ends(self) -> None:
        """W21: OSError in `synth_call.py repair`; the batch ends `failed` after 2 hand-outs."""
        rel = self._old("Old Crash")
        r = _run("repair_loop.sh", _crash_env(self._env(), self.tmp), rel)
        if os.environ.get("PROBE_DEBUG"):
            print(r.returncode, r.stdout[-2000:], self.state()["batches"])
        self.assertEqual([b["status"] for b in self.state()["batches"].values()], ["failed"])
        self.assertEqual(r.returncode, 1, r.stdout[-800:])

    def test_designed_error_at_start_exits_1(self) -> None:
        rel = self._old("Old Start")
        (self.staging / "runs").rename(self.staging / "runs.bak") if (self.staging / "runs").exists() else None
        (self.staging / "runs").write_text("x")
        r = _run("repair_loop.sh", self._env(), rel)
        self.assertEqual(r.returncode, 1, r.stderr[-800:])

    def test_designed_waiting_repair_hides_nothing_after_another_driver(self) -> None:
        """W19: a synthesis request waits; repair_loop.sh (no repair work that waits) must
        not release it and must exit 5."""
        self.fake()
        self.assertEqual(self.synth()["outcome"], "waiting-for-reply")
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        text = OLD_NOTE.format(t="Old Nolit").replace("created:", "sources:\n  - id: vid-NOTRANSCR1\n    speaker: X\ncreated:")
        self.vpath("Old Nolit").write_text(text)
        r = _run("repair_loop.sh", self._env(), f"{PN}/Old Nolit.md")
        self.assertEqual(r.returncode, 5, r.stderr[-800:])


# --------------------------------------------------------------------------- #
# review_loop.sh
# --------------------------------------------------------------------------- #


class ReviewLoopSh(_DriverEnv):
    def test_designed_files_backend_refused_exits_2(self) -> None:
        r = _run("review_loop.sh", self._env())
        self.assertIn("ZR_LLM_BACKEND=files is not supported", r.stderr)
        self.assertEqual(r.returncode, 2)

    def test_defect_files_backend_with_space_is_not_refused_and_spins(self) -> None:
        """W31 refusal bypass: review_loop.sh compares ZR_LLM_BACKEND exactly with 'files';
        synth_call.backend() strips it. With 'files ' the loop runs the files backend and
        spins on the waiting source-check review (the v8 ReviewLoopSpins setup) until
        REVIEW_MAX_ITERS, exit 2 from the cap, not from the refusal."""
        import review_call as rc
        u = self.written_unit()
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]), worker="reviewer-2")
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        rel = f"{PN}/{self.a}.md"
        p = self.zk / rel
        p.write_text(p.read_text().replace("- [[Home]] — home map.", "- [[Home]] — the home map."))
        r = _run("review_loop.sh", dict(self._env(), ZR_LLM_BACKEND="files ", REVIEW_MAX_ITERS="6"))
        if os.environ.get("PROBE_DEBUG"):
            print(r.returncode, r.stdout[-3000:], r.stderr[-1500:])
        self.assertNotIn("is not supported", r.stderr)
        units = [q for q in (self.staging / "runs").glob("*/units/*") if (q / "unit.json").is_file()
                 and json.loads((q / "unit.json").read_text()).get("review_note") == rel]
        self.assertGreaterEqual(len(units), 5, r.stdout[-2000:])
        self.assertIn("review iter cap hit", r.stdout)
