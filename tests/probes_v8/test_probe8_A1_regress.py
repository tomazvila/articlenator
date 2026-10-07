"""Probe 8 A1 (third review, reviewer A1): earlier probes of my area whose setup or API
changed, adapted minimally, and judged by behaviour.

Convention of THIS file: the assertion states the CORRECT behaviour. PASS = fixed.
Exception: a test whose name starts with `test_defect_` states the DEFECT (PASS = defect).
Fake LLMs only, no network."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_synth_single import V5

import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402

V2 = "vid-EXAMPLE0001"


def _rep(old: str, title: str, bids: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bids}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


def _old_note(title: str, bullets: list[str], srcs: tuple = (V5,)) -> str:
    src = "".join(f"  - id: {v}\n    speaker: X\n" for v in srcs)
    return ("---\ntype: permanent note\nsources:\n" + src + "created: 2026-09-12\nverification: unverified\n"
            "tags: [calisthenics, x, zettelkasten, permanent-note]\n---\n\n# " + title + "\n\nLegacy claim.\n\n"
            "## Details\n\n" + "".join(f"- {b}\n" for b in bullets) + "\n## Connected Ideas\n\n- [[Other]]\n")


B_FLOOR = "You do not need parallettes: planche lean presses can be done on the wrists on the floor."
B_FOUR = "Four sets of planche lean presses can be done after the planche workout, like a conditioning exercise."
B_SLOW = "Slow and controlled planche lean presses build strength and feeling for the real planche attempts."


class V1WaitingVideo2(_Repair):
    """V1 (adapted from WaitingVideo2FailsForGood): two passes with no reply, then the reply
    arrives. Pass 3 hands the batch out again and uses the reply; the note is not `failed`."""

    def test_ok_late_reply_is_used(self) -> None:
        from tests.unit.test_run_integrity_r13 import work
        shutil.copy(ZR.parent / "tests/fixtures/note_contract/lit_fictional" / f"{V2}.md", self.staging / "lit")
        rel = self.old("Old Two Late", 2, srcs=(V5, V2))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        r1 = _rep("Old Two Late", self.a, "b1", self.ta,
                  f"Old Two Late.md#b1 | kept in {self.a}\nOld Two Late.md#b2 | dropped: not in this transcript\n")
        self.run_batch(state, r1)
        os.environ["ZR_LLM_BACKEND"] = "files"
        for _ in range(2):
            ri.release_waiting(self.staging)
            sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
            b = sc.next_batch(self.staging, self.zk, state)
            u = self.new_unit("repair", [])
            res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                 b["prior_titles"], b["last"])
            self.assertEqual(res["stopped"], "waiting-for-reply")
            sc.record_answers(state, b, res)
            ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        r2 = _rep("Old Two Late", self.b, "b2", self.tb, f"Old Two Late.md#b2 | kept in {self.b}\n")
        self.assertEqual(work(self.staging / "exchange", lambda req: r2 if req.get("kind") == "repair" else None), 1)
        ri.release_waiting(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        b = sc.next_batch(self.staging, self.zk, state)
        self.assertIsNotNone(b)
        self.assertEqual(b["vid"], V2)
        u = self.new_unit("repair", [])
        res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                             b["prior_titles"], b["last"])
        self.assertNotEqual(res.get("stopped"), "waiting-for-reply")
        sc.record_answers(state, b, res)
        e = json.loads(state.read_text())["notes"][rel]
        self.assertNotEqual(e["status"], "failed")
        self.assertIn(V2, e["bullets"]["Old Two Late.md#b2"]["answers"])  # the reply was used


class V8HealUserEdit(_Repair):
    """V8 / R15 (adapted from HealOverwritesUserEdit): the normal finalize does not heal any
    more; the edited old note stays and is listed for the operator. Also after recover()."""

    def test_ok_edit_survives_and_is_reported(self) -> None:
        rel = self.old("Old Edit", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        r = _rep("Old Edit", self.a, "b1", self.ta, f"Old Edit.md#b1 | kept in {self.a}\n")
        b, u, res, _ = self.run_batch(state, r, finalize=False)
        p = self.vpath("Old Edit")
        p.write_text(p.read_text() + "\nUSER ADDED THIS LINE TODAY.\n")
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        ri.recover(self.staging, self.zk)
        self.assertIn("USER ADDED THIS LINE TODAY.", p.read_text())
        ops = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertIn(rel, [x.get("note") for x in ops])


def _archive(staging: Path, old: str) -> None:
    """Round 21 adaptation: the archived old note (bullets a, b) of a stub."""
    from tests.unit.test_run_integrity import OLD_NOTE
    p = staging / "retired" / "r0" / "u0" / PN / f"{old}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(OLD_NOTE.format(t=old).replace("- legacy bullet\n", "- Planche lean presses can be done on the floor without parallettes.\n- Four sets of planche lean presses after the planche workout are an option.\n"))


class V9RespeakRename(_Repair):
    """V9 (adapted from RenamedTargetDropsLink): the unresolved note is recorded as published
    by this pipeline (respeak refuses other notes now) and uses the new speaker value."""

    def test_ok_stub_keeps_the_renamed_target(self) -> None:
        old = "Old Spk"
        rel = f"{PN}/{old}.md"
        unres = "Unresolved Speaker (Ch) Says Floor Presses Need No Parallettes"
        t_un = self.ta.replace(f"# {self.a}\n", f"# {unres}\n")
        t_un = t_un.replace("verification:", "speaker_status: unresolved\nverification:", 1)
        t_un = re.sub(r'(?m)^(\s+speaker:\s*).*$', r'\1"unresolved speaker, Ch video"', t_un, count=1)
        self.vpath(unres).write_text(t_un)
        self.vpath(self.b).write_text(self.tb)
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{unres}.md", sources=[V5])
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.b}.md", sources=[V5])
        self.vpath(old).write_text(ri.repair_stub(old, [unres, self.b]))
        _archive(self.staging, old)  # round 21 adaptation: a pipeline stub has its archived old text
        st = {"notes": {rel: {"sha": "x", "status": "done", "titles": [unres, self.b], "pos": 0, "sources": [V5],
                              "bullets": {f"{old}.md#b1": {"text": "Planche lean presses can be done on the floor without parallettes.", "answers": {V5: {"decision": "kept", "title": unres}}},
                                          f"{old}.md#b2": {"text": "Four sets of planche lean presses after the planche workout are an option.", "answers": {V5: {"decision": "kept", "title": self.b}}}},
                              "unsupported_bullets": []}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        out = sc.respeak(self.staging, self.zk, V5, "Sasha", apply=True, force_name=True)  # W26 adaptation
        new_title = out[0]["to"][:-3]
        self.assertTrue(self.vpath(new_title).is_file(), out)
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = self.vpath(old).read_text()
        self.assertIn(f"[[{new_title}]]", stub)
        self.assertNotIn(f"bullet: {old}.md#b1", stub)
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])


class V10Chain(_Repair):
    """V10 (adapted from ChainedRepairDropsLink): the stub follows the chain A -> A2."""

    def test_ok_stub_follows_the_chain(self) -> None:
        old = "Old Chain"
        rel = f"{PN}/{old}.md"
        a2 = "Second Version Of Floor Presses"
        self.vpath(a2).write_text(self.ta.replace(f"# {self.a}\n", f"# {a2}\n"))
        self.vpath(self.a).write_text(ri.repair_stub(self.a, [a2]))
        self.vpath(self.b).write_text(self.tb)
        self.vpath(old).write_text(ri.repair_stub(old, [self.a, self.b]))
        _archive(self.staging, old)  # round 21 adaptation: a pipeline stub has its archived old text
        st = {"notes": {rel: {"sha": "x", "status": "done", "titles": [self.a, self.b], "pos": 0, "sources": [V5],
                              "bullets": {f"{old}.md#b1": {"text": "Planche lean presses can be done on the floor without parallettes.", "answers": {V5: {"decision": "kept", "title": self.a}}},
                                          f"{old}.md#b2": {"text": "Four sets of planche lean presses after the planche workout are an option.", "answers": {V5: {"decision": "kept", "title": self.b}}}},
                              "unsupported_bullets": []}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = self.vpath(old).read_text()
        self.assertIn(f"[[{a2}]]", stub)
        self.assertNotIn(f"bullet: {old}.md#b1", stub)


class V11InPlaceWaits(_Repair):
    """V11 (adapted from HealWhileInPlacePending): "in place and split", the in-place version
    waits for review, sibling B publishes. Correct: b1's reviewed in-place text publishes
    later, or b1 stays OPEN for the operator; it is never recorded unsupported, and the old
    note is never left as a complete stub that hides b1."""

    def test_ok_in_place_version_is_not_lost(self) -> None:
        rel = f"{PN}/Old Inw.md"  # real bullet text (the v7 `legacy bullet N` is unverifiable)
        self.vpath("Old Inw").write_text(_old_note("Old Inw", [B_FLOOR, B_FOUR]))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        inp = self.ta.replace(f"# {self.a}\n", "# Old Inw\n")
        rep = (f"=====NOTE: Old Inw | repairs: Old Inw.md | bullets: b1=====\n{inp.rstrip()}\n"
               f"=====NOTE: {self.b} | repairs: Old Inw.md | bullets: b2=====\n{self.tb.rstrip()}\n"
               f"=====BULLETS=====\nOld Inw.md#b1 | kept in Old Inw\nOld Inw.md#b2 | kept in {self.b}\n")
        b, u, res, _ = self.run_batch(state, rep, review=False, finalize=False)
        rc.review_unit(self.staging, self.zk, u)
        for d in u.glob("review-*"):
            if json.loads((d / "request.json").read_text()).get("note", "").endswith("Old Inw.md"):
                shutil.rmtree(d)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.after1 = self.vpath("Old Inw").read_text()
        self.assertEqual(ev.get("stubs_healed") or [], [])
        pu = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        rc.review_unit(self.staging, self.zk, pu)
        ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        sc.next_batch(self.staging, self.zk, state)
        e = json.loads(state.read_text())["notes"][rel]
        now = self.vpath("Old Inw").read_text()
        b1_published = "status: superseded" not in now and "[src:" in now
        self.assertTrue(b1_published or "bullet: Old Inw.md#b1" in now, (self.after1, now))
        self.assertNotIn("Old Inw.md#b1", e.get("unsupported_bullets") or [], now)
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])


class V32DotSlash(_Repair):
    """V32 (adapted from DotSlashPath / SameNoteTwoSpellings): one normalized state entry."""

    def test_ok_one_entry(self) -> None:
        self.old("Old Twice", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [f"./{PN}/Old Twice.md", f"{PN}/Old Twice.md"], state_file=state)
        self.assertEqual(sorted(json.loads(state.read_text())["notes"]), [f"{PN}/Old Twice.md"])


class V34RevertedStub(_Repair):
    """V34 (read in v7): the user reverts a stub to the original text. Correct: the next
    finalize does not stub the note again (or lists it for the operator first)."""

    def test_ok_revert_is_respected(self) -> None:
        old = "Old Revert"
        rel = f"{PN}/{old}.md"
        orig = _old_note(old, [B_FLOOR])
        self.vpath(old).write_text(orig)
        self.vpath(self.a).write_text(self.ta)
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        sha = ri._sha256(self.vpath(old))
        st = {"notes": {rel: {"sha": sha, "status": "done", "titles": [self.a], "pos": 0, "sources": [V5],
                              "bullets": {f"{old}.md#b1": {"text": B_FLOOR, "answers": {V5: {
                                  "decision": "kept", "title": self.a, "titles": [self.a]}}}},
                              "unsupported_bullets": []}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        # the stub was published once; the user put the original text back (same sha)
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        self.assertEqual(self.vpath(old).read_text(), orig, "the reverted note is stubbed again")


class PendingTargetV8(_Repair):
    """R7 / PendingTargetV7, adapted: the old note's bullets have real text that A, B and C
    carry (the v7 fixture bullets `legacy bullet N` share no word with any note). b3 is asked
    of video 1 too (C cites video 1). Correct: the final stub links A, B and C, has no
    pending line, and the invariant is clean."""

    def test_ok_final_stub_links_every_published_target(self) -> None:
        old = "Old Two"
        rel = f"{PN}/{old}.md"
        self.vpath(old).write_text(_old_note(old, [B_FLOOR, B_FOUR, B_SLOW]))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep1 = (f"=====NOTE: {self.a} | repairs: {old}.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                f"=====NOTE: {self.b} | repairs: {old}.md | bullets: b2=====\n{self.tb.rstrip()}\n"
                f"=====NOTE: {self.c} | repairs: {old}.md | bullets: b3=====\n{self.tc.rstrip()}\n"
                f"=====BULLETS=====\n{old}.md#b1 | kept in {self.a}\n{old}.md#b2 | kept in {self.b}\n"
                f"{old}.md#b3 | kept in {self.c}\n")
        b1, u1, res1, _ = self.run_batch(state, rep1, review=False, finalize=False)
        self.assertEqual({k.split("#")[1]: v["decision"] for k, v in res1["bullets"].items()},
                         {"b1": "kept", "b2": "kept", "b3": "kept"})
        rc.review_unit(self.staging, self.zk, u1)
        for d in u1.glob("review-*"):
            if json.loads((d / "request.json").read_text()).get("note", "").endswith(f"{self.b}.md"):
                shutil.rmtree(d)
        ri.finalize(self.staging, self.run_dir, u1, [], 0, self.zk)
        pu = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        rc.review_unit(self.staging, self.zk, pu)
        ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        stub = self.vpath(old).read_text()
        for t in (self.a, self.b, self.c):
            self.assertIn(f"[[{t}]]", stub)
        self.assertNotIn("superseded_pending", stub)
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])

    def test_defect_v7_fixture_accepts_b1_by_time_only(self) -> None:
        """DEFECT form (PASS = defect): with the v7 fixture, `legacy bullet 1` (no content
        word of A) is accepted as kept in A by the time window alone, while `legacy bullet 2`
        in B is unverified. The CHANGED rationale holds only because of where B's Evidence is."""
        lit = (self.staging / "lit" / f"{V5}.md").read_text()
        out = {}
        for bid, bt, t, tx in (("x#b1", "legacy bullet 1", self.a, self.ta), ("x#b2", "legacy bullet 2", self.b, self.tb)):
            p = {"notes": [{"title": t, "text": tx, "complete": True, "truncated": False}],
                 "bullets": {bid: {"decision": "kept", "title": t, "titles": [t]}}, "problems": []}
            sc._verify_ledger(p, {bid: sc.bullet_candidates(lit, bt)}, self.zk, {bid: bt})
            out[bid] = p["bullets"][bid]["decision"]
        self.assertEqual(out, {"x#b1": "kept", "x#b2": "unverified"})
