"""Probe 7 A (reviewer A, round 2): minimal adaptations of first-round A probes whose
failure on v7 came from a setup change, not from behaviour.

Convention of THIS file: tests named `test_ok_*` assert the CORRECT behaviour.
PASS = the fix holds. FAIL = the defect is back. Fake LLMs only."""
from __future__ import annotations

import json
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import ri

import synth_call as sc  # noqa: E402


class CrashBetweenReplacementAndStubV7(_Repair):
    """R3 (adapted from test_rerun_with_fresh_reply_publishes_second_name): next_batch is
    None after the crash because the old note is a stub now. The probe checks the state
    after recovery instead of a second run."""

    def _crash(self):
        rel = self.old("Old Crash", 1)
        old_text = self.vpath("Old Crash").read_text()
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Crash.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Crash.md#b1 | kept in {self.a}\n")
        b, u, res, _ = self.run_batch(state, rep, finalize=False)
        real = ri._Publisher.write
        calls = {"n": 0}

        def dying_write(pub, rel_, text, why):
            calls["n"] += 1
            if calls["n"] == 2:
                raise SystemExit("killed")
            return real(pub, rel_, text, why)

        with mock.patch.object(ri._Publisher, "write", dying_write):
            with self.assertRaises(SystemExit):
                ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.kill_owner(u)
        ri.recover(self.staging, self.zk)
        return rel, state, old_text

    def test_ok_recover_writes_the_stub_and_keeps_the_old_text(self) -> None:
        rel, state, old_text = self._crash()
        stub = self.vpath("Old Crash").read_text()
        self.assertIn(f'superseded_by: "[[{self.a}]]"', stub)
        self.assertEqual(ri.replacement_beside_old(self.staging, self.zk), [])
        arch = [p.read_text() for p in self.staging.rglob("retired/**/Old Crash*.md")]
        self.assertIn(old_text, arch)
        # a rerun plans nothing (no second copy under a new name is possible)
        plan = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertNotIn(rel, [r for k in plan["known"] for r in k["notes"]], plan)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])


class RunningBatchV7(_Repair):
    """R12 (adapted from test_same_batch_forever): the third next_batch is None now."""

    def test_ok_batch_fails_after_two_hand_outs(self) -> None:
        rel = self.old("Old Key", 1)
        p = self.vpath("Old Key")
        p.write_text(p.read_text().replace("- legacy bullet 1", "- legacy bullet 1 sk-proj-abcdefghij0123456789abcd"))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        keys = []
        for _ in range(2):
            b = sc.next_batch(self.staging, self.zk, state)
            keys.append(b["key"])
            u = self.new_unit("repair", [])
            bf = u / "batch.json"
            bf.write_text(json.dumps(b))
            rc_ = sc.main(["repair", "--staging", str(self.staging), "--vault", str(self.zk), "--unit-dir", str(u),
                           "--batch", str(bf), "--state", str(state)])
            self.assertEqual(rc_, 2)
            ri.finalize(self.staging, self.run_dir, u, [], rc_, self.zk)
        self.assertEqual(len(set(keys)), 1)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        st = json.loads(state.read_text())
        self.assertEqual(st["batches"][keys[0]]["status"], "failed")
        self.assertEqual(st["notes"][rel]["unsupported_bullets"], ["Old Key.md#b1"])
        self.assertEqual([x["note"] for x in ri.repair_open_bullets(self.staging)], [rel])


