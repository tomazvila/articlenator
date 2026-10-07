"""Reviewer B, round 3 (frozen-v8): attacks on the files backend after the round-18..20
removals (no automatic obsolete marking, count-open from live unit state, all title fixes
in one pass, hand-out counter counts only failed calls) and on review independence.

CONVENTION OF THIS FILE: the assertion states the DEFECT. PASS = the defect exists,
FAIL = the attack failed (the code is correct). No network, no LLM; fake workers only."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5, _Single, reply
from tests.probes_v6 import test_probe_B_loop as _bl

import synth_call as sc  # noqa: E402


def count_open(staging: Path, *extra: str) -> str:
    r = subprocess.run([sys.executable, str(ZR / "synth_call.py"), "exchange-status", "--staging", str(staging),
                        "--count-open", *extra], capture_output=True, text=True, env=dict(os.environ))
    return r.stdout.strip()


class _Files(_Single):
    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_LLM_BACKEND"] = "files"
        self.xdir = self.staging / "exchange"

    def written_unit(self, notes=None, worker="writer-1") -> Path:
        notes = notes or [(self.a, ["C1"], self.ta)]
        self.fake()
        self.synth()
        claims = [("C1", "01:16", "a"), ("C2", "01:22", "b")][:len(notes)]
        work(self.xdir, lambda r: reply(claims, notes) if r["kind"] == "synth" else None, worker=worker)
        u = self.new_unit("synth", [V5])
        res = sc.synth_unit(self.staging, self.zk, u, [V5])
        assert res["outcome"] == "notes", res
        return u

    def stage(self) -> str:
        return json.loads((self.staging / "queue.json").read_text())["items"][0]["stage"]


# --------------------------------------------------------------------------- #
# 2. count-open from live unit state
# --------------------------------------------------------------------------- #


class UnconsumedReply(_Files):
    def test_answered_but_unconsumed_reply_gives_count_zero(self) -> None:
        """synth_call.py:3347-3361. The video waits (queue stage waiting-for-reply). A worker
        answers while the driver still runs. count-open prints `st['open'] if waits else 0`;
        the answered request is not open, and unconsumed replies are no longer counted, so
        the driver gets '0 0' and exits 0. Nothing consumes the reply until someone runs
        the driver again by hand."""
        self.fake()
        self.assertEqual(self.synth()["outcome"], "waiting-for-reply")
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        self.assertEqual(self.stage(), "waiting-for-reply")
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="w1")
        st = sc.exchange_status(self.xdir, self.staging)
        self.assertEqual(len(st["unconsumed"]), 1)  # an answered reply that no unit took yet
        self.assertEqual(self.stage(), "waiting-for-reply")  # the unit still needs it
        self.assertEqual(count_open(self.staging), "0 0")  # the driver exits 0


class LoopShConcurrentWorker(unittest.TestCase):
    def test_worker_that_answers_during_the_pass_makes_loop_sh_exit_0(self) -> None:
        """Real loop.sh run with a fake worker that answers each synthesis request as soon
        as it appears (a worker pool that runs beside the driver). Both videos end the
        pass in stage waiting-for-reply with an answered, unconsumed reply. The driver
        exits 0 ('done'), not 5: a dispatcher that loops on exit 5 stops with both videos
        unsynthesized."""
        tmp = Path(tempfile.mkdtemp(prefix="probe8B-"))
        try:
            stg, zk, env, replies = _bl.LoopProbes._setup(self, tmp)
            stop = threading.Event()

            def ans(r: dict) -> str | None:
                if r["kind"] == "synth":
                    return next(v for k, v in replies.items() if k in r["user"])
                return None

            def pool() -> None:
                while not stop.is_set():
                    try:
                        if (stg / "exchange" / "requests").is_dir():
                            work(stg / "exchange", ans, worker="pool-1")
                    except Exception:  # noqa: BLE001 - a half-written request file: try again
                        pass
                    time.sleep(0.05)
            th = threading.Thread(target=pool, daemon=True)
            th.start()
            r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            stop.set()
            th.join(5)
            q = json.loads((stg / "queue.json").read_text())
            stages = sorted(it["stage"] for it in q["items"])
            st = sc.exchange_status(stg / "exchange", stg)
            if os.environ.get("PROBE_DEBUG"):
                print(r.returncode, stages, st, r.stderr[-1500:])
            self.assertEqual(stages, ["waiting-for-reply", "waiting-for-reply"], r.stderr[-1500:])
            self.assertEqual(len(st["unconsumed"]), 2)
            self.assertEqual(st["open"], 0)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _DriverEnv(_Files):
    def _env(self) -> dict:
        # round 23 adaptation (Z20): repair_loop.sh's preflight needs a git vault with a
        # committed zettelkasten folder; the probes test other things
        if not (self.zk / ".git").exists() and not (self.zk.parent / ".git").exists():
            for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
                subprocess.run(["git", "-C", str(self.zk), *cmd], capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.zk), "add", "-A"], capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.zk), "commit", "-qm", "probe state", "--allow-empty"], capture_output=True, text=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "ZK_", "PYTEST_"))}
        env.update(ZR_STATE_DIR=str(self.tmp / "state"), ZR_TEST_RUN="1", VAULT=str(self.zk.parent),
                   ZK_FOLDER=self.zk.name, ZR_STAGING=str(self.staging), ZR_LLM_BACKEND="files",
                   ZR_SYNTH_SCRIPT="/bin/false", ZR_REVIEW_SCRIPT="/bin/false", ZR_AGENT_SCRIPT="/bin/false",
                   ZR_REVIEW_REPAIR="0")
        return env


class OtherDriverClearsWaits(_DriverEnv):
    def test_repair_loop_resets_the_waiting_video_and_exits_0(self) -> None:
        """V14 rest. run_integrity.start() calls release_waiting() for EVERY driver
        (run_integrity.py:331): the synthesis video's stage waiting-for-reply goes back to
        `extracted`. repair_loop.sh does not synthesize, so at its end no live marker
        names the open synthesis request: count-open prints 0 and repair_loop.sh exits 0.
        After it, `exchange-status --count-open` gives '0 0' to any dispatcher, while the
        request is still open and the video is not synthesized."""
        from tests.unit.test_run_integrity import OLD_NOTE
        self.fake()
        self.assertEqual(self.synth()["outcome"], "waiting-for-reply")
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        self.assertEqual(self.stage(), "waiting-for-reply")
        self.assertNotEqual(count_open(self.staging), "0 0")  # before: the request counts
        # an old note whose source video has no transcript: the repair driver needs no call
        text = OLD_NOTE.format(t="Old Nolit").replace("created:", "sources:\n  - id: vid-NOTRANSCR1\n    speaker: X\ncreated:")
        self.vpath("Old Nolit").write_text(text)
        r = subprocess.run(["bash", str(ZR / "repair_loop.sh"), f"{PN}/Old Nolit.md"], env=self._env(),
                           capture_output=True, text=True, timeout=300)
        open_ = sc.exchange_list(self.xdir, staging=self.staging)
        if os.environ.get("PROBE_DEBUG"):
            print(r.returncode, r.stdout[-2000:], r.stderr[-2000:])
        self.assertEqual([x["kind"] for x in open_], ["synth"])  # still open, still needed
        self.assertEqual(self.stage(), "extracted")  # the live marker is gone
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertEqual(count_open(self.staging), "0 0")


class VaultNoteReviewWait(_Files):
    def test_review_of_a_vault_note_that_waits_is_not_counted(self) -> None:
        """review_loop.sh source-check of a vault note (a `review` unit): the request waits,
        but finalize stores no pending folder and no queue item for it. count-open sees no
        live marker and prints 0, so review_loop.sh exits 0 with the review request open
        (v7 counted every open request and exited 5)."""
        import review_call as rc
        u = self.written_unit()
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]), worker="reviewer-2")
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        rel = f"{PN}/{self.a}.md"
        self.assertTrue((self.zk / rel).is_file())
        run2 = ri.start(self.staging, "review", self.zk)
        ru = ri.unit_start(run2, "review", [], rel, os.getpid(), options={"review_pass": "source-check"})
        p = self.zk / rel
        p.write_text(p.read_text().replace("- [[Home]] — home map.", "- [[Home]] — the home map."))
        recs = rc.review_unit(self.staging, self.zk, ru)
        self.assertEqual(recs[0]["end"], "waiting-for-reply")
        ri.finalize(self.staging, run2, ru, [], 0, self.zk, review_note=rel)
        open_ = [x for x in sc.exchange_list(self.xdir, staging=self.staging) if x["kind"] == "review"]
        self.assertEqual(len(open_), 1)
        self.assertEqual(count_open(self.staging), "0 0")


class ReviewLoopSpins(_DriverEnv):
    def test_review_loop_takes_the_waiting_note_again_until_the_iteration_cap(self) -> None:
        """review_loop.sh + files backend. A source-check review that waits ends finalize 11;
        zr_review_unit (run_lib.sh:416-422) then calls `review_queue.py release`, and
        `review_queue.py next` returns the SAME note at once. The pass makes a new unit for
        that note on every iteration (one request, no progress) until REVIEW_MAX_ITERS
        (default 3000) and exits 2; the notes after it get no review request."""
        import review_call as rc
        u = self.written_unit()
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]), worker="reviewer-2")
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        rel = f"{PN}/{self.a}.md"
        p = self.zk / rel
        p.write_text(p.read_text().replace("- [[Home]] — home map.", "- [[Home]] — the home map."))
        env = dict(self._env(), REVIEW_MAX_ITERS="6")
        r = subprocess.run(["bash", str(ZR / "review_loop.sh")], env=env, capture_output=True, text=True, timeout=600)
        if os.environ.get("PROBE_DEBUG"):
            print(r.returncode, r.stdout[-3000:], r.stderr[-1500:])
        units = [q for q in (self.staging / "runs").glob("*/units/*") if (q / "unit.json").is_file()
                 and json.loads((q / "unit.json").read_text()).get("review_note") == rel]
        self.assertGreaterEqual(len(units), 5, r.stdout[-2000:])  # the same note, again and again
        self.assertIn("review iter cap hit", r.stdout)
        self.assertEqual(r.returncode, 2)
        reqs = [x for x in sc.exchange_list(self.xdir, staging=self.staging) if x["kind"] == "review"]
        self.assertEqual(len(reqs), 1)  # one request; nothing else progressed


class OperatorMarkObsolete(_Files):
    def test_mark_obsolete_hides_the_request_a_unit_waits_for(self) -> None:
        """`exchange-status --mark-obsolete KEY` (synth_call.py:3341-3343) checks only that
        the request file exists. The key of a video that still waits is accepted: it leaves
        exchange-list --open, and count-open prints '0 0' (driver exit 0). No warning."""
        self.fake()
        self.synth()
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        k = sc.exchange_list(self.xdir)[0]["key"]
        self.assertEqual(self.stage(), "waiting-for-reply")
        out = count_open(self.staging, "--mark-obsolete", k)
        self.assertEqual(out, "0 0")
        self.assertEqual(sc.exchange_list(self.xdir, staging=self.staging), [])
        self.assertEqual(self.stage(), "waiting-for-reply")


# --------------------------------------------------------------------------- #
# repair_loop.sh exit codes
# --------------------------------------------------------------------------- #


class RepairLoopErrorExit0(_DriverEnv):
    def test_repair_call_error_ends_with_exit_0(self) -> None:
        """run_lib.sh:300-304 (round 20) leaves out every finalize code 11, not only the
        waiting units. A repair call that fails (here: check_secrets refuses the request,
        HarnessError, `synth_call.py repair` exits 2) twice makes the batch `failed`; every
        bullet is recorded unsupported, and repair_loop.sh exits 0 (v7: 1)."""
        from tests.unit.test_run_integrity import OLD_NOTE
        text = OLD_NOTE.format(t="Old Key").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        text = text.replace("- legacy bullet", "- legacy bullet sk-proj-abcdefghij0123456789abcd", 1)
        self.vpath("Old Key").write_text(text)
        r = subprocess.run(["bash", str(ZR / "repair_loop.sh"), f"{PN}/Old Key.md"], env=self._env(),
                           capture_output=True, text=True, timeout=300)
        st = json.loads((self.staging / "repair_state.json").read_text())
        if os.environ.get("PROBE_DEBUG"):
            print(r.returncode, r.stdout[-3000:], r.stderr[-2000:])
        self.assertEqual([b["status"] for b in st["batches"].values()], ["failed"])
        self.assertIn("call rc=2", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])


class HandOutCountsOnlyHarnessError(_DriverEnv):
    def test_a_crash_that_is_not_a_harness_error_loops_inside_one_pass(self) -> None:
        """synth_call.py:2534-2537 and 3466-3478: only HarnessError counts a failure. Any other
        crash of `synth_call.py repair` (an OSError, MemoryError, a kill by the OOM killer)
        leaves the batch `running` with failures 0; next_batch hands the same batch out
        again, without end, inside ONE repair_loop.sh pass. The crash is injected with a
        wrapper script that imports synth_call and replaces repair_unit (no LLM)."""
        from tests.unit.test_run_integrity import OLD_NOTE
        text = OLD_NOTE.format(t="Old Crashy").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath("Old Crashy").write_text(text)
        env = self._env()
        wrapper = self.tmp / "synth_wrap.py"
        wrapper.write_text(
            "import runpy, sys\n"
            f"sys.path.insert(0, {str(ZR)!r})\n"
            "import synth_call\n"
            "def boom(*a, **k):\n"
            "    raise OSError(28, 'No space left on device')\n"
            "synth_call.repair_unit = boom\n"
            "sys.exit(synth_call.main(sys.argv[1:]))\n")
        # The loop is driven here exactly as zr_repair_single drives it (next-batch, the
        # `repair` CLI in a new process, finalize), 12 times; the wrapper only replaces
        # repair_unit by a function that raises OSError.
        state = self.staging / "repair_state.json"
        rel = f"{PN}/Old Crashy.md"
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        keys, rcs = [], []
        for _ in range(12):
            b = sc.next_batch(self.staging, self.zk, state)
            if not b:
                break
            keys.append(b["key"])
            u = self.new_unit("repair", [])
            (u / "batch.json").write_text(json.dumps(b))
            p = subprocess.run([sys.executable, str(wrapper), "repair", "--staging", str(self.staging), "--vault",
                                str(self.zk), "--unit-dir", str(u), "--batch", str(u / "batch.json"), "--state",
                                str(state)], capture_output=True, text=True, env=env)
            rcs.append(p.returncode)
            ri.finalize(self.staging, self.run_dir, u, [], p.returncode, self.zk)
        st = json.loads(state.read_text())
        self.assertEqual(len(keys), 12, rcs)  # still handed out after 12 crashes
        self.assertEqual(len(set(keys)), 1)
        self.assertTrue(all(x != 0 for x in rcs), rcs)
        self.assertEqual(st["batches"][keys[0]]["status"], "running")
        self.assertEqual(int(st["batches"][keys[0]].get("failures") or 0), 0)


# --------------------------------------------------------------------------- #
# Files-backend repair: a garbled reply settles the note
# --------------------------------------------------------------------------- #


class GarbledRepairReply(_Files):
    def test_garbled_worker_reply_records_every_bullet_unsupported(self) -> None:
        """synth_call.py:2763-2769 (R11 in the files backend): `_cached_call` moves a reply with
        no note, keep or ledger line aside (the request opens again) but RETURNS it. The
        unit goes on with nothing: record_answers stores `unaccounted` for each bullet of
        this video, the note closes `done-unrepaired` with every bullet unsupported, and the
        reopened request is never consumed (no unit waits for it). repair_groups then skips
        the unchanged note for good (round 20, synth_call.py:2459-2465)."""
        from tests.unit.test_run_integrity import OLD_NOTE
        text = OLD_NOTE.format(t="Old Garb").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        text = text.replace("- legacy bullet\n", "- legacy bullet 1\n- legacy bullet 2\n")
        self.vpath("Old Garb").write_text(text)
        rel = f"{PN}/Old Garb.md"
        state = self.staging / "repair_state.json"
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)

        def one(u_name: str) -> dict:
            b = sc.next_batch(self.staging, self.zk, state)
            u = self.new_unit("repair", [])
            res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                 b["prior_titles"], b["last"])
            sc.record_answers(state, b, res)
            return res
        self.assertEqual(one("p1")["stopped"], "waiting-for-reply")
        ri.release_waiting(self.staging)
        work(self.xdir, lambda r: "I am sorry, I cannot read this transcript.\n", worker="w1")
        one("p2")
        st = json.loads(state.read_text())
        e = st["notes"][rel]
        if os.environ.get("PROBE_DEBUG"):
            print(json.dumps(e, indent=1)[:3000])
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["status"], "done-unrepaired")
        self.assertEqual(sorted(e["unsupported_bullets"]), sorted(e["bullets"]))
        self.assertEqual(len(sc.exchange_list(self.xdir, staging=self.staging)), 1)  # reopened, never consumed
        out = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertEqual(out["skipped"], [{"note": rel, "state": "repair-done-unrepaired"}])


# --------------------------------------------------------------------------- #
# Title fixes
# --------------------------------------------------------------------------- #


def _h1(text: str, t: str) -> str:
    return re.sub(r"(?m)^# .+$", f"# {t}", text, count=1)


class TitleFixBudgetStopLosesNote(_Files):
    def test_max_calls_stop_in_the_first_title_fix_drops_the_note(self) -> None:
        """synth_call.py:2009-2010: `_title_fix` turns BudgetStop into None, so the note is
        `refused` and the unit ends `notes` with the other note only. ZR_MAX_CALLS=1 and one
        request already emitted in this run (another unit): the title fix of B is never
        sent, B's claim is uncovered, and the video is finished; no later pass sends it."""
        self.fake()
        self.synth()
        bad = reply([("C1", "01:16", "a"), ("C2", "01:22", "b")],
                    [(self.a, ["C1"], self.ta), ("Home", ["C2"], _h1(self.tb, "Home"))])
        work(self.xdir, lambda r: bad, worker="w1")
        os.environ["ZR_MAX_CALLS"] = "1"
        run2 = ri.start(self.staging, "synth", self.zk)
        (run2 / "exchange_emitted.json").write_text(json.dumps({"0" * 32: "review-x"}))  # another unit's request
        u = ri.unit_start(run2, "synth", [V5], None, os.getpid())
        res = sc.synth_unit(self.staging, self.zk, u, [V5])
        if os.environ.get("PROBE_DEBUG"):
            print(res)
        self.assertEqual(res["outcome"], "notes")
        self.assertEqual(sorted(res["notes"]), [self.a])
        self.assertFalse([x for x in sc.exchange_list(self.xdir) if x["kind"] == "titlefix"])  # never sent
        self.assertIn("C2", res.get("claims_uncovered") or [])
        ri.finalize(self.staging, run2, u, [V5], 0, self.zk)
        self.assertNotEqual(self.stage(), "waiting-for-reply")  # no later pass sends the fix
        if os.environ.get("PROBE_DEBUG"):
            print(self.stage())


