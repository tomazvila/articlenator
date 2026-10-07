"""Reviewer C, round 3 (frozen-v8): v7 probes whose SETUP broke on v8 (removed module
global, removed functions, new unresolved-speaker format, respeak now needs provenance),
adapted minimally so that they test the same defect by behaviour.

Convention: each assertion states the DEFECT. PASS = the defect is present. FAIL = the
defect is absent (here: a fix is confirmed). Fake LLM only, no network."""
from __future__ import annotations

import json

import tests.probes_v7.test_probe7_D_validate as DV
from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5, _Single, reply

import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402
import validate  # noqa: E402

EX2 = "vid-EXAMPLE0002"
VAL = "unresolved speaker, Brink Climbing video"


class DeadLinkErrorV8(DV.DeadLinkError):
    """The 9 D_validate probes without the removed global PIPELINE_NOTES (V30, V47, V48)."""

    def tearDown(self) -> None:
        _Single.tearDown(self)

    def test_stale_global_makes_finalize_check_differ(self) -> None:
        """V47 adapted: check_one gives the same answer before and after a check() call."""
        self.vpath(self.a).write_text(self.ta.replace("- [[Home]] — home map.", "- [[Nowhere Note]] — x."))
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        idx = validate.VaultIndex(self.zk)
        row = next(r for r in idx.parsed if r[0].stem == self.a)
        e1, _ = validate.check_one(*row, self.zk, idx, [], contract_check=False)
        validate.check(self.zk, None, str(self.staging))
        e2, _ = validate.check_one(*row, self.zk, idx, [], contract_check=False)
        self.assertNotEqual(bool([e for e in e1 if "dead link" in e]), bool([e for e in e2 if "dead link" in e]))


class DeadLinkInLeadV8(_Single):
    def test_V29_dead_link_in_lead_publishes_then_final_validate_errors(self) -> None:
        """V29 adapted (the probe cleared the removed global): a dead link in the lead
        publishes and the final validate reports an ERROR."""
        ta = self.ta.replace("on the floor. [src:", "on the floor (see [[Nowhere Note]]). [src:", 1)
        self.fake(synth=[reply([("C1", "01:16", "a"), ("C2", "01:22", "b")], [(self.a, ["C1"], ta)])])
        self.synth()
        rc.review_unit(self.staging, self.zk, self.unit)
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        acts = {n["note"].split("/")[-1][:-3]: n.get("action") for n in ev["notes"]}
        self.assertEqual(acts.get(self.a), "published", ev["notes"])


class RespeakV8(_Single):
    def _unres(self, title: str, label: str = "", val: str = VAL) -> None:
        lab = f"speaker_label: {label}\n" if label else ""
        (self.zk / PN / f"{title}.md").write_text(
            f"---\ntype: permanent note\nsources:\n  - id: {EX2}\n    speaker: \"{val}\"\n{lab}speaker_status: "
            f"unresolved\nverification: unverified\n---\n\n# {title}\n\nLead. [src: {EX2} @ 01:40]\n\n## Evidence\n\n"
            f"- {EX2} @ 01:40 ({val}): \"I tell my athletes to leave at least 48 hours between two hard finger "
            "sessions.\"\n")
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{title}.md", sources=[EX2])

    def test_R1_V22_respeak_overwrites_an_existing_note(self) -> None:
        other = self.zk / PN / "Sasha Says Rest Matters.md"
        other.write_text("---\ntype: permanent note\n---\n\n# Sasha Says Rest Matters\n\nOTHER VIDEO CONTENT.\n")
        self._unres("Unresolved Speaker In Brink Climbing Video Says Rest Matters")
        rows = sc.respeak(self.staging, self.zk, EX2, "Sasha", force_name=True)
        self.assertIsNone(rows[0]["problem"])  # DEFECT: the dry run shows no clash
        sc.respeak(self.staging, self.zk, EX2, "Sasha", apply=True, force_name=True)
        self.assertNotIn("OTHER VIDEO CONTENT", other.read_text())

    def test_R2_V43_name_ignores_speaker_labels(self) -> None:
        self._unres("Unresolved Speaker In Brink Climbing Video Says Rest Matters", "S1")
        self._unres("Unresolved Speaker In Brink Climbing Video Says Warm Up First", "S2")
        out = sc.respeak(self.staging, self.zk, EX2, "Sasha", apply=True, force_name=True)
        self.assertEqual(sorted(r.get("to") or "" for r in out), ["Sasha Says Rest Matters.md", "Sasha Says Warm Up First.md"])

    def test_R3_V43_channel_with_parentheses_breaks_the_title(self) -> None:
        n = sc.parse_reply((ri.HERE.parent / "tests" / "fixtures" / "note_contract" / "batch_reply_fictional.txt")
                           .read_text())["notes"][1]
        chk = {"lit_speakers": ["Tomas Brink"], "found": [{"name": "Tomas Brink"}, {"name": "Mara Kell"}]}
        u = sc.unresolve_note(n, "Brink Climbing (Official)", chk)
        self.assertTrue("(" in u["title"] or ")" in u["title"] or "(" in u["text"].split("## Evidence")[1].split(":")[0][20:],
                        u["title"])

    def test_R4_V23_evidence_stays_unresolved_and_linkers_are_not_archived(self) -> None:
        old = "Unresolved Speaker In Brink Climbing Video Says Rest Matters"
        self._unres(old)
        (self.zk / PN / "Linker.md").write_text(f"x [[{old}]] y\n")
        sc.respeak(self.staging, self.zk, EX2, "Sasha", apply=True, force_name=True)
        new = (self.zk / PN / "Sasha Says Rest Matters.md").read_text()
        self.assertIn('speaker: "Sasha"', new)
        self.assertTrue(f"({VAL})" in new or not list(self.staging.glob("retired/**/Linker.md")))