class InPlaceRejectedV7(_Repair):
    """R2 (adapted): the in-place version is rejected, sibling B publishes. The entry
    action of the old note is now `published` (a partial stub), so the old probe failed on
    the action tuple. Check the end state instead."""

    def test_ok_old_note_becomes_partial_stub(self) -> None:
        import hashlib
        import os

        import review_call as rc
        from tests.unit.test_run_integrity import PN
        rel = self.old("Old Inp", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        inp = self.ta.replace(f"# {self.a}\n", "# Old Inp\n")
        rep = (f"=====NOTE: Old Inp | repairs: Old Inp.md | bullets: b1=====\n{inp.rstrip()}\n"
               f"=====NOTE: {self.b} | repairs: Old Inp.md | bullets: b2=====\n{self.tb.rstrip()}\n"
               f"=====BULLETS=====\nOld Inp.md#b1 | kept in Old Inp\nOld Inp.md#b2 | kept in {self.b}\n")
        b, u, res, _ = self.run_batch(state, rep, review=False, finalize=False)
        sha = hashlib.sha256((u / "out" / PN / "Old Inp.md").read_text().encode()).hexdigest()
        os.environ["REJECT_SHAS"] = json.dumps([sha])
        os.environ["ZR_REVIEW_REPAIR"] = "0"
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        stub = self.vpath("Old Inp").read_text()
        self.assertIn(f'superseded_by: "[[{self.b}]]"', stub)
        self.assertIn("bullet: Old Inp.md#b1", stub)
        self.assertEqual(ev["replacement_beside_old_note"], [])
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        self.assertIn("Old Inp.md#b1", json.loads(state.read_text())["notes"][rel]["unsupported_bullets"])


class PendingThenRejectedV7(_Repair):
    """R6 (adapted): the old probe asserted that `repair_video` is missing in the pending
    unit. It is there now; check the end state."""

    def test_ok_rejected_in_pending_unit_reopens(self) -> None:
        import hashlib
        import os
        from pathlib import Path

        import review_call as rc
        from tests.unit.test_run_integrity import PN
        rel = self.old("Old Wait", 1)
        old_text = self.vpath("Old Wait").read_text()
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Wait.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Wait.md#b1 | kept in {self.a}\n")
        b, u, res, ev = self.run_batch(state, rep, review=False)
        pu = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        a_sha = hashlib.sha256((pu / "out" / PN / f"{self.a}.md").read_text().encode()).hexdigest()
        os.environ["REJECT_SHAS"] = json.dumps([a_sha])
        os.environ["ZR_REVIEW_REPAIR"] = "0"
        rc.review_unit(self.staging, self.zk, pu)
        ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        self.assertEqual(self.vpath("Old Wait").read_text(), old_text)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["unsupported_bullets"], ["Old Wait.md#b1"])
        self.assertEqual([x["note"] for x in ri.repair_open_bullets(self.staging)], [rel])


class PendingTargetV7(_Repair):
    """R7 (adapted): the stub after video 2 now lists B as pending, so the old probe failed
    early. Check that the final stub links A, B and C."""

    V2 = "vid-EXAMPLE0001"

    def test_ok_final_stub_links_every_published_target(self) -> None:
        import os
        import shutil
        from pathlib import Path

        import review_call as rc
        from tests.unit.test_run_integrity import ZR
        from tests.unit.test_synth_single import V5
        shutil.copy(ZR.parent / "tests/fixtures/note_contract/lit_fictional" / f"{self.V2}.md", self.staging / "lit")
        rel = self.old("Old Two", 3, srcs=(V5, self.V2))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep1 = (f"=====NOTE: {self.a} | repairs: Old Two.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                f"=====NOTE: {self.b} | repairs: Old Two.md | bullets: b2=====\n{self.tb.rstrip()}\n"
                f"=====BULLETS=====\nOld Two.md#b1 | kept in {self.a}\nOld Two.md#b2 | kept in {self.b}\n"
                "Old Two.md#b3 | dropped: not in this transcript\n")
        b1, u1, res1, _ = self.run_batch(state, rep1, review=False, finalize=False)
        rc.review_unit(self.staging, self.zk, u1)
        for d in u1.glob("review-*"):
            if json.loads((d / "request.json").read_text()).get("note", "").endswith(f"{self.b}.md"):
                shutil.rmtree(d)
        ri.finalize(self.staging, self.run_dir, u1, [], 0, self.zk)
        rep2 = (f"=====NOTE: {self.c} | repairs: Old Two.md | bullets: b3=====\n{self.tc.rstrip()}\n"
                f"=====BULLETS=====\nOld Two.md#b3 | kept in {self.c}\n")
        self.run_batch(state, rep2)
        pu = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        rc.review_unit(self.staging, self.zk, pu)
        ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        stub = self.vpath("Old Two").read_text()
        for t in (self.a, self.b, self.c):
            self.assertIn(f"[[{t}]]", stub)
        self.assertNotIn("superseded_pending", stub)
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
