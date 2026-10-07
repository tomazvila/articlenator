"""Probe A (reviewer A): crash and ordering probes of the repair publish path. Fake LLMs only."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from tests.unit.test_run_integrity import OLD_NOTE, PN, ri
from tests.unit.test_synth_single import V5, _Single

import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402
import verify_claims as vc  # noqa: E402


class _Repair(_Single):
    kind = "repair"

    def old(self, title: str, n: int = 2, srcs: tuple = (V5,)) -> str:
        src = "".join(f"  - id: {v}\n    speaker: X\n" for v in srcs)
        text = OLD_NOTE.format(t=title).replace("created:", f"sources:\n{src}created:").replace(
            "- legacy bullet\n", "".join(f"- legacy bullet {i}\n" for i in range(1, n + 1)))
        self.vpath(title).write_text(text)
        return f"{PN}/{title}.md"

    def run_batch(self, state: Path, reply: str | list, review: bool = True, finalize: bool = True):
        b = sc.next_batch(self.staging, self.zk, state)
        self.fake(repair=reply if isinstance(reply, list) else [reply])
        u = self.new_unit("repair", [])
        res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                             b["prior_titles"], b["last"])
        sc.record_answers(state, b, res)
        if review:
            rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk) if finalize else None
        return b, u, res, ev

    def kill_owner(self, unit: Path) -> None:
        info = json.loads((unit / "unit.json").read_text())
        info["owner_pid"] = 2 ** 22 + 12345  # a pid that does not run
        info["owner_start"] = "x"
        (unit / "unit.json").write_text(json.dumps(info))


class CrashBetweenReplacementAndStub(_Repair):
    """A1-A5: the finalize writes the replacement first and the stub last. A kill between the
    two writes leaves the old note unchanged beside its published replacement; recover()
    quarantines the stub, and the repair state closes the note as `done`."""

    def test_kill_after_replacement_write(self) -> None:
        rel = self.old("Old Crash", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Crash.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Crash.md#b1 | kept in {self.a}\n")
        b, u, res, _ = self.run_batch(state, rep, finalize=False)
        old_text = self.vpath("Old Crash").read_text()
        real = ri._Publisher.write
        calls = {"n": 0}

        def dying_write(pub, rel_, text, why):
            calls["n"] += 1
            if calls["n"] == 2:  # the 2nd vault write: the stub of the old note
                raise SystemExit("killed")
            return real(pub, rel_, text, why)

        with mock.patch.object(ri._Publisher, "write", dying_write):
            with self.assertRaises(SystemExit):
                ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.kill_owner(u)
        rec = ri.recover(self.staging, self.zk)
        self.assertTrue(rec)
        # The replacement is published, the old note is unchanged (no stub).
        self.assertTrue(self.vpath(self.a).is_file())
        self.assertEqual(self.vpath("Old Crash").read_text(), old_text)
        beside = ri.replacement_beside_old(self.staging, self.zk)
        self.assertEqual([x["note"] for x in beside], [rel])  # A2: old note beside its replacement
        # The repair state does not see it: the note closes as done, its bullet `kept`.
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["status"], "done")
        self.assertEqual(e["unsupported_bullets"], [])
        # No later step writes the stub: a new finalize (any unit) leaves it beside.
        u2 = self.new_unit("repair", [])
        ev2 = ri.finalize(self.staging, self.run_dir, u2, [], 0, self.zk)
        self.assertEqual([x["note"] for x in ev2["replacement_beside_old_note"]], [rel])
        self.assertEqual(self.vpath("Old Crash").read_text(), old_text)

    def _crash_then_recover(self):
        rel = self.old("Old Crash", 1)
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
        sc.next_batch(self.staging, self.zk, state)  # closes it as done
        return rel, state

    def test_rerun_after_crash_replays_cache_and_stays_stuck(self) -> None:
        """The next run plans the note again (status `done` is not `open`, prior titles are
        forgotten), the request cache replays the first reply, its title exists in the vault,
        so nothing is written: the old note stays beside the replacement for good, and the
        state says `done-unrepaired` with NO open bullet (b1 counts as kept)."""
        rel, state = self._crash_then_recover()
        old_text = self.vpath("Old Crash").read_text()
        plan = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertIn(rel, [r for k in plan["known"] for r in k["notes"]])
        self.assertEqual(json.loads(state.read_text())["notes"][rel]["titles"], [])
        b2, u2, res2, ev2 = self.run_batch(state, "never sent")
        self.assertEqual(len(self.log()), 1)  # the cached reply was replayed
        self.assertEqual(res2["problems"][-1]["type"], "not-written")
        self.assertEqual(res2["bullets"]["Old Crash.md#b1"]["decision"], "kept")
        self.assertEqual(self.vpath("Old Crash").read_text(), old_text)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["unsupported_bullets"]), ("done-unrepaired", []))
        self.assertEqual([x["note"] for x in ri.replacement_beside_old(self.staging, self.zk)], [rel])
        self.assertEqual(ri.repair_open_bullets(self.staging), [])  # D: b1 is nowhere open

    def test_rerun_with_fresh_reply_publishes_second_name(self) -> None:
        """A5: with a new reply (cache cleared, other model or prompt) the model may use
        another title; the same content is published a second time under that name."""
        import shutil
        rel, state = self._crash_then_recover()
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        shutil.rmtree(self.staging / "repair-cache")
        new_title = "Planche Lean Floor Variant"
        tx = self.ta.replace(f"# {self.a}\n", f"# {new_title}\n")
        rep2 = (f"=====NOTE: {new_title} | repairs: Old Crash.md | bullets: b1=====\n{tx.rstrip()}\n"
                f"=====BULLETS=====\nOld Crash.md#b1 | kept in {new_title}\n")
        b2, u2, res2, ev2 = self.run_batch(state, rep2)
        acts = {Path(n["note"]).stem: n["action"] for n in ev2["notes"]}
        self.assertEqual(acts.get(new_title), "published", ev2["notes"])
        _, body_a, _ = vc.split_frontmatter(self.vpath(self.a).read_text())
        _, body_n, _ = vc.split_frontmatter(self.vpath(new_title).read_text())
        self.assertEqual(body_a.replace(self.a, ""), body_n.replace(new_title, ""))
        stub = self.vpath("Old Crash").read_text()
        self.assertIn(f"[[{new_title}]]", stub)
        self.assertNotIn(f"[[{self.a}]]", stub)  # the first replacement is an orphan duplicate


class InPlaceRejectedSplitPublished(_Repair):
    """A2 without a crash: "in place and split". materialize_repair_stubs writes no stub
    when the old title is among the targets; when the in-place version is quarantined and
    the split sibling B publishes, the old text stays unchanged beside B."""

    def test_in_place_quarantined_sibling_published(self) -> None:
        import hashlib
        rel = self.old("Old Inp", 2)
        old_text = self.vpath("Old Inp").read_text()
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        inp = self.ta.replace(f"# {self.a}\n", "# Old Inp\n")
        rep = (f"=====NOTE: Old Inp | repairs: Old Inp.md | bullets: b1=====\n{inp.rstrip()}\n"
               f"=====NOTE: {self.b} | repairs: Old Inp.md | bullets: b2=====\n{self.tb.rstrip()}\n"
               f"=====BULLETS=====\nOld Inp.md#b1 | kept in Old Inp\nOld Inp.md#b2 | kept in {self.b}\n")
        b, u, res, _ = self.run_batch(state, rep, review=False, finalize=False)
        self.assertEqual(res["applied"][0]["action"], "in place and split", res)
        sha = hashlib.sha256((u / "out" / PN / "Old Inp.md").read_text().encode()).hexdigest()
        os.environ["REJECT_SHAS"] = json.dumps([sha])
        os.environ["ZR_REVIEW_REPAIR"] = "0"
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        acts = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertEqual((acts["Old Inp"], acts[self.b]), ("quarantined", "published"), ev["notes"])
        self.assertEqual(self.vpath("Old Inp").read_text(), old_text)
        self.assertEqual([x["note"] for x in ev["replacement_beside_old_note"]], [rel])


class KillBeforeFinalize(_Repair):
    """D: a kill after record_answers (e.g. during the review) leaves the unit to recover():
    recovered finalize quarantines every shadow note but never calls
    reopen_rejected_bullets. The bullets stay `kept` in quarantined titles; the note closes
    `done` with no open bullet while the old text is unchanged."""

    def test_kill_during_review(self) -> None:
        rel = self.old("Old Kill", 2)
        old_text = self.vpath("Old Kill").read_text()
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Kill.md | bullets: b1,b2=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Kill.md#b1 | kept in {self.a}\nOld Kill.md#b2 | corrected in {self.a}\n")
        b, u, res, _ = self.run_batch(state, rep, review=False, finalize=False)
        self.kill_owner(u)
        rec = ri.recover(self.staging, self.zk)
        self.assertEqual(rec[0]["quarantined"], sorted(rec[0]["quarantined"]))
        self.assertIn(f"{PN}/{self.a}.md", rec[0]["quarantined"])
        self.assertEqual(self.vpath("Old Kill").read_text(), old_text)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["titles"], e["unsupported_bullets"]), ("done", [self.a], []))
        self.assertEqual(ri.repair_open_bullets(self.staging), [])


class ModelWrittenStub(_Repair):
    """A4: the repair reply may give, under the OLD title, a stub (`status: superseded`,
    `superseded_by: "[[Any Published Note]]"`). write_note accepts it (title == old stem),
    the checker treats it as a stub (no contract check, no review), and finalize publishes
    it because its target is a published contract note. The old note is replaced by a
    pointer that no review saw; the target need not hold any of its bullets."""

    def test_model_stub_replaces_old_note_without_review(self) -> None:
        self.vpath(self.a).write_text(self.ta)  # any published contract note
        rel = self.old("Old Ptr", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        stub = ("---\ntype: permanent note\nstatus: superseded\n"
                f'superseded_by: "[[{self.a}]]"\nverification: unverified\n---\n\n# Old Ptr\n\n'
                f"This note was replaced by [[{self.a}]].\n")
        rep = (f"=====NOTE: Old Ptr | repairs: Old Ptr.md | bullets: b1=====\n{stub.rstrip()}\n"
               f"=====BULLETS=====\nOld Ptr.md#b1 | kept in {self.a}\n")
        b, u, res, ev = self.run_batch(state, rep)
        self.assertEqual(list(u.glob("review-*")), [])  # no review call at all
        acts = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertEqual(acts.get("Old Ptr"), "published", ev["notes"])
        self.assertIn(f'superseded_by: "[[{self.a}]]"', self.vpath("Old Ptr").read_text())
