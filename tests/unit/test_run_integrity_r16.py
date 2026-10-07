"""Round 16: duplicates merged before publish, links to waiting siblings, review
carry-over, speaker labels, notes already written for a video. Fake LLMs only."""
from __future__ import annotations

import json
import re

from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5, _Single, reply

import synth_call as sc  # noqa: E402
import verify_claims as vc  # noqa: E402


class _Pub:
    def __init__(self, zk):
        self.zk, self.writes = zk, []

    def write(self, rel, text, why=""):
        (self.zk / rel).write_text(text)
        self.writes.append(rel)


class Duplicates(_Single):
    """Round 18: the merge is narrowed to two NEW notes of one unit with the same title
    claim, the same non-empty numbers and a lead overlap of 0.9; everything else that looks
    alike is only listed (`possible_duplicates.json`)."""

    def test_copy_under_another_title_is_only_listed(self) -> None:
        copy = self.ta.replace(f"# {self.a}", "# A Copy With Another Title")
        lead = [ln for ln in copy.split("\n") if "[src:" in ln][0]
        copy = copy.replace(lead, "Clearly, " + lead[0].lower() + lead[1:], 1)
        self.fake(synth=[reply([("C1", "01:16", "a"), ("C3", "01:16", "a again")],
                               [(self.a, ["C1"], self.ta), ("A Copy With Another Title", ["C3"], copy)])])
        self.synth()
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        self.assertFalse([n for n in ev["notes"] if n["action"] == "merged"])
        possible = json.loads((self.staging / "possible_duplicates.json").read_text())
        self.assertEqual(len(possible), 1)
        self.assertTrue(possible[0]["a_lead"] and possible[0]["b_lead"])

    def test_never_merged_into_a_vault_note(self) -> None:
        self.vpath(self.a).write_text(self.ta)
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        copy = self.ta.replace(f"# {self.a}", "# Same Claim Again")
        self.fake(synth=[reply([("C1", "01:16", "a")], [("Same Claim Again", ["C1"], copy)])])
        self.synth()
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        self.assertNotEqual(ev["notes"][0]["action"], "merged")
        self.assertEqual(json.loads((self.staging / "possible_duplicates.json").read_text())[0]["a"], f"{PN}/{self.a}.md")

    def test_never_in_repair_units(self) -> None:
        a_text = self.ta
        b_text = self.ta.replace(f"# {self.a}", "# " + self.a.replace("Radoslav Radev", "radoslav_radev", 1))
        res = [{"rel": f"{PN}/{self.a}.md", "data": a_text.encode(), "stub": False, "existed_before": False, "entry": {}},
               {"rel": f"{PN}/Z.md", "data": b_text.encode(), "stub": False, "existed_before": False, "entry": {}}]
        self.assertEqual(ri.merge_duplicates(self.staging, self.zk, self.unit, res, {r["rel"]: "publish" for r in res},
                                             "repair"), [])

    def test_different_claims_are_not_merged(self) -> None:
        self.assertFalse(ri.same_note(ri._dup_sig(self.ta), ri._dup_sig(self.tb)))


