"""Reviewer C, round 3 (frozen-v8): the unresolved-speaker flow (inventory SPEAKERS ->
unresolve_note -> checker -> finalize -> respeak).

Convention: each assertion states the DEFECT. PASS = the defect is present. FAIL = the
defect is absent. Exception: `test_ok_*` states the CORRECT behaviour (PASS = works).
Fake LLM only, no network."""
from __future__ import annotations

import re

import tests.probes_v7.test_probe7_C_speakers as S
from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import _Single

import synth_call as sc  # noqa: E402
import validate  # noqa: E402
import verify_claims as vc  # noqa: E402

EX2 = S.EX2


def _lead(text: str) -> str:
    return next(ln for ln in text.split("\n") if "[src:" in ln and not ln.startswith(("-", "#", " ")))


class UnresolvedEndToEnd(_Single):
    def setUp(self) -> None:
        import os
        import shutil
        super().setUp()
        os.environ["ZR_SYNTH_STAGES"] = "two"
        shutil.copy(S.NC / "lit_fictional" / f"{EX2}.md", self.staging / "lit")
        self.write_queue([{"id": EX2, "kind": "video", "stage": "claimed", "claimed_from": "extracted",
                           "lit_note": str(self.staging / "lit" / f"{EX2}.md")}])
        self.unit2 = self.new_unit("synth", [EX2])

    def _synth(self) -> None:
        inv = (S.NC / "inventory_reply_fictional.txt").read_text().split("=====SPEAKERS=====")[0] + S.SPK2
        self.fake(inventory=[inv], batch=[S.BATCH, "=====SKIPPED=====\nC8 | not a transferable training claim: "
                                                   "no content beyond the word warm-up\n"])
        res = sc.synth_unit(self.staging, self.zk, self.unit2, [EX2])
        self.assertEqual(res["speaker_status"], "unresolved")

    def test_N1_sibling_links_are_scrubbed_into_dead_links(self) -> None:
        """DEFECT (BLOCKS-SYNTHESIS): unresolve_note (V21 rule) replaces every listed name
        in every non-Evidence line, also inside [[...]]. `[[Tomas Brink Predicts ...]]`
        becomes `[[the speaker Predicts ...]]` BEFORE synth_unit rewrites sibling links
        (it looks for the old title, which is gone). Every note of the video is then
        quarantined as dead-link: a speaker-unresolved video still publishes nothing."""
        self._synth()
        outs = sorted((self.unit2 / "out").rglob("*.md"))
        self.assertTrue(outs)
        self.assertTrue(all("[[the speaker " in p.read_text() for p in outs))
        ev = ri.finalize(self.staging, self.run_dir, self.unit2, [EX2], 0, self.zk)
        acts = {n["note"]: (n["action"], n.get("failure_types")) for n in ev["notes"]}
        self.assertTrue(acts)
        self.assertTrue(all(a == "quarantined" and "dead-link" in ft for a, ft in acts.values()), acts)

    def test_ok_N1_control_with_correct_links_the_notes_are_not_quarantined(self) -> None:
        """CORRECT (control): the same notes with the sibling links pointed at the real
        new titles (and the fixture's link to a missing Mara Kell note replaced by Home)
        are not quarantined. So the link scrub alone blocks the video."""
        self._synth()
        outs = sorted((self.unit2 / "out").rglob("*.md"))
        titles = [p.stem for p in outs]
        for p in outs:
            t = p.read_text()

            def fix(m: re.Match) -> str:
                rest = m.group(1)
                hit = next((x for x in titles if x.endswith(rest)), None)
                return f"[[{hit}]]" if hit else "[[Home]]"
            p.write_text(re.sub(r"\[\[the speaker ([^\]]+)\]\]", fix, t))
        ev = ri.finalize(self.staging, self.run_dir, self.unit2, [EX2], 0, self.zk)
        acts = {n["note"]: (n["action"], n.get("failure_types")) for n in ev["notes"]}
        self.assertTrue(acts)
        self.assertTrue(all(a != "quarantined" for a, _ in acts.values()), acts)


