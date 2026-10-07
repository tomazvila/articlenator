"""Reviewer C, fourth review (frozen-v9): one dedicated behaviour probe for each fix that the
authors list without a dedicated test: pilot-8 point 4 (pending cycles in an active
review/fix chain), W8, W9, W19, W21, W22.

CONVENTION OF THIS FILE: the assertion states the CORRECT behaviour. PASS = fixed.
FAIL = not fixed (read the reason). No network, no LLM; fake workers only."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5, reply
from tests.probes_v6 import test_probe_B_loop as _bl
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv, _Files, _h1, count_open

import synth_call as sc  # noqa: E402


# --------------------------------------------------------------------------- #
# Pilot-8 point 4: an active review -> review-fix -> review chain (files backend)
# --------------------------------------------------------------------------- #


class PendingCyclesActiveChain(unittest.TestCase):
    """Pilot 8: the PPT target went through review, review-fix, review ... in 5 pending
    units, reached `pending_cycles` 5 with a reply on the way, and the driver exited 6 (the
    operator had to re-arm). Correct: with the files backend a chain in which the reviewer
    rejects once, the fixer answers, and the reviewer then accepts, publishes the note
    without exit 6 (default ZR_MAX_PENDING_CYCLES=5). The same chain with
    ZR_MAX_PENDING_CYCLES=1 is in test_probe9_C_new.py (X-C2)."""

    def _chain(self, max_cycles: str, reject_times: int) -> tuple[list[int], Path, Path, list[int]]:
        tmp = Path(tempfile.mkdtemp(prefix="probe9C-p4-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stg, zk, env, replies = _bl.LoopProbes._setup(self, tmp)
        q = json.loads((stg / "queue.json").read_text())
        q["items"] = [it for it in q["items"] if it["id"] == "vid-EXAMPLE0001"]
        (stg / "queue.json").write_text(json.dumps(q))
        env.update(ZR_MAX_PENDING_CYCLES=max_cycles, MAX_ITERS="30")
        seen: dict[str, int] = {}

        def ans(r: dict) -> str | None:
            if r["kind"] == "synth":
                return next(v for k, v in replies.items() if k in r["user"])
            if r["kind"] == "review":
                note = r["user"].split("<<<NOTE\n", 1)[1].split("\nNOTE>>>", 1)[0]
                h1 = re.search(r"(?m)^# (.+)$", note).group(1)
                seen[h1] = seen.get(h1, 0) + 1
                return review_answer(r["user"], reject=lambda _n: seen[h1] <= reject_times)
            if r["kind"] == "review-fix":
                note = r["user"].split("<<<NOTE\n", 1)[1].split("\nNOTE>>>", 1)[0]
                h1 = re.search(r"(?m)^# (.+)$", note).group(1)
                fixed = note.replace("no topic map yet.", "there is no topic map yet.")  # a real (small) change
                return f"=====NOTE: {h1}=====\n{fixed}\n"
            return None
        codes, cycles = [], []
        for _ in range(12):
            r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            codes.append(r.returncode)
            cycles.append(max([int(json.loads(p.read_text()).get("pending_cycles") or 0)
                               for p in stg.glob("pending-review/*--*/meta.json")] or [-1]))
            if r.returncode not in (5,):
                break
            work(stg / "exchange", ans, worker=lambda req: "reviewer-2" if req["kind"] == "review" else "writer-1")
        return codes, stg, zk, cycles

    def test_reject_once_then_accept_publishes_without_exit_6(self) -> None:
        codes, stg, zk, cycles = self._chain("5", 1)
        pubs = [json.loads(x) for x in (stg / "provenance.jsonl").read_text().splitlines()
                if '"note-published"' in x] if (stg / "provenance.jsonl").exists() else []
        self.assertNotIn(6, codes, (codes, cycles))
        self.assertEqual(codes[-1], 0, (codes, cycles))
        self.assertTrue(pubs, (codes, cycles))

    def test_a_reviewer_that_always_rejects_still_ends(self) -> None:
        """The cycle rule must not make an endless chain: a note that the reviewer rejects
        every time ends (quarantined after its one review-fix, or exit 6), never exit 5
        for ever."""
        codes, stg, zk, cycles = self._chain("2", 99)
        self.assertNotEqual(codes[-1], 5, (codes, cycles))


# --------------------------------------------------------------------------- #
# W8: two notes with the same bad title; the title fixer of A must not review A
# --------------------------------------------------------------------------- #


class W8TitleFixWriterKept(_Files):
    def test_both_title_fixers_stay_writers_and_fix_a_cannot_review_a(self) -> None:
        import review_call as rc
        self.fake()
        self.synth()
        bad = reply([("C1", "01:16", "a"), ("C2", "01:22", "b")],
                    [("Home", ["C1"], _h1(self.ta, "Home")), ("Home", ["C2"], _h1(self.tb, "Home"))])
        work(self.xdir, lambda r: bad, worker="w-synth")
        marker_a = self.ta.split("\n# ", 1)[1].split("\n", 2)[2][:200]

        def fixes(r: dict) -> str | None:
            if r["kind"] != "titlefix":
                return None
            return f"=====NOTE: {self.a}=====\n{self.ta}" if marker_a in r["user"] else f"=====NOTE: {self.b}=====\n{self.tb}"
        res, u = None, None
        for _ in range(4):
            u = self.new_unit("synth", [V5])
            res = sc.synth_unit(self.staging, self.zk, u, [V5])
            if res["outcome"] != "waiting-for-reply":
                break
            work(self.xdir, fixes, worker=lambda q: "fix-A" if marker_a in q["user"] else "fix-B")
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]))
        names = sorted(p.name for p in (u / "calls").glob("titlefix-*.json")
                       if not p.name.endswith((".reply.json", ".request.json")))
        self.assertEqual(len(names), 2, names)  # one call record per job
        self.assertIn("fix-A", sc.writer_workers(u))
        self.assertIn("fix-B", sc.writer_workers(u))
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker="fix-A")
        recs = rc.review_unit(self.staging, self.zk, u)
        by_note = {r.get("note"): r for r in recs}
        self.assertEqual(by_note[f"{PN}/{self.a}.md"]["end"], "waiting-for-reply", recs)
        self.assertTrue(list((self.xdir / "replies").glob("*.refused-*.txt")))


# --------------------------------------------------------------------------- #
# W9: a title fix that hits ZR_MAX_CALLS ends the unit budget-stop; a later pass sends it
# --------------------------------------------------------------------------- #


class W9TitleFixBudgetStop(_Files):
    def test_budget_stop_in_a_title_fix_keeps_the_note_for_the_next_pass(self) -> None:
        self.fake()
        self.synth()
        bad = reply([("C1", "01:16", "a"), ("C2", "01:22", "b")],
                    [(self.a, ["C1"], self.ta), ("Home", ["C2"], _h1(self.tb, "Home"))])
        work(self.xdir, lambda r: bad if r["kind"] == "synth" else None, worker="w1")
        os.environ["ZR_MAX_CALLS"] = "1"
        run2 = ri.start(self.staging, "synth", self.zk)
        (run2 / "exchange_emitted.json").write_text(json.dumps({"0" * 32: "review-x"}))  # another unit's request
        u = ri.unit_start(run2, "synth", [V5], None, os.getpid())
        res = sc.synth_unit(self.staging, self.zk, u, [V5])
        self.assertEqual(res["outcome"], "budget-stop", res.get("detail"))
        self.assertEqual(sorted(p.name for p in (u / "out").rglob("*.md")), [])  # nothing half-published
        ri.finalize(self.staging, run2, u, [V5], 0, self.zk)
        self.assertNotIn(self.stage(), ("synthesized", "done", "no-output"))
        self.assertFalse((self.zk / PN / f"{self.a}.md").exists())  # A waits with B
        # the next pass (no call limit) sends the title fix of B and publishes both notes' text
        os.environ.pop("ZR_MAX_CALLS", None)
        run3 = ri.start(self.staging, "synth", self.zk)
        res = None
        for _ in range(3):
            u3 = ri.unit_start(run3, "synth", [V5], None, os.getpid())
            res = sc.synth_unit(self.staging, self.zk, u3, [V5])
            if res["outcome"] != "waiting-for-reply":
                break
            work(self.xdir, lambda r: f"=====NOTE: {self.b}=====\n{self.tb}" if r["kind"] == "titlefix" else None,
                 worker="fix-1")
        self.assertEqual(res["outcome"], "notes", res)
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]))
        self.assertNotIn("C2", res.get("claims_uncovered") or [])


# --------------------------------------------------------------------------- #
# W19: a driver releases only the waits of its own kind
# --------------------------------------------------------------------------- #


class W19OwnWaitsOnly(_DriverEnv):
    def test_repair_loop_keeps_the_synthesis_wait_and_does_not_exit_0(self) -> None:
        from tests.unit.test_run_integrity import OLD_NOTE
        self.fake()
        self.assertEqual(self.synth()["outcome"], "waiting-for-reply")
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        self.assertEqual(self.stage(), "waiting-for-reply")
        text = OLD_NOTE.format(t="Old Nolit").replace("created:", "sources:\n  - id: vid-NOTRANSCR1\n    speaker: X\ncreated:")
        self.vpath("Old Nolit").write_text(text)
        r = subprocess.run(["bash", str(ZR / "repair_loop.sh"), f"{PN}/Old Nolit.md"], env=self._env(),
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(self.stage(), "waiting-for-reply")
        self.assertNotEqual(count_open(self.staging), "0 0")
        self.assertNotEqual(r.returncode, 0, r.stderr[-800:])

    def test_synth_start_keeps_a_waiting_repair_batch(self) -> None:
        (self.staging / "repair_state.json").write_text(json.dumps(
            {"notes": {}, "batches": {"k1": {"status": "waiting", "handed_out": True, "failures": 0}}}))
        ri.start(self.staging, "synth", self.zk)
        self.assertEqual(json.loads((self.staging / "repair_state.json").read_text())["batches"]["k1"]["status"],
                         "waiting")
        ri.start(self.staging, "review", self.zk)
        self.assertEqual(json.loads((self.staging / "repair_state.json").read_text())["batches"]["k1"]["status"],
                         "waiting")
        ri.start(self.staging, "repair", self.zk)
        self.assertEqual(json.loads((self.staging / "repair_state.json").read_text())["batches"]["k1"]["status"],
                         "running")


# --------------------------------------------------------------------------- #
# W21: a crash that is not a HarnessError counts as a failed hand-out
# --------------------------------------------------------------------------- #


class W21CrashCounts(_DriverEnv):
    def test_oserror_crash_ends_the_batch_after_two_hand_outs(self) -> None:
        from tests.unit.test_run_integrity import OLD_NOTE
        text = OLD_NOTE.format(t="Old Crashy").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath("Old Crashy").write_text(text)
        env = self._env()
        wrapper = self.tmp / "synth_wrap.py"
        wrapper.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ZR)!r})\n"
            "import synth_call\n"
            "def boom(*a, **k):\n"
            "    raise OSError(28, 'No space left on device')\n"
            "synth_call.repair_unit = boom\n"
            "sys.exit(synth_call.main(sys.argv[1:]))\n")
        state = self.staging / "repair_state.json"
        rel = f"{PN}/Old Crashy.md"
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        keys = []
        for _ in range(8):
            b = sc.next_batch(self.staging, self.zk, state)
            if not b:
                break
            keys.append(b["key"])
            u = self.new_unit("repair", [])
            (u / "batch.json").write_text(json.dumps(b))
            p = subprocess.run([sys.executable, str(wrapper), "repair", "--staging", str(self.staging), "--vault",
                                str(self.zk), "--unit-dir", str(u), "--batch", str(u / "batch.json"), "--state",
                                str(state)], capture_output=True, text=True, env=env)
            self.assertNotEqual(p.returncode, 0)
            ri.finalize(self.staging, self.run_dir, u, [], p.returncode, self.zk)
        st = json.loads(state.read_text())
        self.assertLessEqual(len(keys), 2, keys)
        self.assertEqual(st["batches"][keys[0]]["status"], "failed")
        self.assertEqual(st["notes"][rel]["sources"], [V5])
        self.assertNotEqual(st["notes"][rel]["status"], "open")  # closed, not handed out again

    def test_a_released_wait_is_not_a_failure(self) -> None:
        (self.staging / "repair_state.json").write_text(json.dumps(
            {"notes": {}, "batches": {"k1": {"status": "waiting", "handed_out": True, "failures": 0,
                                             "vid": V5, "notes": [], "unknown": [], "bullets": {}}}}))
        for _ in range(4):  # four passes in which the batch waits and is released
            ri.release_waiting(self.staging, "repair")
            sc.next_batch(self.staging, self.zk, self.staging / "repair_state.json")
            st = json.loads((self.staging / "repair_state.json").read_text())
            st["batches"]["k1"]["status"] = "waiting"  # the call waited again
            st["batches"]["k1"]["released"] = True
            (self.staging / "repair_state.json").write_text(json.dumps(st))
        self.assertEqual(int(json.loads((self.staging / "repair_state.json").read_text())["batches"]["k1"]
                             .get("failures") or 0), 0)


# --------------------------------------------------------------------------- #
# W22: respeak and recovery records keep the writer
# --------------------------------------------------------------------------- #


class W22WriterKept(_Files):
    def _respeak(self) -> str:
        unres = "Unresolved Speaker In Ch Video Says Floor Presses Need No Parallettes"
        t_un = self.ta.replace(f"# {self.a}\n", f"# {unres}\n").replace(
            "verification:", "speaker_status: unresolved\nverification:", 1)
        t_un = re.sub(r'(?m)^(\s+speaker:\s*).*$', r'\1"unresolved speaker, Ch video"', t_un, count=1)
        self.vpath(unres).write_text(t_un)
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{unres}.md", sources=[V5],
                             synth_worker=["writer-1"], review_worker="reviewer-2")
        out = sc.respeak(self.staging, self.zk, V5, "Sasha", apply=True, force_name=True)
        self.assertFalse(out[0].get("problem"), out)
        return f"{PN}/{out[0]['to']}"

    def _rereview(self, rel: str, worker: str) -> list[dict]:
        import review_call as rc
        run2 = ri.start(self.staging, "review", self.zk)
        ru = ri.unit_start(run2, "review", [], rel, os.getpid(), options={"review_pass": "source-check"})
        recs = rc.review_unit(self.staging, self.zk, ru)
        self.assertEqual(recs[0]["end"], "waiting-for-reply", recs)
        work(self.xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker=worker)
        return rc.review_unit(self.staging, self.zk, ru)

    def test_respeak_record_keeps_the_writer(self) -> None:
        new_rel = self._respeak()
        rec = ri.last_published(self.staging, new_rel)
        self.assertEqual(rec.get("synth_worker"), ["writer-1"])

    def test_writer_cannot_review_the_renamed_note(self) -> None:
        new_rel = self._respeak()
        recs = self._rereview(new_rel, "writer-1")
        self.assertEqual(recs[0]["end"], "waiting-for-reply", recs)
        self.assertTrue(list((self.xdir / "replies").glob("*.refused-*.txt")))

    def test_case_variant_of_the_writer_cannot_review_the_renamed_note(self) -> None:
        new_rel = self._respeak()
        recs = self._rereview(new_rel, "Writer-1")
        self.assertEqual(recs[0]["end"], "waiting-for-reply", recs)


class W22RecoveredRecord(_Repair):
    def test_recovered_publish_record_names_the_writer(self) -> None:
        rel = self.old("Old Rw", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Rw.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Rw.md#b1 | kept in {self.a}\n")
        _b, u, _res, _ = self.run_batch(state, rep, finalize=False)
        # minimal adaptation: the fake (openrouter) backend records no worker; give the
        # repair call record the worker id that the files backend writes
        for p in (u / "calls").glob("*.json"):
            if not p.name.endswith((".reply.json", ".request.json")):
                d = json.loads(p.read_text())
                d["worker"] = "writer-9"
                p.write_text(json.dumps(d))
        real_rec = ri.provenance.record

        def dying_record(staging, event, **kw):
            if event == "note-published" and str(kw.get("note", "")).endswith(f"{self.a}.md"):
                raise SystemExit("killed after the vault write")
            return real_rec(staging, event, **kw)
        with mock.patch.object(ri.provenance, "record", dying_record):
            with self.assertRaises(SystemExit):
                ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.kill_owner(u)
        ri.recover(self.staging, self.zk)
        rec = ri.last_published(self.staging, f"{PN}/{self.a}.md")
        self.assertTrue(rec.get("recovered"), rec)
        self.assertEqual(rec.get("synth_worker"), ["writer-9"])
        self.assertTrue(rec.get("bullets"))  # W34 too