class Links(_Single):
    def test_unlink_keeps_connected_ideas_shaped_and_offline_relink_works(self) -> None:
        code_mark = chr(96)
        text = (
            "---\ntype: permanent note\nsources:\n  - id: vid-5zALUKd7h3g\n    speaker: \"Radoslav Radev\"\n"
            "verification: unverified\n---\n\n# X\n\nClaim.\n\n"
            "## Details\n\n- [[Y]] — details connection.\n- Already plain text.\n- " + code_mark + "[[Y]]" + code_mark +
            " is code.\n| [[Y]] |\n\n"
            "## Evidence\n\n- vid-5zALUKd7h3g @ 01:16 (Radoslav Radev): \"A quoted passage. [[Y]]\"\n\n"
            "## Connected Ideas\n\n- [[Y]] — first connection.\n"
            "- [[Y]] and [[Keep]] — keep the independent link.\n"
        )
        results = [
            {"rel": f"{PN}/X.md", "data": text.encode(), "entry": {}, "existed_before": False},
            {"rel": f"{PN}/Y.md", "data": b"# Y\n", "entry": {}, "existed_before": False},
        ]
        decisions = {f"{PN}/X.md": "publish", f"{PN}/Y.md": "pending"}
        (self.zk / "Home.md").unlink(missing_ok=True)
        self.assertFalse((self.zk / "Home.md").exists())
        ri.unlink_waiting_siblings(self.staging, results, decisions, self.zk)
        unlinked = results[0]["data"].decode()
        body = vc.split_frontmatter(unlinked)[1]
        ci_failures = [f for f in ri.shape_failures(vc.parse_body_full(body)) if f["where"] == "Connected Ideas"]
        self.assertEqual(ci_failures, [])
        self.assertTrue((self.zk / "Home.md").is_file())
        self.assertIn("- Y — first connection. — index: [[Home]]", unlinked)
        self.assertIn("- Y and [[Keep]] — keep the independent link.", unlinked)
        self.assertNotIn("index: [[Home]]", unlinked.split("## Connected Ideas", 1)[0])
        self.assertIn("- Y — details connection.", unlinked)
        self.assertIn("- Already plain text.", unlinked)
        self.assertIn(code_mark + "[[Y]]" + code_mark + " is code.", unlinked)
        self.assertIn("| [[Y]] |", unlinked)
        evidence_line = '- vid-5zALUKd7h3g @ 01:16 (Radoslav Radev): "A quoted passage. [[Y]]"'
        self.assertIn(evidence_line, unlinked)
        records = json.loads((self.staging / "unlinked.json").read_text())
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertIn(record["line"], unlinked.splitlines())
        self.assertEqual(records[1]["line"], "- Y — first connection. — index: [[Home]]")
        self.assertEqual(records[2]["line"], "- Y and [[Keep]] — keep the independent link.")

        self.vpath("X").write_text(unlinked, encoding="utf-8")
        self.vpath("Y").write_text(self.ta.replace(f"# {self.a}", "# Y", 1), encoding="utf-8")
        out = ri.relink(self.staging, self.zk, apply=True)
        self.assertEqual(len(out["restored"]), 3)
        restored = self.vpath("X").read_text(encoding="utf-8")
        self.assertIn("- [[Y]] — first connection. — index: [[Home]]", restored)
        self.assertIn("- [[Y]] and [[Keep]] — keep the independent link.", restored)
        self.assertIn("- [[Y]] — details connection.", restored)
        self.assertEqual(json.loads((self.staging / "unlinked.json").read_text()), [])

    def test_unlink_only_no_restore(self) -> None:
        results = [{"rel": f"{PN}/X.md", "data": b"# X\n\n- [[Y]] - see.\n- `[[Y]]` code\n\n## Evidence\n\n- [[Y]] q\n",
                    "entry": {}, "existed_before": False},
                   {"rel": f"{PN}/Y.md", "data": b"# Y\n", "entry": {}, "existed_before": False}]
        decisions = {f"{PN}/X.md": "publish-reviewed", f"{PN}/Y.md": "quarantine"}
        ri.unlink_waiting_siblings(self.staging, results, decisions, self.zk)
        txt = results[0]["data"].decode()
        self.assertIn("- Y - see.", txt)
        self.assertIn("`[[Y]]` code", txt)  # inline code untouched
        self.assertIn("- [[Y]] q", txt)  # Evidence untouched
        self.assertEqual(json.loads((self.staging / "unlinked.json").read_text())[0]["target"], "Y")
        self.assertFalse(hasattr(ri, "relink_published"))  # removed: no automatic restore

    def test_existing_vault_note_is_never_unlinked(self) -> None:
        self.vpath("Y").write_text("# Y\n")
        results = [{"rel": f"{PN}/X.md", "data": b"# X\n\n- [[Y]] - see.\n", "entry": {}, "existed_before": False},
                   {"rel": f"{PN}/Y.md", "data": b"# Y\n", "entry": {}, "existed_before": True}]
        ri.unlink_waiting_siblings(self.staging, results, {f"{PN}/X.md": "publish", f"{PN}/Y.md": "quarantine"}, self.zk)
        self.assertIn("[[Y]]", results[0]["data"].decode())

    def test_validate_dead_link_in_a_pipeline_note_is_an_error(self) -> None:
        import validate
        self.vpath(self.a).write_text(self.ta.replace("- [[Home]] — home map.", "- [[Nowhere Note]] — x."))
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        errors, warnings, _n = validate.check(self.zk, None, str(self.staging))
        self.assertTrue([e for e in errors if "dead link [[Nowhere Note]]" in e])
        errors, warnings, _n = validate.check(self.zk, None, None)  # without the staging: an old note, a warning
        self.assertFalse([e for e in errors if "Nowhere Note" in e])