class UnresolveNames(_Single):
    def _note(self) -> dict:
        return sc.parse_reply(S.BATCH)["notes"][0]

    def test_N2_channel_named_after_the_person_keeps_the_name_in_the_lead(self) -> None:
        """DEFECT (SHOULD-FIX, V21 rest): `persons` leaves out the channel name. Most
        channels carry the owner's name, so with channel == lit speaker 'Tomas Brink' the
        lead of an unresolved note still says 'Tomas Brink tells ...'. The checker reads
        only sources[].speaker, so the note passes."""
        n = self._note()
        chk = {"lit_speakers": ["Tomas Brink"], "channel": "Tomas Brink",
               "found": [{"label": "S1", "name": "Tomas Brink"}, {"label": "S2", "name": "unknown"}]}
        u = sc.unresolve_note(n, "Tomas Brink", chk)
        self.assertTrue(u["title"].startswith("Unresolved Speaker In Tomas Brink Video"))
        self.assertTrue(_lead(u["text"]).startswith("Tomas Brink tells his athletes"))
        p = self.tmp / f"{u['title']}.md"
        p.write_text(u["text"])
        self.assertEqual(vc.verify_note(p, vc.LitIndex([S.NC / "lit_fictional"]))["failures"], [])

    def test_N3_a_first_name_alone_stays(self) -> None:
        """DEFECT (SHOULD-FIX, V21 rest): only the full listed name is replaced; 'Mara tells
        her athletes' (the guest's first name, found as 'Mara Kell') stays in the lead."""
        n = self._note()
        text = n["text"].replace("Tomas Brink tells his athletes", "Mara tells her athletes")
        chk = S._check()
        u = sc.unresolve_note(dict(n, text=text), chk["channel"], chk)
        self.assertTrue(_lead(u["text"]).startswith("Mara tells her athletes"))

    def test_N4_a_short_name_that_is_a_word_corrupts_the_text(self) -> None:
        """DEFECT (MINOR): a found name that is also a word ('Max') is replaced
        case-insensitively in the lead, Details and frontmatter: 'two max hangs sessions'
        becomes 'two the speaker hangs sessions'."""
        n = self._note()
        text = n["text"].replace("between two hard finger sessions. [src: vid-EXAMPLE0002 @ 01:40]\n\n## Details",
                                 "between two max hangs sessions. [src: vid-EXAMPLE0002 @ 01:40]\n\n## Details", 1)
        chk = {"lit_speakers": ["Tomas Brink"], "channel": "Tomas Brink Climbing",
               "found": [{"label": "S1", "name": "Tomas Brink"}, {"label": "S2", "name": "Max"}]}
        u = sc.unresolve_note(dict(n, text=text), "Tomas Brink Climbing", chk)
        self.assertIn("two the speaker hangs sessions", _lead(u["text"]))


