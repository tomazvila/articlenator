"""Probe A (reviewer A): repair bullet bookkeeping (question D). Fake LLMs only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri

import synth_call as sc  # noqa: E402


class GhostTitleInLedger(_Repair):
    """D: `kept in <Title>` is trusted without a check that <Title> was written and
    published. A bullet kept in a title that does not exist counts as settled."""

    def test_bullet_kept_in_unwritten_title_vanishes(self) -> None:
        rel = self.old("Old Ghost", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Ghost.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Ghost.md#b1 | kept in {self.a}\n"
               "Old Ghost.md#b2 | kept in Ghost Note That Was Never Written\n")
        b, u, res, ev = self.run_batch(state, rep)
        self.assertFalse(self.vpath("Ghost Note That Was Never Written").exists())
        stub = self.vpath("Old Ghost").read_text()
        self.assertIn("status: superseded", stub)
        self.assertNotIn("superseded_pending", stub)  # a COMPLETE stub: b2 is not named anywhere
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["unsupported_bullets"]), ("done", []))
        self.assertEqual(ri.repair_open_bullets(self.staging), [])
        self.assertEqual(ev["bullets_dropped"], [])  # the unit event does not list it either

    def test_bullet_kept_in_truncated_note_vanishes(self) -> None:
        """The only note of the reply is cut (no closing sections): nothing is written, but
        the ledger line `kept in A` settles the bullet; the note closes `done-unrepaired`
        with no open bullet."""
        rel = self.old("Old Cut", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        cut = self.ta.split("## Evidence")[0]
        rep = (f"=====NOTE: {self.a} | repairs: Old Cut.md | bullets: b1=====\n{cut.rstrip()}\n"
               f"=====BULLETS=====\nOld Cut.md#b1 | kept in {self.a}\n")
        b, u, res, ev = self.run_batch(state, rep)
        self.assertEqual(res["written"], [])
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["unsupported_bullets"]), ("done-unrepaired", []))
        self.assertEqual(ri.repair_open_bullets(self.staging), [])


class ExtendUnseenNote(_Repair):
    """D: a note split across two calls of one video (cap ZR_REPAIR_MAX_BULLETS). The prompt
    tells the model to "extend" an already written note with "the full new version under
    the same title", but the request holds only its TITLE. The second call's version
    replaces the published note; the bullets that the first call kept in it are gone from
    it, while the state still says `kept in A`."""

    def test_second_call_overwrites_first_calls_note(self) -> None:
        os.environ["ZR_REPAIR_MAX_BULLETS"] = "1"
        rel = self.old("Old Split", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep1 = (f"=====NOTE: {self.a} | repairs: Old Split.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                f"=====BULLETS=====\nOld Split.md#b1 | kept in {self.a}\n")
        b1, u1, res1, ev1 = self.run_batch(state, rep1)
        self.assertEqual(b1["bullets"][rel], ["Old Split.md#b1"])
        first_a = self.vpath(self.a).read_text()
        self.assertIn("superseded_pending", self.vpath("Old Split").read_text())
        # Call 2: the model "extends" A; it never saw A's text, so its version holds other content.
        tb_as_a = self.tb.replace(f"# {self.b}\n", f"# {self.a}\n")
        rep2 = (f"=====NOTE: {self.a} | repairs: Old Split.md | bullets: b2=====\n{tb_as_a.rstrip()}\n"
                f"=====BULLETS=====\nOld Split.md#b2 | kept in {self.a}\n")
        b2, u2, res2, ev2 = self.run_batch(state, rep2)
        user2 = [c for c in self.log() if c["kind"] == "repair"][-1]["user"]
        lead_a = [ln for ln in first_a.split("\n# ", 1)[1].split("\n") if ln.strip()][1]
        self.assertNotIn(lead_a, user2)  # the request does not show A's text
        acts = {Path(n["note"]).stem: n["action"] for n in ev2["notes"]}
        self.assertEqual(acts.get(self.a), "published", ev2["notes"])
        now_a = self.vpath(self.a).read_text()
        self.assertNotIn(lead_a, now_a)  # the first call's content left the published note
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["bullets"]["Old Split.md#b1"]["answers"][b1["vid"]]["decision"], "kept")
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        self.assertEqual(json.loads(state.read_text())["notes"][rel]["unsupported_bullets"], [])


class PendingThenRejectedNoReopen(_Repair):
    """D (round 12 hole): reopen_rejected_bullets reads `repair_video` from unit.json. A
    pending-review unit (pending_units) writes a new unit.json without it, so a target that
    waits for its review and is rejected later is NOT reopened: its bullets stay `kept`,
    the note closes `done` with no open bullet, while the old note stays unrepaired."""

    def test_rejected_in_pending_unit_keeps_bullets_settled(self) -> None:
        import hashlib

        import review_call as rc
        rel = self.old("Old Wait", 1)
        old_text = self.vpath("Old Wait").read_text()
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Wait.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Wait.md#b1 | kept in {self.a}\n")
        b, u, res, ev = self.run_batch(state, rep, review=False)  # no review: the note waits
        acts = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertEqual(acts[self.a], "pending-review")
        units = ri.pending_units(self.staging, self.run_dir, os.getpid())
        pu = Path(units[0])
        self.assertNotIn("repair_video", json.loads((pu / "unit.json").read_text()))
        a_sha = hashlib.sha256((pu / "out" / PN / f"{self.a}.md").read_text().encode()).hexdigest()
        os.environ["REJECT_SHAS"] = json.dumps([a_sha])
        os.environ["ZR_REVIEW_REPAIR"] = "0"
        rc.review_unit(self.staging, self.zk, pu)
        ev2 = ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        acts2 = {Path(n["note"]).stem: n["action"] for n in ev2["notes"]}
        self.assertEqual(acts2[self.a], "quarantined")
        self.assertEqual(self.vpath("Old Wait").read_text(), old_text)
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["bullets"]["Old Wait.md#b1"]["answers"][b["vid"]]["decision"], "kept")  # not reopened
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["unsupported_bullets"]), ("done", []))
        self.assertEqual(ri.repair_open_bullets(self.staging), [])


class PendingTargetRewritesNewerStub(_Repair):
    """D/A: video 1 writes A (published) and B (waits for its review). Video 2 writes C;
    its stub names [A, C] (B is in no folder it looks at). The pending unit of B later
    publishes B and REWRITES the stub from video 1's repair_map: [A, B]. C is published,
    holds bullets of the old note, and no stub names it any more."""

    V2 = "vid-EXAMPLE0001"

    def test_stub_loses_a_published_target(self) -> None:
        import shutil

        import review_call as rc
        from tests.unit.test_run_integrity import ZR
        shutil.copy(ZR.parent / "tests/fixtures/note_contract/lit_fictional" / f"{self.V2}.md", self.staging / "lit")
        from tests.unit.test_synth_single import V5
        rel = self.old("Old Two", 3, srcs=(V5, self.V2))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep1 = (f"=====NOTE: {self.a} | repairs: Old Two.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                f"=====NOTE: {self.b} | repairs: Old Two.md | bullets: b2=====\n{self.tb.rstrip()}\n"
                f"=====BULLETS=====\nOld Two.md#b1 | kept in {self.a}\nOld Two.md#b2 | kept in {self.b}\n"
                "Old Two.md#b3 | dropped: not in this transcript\n")
        b1, u1, res1, _ = self.run_batch(state, rep1, review=False, finalize=False)
        rc.review_unit(self.staging, self.zk, u1)
        for d in u1.glob("review-*"):  # B's review did not finish (timeout): no verdict
            if json.loads((d / "request.json").read_text()).get("note", "").endswith(f"{self.b}.md"):
                shutil.rmtree(d)
        ev1 = ri.finalize(self.staging, self.run_dir, u1, [], 0, self.zk)
        acts1 = {Path(n["note"]).stem: n["action"] for n in ev1["notes"]}
        self.assertEqual((acts1[self.a], acts1[self.b]), ("published", "pending-review"))
        # Video 2 writes C for b3.
        rep2 = (f"=====NOTE: {self.c} | repairs: Old Two.md | bullets: b3=====\n{self.tc.rstrip()}\n"
                f"=====BULLETS=====\nOld Two.md#b3 | kept in {self.c}\n")
        b2, u2, res2, ev2 = self.run_batch(state, rep2)
        self.assertEqual(b2["vid"], self.V2)
        acts2 = {Path(n["note"]).stem: n["action"] for n in ev2["notes"]}
        self.assertEqual(acts2[self.c], "published")
        stub = self.vpath("Old Two").read_text()
        self.assertIn(f"[[{self.c}]]", stub)
        self.assertNotIn(f"[[{self.b}]]", stub)  # B waits, and the stub no longer says so
        self.assertNotIn("superseded_pending", stub)
        # The pending unit of B publishes B and rewrites the stub.
        pu = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        rc.review_unit(self.staging, self.zk, pu)
        ev3 = ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        acts3 = {Path(n["note"]).stem: n["action"] for n in ev3["notes"]}
        self.assertEqual(acts3[self.b], "published")
        stub = self.vpath("Old Two").read_text()
        self.assertIn(f"[[{self.b}]]", stub)
        self.assertNotIn(f"[[{self.c}]]", stub)  # C is published but the stub dropped it
        self.assertTrue(self.vpath(self.c).is_file())


class FixCallDropOrRename(_Repair):
    """D: the ledger is recorded in repair_state.json right after the repair call
    (record_answers), BEFORE the fix calls. A fix call that DROPS the target note, or
    RENAMES it and the renamed note is then quarantined, does not reopen the bullets:
    reopen_rejected_bullets matches the quarantined stems against the ledger titles, and
    a dropped note is in no result at all."""

    def _first(self, title: str):
        import hashlib
        rel = self.old(title, 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: {title}.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\n{title}.md#b1 | kept in {self.a}\n")
        b, u, res, _ = self.run_batch(state, rep, review=False, finalize=False)
        a_sha = hashlib.sha256((u / "out" / PN / f"{self.a}.md").read_text().encode()).hexdigest()
        return rel, state, b, u, a_sha

    def test_fix_drop(self) -> None:
        import review_call as rc
        rel, state, b, u, a_sha = self._first("Old Drop")
        os.environ["REJECT_SHAS"] = json.dumps([a_sha])
        rc.review_unit(self.staging, self.zk, u)
        self.fake(fix=["=====DROP=====\nthe passage does not support the title"])
        out = sc.fix_unit(self.staging, self.zk, u, "review")
        self.assertEqual(out[0]["result"], "dropped")
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.assertFalse(self.vpath(self.a).exists())
        self.assertNotIn("superseded", self.vpath("Old Drop").read_text())
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["titles"], e["unsupported_bullets"]), ("done", [self.a], []))
        self.assertEqual(ri.repair_open_bullets(self.staging), [])

    def test_fix_rename_then_quarantined(self) -> None:
        import hashlib

        import review_call as rc
        rel, state, b, u, a_sha = self._first("Old Ren")
        os.environ["REJECT_SHAS"] = json.dumps([a_sha])
        rc.review_unit(self.staging, self.zk, u)
        new = "Planche Lean Floor Hedged"
        tx = self.ta.replace(f"# {self.a}\n", f"# {new}\n")
        self.fake(fix=[f"=====NOTE: {new}=====\n{tx}"])
        out = sc.fix_unit(self.staging, self.zk, u, "review")
        self.assertEqual(out[0]["result"], "renamed", out)
        new_sha = hashlib.sha256((u / "out" / PN / f"{new}.md").read_text().encode()).hexdigest()
        os.environ["REJECT_SHAS"] = json.dumps([new_sha])
        ri.check_unit(self.staging, u, self.zk)  # as zr_review_repair_single does
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        acts = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertEqual(acts.get(new), "quarantined", ev["notes"])
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["bullets"]["Old Ren.md#b1"]["answers"][b["vid"]]["decision"], "kept")  # not reopened
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["unsupported_bullets"]), ("done", []))


class PartialStubNeverCloses(_Repair):
    """A note with two source videos whose bullets ALL settle in video 1: `last` was False
    (pos 0 of 2), so the stub says `superseded_pending: bullets with a later source video`.
    next_batch then skips video 2 (nothing pending) and closes the note: no later unit
    writes the complete stub, and nothing sets `closed` in repair_partial.json, so the
    partial stub and its registry entry stay for good."""

    V2 = "vid-EXAMPLE0001"

    def test_pending_line_stays_forever(self) -> None:
        import shutil

        from tests.unit.test_run_integrity import ZR
        from tests.unit.test_synth_single import V5
        shutil.copy(ZR.parent / "tests/fixtures/note_contract/lit_fictional" / f"{self.V2}.md", self.staging / "lit")
        rel = self.old("Old Both", 1, srcs=(V5, self.V2))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Both.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Both.md#b1 | kept in {self.a}\n")
        b, u, res, ev = self.run_batch(state, rep)
        self.assertEqual(b["last"][rel], False)
        self.assertIn("superseded_pending", self.vpath("Old Both").read_text())
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))  # video 2 is never asked
        self.assertEqual(json.loads(state.read_text())["notes"][rel]["status"], "done")
        u2 = self.new_unit("repair", [])
        ri.finalize(self.staging, self.run_dir, u2, [], 0, self.zk)  # any later finalize
        self.assertIn("bullets with a later source video", self.vpath("Old Both").read_text())
        self.assertIn(rel, ri.summary(self.run_dir, self.staging)["old_notes_partially_replaced"])


class DropCheckReplacesWrittenNote(_Repair):
    """D: the drop-check request lists only the TITLES of the notes already written; its
    prompt asks for "the full new version of an already written note with this bullet
    added". parse result: the drop-check note REPLACES the first reply's note of the same
    title (synth_call._drop_check). The bullets that the first reply kept in it stay
    `kept`, but their text is no longer in the note that is published."""

    def test_dropcheck_version_replaces_note(self) -> None:
        import re

        from tests.unit.test_synth_single import V5
        lit = (self.staging / "lit" / f"{V5}.md").read_text()
        quote = re.search(r"\[\d\d:\d\d\]\s*(.{40,120})", lit).group(1)
        rel = self.old("Old Dc", 2)
        p = self.vpath("Old Dc")
        p.write_text(p.read_text().replace("- legacy bullet 2", f"- {quote}"))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Dc.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Dc.md#b1 | kept in {self.a}\nOld Dc.md#b2 | dropped: not in this transcript\n")
        tb_as_a = self.tb.replace(f"# {self.b}\n", f"# {self.a}\n")
        self.fake(repair=[rep], synth=[f"=====NOTE: {self.a} | repairs: Old Dc.md | bullets: b2=====\n{tb_as_a.rstrip()}\n"])
        b = sc.next_batch(self.staging, self.zk, state)
        u = self.new_unit("repair", [])
        res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                             b["prior_titles"], b["last"])
        dc = [c for c in self.log() if c["user"].startswith("DROP CHECK")]
        self.assertEqual(len(dc), 1)
        lead_a = [ln for ln in self.ta.split("\n# ", 1)[1].split("\n") if ln.strip()][1]
        self.assertNotIn(lead_a, dc[0]["user"])  # the written note's text is not in the request
        shadow_a = (u / "out" / PN / f"{self.a}.md").read_text()
        self.assertNotIn(lead_a, shadow_a)  # the first reply's version of A is gone
        self.assertEqual(res["bullets"]["Old Dc.md#b1"]["decision"], "kept")
        self.assertEqual(res["bullets"]["Old Dc.md#b2"]["decision"], "corrected")


class BudgetStopInFollowUp(_Repair):
    """D: a budget stop in a follow-up call (here the ledger call) keeps the notes of the
    first reply (they are written and finalized), but record_answers sees `stopped` and
    records NO answer. The note is handled by the pending unit; when the batch is handed
    out again the note is a stub, so next_batch closes it as `done-elsewhere` without
    `unsupported_bullets`: the bullet that the ledger call dropped is open nowhere."""

    def test_follow_up_budget_stop_loses_open_bullet(self) -> None:
        from unittest import mock

        import review_call as rc
        rel = self.old("Old Bud", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Bud.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Bud.md#b1 | kept in {self.a}\n")  # b2 missing: a ledger call follows
        b = sc.next_batch(self.staging, self.zk, state)
        self.fake(repair=[rep])
        u = self.new_unit("repair", [])
        real = ri.budget_reserve
        n = {"k": 0}

        def reserve(run_dir, est):
            n["k"] += 1
            d = real(run_dir, est)
            return dict(d, over=True) if n["k"] == 2 else d

        with mock.patch.object(ri, "budget_reserve", reserve):
            res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                 b["prior_titles"], b["last"])
        self.assertEqual(res["stopped"], "budget-stop")
        self.assertEqual(res["written"], [self.a])
        sc.record_answers(state, b, res)
        self.assertEqual(json.loads(state.read_text())["notes"][rel]["bullets"]["Old Bud.md#b1"]["answers"], {})
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)  # no review (budget): pending
        # Next run: the pending unit reviews and publishes A and the stub.
        pu = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        rc.review_unit(self.staging, self.zk, pu)
        ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        self.assertTrue(self.vpath(self.a).is_file())
        self.assertIn("status: superseded", self.vpath("Old Bud").read_text())
        # The batch is handed out again; the ledger call now drops b2.
        b2 = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual(b2["key"], b["key"])
        self.fake(repair=["=====BULLETS=====\nOld Bud.md#b2 | dropped: not in this transcript\n"])
        u2 = self.new_unit("repair", [])
        res2 = sc.repair_unit(self.staging, self.zk, u2, b2["vid"], b2["notes"], b2["unknown"], b2["bullets"],
                              b2["prior_titles"], b2["last"])
        sc.record_answers(state, b2, res2)
        self.assertEqual(res2["bullets"]["Old Bud.md#b2"]["decision"], "dropped")
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["status"], "done-elsewhere")
        self.assertNotIn("unsupported_bullets", e)
        self.assertEqual(ri.repair_open_bullets(self.staging), [])  # b2 is open nowhere


class TwoTitlesOneRejected(_Repair):
    """D (over-report): `kept in A; B` with A published and B quarantined: the whole answer
    becomes `target-rejected`, so the bullet is listed open (and sent to later videos)
    although the published A holds it."""

    def test_two_title_line_reopened(self) -> None:
        import hashlib

        import review_call as rc
        rel = self.old("Old Pair", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Pair.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====NOTE: {self.b} | repairs: Old Pair.md | bullets: b1=====\n{self.tb.rstrip()}\n"
               f"=====BULLETS=====\nOld Pair.md#b1 | kept in {self.a}; {self.b}\n")
        b, u, res, _ = self.run_batch(state, rep, review=False, finalize=False)
        sha = hashlib.sha256((u / "out" / PN / f"{self.b}.md").read_text().encode()).hexdigest()
        os.environ["REJECT_SHAS"] = json.dumps([sha])
        os.environ["ZR_REVIEW_REPAIR"] = "0"
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.assertTrue(self.vpath(self.a).is_file())
        self.assertIn("bullet: Old Pair.md#b1", self.vpath("Old Pair").read_text())
        sc.next_batch(self.staging, self.zk, state)
        self.assertEqual(ri.repair_open_bullets(self.staging)[0]["bullets"], ["Old Pair.md#b1"])
