"""Reviewer C, round 2 (frozen-v7): speakers (speaker_check, unresolve_note, respeak).

Convention: each assertion states the DEFECT. PASS = the defect is present.
FAIL = the defect is fixed (or the API changed). Fake LLM only, no network."""
from __future__ import annotations

import os
import shutil

from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_synth_single import _Single

import synth_call as sc  # noqa: E402
import verify_claims as vc  # noqa: E402

NC = ZR.parent / "tests" / "fixtures" / "note_contract"
EX2 = "vid-EXAMPLE0002"
BATCH = (NC / "batch_reply_fictional.txt").read_text()
SPK2 = ("=====SPEAKERS=====\nS1 | Tomas Brink | host | \"welcome back\" 00:00\n"
        "S2 | Mara Kell | guest | \"tomas asked me\" 01:40\n")


def _tr():
    return vc.LitIndex([NC / "lit_fictional"]).get(EX2)


def _check():
    return sc.speaker_check(_tr(), [{"label": "S1", "name": "Tomas Brink", "role": "host"},
                                    {"label": "S2", "name": "Mara Kell", "role": "guest"}])


class UnresolveNote(_Single):
    def test_U1_unresolved_note_fails_the_checker_evidence_format(self) -> None:
        """DEFECT (BLOCKS-SYNTHESIS for several-voice videos): unresolve_note writes the
        Evidence speaker as `(unresolved (<ch> video, several voices))`. EVIDENCE_ITEM
        allows no `)` inside the parentheses, so every Evidence item is `evidence-format`
        and every tag has `no-evidence-for-tag`: the note can never pass the check."""
        n = sc.parse_reply(BATCH)["notes"][0]
        chk = _check()
        self.assertEqual(chk["status"], "unresolved")
        u = sc.unresolve_note(n, chk["channel"], chk)
        p = self.tmp / f"{u['title']}.md"
        p.write_text(u["text"])
        types = {f["type"] for f in vc.verify_note(p, vc.LitIndex([NC / "lit_fictional"]))["failures"]}
        self.assertIn("evidence-format", types)
        self.assertIn("no-evidence-for-tag", types)
        # the same note before unresolve_note has no evidence failure
        p0 = self.tmp / "orig.md"
        p0.write_text(n["text"])
        types0 = {f["type"] for f in vc.verify_note(p0, vc.LitIndex([NC / "lit_fictional"]))["failures"]}
        self.assertNotIn("evidence-format", types0)

    def test_U2_lead_and_details_keep_the_unconfirmed_name(self) -> None:
        """DEFECT: only `speaker:`, the Evidence parentheses and a title that STARTS with a
        name change. The lead keeps 'Mara Kell ...' (a name the lit data does not list),
        and no checker rule looks at the lead: speaker-unresolved-named reads only
        sources[].speaker."""
        n = sc.parse_reply(BATCH)["notes"][0]
        text = n["text"].replace("Tomas Brink tells his athletes", "Mara Kell tells her athletes")
        text = text.replace('speaker: "Tomas Brink"', 'speaker: "Mara Kell"')
        title = n["title"].replace("Tomas Brink", "Mara Kell")
        text = text.replace(f"# {n['title']}", f"# {title}")
        chk = _check()
        u = sc.unresolve_note(dict(n, title=title, text=text), chk["channel"], chk)
        self.assertTrue(u["title"].startswith("Unresolved Speaker ("))
        self.assertIn("Mara Kell tells her athletes", u["text"])  # the name is still published
        self.assertIn("'Mara Kell' is not in the lit data", chk["reasons"])
        p = self.tmp / f"{u['title']}.md"
        p.write_text(u["text"])
        types = {f["type"] for f in vc.verify_note(p, vc.LitIndex([NC / "lit_fictional"]))["failures"]}
        self.assertNotIn("speaker-unresolved-named", types)

    def test_U3_title_with_the_name_not_at_the_start_keeps_the_name(self) -> None:
        """DEFECT: 'Coach Mara Kell Recommends ...' or 'Why Mara Kell ...' does not start with
        a listed name, so the title is not renamed and publishes the unconfirmed name."""
        n = sc.parse_reply(BATCH)["notes"][0]
        chk = _check()
        for title in ("Coach Mara Kell Advises 48 Hours Between Hard Finger Sessions",
                      "Why Mara Kell Leaves 48 Hours Between Hard Finger Sessions"):
            text = n["text"].replace(f"# {n['title']}", f"# {title}")
            u = sc.unresolve_note(dict(n, title=title, text=text), chk["channel"], chk)
            self.assertEqual(u["title"], title)
            self.assertIn("Mara Kell", u["title"])


class UnresolvedEndToEnd(_Single):
    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_SYNTH_STAGES"] = "two"
        shutil.copy(NC / "lit_fictional" / f"{EX2}.md", self.staging / "lit")
        self.write_queue([{"id": EX2, "kind": "video", "stage": "claimed", "claimed_from": "extracted",
                           "lit_note": str(self.staging / "lit" / f"{EX2}.md")}])
        self.unit2 = self.new_unit("synth", [EX2])

    def test_U4_no_note_of_an_unresolved_video_publishes(self) -> None:
        """DEFECT (end to end): the inventory finds a host and a guest; every note is
        written with nested parentheses in Evidence and is quarantined at finalize."""
        inv = (NC / "inventory_reply_fictional.txt").read_text().split("=====SPEAKERS=====")[0] + SPK2
        self.fake(inventory=[inv], batch=[BATCH, "=====SKIPPED=====\nC8 | not a transferable training claim: "
                                                 "no content beyond the word warm-up\n"])
        res = sc.synth_unit(self.staging, self.zk, self.unit2, [EX2])
        self.assertEqual(res["speaker_status"], "unresolved")
        self.assertTrue(res["notes"])
        ev = ri.finalize(self.staging, self.run_dir, self.unit2, [EX2], 0, self.zk)
        acts = {n["note"]: (n["action"], n.get("failure_types")) for n in ev["notes"]}
        self.assertTrue(acts)
        self.assertTrue(all(a == "quarantined" and "evidence-format" in ft for a, ft in acts.values()), acts)


