"""Round 18: the probes of the second review (frozen-v7), copied to tests/probes_v7/
(only import paths changed), and their result now. Conventions (REVIEW.md):
A_regress / B_regress state the correct behaviour (PASS = fixed); every other file states
the defect (PASS = defect present; `test_ok_*` excepted). OPEN lists the probes that still
show their defect, with the reason. D_validate probes use the removed module global
`PIPELINE_NOTES` (V47) and now ERROR at setup; ValidateRound18 below proves V30/V47/V48."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_synth_single import V5, _Single

import synth_call as sc  # noqa: E402

ROOT = ZR.parent
OPEN = {
    "B_attacks.py::CarryOver::test_connected_ideas_prose_carries":
        "V35: kept on purpose: Connected Ideas are outside the reviewed content (link-only fixes keep the review)",
    "B_attacks.py::CarryOver::test_link_target_change_carries": "V35: as above",
    "C_merge.py::SameNoteSignatures::test_M1_two_distinct_cues_without_numbers_are_one_note":
        "removed mechanism: same_note() now only REPORTS (possible_duplicates); the merge uses the narrow rule",
    "C_merge.py::SameNoteSignatures::test_M2_same_numbers_other_exercise_when_one_skill_is_not_stated": "as M1",
    "C_merge.py::SameNoteSignatures::test_M3_equal_title_ignores_video_numbers_and_claim": "as M1 (V40)",
    "C_regress.py::AdaptedPrompts::test_P2_worker_id_is_still_written_by_the_worker":
        "V36: not fixed (the dispatcher must write the meta; a design change)",
}

# Probes replaced by the third review's adapted version (review-v8); their result here is not used.
SUPERSEDED = {
    "A_regress.py::PendingTargetV7::test_ok_final_stub_links_every_published_target":
        "fixture bullets ('legacy bullet N') hold no content word; the reviewer's adapted "
        "tests/probes_v8 PendingTargetV8::test_ok_final_stub_links_every_published_target (real bullet text) "
        "is checked in test_review_v8_probes.py",
    "A_regress.py::InPlaceRejectedV7::test_ok_old_note_becomes_partial_stub":
        "asserts the superseded rule 'a rejected target makes its bullet unsupported'; pilot 8 (round 21): it "
        "stays OPEN (`rejected_bullets`, stub line `open: target rejected`); test_r21_pilot8.py covers it",
    "A_regress.py::PendingThenRejectedV7::test_ok_rejected_in_pending_unit_reopens": "as InPlaceRejectedV7",
}


class ReviewV7Probes(unittest.TestCase):
    def test_probe_results(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "PYTEST_"))}
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/probes_v7", "-q", "-p", "no:cacheprovider",
                            "-p", "no:randomly", "-rA"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=1500)
        got = {m.group(2): m.group(1) for m in re.finditer(r"(?m)^(PASSED|FAILED|ERROR) tests/probes_v7/test_probe7_(\S+)",
                                                           r.stdout)}
        self.assertGreaterEqual(len(got), 90, r.stdout[-1500:])
        bad = {}
        for name, res in got.items():
            correct = name.startswith(("A_regress", "B_regress")) or "test_ok_" in name
            if name.startswith("D_validate") or name in SUPERSEDED:
                continue  # ValidateRound18 / SUPERSEDED
            want = ("FAILED" if name in OPEN else "PASSED") if correct else ("PASSED" if name in OPEN else "FAILED")
            if res != want and not (want == "FAILED" and res == "ERROR"):
                bad[name] = (res, want)
        self.assertEqual(bad, {})


class Round18(_Single):
    def test_V4_unresolved_value_passes_evidence_item(self) -> None:
        import verify_claims as vc
        n = {"title": "Tomas Brink Says X", "text": "---\nsources:\n  - id: vid-x\n    speaker: \"Tomas Brink\"\n"
             "verification: unverified\n---\n\n# Tomas Brink Says X\n\nTomas Brink says x. [src: vid-x @ 00:10]\n\n"
             "## Evidence\n\n- vid-x @ 00:10 (Tomas Brink): \"x\"\n"}
        out = sc.unresolve_note(n, "Brink (Climbing)", {"lit_speakers": ["Tomas Brink"], "found": []})
        self.assertIn('speaker: "unresolved speaker, Brink Climbing video"', out["text"])
        ev = [ln for ln in out["text"].split("\n") if ln.startswith("- vid-x")][0]
        self.assertTrue(vc.EVIDENCE_ITEM.match(ev), ev)
        self.assertNotIn("Tomas Brink", out["title"] + out["text"].split("## Evidence")[0].split("---", 2)[2])  # V21

    def test_V1_waits_and_budget_stops_never_count(self) -> None:
        state = self.staging / "repair_state.json"
        import json
        state.write_text(json.dumps({"notes": {}, "batches": {"k": {"status": "running", "vid": V5, "bullets": {},
                                                                     "notes": [], "unknown": []}}}))
        for _ in range(4):
            b = sc.next_batch(self.staging, self.zk, state)
            self.assertEqual(b["key"], "k")  # no failure recorded: handed out again, never `failed`
            sc.record_answers(state, b, {"stopped": "waiting-for-reply"})
            ri.release_waiting(self.staging)
        self.assertEqual(json.loads(state.read_text())["batches"]["k"]["status"], "running")

    def test_V6_cache_entry_of_another_request_is_not_used(self) -> None:
        sc._CACHE.clear()
        sc._CACHE.update({"path": str(self.tmp / "c.json"), "data": {"replies": {"batch-3": {
            "content": "old", "finish": "stop", "request_key": "0" * 32}}}})
        os.environ["ZR_LLM_BACKEND"] = "files"
        with self.assertRaises(sc.PendingReply):  # a new request, not the cached reply of another one
            sc.call(self.unit, "batch-3", [{"role": "user", "content": "C6"}], {"model": "m", "max_tokens": 1})
        sc._CACHE.clear()

    def test_V18_no_note_keeps_needs_attention(self) -> None:
        src = (ZR / "run_integrity.py").read_text()
        self.assertIn('elif it["stage"] == "needs-attention" and total and', src)

    def test_V19_bare_id_in_a_multi_part_batch(self) -> None:
        p = sc.parse_reply("=====SKIPPED=====\nC1 | passage unreadable\nC2 | damaged span\n")
        sc._map_bare_ids(p, {"P1-C1", "P2-C2", "P1-C2"})
        self.assertEqual(p["skips"], {"P1-C1": "passage unreadable"})
        self.assertIn("claim-id-ambiguous", {x["type"] for x in p["problems"]})

    def test_V32_dot_slash(self) -> None:
        self.assertEqual(ri.safe_note_rel(self.zk, f"./{PN}/X.md"), f"{PN}/X.md")

    def test_V33_archive_versions_only(self) -> None:
        d = self.staging / "retired" / "r" / "u" / PN
        d.mkdir(parents=True)
        (d / "Old.v2.md").write_text("mine")
        (d / "Old.vintage.md").write_text("other note")
        self.assertEqual(ri.archived_old_text(self.staging, f"{PN}/Old.md"), "mine")

    def test_V13_V16_review_guards_in_source(self) -> None:
        src = (ZR / "review_call.py").read_text()
        self.assertIn("_provenance_writers(staging, rel)", src)
        self.assertIn("except synth_call.BudgetStop as exc:  # V16", src)

    def test_V22_respeak_refuses_an_existing_target(self) -> None:
        title = "Unresolved Speaker (Ch) Says One"
        (self.zk / PN / f"{title}.md").write_text(
            "---\ntype: permanent note\nsources:\n  - id: vid-5zALUKd7h3g\n    speaker: \"unresolved speaker, Ch video\"\n"
            "speaker_status: unresolved\nverification: unverified\n---\n\n# " + title + "\n\nLead.\n")
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{title}.md", sources=[V5])
        (self.zk / PN / "Sasha Says One.md").write_text("# Sasha Says One\n")
        out = sc.respeak(self.staging, self.zk, V5, "Sasha", apply=True, force_name=True)
        self.assertIn("exists already", out[0]["problem"])
        self.assertEqual((self.zk / PN / "Sasha Says One.md").read_text(), "# Sasha Says One\n")

    def test_V23_respeak_only_pipeline_notes(self) -> None:
        title = "Unresolved Speaker (Ch) Says Two"
        (self.zk / PN / f"{title}.md").write_text(
            "---\ntype: permanent note\nsources:\n  - id: vid-5zALUKd7h3g\n    speaker: \"unresolved speaker, Ch video\"\n"
            "speaker_status: unresolved\nverification: unverified\n---\n\n# " + title + "\n\nLead.\n")
        out = sc.respeak(self.staging, self.zk, V5, "Sasha", apply=True, force_name=True)
        self.assertIn("not published by this pipeline", out[0]["problem"])


class ValidateRound18(_Single):
    def test_V30_V47_V48_obsidian_resolution_and_no_global(self) -> None:
        import validate
        text = self.ta.replace("- [[Home]] — home map.",
                               "- [[Home]] — home map.\n- [[home]] case\n- [[Home.md]] suffix\n- [[sub/Home]] path\n"
                               "- ![[img.png]] embed\n- [[doc.pdf]] file\n- `[[Nowhere]]` code\n")
        self.vpath(self.a).write_text(text)
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        self.assertEqual([e for e in errors if "dead link" in e], [])
        self.assertFalse(hasattr(validate, "PIPELINE_NOTES"))  # V47: no module global
