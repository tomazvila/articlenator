"""Reviewer C, round 2 (frozen-v7): run_integrity.merge_duplicates / same_note.

Convention: each assertion states the DEFECT. PASS = the defect is present.
FAIL = the defect is fixed (or the API changed). Fake LLM only, no network."""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5, _Single, reply

import synth_call as sc  # noqa: E402


def _retitle(text: str, title: str, lead: str | None = None) -> str:
    text = re.sub(r"(?m)^# .+$", f"# {title}", text, count=1)
    if lead is not None:
        old = next(ln for ln in text.split("\n") if ln.strip() and "[src:" in ln and not ln.startswith(("-", "#")))
        text = text.replace(old, lead, 1)
    return text


TA = "Radoslav Radev Cues Locked Elbows In Planche Lean Presses On The Floor"
TB = "Radoslav Radev Cues Protracted Scapula In Planche Lean Presses On The Floor"


def _two_cues(base: str) -> tuple[str, str]:
    """Two distinct technique cues of one speaker, 6 s apart, no numbers."""
    a = _retitle(base, TA, "Radoslav Radev says to keep the elbows locked in planche lean presses on the floor. "
                           "[src: vid-5zALUKd7h3g @ 01:16]")
    b = _retitle(base, TB, "Radoslav Radev says to keep the scapula protracted in planche lean presses on the floor. "
                           "[src: vid-5zALUKd7h3g @ 01:22]")
    return a, b


class SameNoteSignatures(_Single):
    def test_M1_two_distinct_cues_without_numbers_are_one_note(self) -> None:
        """DEFECT (BLOCKS-SYNTHESIS): no numbers on both sides (nums == set()), equal
        speaker/skill/modality, tags 6 s apart, and the lead overlap is measured against the
        SHORTER lead. 'can be done on the floor' and 'never with the fingers forward' are
        two cues (elbows locked / scapula protracted), but same_note says True: one of them is not published."""
        a, b = _two_cues(self.ta)
        sa, sb = ri._dup_sig(a), ri._dup_sig(b)
        self.assertEqual(sa["nums"], set())
        self.assertNotEqual(sa["key"], sb["key"])
        self.assertTrue(ri.same_note(sa, sb))

    def test_M2_same_numbers_other_exercise_when_one_skill_is_not_stated(self) -> None:
        """DEFECT: 'four sets of planche lean presses' and 'four sets of pseudo planche
        push-ups' (skill `not stated` on one side) are one note: the exercise is only in the
        words, and the `not stated` skill disables the skill rule."""
        b = self.tb.replace("planche lean presses", "pseudo planche push-ups").replace(
            'skill: "pseudo planche push-ups"', 'skill: "not stated"')
        b = re.sub(r"(?m)^# .+$", "# Radoslav Radev Says Four Sets Of Pseudo Planche Push-Ups Are An Option", b, count=1)
        sa, sb = ri._dup_sig(self.tb), ri._dup_sig(b)
        self.assertEqual(sa["nums"], sb["nums"])
        self.assertEqual(sb["skill"], "not stated")
        self.assertTrue(ri.same_note(sa, sb))

    def test_M3_equal_title_ignores_video_numbers_and_claim(self) -> None:
        """DEFECT (MINOR, rare in single mode): equal normalized title plus same speaker is
        a merge even with other videos, other numbers, other modality and no shared time."""
        sa = ri._dup_sig(self.tb)
        other = self.tb.replace("vid-5zALUKd7h3g", "vid-OTHERVIDEO1").replace("four sets", "ten sets").replace(
            'modality: "option"', 'modality: "requirement"')
        sb = ri._dup_sig(other)
        self.assertEqual(sa["key"], sb["key"])
        self.assertFalse(sa["vids"] & sb["vids"])
        self.assertNotEqual(sa["nums"], sb["nums"])
        self.assertTrue(ri.same_note(sa, sb))


