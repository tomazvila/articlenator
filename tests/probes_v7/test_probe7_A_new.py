"""Probe 7 A (reviewer A, round 2): attacks on the round-14..17 repair mechanisms.

Convention of THIS file: the assertion states the DEFECT. PASS = the defect is present.
FAIL = the defect is not present (or the setup broke; read the failure). Fake LLMs only."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_synth_single import V5

import synth_call as sc  # noqa: E402

V2 = "vid-EXAMPLE0001"


def _rep(old: str, title: str, bids: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bids}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class WaitingBatchFails(_Repair):
    """N1: files mode. A repair batch that waits for a worker reply is set back to
    `running` at each run start (release_waiting), and next_batch counts each hand-out.
    After two passes with no reply, the batch is `failed`, the note is `failed`, and its
    bullets are recorded unsupported, although no call failed."""

    def test_waiting_twice_marks_the_note_failed(self) -> None:
        rel = self.old("Old Slow", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        with mock.patch.object(sc, "_cached_call", side_effect=sc.PendingReply("k123", Path("/nonexistent/req.json"))):
            for _ in range(2):  # pass 1 and pass 2: the worker has not answered
                b = sc.next_batch(self.staging, self.zk, state)
                self.assertIsNotNone(b)
                u = self.new_unit("repair", [])
                res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                     b["prior_titles"], b["last"])
                self.assertEqual(res["stopped"], "waiting-for-reply")
                sc.record_answers(state, b, res)
                ri.release_waiting(self.staging)  # the next pass starts
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))  # pass 3: the batch is dead
        st = json.loads(state.read_text())
        self.assertEqual(st["notes"][rel]["status"], "failed")
        self.assertEqual(st["notes"][rel]["unsupported_bullets"], ["Old Slow.md#b1"])
        self.assertEqual([b_["status"] for b_ in st["batches"].values()], ["failed"])


class BudgetStopFails(_Repair):
    """N2: the same counter for a budget stop of the first call: two runs that start with
    an empty budget mark the note `failed` and its bullets unsupported."""

    def test_two_budget_stops_mark_the_note_failed(self) -> None:
        rel = self.old("Old Poor", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        with mock.patch.object(sc, "_cached_call", side_effect=sc.BudgetStop("budget")):
            for _ in range(2):
                b = sc.next_batch(self.staging, self.zk, state)
                self.assertIsNotNone(b)
                u = self.new_unit("repair", [])
                res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                     b["prior_titles"], b["last"])
                self.assertEqual(res["stopped"], "budget-stop")
                sc.record_answers(state, b, res)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["unsupported_bullets"]), ("failed", ["Old Poor.md#b1"]))


class ExtendDropsContent(_Repair):
    """N3: the R5 check compares only the bullet ids in the NOTE header. Call 2 (same
    video, ZR_REPAIR_MAX_BULLETS=1) rewrites note A with the header `bullets: b1,b2` but with
    a text that holds no part of call 1's version. A publishes with the new text, b1 stays
    `kept in A`, and bullet_invariant reports nothing."""

    def test_extended_note_loses_text_and_invariant_is_clean(self) -> None:
        os.environ["ZR_REPAIR_MAX_BULLETS"] = "1"
        rel = self.old("Old Ext", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        r1 = _rep("Old Ext", self.a, "b1", self.ta, f"Old Ext.md#b1 | kept in {self.a}\n")
        b1, u1, res1, ev1 = self.run_batch(state, r1)
        self.assertEqual(self.vpath(self.a).read_text().split("---", 2)[2], self.ta.split("---", 2)[2])
        lead_a = [ln for ln in self.ta.split("\n") if "[src:" in ln][0]
        tb_as_a = self.tb.replace(f"# {self.b}\n", f"# {self.a}\n")
        r2 = _rep("Old Ext", self.a, "b1,b2", tb_as_a, f"Old Ext.md#b2 | kept in {self.a}\n")
        b2, u2, res2, ev2 = self.run_batch(state, r2)
        self.assertEqual(b2["prior_titles"][rel], [self.a])
        self.assertNotIn("extend-lost-bullets", {p["type"] for p in res2["problems"]})
        a_now = self.vpath(self.a).read_text()
        self.assertNotIn(lead_a, a_now)  # call 1's text of A is gone from the vault
        st = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(st["bullets"]["Old Ext.md#b1"]["answers"][V5]["decision"], "kept")
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        self.assertEqual(ri.repair_open_bullets(self.staging), [])


class HealOverwritesUserEdit(_Repair):
    """N4 (R15): the user edits the old note while the unit runs. The harness stub fails
    `conflict-stale-base`, the replacement publishes, recompute_repair_stubs leaves the
    edited note (R15 guard), and then heal_beside, in the same finalize, replaces the
    edited note with a stub. The user's added text is only in retired/."""

    def test_heal_ignores_the_r15_guard(self) -> None:
        rel = self.old("Old Edit", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        r = _rep("Old Edit", self.a, "b1", self.ta, f"Old Edit.md#b1 | kept in {self.a}\n")
        b, u, res, _ = self.run_batch(state, r, finalize=False)
        p = self.vpath("Old Edit")
        p.write_text(p.read_text() + "\nUSER ADDED THIS LINE TODAY.\n")
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        acts = {Path(n["note"]).stem: n for n in ev["notes"]}
        self.assertIn("conflict-stale-base", acts["Old Edit"]["failure_types"])
        self.assertEqual(ev["stubs_recomputed"], [])
        self.assertEqual([h["note"] for h in ev["stubs_healed"]], [rel])
        self.assertNotIn("USER ADDED THIS LINE TODAY.", p.read_text())
        self.assertIn("status: superseded", p.read_text())
        arch = [q.read_text() for q in self.staging.rglob("retired/**/Old Edit*.md")]
        self.assertTrue(any("USER ADDED THIS LINE TODAY." in t for t in arch))  # only in the archive
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])  # nothing reports it