class Respeak(_Single):
    def _put(self, channel: str = "Tomas Brink Climbing") -> str:
        import shutil  # round 21 adaptation: the lit file is in the staging (as in a real run, W26)
        shutil.copy(S.NC / "lit_fictional" / f"{EX2}.md", self.staging / "lit")
        n = sc.parse_reply(S.BATCH)["notes"][1]
        u = sc.unresolve_note(n, channel, S._check())
        p = self.zk / PN / f"{u['title']}.md"
        p.write_text(u["text"])
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{p.name}", sources=[EX2])
        return p.stem

    def _types(self, title: str) -> set[str]:
        r = vc.verify_note(self.zk / PN / f"{title}.md", vc.LitIndex([S.NC / "lit_fictional"]))
        return {f["type"] for f in r["failures"]}

    def test_ok_respeak_with_the_lit_speaker_passes_the_checker(self) -> None:
        """CORRECT (control): respeak with the lit speaker gives a note that passes."""
        self._put()
        out = sc.respeak(self.staging, self.zk, EX2, "Tomas Brink", apply=True)
        self.assertIsNone(out[0]["problem"])
        self.assertEqual(self._types(out[0]["to"][:-3]), set())

    def test_N5_respeak_publishes_a_name_the_lit_data_does_not_support(self) -> None:
        """DEFECT (SHOULD-FIX): respeak never checks the name against the video's lit
        speakers (nor speaker_evidence). A name that is not in the lit file (a typo, or
        speakers.yaml not re-ingested yet) is written to the vault; the note now fails the
        checker with wrong-speaker, and the final validate (contract) reports it."""
        self._put()
        out = sc.respeak(self.staging, self.zk, EX2, "Sasha Vey", apply=True)
        self.assertIsNone(out[0]["problem"])
        self.assertTrue((self.zk / PN / out[0]["to"]).is_file())
        self.assertIn("wrong-speaker", self._types(out[0]["to"][:-3]))

    def test_N5b_a_name_with_brackets_breaks_the_evidence_items(self) -> None:
        """DEFECT (MINOR): '--name "Tomas Brink (host)"' is accepted; the Evidence items get
        nested parentheses (V4 again) and the note fails evidence-format."""
        self._put()
        out = sc.respeak(self.staging, self.zk, EX2, "Tomas Brink (host)", apply=True)
        self.assertIsNone(out[0]["problem"])
        self.assertIn("evidence-format", self._types(out[0]["to"][:-3]))

    def test_N6_channel_with_the_word_video_breaks_the_new_title(self) -> None:
        """DEFECT (MINOR): respeak finds the prefix with `In .+? Video` (non-greedy). For the
        channel 'Brink Video Lab' the prefix is 'Unresolved Speaker In Brink Video Lab
        Video'; the regex stops at the first ' Video', and the new title keeps 'Lab Video'."""
        old = self._put(channel="Brink Video Lab")
        self.assertTrue(old.startswith("Unresolved Speaker In Brink Video Lab Video "))
        out = sc.respeak(self.staging, self.zk, EX2, "Tomas Brink", apply=False)
        self.assertTrue(out[0]["to"].startswith("Tomas Brink Lab Video Predicts"), out)

    def test_N7_respeak_leaves_case_path_and_md_links_dead(self) -> None:
        """DEFECT (SHOULD-FIX): validate (V30) resolves `[[lower case]]`, `[[folder/Title]]` and
        `[[Title.md]]` as Obsidian does, so these links are valid before respeak. rewrite_links
        matches only the exact `[[Title` form; after the rename the three links are dead
        (an ERROR when the linking note is a pipeline note)."""
        old = self._put()
        linker = "Linker Note"
        (self.zk / PN / f"{linker}.md").write_text(
            f"---\ntype: permanent note\n---\n\n# {linker}\n\nx [[{old.lower()}]] y [[{PN}/{old}]] z [[{old}.md]]\n")
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{linker}.md", sources=["vid-OTHER"])
        before = [e for e in validate.check(self.zk, None, str(self.staging))[0] if "dead link" in e and linker in e]
        self.assertEqual(before, [])
        out = sc.respeak(self.staging, self.zk, EX2, "Tomas Brink", apply=True)
        self.assertIsNone(out[0]["problem"])
        self.assertEqual(out[0]["links_rewritten"], [])
        after = [e for e in validate.check(self.zk, None, str(self.staging))[0] if "dead link" in e and linker in e]
        self.assertEqual(len(after), 3, after)


class ModelWrittenUnresolved(_Single):
    def test_N8_a_model_can_mark_its_own_note_unresolved(self) -> None:
        """DEFECT (MINOR): the contract says only the harness writes `speaker_status:
        unresolved` and the `unresolved speaker, ...` value, but nothing enforces it. In the
        ONE-speaker fictional video (lit: Tomas Brink, multi_speaker false) a model note
        with that pair and the vague title 'A Speaker In The Video ...' passes the checker:
        no wrong-speaker, no vague-speaker."""
        n = sc.parse_reply(S.BATCH)["notes"][1]
        title = "A Speaker In The Video Predicts That Most People Add One Grade After About Six Weeks On His Plan"
        t = n["text"].replace(f"# {n['title']}", f"# {title}").replace('speaker: "Tomas Brink"',
                                                                       'speaker: "unresolved speaker, any video"')
        t = t.replace("(Tomas Brink):", "(unresolved speaker, any video):").replace(
            "verification: unverified", "speaker_status: unresolved\nverification: unverified")
        p = self.tmp / f"{title}.md"
        p.write_text(t)
        r = vc.verify_note(p, vc.LitIndex([S.NC / "lit_fictional"]))
        self.assertEqual([f["type"] for f in r["failures"]], [])