class MergeEndToEnd(_Single):
    def test_M4_distinct_cue_is_dropped_from_publication_end_to_end(self) -> None:
        """DEFECT: one reply, two notes for two claims (C1 01:16, C2 01:22). finalize
        merges one into the other; its text is only in staging/merged; claims.json says the
        claim is covered by the other note."""
        a, b = _two_cues(self.ta)
        at, bt = TA, TB
        self.fake(synth=[reply([("C1", "01:16", "floor"), ("C2", "01:22", "fingers forward")],
                               [(at, ["C1"], a), (bt, ["C2"], b)])])
        self.synth()
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        merged = [n for n in ev["notes"] if n["action"] == "merged"]
        self.assertEqual(len(merged), 1)
        cov = json.loads((self.unit / "claims.json").read_text())["coverage"]
        self.assertEqual(cov["C1"]["note"], cov["C2"]["note"])  # both claims "covered" by one note

    def test_M5_merged_into_a_note_that_is_still_pending(self) -> None:
        """DEFECT (SHOULD-FIX): the kept note is chosen by Evidence count only. A note that
        waits for its review (pending) is kept, and a note that passed its review is
        `merged` (not published). If the pending note is rejected later, neither publishes."""
        a = self.tb  # 3 Evidence items
        b = _retitle(self.tb, "Radoslav Radev Allows Four Sets Of Planche Lean Presses After The Workout")
        b = b.split("## Evidence")[0] + "## Evidence\n\n" + "\n".join(
            ln for ln in self.tb.split("## Evidence")[1].split("## Connected")[0].strip().split("\n")[:1]) + \
            "\n\n## Connected Ideas\n\n- [[Home]] — home.\n"
        ra = {"rel": f"{PN}/{self.b}.md", "data": a.encode(), "stub": False, "existed_before": False, "entry": {}}
        rb = {"rel": f"{PN}/Radoslav Radev Allows Four Sets Of Planche Lean Presses After The Workout.md",
              "data": b.encode(), "stub": False, "existed_before": False, "entry": {}}
        self.assertTrue(ri.same_note(ri._dup_sig(a), ri._dup_sig(b)))
        decisions = {ra["rel"]: "pending", rb["rel"]: "publish-reviewed"}
        ri.merge_duplicates(self.staging, self.zk, self.unit, [ra, rb], decisions)
        self.assertEqual(decisions, {ra["rel"]: "pending", rb["rel"]: "merged"})

    def test_M6_merged_into_a_published_note_marked_unsupported(self) -> None:
        """DEFECT (SHOULD-FIX): the vault note of this pipeline has `verification:
        unsupported` (a later review rejected it). _published_contract only checks that it
        is a contract note and not a stub, so the new note of the same claim is merged
        into the rejected one and is not published."""
        self.vpath(self.a).write_text(self.ta.replace("verification: unverified", "verification: unsupported"))
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        copy = self.ta.replace(f"# {self.a}", "# Same Claim Written Again")
        self.fake(synth=[reply([("C1", "01:16", "a")], [("Same Claim Written Again", ["C1"], copy)])])
        self.synth()
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        n = ev["notes"][0]
        self.assertEqual((n["action"], n["merged_into"]), ("merged", f"{PN}/{self.a}.md"))


class MergeInRepair(_Single):
    def test_M7_replacement_merged_into_the_old_note_it_replaces(self) -> None:
        """DEFECT (BLOCKS-REPAIR): the old note X was published by this pipeline earlier, so
        pipeline_published() lists it, and its OLD vault text is in pub_sigs. The repair
        replacement Y (same claim, new title) is `same_note` as X and is merged into X.
        The stub of X was decided before the merge; the link rewrite turns its
        `superseded_by: [[Y]]` into `[[X]]`. Result: X is a stub that points to itself and
        Y is in no vault file."""
        import review_call as rc  # noqa: PLC0415
        old_t = "Old Floor Note"
        self.vpath(old_t).write_text(_retitle(self.ta, old_t))
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{old_t}.md", sources=[V5])
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [f"{PN}/{old_t}.md"], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: {old_t}.md | bullets: b1,b2=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\n{old_t}.md#b1 | kept in {self.a}\n{old_t}.md#b2 | kept in {self.a}\n")
        self.fake(repair=[rep])
        unit = self.new_unit("repair", [])
        b = sc.next_batch(self.staging, self.zk, state)
        res = sc.repair_unit(self.staging, self.zk, unit, b["vid"], b["notes"], b["unknown"], b.get("bullets"),
                             b.get("prior_titles"), b.get("last"))
        sc.record_answers(state, b, res)
        rc.review_unit(self.staging, self.zk, unit)
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
        acts = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertEqual(acts.get(self.a), "merged", (acts, ev.get("duplicates_merged")))
        self.assertFalse(self.vpath(self.a).exists())  # the replacement is in no vault file
        stub = self.vpath(old_t).read_text()
        self.assertIn(f"[[{old_t}]]", stub)  # the old note is a stub that points to itself
        self.assertNotIn(f"[[{self.a}]]", stub)

    def test_M7b_bullet_invariant_does_not_see_the_loss(self) -> None:
        """DEFECT: after M7 the bullets say `kept in <Old Floor Note>` (moved by
        _merge_records), the old note is a stub, and bullet_invariant reports nothing
        because it needs a published target to report a lost bullet."""
        self.test_M7_replacement_merged_into_the_old_note_it_replaces()
        st = json.loads(sc.state_path(self.staging).read_text())
        e = st["notes"][f"{PN}/Old Floor Note.md"]
        titles = {t for b in e["bullets"].values() for a in b["answers"].values() for t in (a.get("titles") or [a.get("title")])}
        self.assertEqual(titles, {"Old Floor Note"}, json.dumps(e)[:1500])
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])


class MergeRecords(_Single):
    def test_M8_untagged_bullet_settles_in_any_kept_note(self) -> None:
        """DEFECT (SHOULD-FIX): `covered = not bt or ...`: a bullet with no [src: ...] tag
        (every legacy bullet, see OLD_NOTE) counts as covered by the kept note whatever
        the kept note cites."""
        st = {"notes": {"o": {"titles": ["Drop"], "bullets": {
            "o#b1": {"text": "legacy bullet about ring dips", "answers": {"v": {"decision": "kept", "title": "Drop"}}}}}},
            "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        ri._merge_records(self.staging, self.zk, self.unit, "Drop", "Kept", {"evidence_times": {("vid-zzz", 9999)}})
        a = json.loads((self.staging / "repair_state.json").read_text())["notes"]["o"]["bullets"]["o#b1"]["answers"]["v"]
        self.assertEqual((a["decision"], a["title"]), ("kept", "Kept"))