class TitleFixSameTitleWriterLost(_Files):
    def test_title_fix_worker_of_note_a_reviews_note_a(self) -> None:
        """Independence. Two notes with the same refused title share the call name
        `titlefix-<sha(title)>` (synth_call.py:2006). V15 is fixed by the request-key check,
        but the second job writes calls/titlefix-<tag>.json over the first: the worker that
        fixed (rewrote) note A is no longer in writer_workers() or in the request keys. That
        worker's review of note A is accepted."""
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
        res = None
        u = None
        for _ in range(4):
            u = self.new_unit("synth", [V5])
            res = sc.synth_unit(self.staging, self.zk, u, [V5])
            if res["outcome"] != "waiting-for-reply":
                break
            work(self.xdir, fixes, worker=lambda q: "fix-A" if marker_a in q["user"] else "fix-B")
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]))
        self.assertNotIn("fix-A", sc.writer_workers(u))
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker="fix-A")
        recs = rc.review_unit(self.staging, self.zk, u)
        by_note = {r.get("note"): r for r in recs}
        if os.environ.get("PROBE_DEBUG"):
            print(recs)
        rel_a = f"{PN}/{self.a}.md"
        self.assertEqual(by_note[rel_a]["end"], "finished", recs)  # fix-A's review of its own note
        ev = ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        pub = [json.loads(x) for x in (self.staging / "provenance.jsonl").read_text().splitlines()
               if '"note-published"' in x and rel_a in x]
        self.assertTrue(pub, ev["notes"])
        self.assertEqual(pub[-1].get("review_worker"), "fix-A")
        self.assertNotIn("fix-A", pub[-1].get("synth_worker") or [])