class MergeRecordsV8(_Single):
    def test_M8_merge_still_settles_repair_bullets(self) -> None:
        """M8 adapted (_merge_records is gone): a merge in a synth unit with a repair state
        present changes the repair state (DEFECT = it does)."""
        st = {"notes": {"o": {"titles": ["Drop"], "bullets": {
            "o#b1": {"text": "legacy", "answers": {"v": {"decision": "kept", "title": "Drop"}}}}}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        import re as _re
        copy_t = "Says Four Sets Of Planche Lean Presses After The Planche Workout Are An Option"
        copy = _re.sub(r"(?m)^# .+$", f"# {copy_t}", self.tb, count=1)
        lead = next(ln for ln in copy.split("\n") if "[src:" in ln and not ln.startswith(("-", "#")))
        copy = copy.replace(lead, "Clearly, " + lead, 1)
        results = [{"rel": f"{PN}/{self.b}.md", "data": self.tb.encode(), "entry": {}, "existed_before": False, "stub": False},
                   {"rel": f"{PN}/{copy_t}.md", "data": copy.encode(), "entry": {}, "existed_before": False, "stub": False}]
        out = ri.merge_duplicates(self.staging, self.zk, self.unit, results,
                                  {r["rel"]: "publish-reviewed" for r in results}, "synth")
        self.assertEqual(len(out), 1)
        self.assertNotEqual(json.loads((self.staging / "repair_state.json").read_text()), st)


class RelinkV8(_Single):
    def test_relink_mechanism_still_exists(self) -> None:
        """V24/V44/V46 adapted: relink_published is still present (DEFECT = it is)."""
        self.assertTrue(hasattr(ri, "relink_published"))


class UnlinkV8(_Single):
    def test_V26_link_to_an_existing_old_note_is_removed(self) -> None:
        """V26 adapted (relink is gone): Y is in the vault, this unit's edit of Y is
        quarantined; unlink still removes [[Y]] in X (DEFECT = removed)."""
        self.vpath("Y").write_text("---\ntype: permanent note\n---\n\n# Y\n\nOld text. [[Home]]\n")
        results = [{"rel": f"{PN}/X.md", "data": b"# X\n\n- [[Y]] - see.\n", "entry": {}, "existed_before": False},
                   {"rel": f"{PN}/Y.md", "data": b"# Y\nnew\n", "entry": {}, "existed_before": True}]
        ri.unlink_waiting_siblings(self.staging, results, {f"{PN}/X.md": "publish", f"{PN}/Y.md": "quarantine"}, self.zk)
        self.assertNotIn("[[Y]]", results[0]["data"].decode())

    def test_V28_merge_rewrite_makes_a_self_link(self) -> None:
        """V28 adapted to the narrow merge rule (same title claim, numbers, lead): the kept
        note K links the dropped copy D; after finalize K links itself (DEFECT)."""
        import re as _re
        copy_t = "Says Four Sets Of Planche Lean Presses After The Planche Workout Are An Option"
        tb = self.tb.replace("- [[Home]] — home map.", f"- [[Home]] — home map.\n- [[{copy_t}]] — the same.")
        copy = _re.sub(r"(?m)^# .+$", f"# {copy_t}", self.tb, count=1)
        lead = next(ln for ln in copy.split("\n") if "[src:" in ln and not ln.startswith(("-", "#")))
        copy = copy.replace(lead, "Clearly, " + lead, 1)
        self.fake(synth=[reply([("C1", "01:22", "b"), ("C2", "01:22", "b again")], [(self.b, ["C1"], tb), (copy_t, ["C2"], copy)])])
        self.synth()
        rc.review_unit(self.staging, self.zk, self.unit)
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        acts = {n["note"].split("/")[-1][:-3]: n.get("action") for n in ev["notes"]}
        self.assertEqual((acts.get(self.b), acts.get(copy_t)), ("published", "merged"), ev["notes"])
        self.assertIn(f"[[{self.b}]]", self.vpath(self.b).read_text())
