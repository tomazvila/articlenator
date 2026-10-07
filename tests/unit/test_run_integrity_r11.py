"""Round 11 (pilot 5): reasoning refusal, one budget authority, two-stage synthesis,
duplicate notes, partial replacement stubs, persistent repair state, bullet candidate
passages, unknown-source candidates, smaller checker items. Fake LLMs only, no network."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from tests.unit.test_run_integrity import PN, ZR, _Env, ri
from tests.unit.test_synth_single import V5, _Single

import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402
import verify_claims as vc  # noqa: E402


# --------------------------------------------------------------------------- #
# A: a refused reasoning object stops the call
# --------------------------------------------------------------------------- #


class ReasoningRefusal(_Single):
    def test_review_refusal_for_a_reasoning_model_stops(self) -> None:
        os.environ["ZR_REVIEW_MODEL"] = "anthropic/claude-sonnet-5.5"
        self.assertEqual(rc.review_reasoning("anthropic/claude-sonnet-5.5"), {"effort": "low"})
        self.assertEqual(rc.retry_reasoning("anthropic/claude-sonnet-5.5"), {"effort": "low"})
        self.assertIsNone(rc.review_reasoning("test/plain"))


# --------------------------------------------------------------------------- #
# G: one budget authority
# --------------------------------------------------------------------------- #


class BudgetAuthority(_Single):
    def test_first_call_is_checked_and_the_stop_is_sticky(self) -> None:
        os.environ["ZR_MAX_COST_USD"] = "0.01"  # below one synthesis reservation
        self.fake(synth=["never sent"])
        res = self.synth()
        self.assertEqual(res["outcome"], "budget-stop")
        self.assertEqual(self.log(), [])
        self.assertTrue((self.run_dir / "budget-stop.json").exists())
        os.environ["ZR_MAX_COST_USD"] = "100"
        self.assertTrue(ri.budget_status(self.run_dir)["over"])  # the driver stops too: sticky

    def test_driver_and_call_use_the_same_rule(self) -> None:
        os.environ.update(ZR_MAX_COST_USD="0.5", ZR_BUDGET_NEXT_CALL_USD="0.6")
        self.assertTrue(ri.budget_status(self.run_dir)["over"])
        lib = (ZR / "run_lib.sh").read_text()
        self.assertIn('exit "$ZR_BUDGET_STOP"', lib)


# --------------------------------------------------------------------------- #
# B: two-stage synthesis (B's fixtures)
# --------------------------------------------------------------------------- #

NC = ZR.parent / "tests" / "fixtures" / "note_contract"
EX2 = "vid-EXAMPLE0002"


class TwoStage(_Single):
    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_SYNTH_STAGES"] = "two"
        import shutil
        shutil.copy(NC / "lit_fictional" / f"{EX2}.md", self.staging / "lit")
        self.write_queue([{"id": EX2, "kind": "video", "stage": "claimed", "claimed_from": "extracted",
                           "lit_note": str(self.staging / "lit" / f"{EX2}.md")}])
        self.unit2 = self.new_unit("synth", [EX2])

    def run2(self) -> dict:
        return sc.synth_unit(self.staging, self.zk, self.unit2, [EX2])

    def test_inventory_batches_skip_check_and_yield(self) -> None:
        inv = (NC / "inventory_reply_fictional.txt").read_text()
        batch = (NC / "batch_reply_fictional.txt").read_text()
        self.fake(inventory=[inv], batch=[batch, "=====SKIPPED=====\nC8 | passage unreadable\n",
                                          "=====SKIPPED=====\nC8 | not a transferable training claim: he only "
                                          "repeats the general warm-up advice without content\n"])
        res = self.run2()
        log = self.log()
        self.assertEqual([c["kind"] for c in log], ["inventory", "batch", "batch", "batch"])
        b1 = log[1]["user"]
        for head in ("CLAIMS OF THIS BATCH", "NOTES ALREADY WRITTEN FOR THIS VIDEO", "EXISTING NOTES YOU MAY LINK TO",
                     "C2 | 01:40 |", "| recommendation", "EXCERPT:"):
            self.assertIn(head, b1)
        self.assertNotIn("C1 |", b1)  # typed `other`: skipped without a call
        self.assertNotIn("C8 |", b1)  # batch of at most 5
        self.assertIn("FLAG: the passage is readable", log[3]["user"])
        self.assertIn("NOTES ALREADY WRITTEN FOR THIS VIDEO:\n- none", log[2]["user"])  # round 15: batches are independent
        self.assertEqual(len(res["notes"]), 4)
        y = res["yield"]
        self.assertEqual((y["inventoried"], y["skipped_by_type"], y["written_notes"]), (8, 2, 4))
        self.assertEqual(y["rejected_skips"][0]["why"], "unreadable-on-clean-passage")
        self.assertEqual(res["claims_uncovered"], [])
        self.assertIn("transferable", res["coverage"]["C8"]["skip"])
        claims = json.loads((self.unit2 / "claims.json").read_text())
        self.assertEqual(claims["yield"]["skipped_by_reason"].get("inventory type anecdote"), 1)

    def test_catch_all_without_why_is_resent_once(self) -> None:
        inv = "=====CLAIMS=====\nC1 | 01:40 | leave 48 hours between hard sessions | recommendation\n"
        self.fake(inventory=[inv], batch=["=====SKIPPED=====\nC1 | not a transferable training claim\n",
                                          "=====SKIPPED=====\nC1 | not a transferable training claim\n"])
        res = self.run2()
        self.assertEqual([c["kind"] for c in self.log()], ["inventory", "batch", "batch"])
        self.assertEqual(res["claims_uncovered"], ["C1"])  # still a bare catch-all after the re-send
        self.assertEqual(len(res["yield"]["rejected_skips"]), 2)

    def test_note_without_claims_field_is_matched_by_time_and_duplicates_are_one_note(self) -> None:
        inv = (NC / "inventory_reply_fictional.txt").read_text()
        batch = (NC / "batch_reply_fictional.txt").read_text()
        blocks = batch.split("=====NOTE: ")
        no_field = "=====NOTE: " + blocks[2].replace(" | claims: C4=====", "=====", 1)
        first = "=====NOTE: " + blocks[1] + no_field + "=====SKIPPED=====\nC5 | already covered by [[X]]\n" \
            "C6 | mixed voices and no way to tell the speaker\n"
        # the re-sent C5 gets the predictions note again under another title: the same note
        again = no_field.replace("Tomas Brink Predicts That Most People", "Tomas Brink Predicts Most People", 1) \
            + "=====SKIPPED=====\n"
        self.fake(inventory=[inv], batch=[first, again, "=====SKIPPED=====\nC8 | not a transferable training "
                                                         "claim: no content beyond the word warm-up\n"])
        res = self.run2()
        self.assertIn("claims-matched-by-time", {p["type"] for p in res["problems"]})
        self.assertEqual(res["coverage"]["C4"]["note"].startswith("Tomas Brink Predicts That"), True)
        self.assertIn("duplicate-note", {p["type"] for p in res["problems"]})
        self.assertEqual(sum(1 for t in res["notes"] if t.startswith("Tomas Brink Predicts")), 1)

    def test_excerpts_merge_and_line_numbers(self) -> None:
        lit = (self.staging / "lit" / f"{EX2}.md").read_text()
        ex = sc.excerpts(lit, [{"id": "C2", "at": "01:40", "text": "x"}, {"id": "C3", "at": "01:47", "text": "y"}])
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["ids"], ["C2", "C3"])
        self.assertTrue(sc.excerpts("---\nid: x\n---\n" + "word " * 2000 + "\n" * 5, [{"id": "C1", "at": "line 1",
                                                                                     "text": "z"}]))

    def test_unsafe_speaker_title_is_normalized_without_a_call(self) -> None:
        self.assertEqual(sc._auto_safe_title("sthenics_ Says Ten Sets Are Enough"), "Sthenics Says Ten Sets Are Enough")
        self.assertIsNone(sc._auto_safe_title("Plain Safe Title"))


# --------------------------------------------------------------------------- #
# I: smaller items
# --------------------------------------------------------------------------- #

FP5 = ZR.parent / "tests" / "fixtures" / "pilot5_false_positives"


class SmallerItems(_Env):
    def verify(self, part: str) -> dict:
        p = next(p for p in (FP5 / "notes").glob("*.md") if part in p.name)
        return vc.verify_note(p, vc.LitIndex([FP5 / "lit"]))

    def test_pilot5_checker_false_positives(self) -> None:
        r = self.verify("Three Weeks Is Definitely Enough")  # step numbers with a nearby period
        self.assertEqual([f["type"] for f in r["failures"]], [])
        r = self.verify("Around 10 Effective Sets")  # quote across segments; period from the line before
        self.assertEqual([f["type"] for f in r["failures"]], [])
        self.assertIn("scope-period-from-context", {w["type"] for w in r["warnings"]})

    def test_twice_and_once_forms(self) -> None:
        for a, b in (("train twice a day", "train two times a day"), ("train once a week", "train one time a week")):
            qa, qb = vc.extract_quantities(a)[0], vc.extract_quantities(b)[0]
            self.assertEqual(vc.match_quantity(qa, [qb])[0], "supported", a)

    def test_host_question_is_speaker_evidence(self) -> None:
        q = "so another question how often should you train the front lever"
        self.assertFalse(vc.speaker_evidence_is_claim(q, [q], "He trains the front lever often.", ["David Packer"]))
        self.assertTrue(vc.speaker_cue_match(q, ["David Packer"]).startswith("host-question:"))
        self.assertEqual(vc.speaker_cue_match("david says so", ["David Packer"]), "name:david")
        guest = "light one's very good like yeah but again only when you're close to the real skill"
        self.assertTrue(vc.speaker_evidence_is_claim(guest, [guest], "x", ["David Packer"]))

    def test_fix_passages_carry_timestamps_and_lit_path(self) -> None:
        self.assertIn("with_ts=True", (ZR / "synth_call.py").read_text())
        lib = (ZR / "run_lib.sh").read_text()
        self.assertIn('zr_review_repair_single "$unit_dir"\n      fi', lib)  # the pending step makes the review fix


# --------------------------------------------------------------------------- #
# D, E, F: end-to-end repair (two videos, a split, a rejected target, a budget stop)
# --------------------------------------------------------------------------- #


class RepairEndToEnd(_Single):
    kind = "repair"
    V2 = "vid-EXAMPLE0001"

    def test_split_rejected_target_budget_stop_and_resume(self) -> None:
        import hashlib
        import shutil

        from tests.unit.test_run_integrity import OLD_NOTE
        shutil.copy(ZR.parent / "tests/fixtures/note_contract/lit_fictional" / f"{self.V2}.md", self.staging / "lit")
        src = "".join(f"  - id: {v}\n    speaker: X\n" for v in (V5, self.V2))
        text = OLD_NOTE.format(t="Old Six").replace("created:", f"sources:\n{src}created:").replace(
            "- legacy bullet\n", "".join(f"- legacy bullet {i}\n" for i in range(1, 7)))
        self.vpath("Old Six").write_text(text)
        rel = f"{PN}/Old Six.md"
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        # Video 1: split into two notes; b4..b6 are not in this transcript.
        b1 = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual((b1["vid"], len(b1["bullets"][rel])), (V5, 6))
        ledger = "\n".join([f"Old Six.md#b1 | kept in {self.a}", f"Old Six.md#b2 | corrected in {self.a}",
                            f"Old Six.md#b3 | kept in {self.b}"] +
                           [f"Old Six.md#b{i} | dropped: not in this transcript" for i in (4, 5, 6)])
        rep1 = (f"=====NOTE: {self.a} | repairs: Old Six.md | bullets: b1,b2=====\n{self.ta.rstrip()}\n"
                f"=====NOTE: {self.b} | repairs: Old Six.md | bullets: b3=====\n{self.tb.rstrip()}\n"
                f"=====BULLETS=====\n{ledger}\n")
        self.fake(repair=[rep1, "never sent"])
        u1 = self.new_unit("repair", [])
        res1 = sc.repair_unit(self.staging, self.zk, u1, b1["vid"], b1["notes"], b1["unknown"], b1["bullets"],
                              b1["prior_titles"], b1["last"])
        sc.record_answers(state, b1, res1)
        user1 = [c for c in self.log() if c["kind"] == "repair"][0]["user"]
        self.assertIn("Old Six.md#b4 | legacy bullet 4\n  candidates: [", user1)  # F1
        # The review rejects note B: B is quarantined, A publishes, the old note becomes a stub
        # with superseded_pending (D), never the old text beside A.
        b_sha = hashlib.sha256((u1 / "out" / PN / f"{self.b}.md").read_text().encode()).hexdigest()
        os.environ["REJECT_SHAS"] = json.dumps([b_sha])
        os.environ["ZR_REVIEW_REPAIR"] = "0"
        rc.review_unit(self.staging, self.zk, u1)
        ev1 = ri.finalize(self.staging, self.run_dir, u1, [], 0, self.zk)
        acts = {Path(n["note"]).stem: n["action"] for n in ev1["notes"]}
        self.assertEqual((acts[self.a], acts[self.b], acts["Old Six"]), ("published", "quarantined", "published"))
        stub = self.vpath("Old Six").read_text()
        self.assertIn(f'superseded_by: "[[{self.a}]]"', stub)
        self.assertIn("superseded_pending:", stub)
        self.assertNotIn(f"note: {self.b}", stub)  # round 14 (R14): a quarantined target is not pending
        self.assertIn("bullet: Old Six.md#b3", stub)  # round 12: B's bullet is open again
        self.assertEqual(ev1["replacement_beside_old_note"], [])
        s = ri.summary(self.run_dir, self.staging)
        self.assertIn(rel, s["old_notes_partially_replaced"])
        self.assertEqual(s["replacement_beside_old_note"], [])
        # Video 2: the budget stops the call; nothing is paid, the batch stays open.
        b2 = sc.next_batch(self.staging, self.zk, state)
        # Round 12: b3 (kept in the rejected note B) goes on to video 2 with b4..b6.
        self.assertEqual((b2["vid"], b2["bullets"][rel]), (self.V2, [f"Old Six.md#b{i}" for i in (3, 4, 5, 6)]))
        self.assertNotIn(self.b, b2["prior_titles"][rel])
        os.environ["ZR_MAX_COST_USD"] = "0.0001"
        u2 = self.new_unit("repair", [])
        res2 = sc.repair_unit(self.staging, self.zk, u2, b2["vid"], b2["notes"], b2["unknown"], b2["bullets"],
                              b2["prior_titles"], b2["last"])
        sc.record_answers(state, b2, res2)
        self.assertEqual(res2["stopped"], "budget-stop")
        n_calls = len(self.log())
        # Second run: resumes at video 2 (the same batch), video 1 is not asked again.
        os.environ["ZR_MAX_COST_USD"] = "5"
        run2 = ri.start(self.staging, "repair", self.zk)
        b2b = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual((b2b["key"], b2b["vid"]), (b2["key"], self.V2))
        self.assertIn(self.a, b2b["prior_titles"][rel])
        c_title = self.c
        rep2 = (f"=====NOTE: {c_title} | repairs: Old Six.md | bullets: b4=====\n{self.tc.rstrip()}\n"
                f"=====BULLETS=====\nOld Six.md#b3 | dropped: not in this transcript\nOld Six.md#b4 | kept in {c_title}\n"
                "Old Six.md#b5 | dropped: not in this transcript\nOld Six.md#b6 | dropped: not in this transcript\n")
        self.fake(repair=[rep2])
        u3 = (run2 / "units" / "repair-v2")
        u3.mkdir(parents=True)
        ri._write_json(u3 / "unit.json", {"kind": "repair", "ids": [], "options": {}})
        res3 = sc.repair_unit(self.staging, self.zk, u3, b2b["vid"], b2b["notes"], b2b["unknown"], b2b["bullets"],
                              b2b["prior_titles"], b2b["last"])
        if os.environ.get("E2E_DEBUG"):
            print("RES3", res3.get("written"), res3.get("bullets", {}).get("Old Six.md#b4"), res3.get("stopped"),
                  [p["type"] for p in res3.get("problems", [])])
        sc.record_answers(state, b2b, res3)
        log = self.log()
        self.assertEqual(len(log), n_calls + 1)
        self.assertNotIn("Old Six.md#b1 |", log[-1]["user"])
        os.environ["REJECT_SHAS"] = "[]"
        rc.review_unit(self.staging, self.zk, u3)
        ev3 = ri.finalize(self.staging, run2, u3, [], 0, self.zk)
        acts3 = {Path(n["note"]).stem: n["action"] for n in ev3["notes"]}
        self.assertEqual(acts3[c_title], "published")
        stub = self.vpath("Old Six").read_text()
        self.assertIn(f"[[{c_title}]]", stub)
        self.assertIn(f"[[{self.a}]]", stub)
        self.assertEqual(ri.replacement_beside_old(self.staging, self.zk), [])
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        if os.environ.get("E2E_DEBUG"):
            print(json.dumps(e["bullets"]["Old Six.md#b4"], indent=1), e.get("status"), e.get("titles"))
        # Round 20: the fixture notes cite no time near the candidate windows of b3 (video 1, note B)
        # and b4 (video 2, note C), so both "kept in" claims are unverified and stay open for the
        # operator; they are never counted as unsupported or as settled.
        self.assertEqual(e["unsupported_bullets"], ["Old Six.md#b5", "Old Six.md#b6"])
        # pilot 8 (round 21): b3's named note B was rejected: target-rejected, OPEN (never unsupported)
        self.assertEqual(e["unverified_bullets"], ["Old Six.md#b4"])
        self.assertEqual(e["rejected_bullets"], ["Old Six.md#b3"])
        s = ri.summary(run2, self.staging)
        open_b = {x["note"]: x for x in s["repair_bullets_open"]}[rel]
        # never counted as settled: all four open bullets are in the summary, with the reason
        self.assertEqual(open_b["bullets"], [f"Old Six.md#b{i}" for i in (3, 4, 5, 6)])
        self.assertEqual(open_b["ledger_unverified"], ["Old Six.md#b4"])
        self.assertEqual(open_b["target_rejected"], ["Old Six.md#b3"])
        import validate
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        self.assertFalse([x for x in errors if "beside its published replacement" in x])

    def test_old_note_beside_published_replacement_is_reported(self) -> None:
        from tests.unit.test_run_integrity import OLD_NOTE
        self.vpath("Old Lone").write_text(OLD_NOTE.format(t="Old Lone"))
        self.vpath(self.a).write_text(self.ta)
        ri.provenance.record(self.staging, "note-repair", note=f"{PN}/Old Lone.md", video=V5, action="renamed",
                             new=[self.a], unit="u")
        beside = ri.replacement_beside_old(self.staging, self.zk)
        self.assertEqual(beside[0]["note"], f"{PN}/Old Lone.md")
        import validate
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        self.assertTrue([x for x in errors if "beside its published replacement" in x])

    def test_drop_suspect_is_asked_once(self) -> None:
        lit = (self.staging / "lit" / f"{V5}.md").read_text()
        quote = re.search(r"\[\d\d:\d\d\]\s*(.{40,120})", lit).group(1)
        cands = sc.bullet_candidates(lit, quote)
        self.assertTrue(sc.drop_suspect(cands))
        self.assertFalse(sc.drop_suspect(sc.bullet_candidates(lit, "zebra quilting needs silk thread")))


class CandidateWindows(_Env):
    """Round 12: no H1 or frontmatter in a window; [MM:SS] on timestamped transcripts."""

    def test_windows_excerpts_and_drop_check_lines(self) -> None:
        lit = (NC / "lit_fictional" / "vid-EXAMPLE0002.md").read_text()
        for bullet in ("Leave 24 hours between hard finger sessions.", "Welcome back, finger strength without injuries."):
            for c in sc.bullet_candidates(lit, bullet):
                self.assertRegex(c["at"], r"^\d\d:\d\d$")
                self.assertNotIn("# Finger Strength", c["text"])
                self.assertNotIn("asr_quality", c["text"])
        line = sc.candidates_line(sc.bullet_candidates(lit, "Welcome back, finger strength without injuries."))
        self.assertTrue(line.startswith('  candidates: [00:00] "Welcome back.'), line)
        ex = sc.excerpts(lit, [{"id": "C1", "at": "00:00", "text": "intro"}])
        self.assertNotIn("# Finger Strength", ex[0]["text"])
        self.assertTrue(ex[0]["text"].startswith("[00:00]"))
        plain = "---\nid: x\n---\n\n# Title\n\nfirst words here\nsecond line\nthird line\n"
        self.assertTrue(sc.bullet_candidates(plain, "first words")[0]["at"].startswith("line "))
        self.assertNotIn("Title", " ".join(c["text"] for c in sc.bullet_candidates(plain, "title words")))
        src = (ZR / "synth_call.py").read_text()
        self.assertIn('H_PASSAGE = \'  passage [\'', src)