class Respeak(_Single):
    def _unres(self, title: str, label: str = "", ch: str = "Brink Climbing", vid: str = EX2) -> None:
        lab = f"speaker_label: {label}\n" if label else ""
        (self.zk / PN / f"{title}.md").write_text(
            f"---\ntype: permanent note\nsources:\n  - id: {vid}\n    speaker: \"unresolved ({ch} video, several "
            f"voices)\"\n{lab}speaker_status: unresolved\nverification: unverified\n---\n\n# {title}\n\nLead. "
            f"[src: {vid} @ 01:40]\n\n## Evidence\n\n- {vid} @ 01:40 (unresolved ({ch} video, several voices)): "
            "\"I tell my athletes to leave at least 48 hours between two hard finger sessions.\"\n")

    def test_R1_respeak_overwrites_an_existing_note_of_another_video_without_archive(self) -> None:
        """DEFECT (SHOULD-FIX): the new title is checked only by _safe_title (characters,
        reserved names), not against existing notes; os.replace overwrites the note of
        another video with that title. Only the renamed note is archived."""
        other = self.zk / PN / "Sasha Says Rest Matters.md"
        other.write_text("---\ntype: permanent note\nsources:\n  - id: vid-OTHER\n    speaker: \"Sasha\"\n---\n\n"
                         "# Sasha Says Rest Matters\n\nOTHER VIDEO CONTENT.\n")
        self._unres("Unresolved Speaker (Brink Climbing) Says Rest Matters")
        rows = sc.respeak(self.staging, self.zk, EX2, "Sasha")
        self.assertIsNone(rows[0]["problem"])  # the dry run does not show the clash
        sc.respeak(self.staging, self.zk, EX2, "Sasha", apply=True)
        self.assertNotIn("OTHER VIDEO CONTENT", other.read_text())
        archived = [p.read_text() for p in self.staging.glob("retired/respeak-*/**/*.md")]
        self.assertFalse(any("OTHER VIDEO CONTENT" in t for t in archived))

    def test_R2_name_ignores_speaker_labels(self) -> None:
        """DEFECT (MINOR): `--name` gives one name to the notes of two different voices
        (speaker_label S1 and S2) with no warning; only `--map` reads the labels."""
        self._unres("Unresolved Speaker (Brink Climbing) Says Rest Matters", "S1")
        self._unres("Unresolved Speaker (Brink Climbing) Says Warm Up First", "S2")
        out = sc.respeak(self.staging, self.zk, EX2, "Sasha", apply=True)
        self.assertEqual(sorted(r["to"] for r in out), ["Sasha Says Rest Matters.md", "Sasha Says Warm Up First.md"])
        self.assertTrue(all(r["problem"] is None for r in out))

    def test_R3_channel_with_parentheses_breaks_the_new_title(self) -> None:
        """DEFECT (MINOR): `^Unresolved Speaker \\([^)]*\\)` stops at the first `)`; a
        channel such as 'Brink Climbing (Official)' gives 'Sasha) Says ...'."""
        self._unres("Unresolved Speaker (Brink Climbing (Official)) Says Rest Matters", ch="Brink Climbing (Official)")
        out = sc.respeak(self.staging, self.zk, EX2, "Sasha", apply=True)
        self.assertEqual(out[0]["to"], "Sasha) Says Rest Matters.md")
        self.assertTrue((self.zk / PN / "Sasha) Says Rest Matters.md").exists())

    def test_R4_respeak_leaves_unresolved_evidence_and_rewrites_links_without_archive(self) -> None:
        """DEFECT (SHOULD-FIX): respeak sets `speaker:` but leaves the Evidence items as
        `(unresolved (...))` (the note still fails evidence-format), and rewrite_links
        changes other vault notes in place with no archive copy and no provenance."""
        old = "Unresolved Speaker (Brink Climbing) Says Rest Matters"
        self._unres(old)
        (self.zk / PN / "Linker.md").write_text(f"x [[{old}]] y\n")
        sc.respeak(self.staging, self.zk, EX2, "Sasha", apply=True)
        new = (self.zk / PN / "Sasha Says Rest Matters.md").read_text()
        self.assertIn('speaker: "Sasha"', new)
        self.assertIn("(unresolved (Brink Climbing video, several voices)):", new)
        self.assertIn("[[Sasha Says Rest Matters]]", (self.zk / PN / "Linker.md").read_text())
        self.assertFalse([p for p in self.staging.glob("retired/**/Linker.md")])


class SpeakerCheckSubstring(_Single):
    def test_S1_a_short_name_inside_a_listed_name_counts_as_listed(self) -> None:
        """DEFECT (MINOR): speaker_check accepts a found name when it is a SUBSTRING of a
        listed name ('Tom' in 'tomasbrink'), so a second person named Tom is `resolved`."""
        from types import SimpleNamespace
        tr = SimpleNamespace(meta={"channel": "Tomas Brink Climbing", "multi_speaker": "true"},
                             speakers=["Tomas Brink", "Sasha"])
        got = sc.speaker_check(tr, [{"label": "S1", "name": "Tomas Brink", "role": "host"},
                                    {"label": "S2", "name": "Tom", "role": "guest"}])
        self.assertEqual(got["status"], "resolved")
