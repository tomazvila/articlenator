"""Round 15 (pilot 6): stuck pending notes, stubs from the persistent repair state,
unresolved speakers, link rewrites, unknown-source candidates. Fake LLMs only."""
from __future__ import annotations

import json
import os
import shutil

from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_synth_single import _Single

import synth_call as sc  # noqa: E402
import verify_claims as vc  # noqa: E402

NC = ZR.parent / "tests" / "fixtures" / "note_contract"
EX2 = "vid-EXAMPLE0002"


class StuckExit(_Single):
    def test_folder_at_the_cycle_limit_is_reported_and_never_exit_0(self) -> None:
        pdir = self.staging / "pending-review" / "20261004T000000Z-1--u"
        (pdir / "out").mkdir(parents=True)
        (pdir / "meta.json").write_text(json.dumps({"pending_cycles": ri.MAX_PENDING_CYCLES, "notes": [f"{PN}/X.md"],
                                                    "reasons": {f"{PN}/X.md": "review-rejected"}}))
        stuck = ri.stuck_notes(self.staging)
        self.assertEqual(stuck["review-rejected"][0]["note"], f"{PN}/X.md")
        import subprocess
        import sys
        r = subprocess.run([sys.executable, str(ZR / "synth_call.py"), "exchange-status", "--staging",
                            str(self.staging), "--count-open"], capture_output=True, text=True)
        self.assertEqual(r.stdout.split()[1], "1")  # the driver ends with exit 6, never 0
        self.assertIn('exit "$ZR_EXCHANGE_STUCK"', (ZR / "run_lib.sh").read_text())

    def test_review_fix_for_a_note_not_repaired_before(self) -> None:
        src = (ZR / "synth_call.py").read_text()
        self.assertIn('if r["rel"] not in done_before', src)