class CrashHealMultiVideo(_Repair):
    """N5: a kill between the replacement write and the stub write, for an old note with a
    second source video and an open bullet. After recover, is video 2 still asked?"""

    def test_crash_heal_skips_video_2(self) -> None:
        shutil.copy(ZR.parent / "tests/fixtures/note_contract/lit_fictional" / f"{V2}.md", self.staging / "lit")
        rel = self.old("Old Mv", 2, srcs=(V5, V2))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        r = _rep("Old Mv", self.a, "b1", self.ta,
                 f"Old Mv.md#b1 | kept in {self.a}\nOld Mv.md#b2 | dropped: not in this transcript\n")
        b, u, res, _ = self.run_batch(state, r, finalize=False)
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
        nb = sc.next_batch(self.staging, self.zk, state)
        # DEFECT form: video 2 is never asked for b2. (Result on v7: FAIL, video 2 is asked:
        # recompute_repair_stubs writes the partial stub before heal_beside runs.)
        self.assertIsNone(nb, "video 2 is asked: no defect")


class RenamedTargetDropsLink(_Repair):
    """N6: a published target is renamed later (respeak of an unresolved speaker). The
    repair state still names the old title. recompute_repair_stubs then rewrites the old
    note's stub WITHOUT the link to the renamed note and lists its bullet as open for
    good; bullet_invariant reports clean (the bullet is 'listed'), repair_open_bullets is
    empty, and no later video will handle the bullet (status done)."""

    def test_respeak_then_recompute(self) -> None:
        old = "Old Spk"
        self.vpath(old).write_text("x")
        rel = f"{PN}/{old}.md"
        unres = "Unresolved Speaker (Ch) Says Floor Presses Need No Parallettes"
        t_un = self.ta.replace(f"# {self.a}\n", f"# {unres}\n")
        t_un = t_un.replace("verification:", "speaker_status: unresolved\nverification:", 1)
        import re
        t_un = re.sub(r'(?m)^(\s+speaker:\s*).*$', r'\1"unresolved (Ch video, several voices)"', t_un, count=1)
        self.vpath(unres).write_text(t_un)
        self.vpath(self.b).write_text(self.tb)
        self.vpath(old).write_text(ri.repair_stub(old, [unres, self.b]))
        st = {"notes": {rel: {"sha": "x", "status": "done", "titles": [unres, self.b], "pos": 0, "sources": [V5],
                              "bullets": {f"{old}.md#b1": {"text": "a", "answers": {V5: {"decision": "kept", "title": unres}}},
                                          f"{old}.md#b2": {"text": "b", "answers": {V5: {"decision": "kept", "title": self.b}}}},
                              "unsupported_bullets": []}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        out = sc.respeak(self.staging, self.zk, V5, "Sasha", apply=True)
        new_title = out[0]["to"][:-3]
        self.assertTrue(self.vpath(new_title).is_file())
        self.assertIn(f"[[{new_title}]]", self.vpath(old).read_text())  # rewrite_links fixed the stub
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = self.vpath(old).read_text()
        self.assertNotIn(f"[[{new_title}]]", stub)  # the link to the note that holds b1 is gone
        self.assertIn(f"bullet: {old}.md#b1", stub)  # b1 'open' with status done: nobody handles it
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        self.assertEqual(ri.repair_open_bullets(self.staging), [])


class ChainedRepairDropsLink(_Repair):
    """N7: target A of old note O is itself repaired later (A was marked needs-repair by a
    review pass and became a stub of A2). recompute_repair_stubs does not follow the stub
    chain: O's stub loses [[A]] and lists b1 as open for good."""

    def test_target_becomes_stub(self) -> None:
        old = "Old Chain"
        rel = f"{PN}/{old}.md"
        a2 = "Second Version Of Floor Presses"
        self.vpath(a2).write_text(self.ta.replace(f"# {self.a}\n", f"# {a2}\n"))
        self.vpath(self.a).write_text(ri.repair_stub(self.a, [a2]))
        self.vpath(self.b).write_text(self.tb)
        self.vpath(old).write_text(ri.repair_stub(old, [self.a, self.b]))
        st = {"notes": {rel: {"sha": "x", "status": "done", "titles": [self.a, self.b], "pos": 0, "sources": [V5],
                              "bullets": {f"{old}.md#b1": {"text": "a", "answers": {V5: {"decision": "kept", "title": self.a}}},
                                          f"{old}.md#b2": {"text": "b", "answers": {V5: {"decision": "kept", "title": self.b}}}},
                              "unsupported_bullets": []}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = self.vpath(old).read_text()
        self.assertNotIn(f"[[{self.a}]]", stub)
        self.assertIn(f"bullet: {old}.md#b1", stub)
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        self.assertEqual(ri.repair_open_bullets(self.staging), [])


class MergeSettlesUntaggedBullet(_Repair):
    """N8: _merge_records settles a bullet in the KEPT note when the bullet text has no
    [src:] tag (`covered = not bt or ...`). Old-format bullets have no tag, so the time
    rule of round 16 never applies to them."""

    def test_untagged_bullet_moves_to_kept(self) -> None:
        st = {"notes": {"o": {"titles": ["Drop"], "bullets": {
            "o#b1": {"text": "legacy bullet with no tag", "answers": {"v": {"decision": "kept", "title": "Drop"}}}}}},
              "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        ri._merge_records(self.staging, self.zk, self.new_unit("repair", []), "Drop", "Kept",
                          {"evidence_times": {(V5, 3000)}})  # Kept cites only 50:00
        a = json.loads((self.staging / "repair_state.json").read_text())["notes"]["o"]["bullets"]["o#b1"]["answers"]["v"]
        self.assertEqual((a["decision"], a["title"]), ("kept", "Kept"))


class DotSlashPath(_Repair):
    """N9 (R1, odd path): `./01 Permanent Notes/X.md` passes safe_note_rel unchanged. The
    unit's repair_map key then differs from the checker's shadow path, so the harness stub
    is not recognised (R10 rule) and is quarantined as `model-written-stub`."""

    def test_dot_slash_note_path(self) -> None:
        self.old("Old Dot", 1)
        rel = f"./{PN}/Old Dot.md"
        state = sc.state_path(self.staging)
        plan = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertIn(rel, [r for k in plan["known"] for r in k["notes"]])
        r = _rep("Old Dot", self.a, "b1", self.ta, f"Old Dot.md#b1 | kept in {self.a}\n")
        b, u, res, ev = self.run_batch(state, r)
        acts = {n["note"]: n for n in ev["notes"]}
        stub_e = acts[f"{PN}/Old Dot.md"]
        self.assertEqual(stub_e["action"], "quarantined")
        self.assertIn("model-written-stub", stub_e["failure_types"])
        # Net effect on v7: recompute_repair_stubs writes the stub in the same finalize.
        self.assertIn("status: superseded", self.vpath("Old Dot").read_text())


class ArchiveGlobOtherNote(_Repair):
    """N10: archived_old_text globs `<stem>.v*.md`. Another note whose name starts with
    `<stem>.v` (here `Old.very old draft`) is taken as an archive of `Old`."""

    def test_glob_matches_another_note(self) -> None:
        d = self.staging / "retired" / "r1" / "u1" / PN
        d.mkdir(parents=True)
        (d / "Old.md").write_text(ri.repair_stub("Old", ["A"]))
        other = d / "Old.very old draft.md"
        other.write_text("---\ntype: permanent note\n---\n\n# Old.very old draft\n\nOTHER NOTE TEXT\n")
        self.assertIn("OTHER NOTE TEXT", ri.archived_old_text(self.staging, f"{PN}/Old.md") or "")


class WaitingBatchFailsFilesBackend(_Repair):
    """N1b: N1 with the real files backend (no mock). Two passes with no worker reply; the
    reply arrives before pass 3. Pass 3 hands out nothing: the late reply is never used and
    the note is `failed` with its bullet recorded unsupported."""

    def test_late_reply_is_never_used(self) -> None:
        from tests.unit.test_run_integrity_r13 import work
        os.environ["ZR_LLM_BACKEND"] = "files"
        rel = self.old("Old Late", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        for _ in range(2):
            b = sc.next_batch(self.staging, self.zk, state)
            self.assertIsNotNone(b)
            u = self.new_unit("repair", [])
            res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                 b["prior_titles"], b["last"])
            self.assertEqual(res["stopped"], "waiting-for-reply")
            sc.record_answers(state, b, res)
            ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
            ri.release_waiting(self.staging)
        good = _rep("Old Late", self.a, "b1", self.ta, f"Old Late.md#b1 | kept in {self.a}\n")
        self.assertEqual(work(self.staging / "exchange", lambda req: good if req.get("kind") == "repair" else None), 1)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["unsupported_bullets"]), ("failed", ["Old Late.md#b1"]))
        self.assertFalse(self.vpath(self.a).exists())


class WaitingVideo2FailsForGood(_Repair):
    """N1c: the real pass order (run start: release_waiting; zr_repair_single:
    repair-groups, then next-batch). Video 1 publishes A; the old note is a partial stub.
    Video 2's request waits for two passes. Pass 3: next_batch marks the batch and the note
    `failed`; pass 4: repair-groups does not plan the note again (state `stub`). The reply
    that arrived is never used.
    b2 stays `unsupported` with status `failed` for good."""

    def test_video_2_reply_never_used(self) -> None:
        from tests.unit.test_run_integrity_r13 import work
        shutil.copy(ZR.parent / "tests/fixtures/note_contract/lit_fictional" / f"{V2}.md", self.staging / "lit")
        rel = self.old("Old Two Late", 2, srcs=(V5, V2))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        r1 = _rep("Old Two Late", self.a, "b1", self.ta,
                  f"Old Two Late.md#b1 | kept in {self.a}\nOld Two Late.md#b2 | dropped: not in this transcript\n")
        self.run_batch(state, r1)
        self.assertIn("superseded_pending", self.vpath("Old Two Late").read_text())
        os.environ["ZR_LLM_BACKEND"] = "files"
        for _ in range(2):
            ri.release_waiting(self.staging)
            sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
            b = sc.next_batch(self.staging, self.zk, state)
            self.assertEqual(b["vid"], V2)
            u = self.new_unit("repair", [])
            res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                 b["prior_titles"], b["last"])
            self.assertEqual(res["stopped"], "waiting-for-reply")
            sc.record_answers(state, b, res)
            ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        r2 = _rep("Old Two Late", self.b, "b2", self.tb, f"Old Two Late.md#b2 | kept in {self.b}\n")
        self.assertEqual(work(self.staging / "exchange", lambda req: r2 if req.get("kind") == "repair" else None), 1)
        ri.release_waiting(self.staging)  # pass 3
        plan = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertEqual(plan["resumed"], [rel])  # pass 3: still open before next_batch
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))  # ... and failed inside it
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual((e["status"], e["unsupported_bullets"]), ("failed", ["Old Two Late.md#b2"]))
        ri.release_waiting(self.staging)  # pass 4: the note is not planned again (partial stub)
        plan4 = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertEqual([x["state"] for x in plan4["skipped"] if x["note"] == rel], ["stub"])
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        self.assertFalse(self.vpath(self.b).exists())