# --------------------------------------------------------------------------- #
# 3. Review independence
# --------------------------------------------------------------------------- #


class WorkerIdSpoof(_Files):
    def _check(self, spoof: str) -> None:
        import review_call as rc
        u = self.written_unit(worker="writer-1")
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker=spoof)
        recs = rc.review_unit(self.staging, self.zk, u)
        self.assertEqual(recs[0]["end"], "finished")  # accepted
        self.assertFalse(list((self.xdir / "replies").glob("*.refused-*.txt")))

    def test_upper_case_worker_id_passes(self) -> None:
        """synth_call.py:243-244: `meta["worker"] in refuse` compares exact strings."""
        self._check("Writer-1")

    def test_trailing_space_worker_id_passes(self) -> None:
        self._check("writer-1 ")


class RespeakRenamedRereview(_Files):
    def test_writer_reviews_its_note_after_respeak(self) -> None:
        """V13 rest. `_provenance_writers` (review_call.py:543-548) reads `synth_worker` of
        the `note-published` records of exactly this path. respeak writes a new
        `note-published` record for the renamed note with no synth_worker
        (synth_call.py:3226-3228). A source-check rereview of the renamed note then
        refuses nobody, and the worker that wrote the note reviews it."""
        import review_call as rc
        unres = "Unresolved Speaker In Ch Video Says Floor Presses Need No Parallettes"
        t_un = self.ta.replace(f"# {self.a}\n", f"# {unres}\n").replace(
            "verification:", "speaker_status: unresolved\nverification:", 1)
        t_un = re.sub(r'(?m)^(\s+speaker:\s*).*$', r'\1"unresolved speaker, Ch video"', t_un, count=1)
        self.vpath(unres).write_text(t_un)
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{unres}.md", sources=[V5],
                             synth_worker=["writer-1"], review_worker="reviewer-2")
        out = sc.respeak(self.staging, self.zk, V5, "Sasha", apply=True, force_name=True)
        self.assertFalse(out[0].get("problem"), out)
        new_rel = f"{PN}/{out[0]['to']}"
        self.assertTrue((self.zk / new_rel).is_file())
        run2 = ri.start(self.staging, "review", self.zk)
        ru = ri.unit_start(run2, "review", [], new_rel, os.getpid(), options={"review_pass": "source-check"})
        recs = rc.review_unit(self.staging, self.zk, ru)
        self.assertEqual(recs[0]["end"], "waiting-for-reply", recs)
        work(self.xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker="writer-1")
        recs = rc.review_unit(self.staging, self.zk, ru)
        self.assertEqual(recs[0]["end"], "finished", recs)  # the writer's review is accepted
        self.assertFalse(list((self.xdir / "replies").glob("*.refused-*.txt")))