class ReviewCarryOver(_Single):
    def test_hash_keeps_links_and_speaker_spelling_but_not_content(self) -> None:
        h = ri.reviewed_hash(self.ta)
        self.assertEqual(h, ri.reviewed_hash(self.ta.replace("- [[Home]] — home map.", "- [[Other Note]] — x.")))
        self.assertEqual(h, ri.reviewed_hash(self.ta.replace("verification: unverified", "verification: quote-checked")))
        speaker_title = self.ta.replace("# Radoslav Radev ", "# radoslav_radev ", 1)
        self.assertEqual(h, ri.reviewed_hash(speaker_title))
        self.assertNotEqual(h, ri.reviewed_hash(self.ta.replace("# Radoslav Radev Says", "# Radoslav Radev Thinks", 1)))
        ev = self.ta.split("## Evidence", 1)
        self.assertNotEqual(h, ri.reviewed_hash(ev[0] + "## Evidence" + ev[1].replace('"', '"really ', 1)))

    def test_supported_review_carries_over_a_link_only_change(self) -> None:
        import review_call as rc
        from tests.unit.test_run_integrity_r13 import review_answer  # noqa: F401
        self.fake(synth=[reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)])])
        self.synth()
        rc.review_unit(self.staging, self.zk, self.unit)
        p = self.unit / "out" / PN / f"{self.a}.md"
        v1 = ri.verdict_for(self.unit, f"{PN}/{self.a}.md", p.read_bytes())
        self.assertFalse(v1.get("bad"))
        p.write_text(p.read_text().replace("- [[Home]] — home map.", "- [[Home]] — the home map of the vault."))
        v2 = ri.verdict_for(self.unit, f"{PN}/{self.a}.md", p.read_bytes())
        self.assertTrue(v2.get("carried_over"))  # no new review for a link-only change
        p.write_text(p.read_text().replace(f"# {self.a}", f"# {self.a} Always"))
        v3 = ri.verdict_for(self.unit, f"{PN}/{self.a}.md", p.read_bytes())
        self.assertEqual(v3["code"], "review-stale")  # the title's claim changed: a new review


class SpeakerLabels(_Single):
    def test_respeak_by_label(self) -> None:
        for label, title in (("S1", "Unresolved Speaker (Ch) Says One"), ("S2", "Unresolved Speaker (Ch) Says Two")):
            (self.zk / PN / f"{title}.md").write_text(
                "---\ntype: permanent note\nsources:\n  - id: vid-5zALUKd7h3g\n    speaker: \"unresolved speaker, "
                f"Ch video\"\nspeaker_label: {label}\nspeaker_status: unresolved\nverification: unverified\n---\n\n"
                f"# {title}\n\nLead.\n")
        for title in ("Unresolved Speaker (Ch) Says One", "Unresolved Speaker (Ch) Says Two"):
            ri.provenance.record(self.staging, "note-published", note=f"{PN}/{title}.md", sources=[V5])
        out = sc.respeak(self.staging, self.zk, V5, "", apply=True, label_map={"S1": "Host Name", "S2": "Sasha"}, force_name=True)
        self.assertEqual(sorted(r["to"] for r in out), ["Host Name Says One.md", "Sasha Says Two.md"])
        self.assertIn('speaker: "Sasha"', (self.zk / PN / "Sasha Says Two.md").read_text())