class SameNoteTwoSpellings(_Repair):
    """N9b: `./01 Permanent Notes/X.md` and `01 Permanent Notes/X.md` (for example, one from
    the command line and one from --from-candidates or a later run) are two entries of
    repair_state.json for one file, and one batch holds both."""

    def test_two_entries_one_file(self) -> None:
        self.old("Old Twice", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [f"./{PN}/Old Twice.md", f"{PN}/Old Twice.md"], state_file=state)
        self.assertEqual(sorted(json.loads(state.read_text())["notes"]), [f"./{PN}/Old Twice.md", f"{PN}/Old Twice.md"])
        b = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual(sorted(b["notes"]), [f"./{PN}/Old Twice.md", f"{PN}/Old Twice.md"])


class HealWhileInPlacePending(_Repair):
    """N11: "in place and split". The in-place version waits for its review (pending) and
    sibling B publishes. heal_beside sees the old note (not a stub) beside published B and
    overwrites it with a COMPLETE stub [B] in the same finalize: no `note: <old>` and no
    `bullet: b1` pending line, while b1 is in the waiting in-place version."""

    def test_heal_stubs_a_note_whose_in_place_version_waits(self) -> None:
        import review_call as rc
        rel = self.old("Old Inw", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        inp = self.ta.replace(f"# {self.a}\n", "# Old Inw\n")
        rep = (f"=====NOTE: Old Inw | repairs: Old Inw.md | bullets: b1=====\n{inp.rstrip()}\n"
               f"=====NOTE: {self.b} | repairs: Old Inw.md | bullets: b2=====\n{self.tb.rstrip()}\n"
               f"=====BULLETS=====\nOld Inw.md#b1 | kept in Old Inw\nOld Inw.md#b2 | kept in {self.b}\n")
        b, u, res, _ = self.run_batch(state, rep, review=False, finalize=False)
        rc.review_unit(self.staging, self.zk, u)
        for d in u.glob("review-*"):  # the in-place version's review did not finish
            if json.loads((d / "request.json").read_text()).get("note", "").endswith("Old Inw.md"):
                shutil.rmtree(d)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        acts = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertEqual((acts["Old Inw"], acts[self.b]), ("pending-review", "published"))
        stub = self.vpath("Old Inw").read_text()
        self.assertEqual([h["note"] for h in ev["stubs_healed"]], [rel])
        self.assertIn(f'superseded_by: "[[{self.b}]]"', stub)
        self.assertNotIn("superseded_pending", stub)  # complete stub: b1 / the waiting version not named
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        # The waiting in-place version is released later.
        pu = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        rc.review_unit(self.staging, self.zk, pu)
        ev2 = ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        n2 = {Path(n["note"]).stem: n for n in ev2["notes"]}
        # The reviewed in-place version now fails conflict-stale-base (the heal changed the
        # vault file) and is replaced by a stub: b1's repaired text is never published.
        self.assertIn("conflict-stale-base", n2["Old Inw"]["failure_types"])
        self.assertIn("status: superseded", self.vpath("Old Inw").read_text())
        self.assertIn("bullet: Old Inw.md#b1", self.vpath("Old Inw").read_text())
        sc.next_batch(self.staging, self.zk, state)
        e = json.loads(state.read_text())["notes"][rel]
        self.assertIn("Old Inw.md#b1", e["unsupported_bullets"])  # recorded as if its target was rejected
