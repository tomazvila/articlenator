"""Probe A (reviewer A): paths outside the shadow folder, loops, the reply cache. Fake LLMs only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri

import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402


class AbsoluteNotePath(_Repair):
    """A6/A1/A3: an old note given as an ABSOLUTE path (repair_loop.sh "$ZK_DIR/01 ...")
    passes repair_groups (only `..` is refused). materialize_repair_stubs writes
    `unit_dir / "out" / rel`, and an absolute rel makes that the vault file itself: the stub
    goes into the vault before any check, review or archive."""

    def test_absolute_rel_writes_stub_into_vault_without_archive(self) -> None:
        self.old("Old Abs", 1)
        rel = str(self.vpath("Old Abs"))  # absolute
        old_text = self.vpath("Old Abs").read_text()
        state = sc.state_path(self.staging)
        plan = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertIn(rel, [r for k in plan["known"] for r in k["notes"]])
        rep = (f"=====NOTE: {self.a} | repairs: Old Abs.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Abs.md#b1 | kept in {self.a}\n")
        b, u, res, _ = self.run_batch(state, rep, review=False, finalize=False)
        a_sha = hashlib.sha256((u / "out" / PN / f"{self.a}.md").read_text().encode()).hexdigest()
        os.environ["REJECT_SHAS"] = json.dumps([a_sha])
        os.environ["ZR_REVIEW_REPAIR"] = "0"
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        acts = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertEqual(acts.get(self.a), "quarantined")
        self.assertFalse(self.vpath(self.a).exists())  # the replacement is NOT published
        now = self.vpath("Old Abs").read_text()
        self.assertNotEqual(now, old_text)
        self.assertIn(f'superseded_by: "[[{self.a}]]"', now)  # A3: a stub that links a missing note
        self.assertEqual(list(self.staging.glob("retired/**/Old Abs.md")), [])  # A1: no archive copy


class RunningBatchLoopsForever(_Repair):
    """A repair call that raises before record_answers (here: a key-shaped string in the old
    note; check_secrets raises HarnessError, the CLI exits 2) leaves its batch `running`.
    next_batch hands the same batch out first, every time: zr_repair_single loops with no
    cost (the budget never stops it) and no attempt count."""

    def test_same_batch_forever(self) -> None:
        rel = self.old("Old Key", 1)
        p = self.vpath("Old Key")
        p.write_text(p.read_text().replace("- legacy bullet 1", "- legacy bullet 1 sk-proj-abcdefghij0123456789abcd"))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        keys = []
        for _ in range(3):
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
        self.assertEqual(json.loads(state.read_text())["batches"][keys[0]]["status"], "running")
        self.assertFalse(ri.budget_status(self.run_dir)["over"])


class EmptyReplyCachedForever(_Repair):
    """An empty (or garbled) reply is saved in the request cache like a good one. The
    note closes `done-unrepaired`; every later run plans it again, rebuilds the same
    request and replays the empty reply: the note is never repaired, and no call is made."""

    def test_empty_reply_poisons_the_note(self) -> None:
        rel = self.old("Old Empty", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.run_batch(state, "", review=False)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        self.assertEqual(json.loads(state.read_text())["notes"][rel]["status"], "done-unrepaired")
        n_calls = len(self.log())
        # Next run: planned again; a good reply waits in the fake queue but is never asked for.
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        good = (f"=====NOTE: {self.a} | repairs: Old Empty.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                f"=====BULLETS=====\nOld Empty.md#b1 | kept in {self.a}\n")
        _, u2, res2, ev2 = self.run_batch(state, good)
        self.assertEqual(len(self.log()), n_calls)  # no call: the empty reply was replayed
        self.assertEqual(res2["written"], [])
        self.assertFalse(self.vpath(self.a).exists())
        self.assertNotIn("superseded", self.vpath("Old Empty").read_text())