class VideoNotes(_Single):
    def test_notes_written_for_this_video_by_other_old_notes(self) -> None:
        notes = {"o1": {"bullets": {"O1.md#b1": {"answers": {V5: {"decision": "kept", "title": "T1"}}}}},
                 "o2": {"bullets": {"O2.md#b1": {"answers": {"vid-x": {"decision": "kept", "title": "T9"}}}}}}
        self.assertEqual(sc._video_notes(notes, V5, {"o3"}), {"T1": ["O1.md#b1"]})
        self.assertEqual(sc._video_notes(notes, V5, {"o1"}), {})


class BRound13(_Single):
    def test_speakers_block_and_unknown_speaker(self) -> None:
        user = sc._batch_user("---\nid: x\n---\n", [{"id": "C1", "at": "00:10", "text": "t", "type": "rule"}], 1, 1, [],
                              [], {}, [{"label": "S1", "name": "unknown", "role": "guest", "evidence": "\"hi\" 00:05"}])
        self.assertIn("SPEAKERS OF THIS VIDEO:\nS1 | unknown | guest | \"hi\" 00:05", user)
        n = {"title": "Unknown Speaker Says Rest", "text": "---\nsources:\n  - id: vid-x\n    speaker: \"unknown\"\n"
             "speaker_label: S1\nverification: unverified\n---\n\n# Unknown Speaker Says Rest\n"}
        out = sc.unresolve_note(n, "Brink Climbing", {"lit_speakers": [], "found": []})
        self.assertEqual(out["title"], "Unresolved Speaker In Brink Climbing Video Says Rest")
        self.assertIn("speaker_label: S1", out["text"])

    def test_checker_unknown_speaker(self) -> None:
        import verify_claims as vc
        from tests.unit.test_run_integrity_r15 import NC
        lit = vc.LitIndex([NC / "lit_fictional"])
        t = (NC / "batch_reply_fictional.txt").read_text().split("=====NOTE: ")[1].split("\n", 1)[1].split("=====")[0]
        t = re.sub(r'(?m)^(\s+speaker:\s*).*$', r'\1"unknown"', t)
        p = self.tmp / "n.md"
        p.write_text(t)
        self.assertIn("speaker-unknown", {f["type"] for f in vc.verify_note(p, lit)["failures"]})  # one-voice video


class ConservativeMerge(_Single):
    """Round 17: a false merge loses a note; only a clear duplicate is merged."""

    def test_other_skill_number_speaker_or_modality_is_not_a_duplicate(self) -> None:
        base = dict(ri._dup_sig(self.ta), skill="half tuck")
        for change in ("skill", "nums", "speaker", "modality"):
            other = dict(base)
            other[change] = {"skill": "half pike", "nums": {(99.0, "week")}, "speaker": (("someone else",), ""),
                             "modality": "prediction"}[change]
            other["key"] = "another title"
            self.assertFalse(ri.same_note(base, other), change)
        same_title_other_speaker = dict(base, speaker=(("someone else",), ""))
        self.assertFalse(ri.same_note(base, same_title_other_speaker))
        self.assertTrue(ri.same_note(base, dict(base, key="another title")))

    def test_merged_text_is_kept_and_listed(self) -> None:
        a_text = self.tb
        b_text = self.tb.replace(f"# {self.b}", "# " + self.b.replace("Radoslav Radev", "radoslav_radev", 1))
        if not ri._dup_sig(a_text)["nums"]:
            self.skipTest("fixture lead without numbers")
        res = [{"rel": f"{PN}/{self.b}.md", "data": a_text.encode(), "stub": False, "existed_before": False, "entry": {}},
               {"rel": f"{PN}/Z.md", "data": b_text.encode(), "stub": False, "existed_before": False, "entry": {}}]
        out = ri.merge_duplicates(self.staging, self.zk, self.unit, res, {r["rel"]: "publish" for r in res}, "synth")
        self.assertEqual(out[0]["rule"], "same-unit-equal-title-numbers-lead0.9")
        self.assertEqual(json.loads((self.unit / "merged.json").read_text()), out)