class Speakers(_Single):
    """Item 3: the pilot's 9yFK case: the lit file says one speaker (`sthenics_`), the
    inventory finds a host and a guest: the notes carry no person's name."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_SYNTH_STAGES"] = "two"
        shutil.copy(NC / "lit_fictional" / f"{EX2}.md", self.staging / "lit")
        self.write_queue([{"id": EX2, "kind": "video", "stage": "claimed", "claimed_from": "extracted",
                           "lit_note": str(self.staging / "lit" / f"{EX2}.md")}])
        for p in (NC / "vault_fictional").rglob("Mara Kell*.md"):
            shutil.copy(p, self.zk / PN / p.name)
        self.unit2 = self.new_unit("synth", [EX2])

    def test_unresolved_speakers(self) -> None:
        inv = (NC / "inventory_reply_fictional.txt").read_text() + (
            "=====SPEAKERS=====\nS1 | Tomas Brink | host | \"welcome back\" 00:00\n"
            "S2 | Sasha | guest | \"tomas asked me\" 01:40\n")
        batch = (NC / "batch_reply_fictional.txt").read_text()
        self.fake(inventory=[inv], batch=[batch, "=====SKIPPED=====\nC8 | not a transferable training claim: "
                                                 "no content beyond the word warm-up\n"])
        res = sc.synth_unit(self.staging, self.zk, self.unit2, [EX2])
        self.assertEqual(res["speaker_status"], "unresolved")
        self.assertTrue(res["notes"])
        for t in res["notes"]:
            self.assertTrue(t.startswith("Unresolved Speaker In "), t)
            text = (self.unit2 / "out" / PN / f"{t}.md").read_text()
            self.assertIn("speaker_status: unresolved", text)
            self.assertNotIn("speaker: \"Tomas Brink\"", text)
            self.assertNotIn("[[Tomas Brink Predicts", text)  # sibling links follow the new title
        rep = vc.verify_note(next((self.unit2 / "out" / PN).glob("*.md")), vc.LitIndex(vc.lit_dirs(self.staging)))
        self.assertNotIn("wrong-speaker", {f["type"] for f in rep["failures"]}, rep["failures"])

    def test_checker_rejects_a_name_when_unresolved_and_a_vague_title(self) -> None:
        p = self.zk / PN / "Tomas Brink Says X.md"
        t = (NC / "batch_reply_fictional.txt").read_text().split("=====NOTE: ")[1].split("\n", 1)[1]
        t = t.split("=====")[0].replace("verification:", "speaker_status: unresolved\nverification:", 1)
        p.write_text(t)
        r = vc.verify_note(p, vc.LitIndex([NC / "lit_fictional"]))
        self.assertIn("speaker-unresolved-named", {f["type"] for f in r["failures"]})
        self.assertTrue(vc.VAGUE_SPEAKER.search("A Speaker In The Sthenics Video Says Ten Sets"))
        self.assertFalse(vc.VAGUE_SPEAKER.search("Sthenics Says Ten Sets"))

    def test_resolved_when_the_block_matches_the_lit_data(self) -> None:
        tr = vc.LitIndex([NC / "lit_fictional"]).get(EX2)
        ok = sc.speaker_check(tr, [{"label": "S1", "name": "Tomas Brink", "role": "solo"}])
        self.assertEqual(ok["status"], "resolved")
        self.assertEqual(sc.speaker_check(tr, [])["status"], "lit")

    def test_respeak_renames_and_rewrites_links(self) -> None:
        old = "Unresolved Speaker (Brink Climbing) Says Rest Matters"
        text = ("---\ntype: permanent note\nsources:\n  - id: vid-EXAMPLE0002\n    speaker: \"unresolved speaker, "
                "Brink Climbing video\"\nspeaker_status: unresolved\nverification: unverified\n---\n\n"
                f"# {old}\n\nLead.\n")
        (self.zk / PN / f"{old}.md").write_text(text)
        (self.zk / PN / "Linker.md").write_text(f"x [[{old}]] y\n")
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{old}.md", sources=[EX2])
        dry = sc.respeak(self.staging, self.zk, EX2, "Sasha", force_name=True)
        self.assertEqual(dry[0]["to"], "Sasha Says Rest Matters.md")
        self.assertTrue((self.zk / PN / f"{old}.md").exists())  # dry run writes nothing
        sc.respeak(self.staging, self.zk, EX2, "Sasha", apply=True, force_name=True)
        new = (self.zk / PN / "Sasha Says Rest Matters.md").read_text()
        self.assertIn('speaker: "Sasha"', new)
        self.assertNotIn("speaker_status", new)
        self.assertIn("[[Sasha Says Rest Matters]]", (self.zk / PN / "Linker.md").read_text())
        self.assertTrue(list(self.staging.glob("retired/respeak-*/**/*.md")))


class StubsFromState(_Single):
    """Item 2: a target published in a LATER pass is added to the stub; quarantined
    targets' bullets are listed; the invariant holds."""

    def test_stub_recomputed_from_state(self) -> None:
        from tests.unit.test_run_integrity import OLD_NOTE
        self.vpath("Old R").write_text(OLD_NOTE.format(t="Old R").replace("- legacy bullet\n", "- Planche lean presses can be done on the floor without parallettes.\n- Four sets of planche lean presses after the planche workout are an option.\n"))
        rel = f"{PN}/Old R.md"
        sha = ri._sha256(self.vpath("Old R"))
        st = {"notes": {rel: {"sha": sha, "status": "done", "titles": [self.a],
                              "bullets": {"Old R.md#b1": {"text": "Planche lean presses can be done on the floor without parallettes.", "answers": {"v": {"decision": "kept", "title": self.a}}},
                                          "Old R.md#b2": {"text": "Four sets of planche lean presses after the planche workout are an option.", "answers": {"v": {"decision": "kept", "title": self.b}}}},
                              "unsupported_bullets": []}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        self.vpath(self.a).write_text(self.ta)  # A published; B not published
        u = self.new_unit("repair", [])
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        stub = self.vpath("Old R").read_text()
        self.assertIn(f'superseded_by: "[[{self.a}]]"', stub)
        self.assertIn("bullet: Old R.md#b2", stub)
        self.assertEqual(ev["stubs_recomputed"][0]["note"], rel)
        self.assertTrue(list(self.staging.glob("retired/*/*/01 Permanent Notes/Old R.md")))
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        # B publishes in a later pass: the stub links it and lists nothing open
        self.vpath(self.b).write_text(self.tb)
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = self.vpath("Old R").read_text()
        self.assertIn(f"[[{self.b}]]", stub)
        self.assertNotIn("superseded_pending", stub)
        self.assertEqual(len(list(self.staging.glob("retired/*/*/01 Permanent Notes/Old R*.md"))), 2)  # both kept


class UnknownSourceCandidates(_Single):
    def test_up_to_three_candidates_above_the_next_score(self) -> None:
        from unittest import mock
        pre = [("v1", 0.34), ("v2", 0.24), ("v3", 0.22), ("v4", 0.1)]
        with mock.patch.object(sc, "candidate_videos", return_value=pre), \
                mock.patch.object(sc, "bullet_video_scores", return_value=[("v1", 1.7, 2), ("v3", 0.8, 1)]):
            got = sc.unknown_candidates(self.staging, "text")
        self.assertEqual([v for v, _ in got], ["v1", "v2", "v3"])  # pilot 6 "Macrocycle": v2 was the right one
        with mock.patch.object(sc, "candidate_videos", return_value=[("v1", 0.2)]):
            self.assertEqual(sc.unknown_candidates(self.staging, "text"), [])


class LinksAndLabels(_Single):
    def test_rewrite_links_and_constants(self) -> None:
        d = self.tmp / "o"
        d.mkdir()
        (d / "a.md").write_text("[[Old T]] and [[Old T|x]] and [[Old T#h]] and [[Old Tea]]")
        sc.rewrite_links(d, "Old T", "New T")
        self.assertEqual((d / "a.md").read_text(), "[[New T]] and [[New T|x]] and [[New T#h]] and [[Old Tea]]")
        self.assertEqual(sc.H_LINK_CANDIDATES, "EXISTING NOTES YOU MAY LINK TO")
        self.assertNotIn("CLAIMS", sc.COVERAGE_REMINDER.replace("No CLAIMS block", ""))
        self.assertIn("first 32 hex characters", (ZR / "README.md").read_text())


class AllBatchesOnePass(Speakers):
    def test_every_batch_is_emitted_in_one_pass(self) -> None:
        from tests.unit.test_run_integrity_r13 import work
        os.environ["ZR_LLM_BACKEND"] = "files"
        xdir = self.staging / "exchange"
        self.assertEqual(sc.synth_unit(self.staging, self.zk, self.unit2, [EX2])["outcome"], "waiting-for-reply")
        work(xdir, lambda r: (NC / "inventory_reply_fictional.txt").read_text())
        ri.release_waiting(self.staging)
        u = self.new_unit("synth", [EX2])
        sc.synth_unit(self.staging, self.zk, u, [EX2])
        kinds = [r["kind"] for r in sc.exchange_list(xdir)]
        self.assertEqual(kinds.count("batch"), 2)  # 6 claims to write: batches of 5 and 1, both in this pass
        u2 = self.new_unit("synth", [EX2])
        sc.synth_unit(self.staging, self.zk, u2, [EX2])
        self.assertEqual(len(list((xdir / "requests").glob("*.json"))), 3)  # no new key for the same batches
