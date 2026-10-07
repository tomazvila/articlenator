"""Reviewer C, fourth review (frozen-v9): earlier probes whose setup broke on v9, adapted
minimally. Each class names the original probe and the adaptation.

CONVENTION: given per class in its docstring.
- `Defect*` classes: the assertion states the DEFECT. PASS = defect present.
- `Ok*` classes: the assertion states the CORRECT behaviour. PASS = fixed.
No network, no LLM; fake models and fake workers only. The replay class reads the
authors' round-21 pilot-7 replay (read-only) and is skipped when it is missing."""
from __future__ import annotations

import json
import re
import shutil
import unittest
from pathlib import Path

from tests.probes_v8.test_probe8_A1_new import B_FOUR_Q, V2, _Ledger
from tests.probes_v8.test_probe8_B_attacks import _Files
from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_synth_single import V5
from tests.probes_v8 import test_probe8_C_speakers as _cs

import synth_call as sc  # noqa: E402

REPLAY_P7 = Path("/tmp/claude-1000/-home-deploy/8cef0e1f-75a1-4258-8f75-cea6749f2270/scratchpad/pipefix/review-r21/p7")


class DefectW1GenericCitation(_Ledger):
    """DEFECT convention. Original: test_probe8_A1_new.py::GenericCitationSurvivesFix.
    Adaptation: `cited` items are now [video, time, quote hash], and `_verify_ledger` gets the
    video id as in production. Defect: after a fix removes the 07:10 item (the one that
    carries "10 to 15 reps ... quality"), the bullet stays `kept` in B."""

    def test_fix_removes_the_carrying_item(self) -> None:
        lit = (self.staging / "lit" / f"{V5}.md").read_text()
        bid = "Old Q.md#b1"
        p = {"notes": [{"title": self.b, "text": self.tb, "complete": True, "truncated": False}],
             "bullets": {bid: {"decision": "kept", "title": self.b, "titles": [self.b]}}, "problems": []}
        sc._verify_ledger(p, {bid: sc.bullet_candidates(lit, B_FOUR_Q, top=sc.LEDGER_VERIFY_TOP)}, self.zk,
                          {bid: B_FOUR_Q}, V5)
        d = p["bullets"][bid]
        self.assertEqual(d["decision"], "kept", d)
        cited_times = sorted(c[1] for c in d["cited"])
        st = {"notes": {f"{PN}/Old Q.md": {"status": "done", "titles": [self.b], "sources": [V5], "pos": 0,
                                            "bullets": {bid: {"text": B_FOUR_Q, "answers": {V5: d}}}}}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        fixed = re.sub(r"(?m)^- .*@ 07:10.*\n", "", self.tb)
        self.assertNotIn("07:10", fixed)
        ri.reopen_uncited_bullets(self.staging, self.zk, {self.b: fixed})
        a = json.loads((self.staging / "repair_state.json").read_text())["notes"][f"{PN}/Old Q.md"]["bullets"][bid]
        self.assertEqual(a["answers"][V5]["decision"], "kept", (cited_times, a["answers"][V5]))


class DefectW32CrossVideo(_Ledger):
    """DEFECT convention. Original: test_probe8_A1_new.py::CrossVideoEvidence. Adaptation:
    the video id (video 2) is passed as in production; `cited` has 3 fields. Defect: an
    Evidence item of video 1 verifies a video-2 answer."""

    def test_other_video_item_counts(self) -> None:
        shutil.copy(Path(__file__).resolve().parents[1] / "fixtures/note_contract/lit_fictional" / f"{V2}.md",
                    self.staging / "lit")
        lit2 = (self.staging / "lit" / f"{V2}.md").read_text()
        bt = "Row steady pieces."
        bid = "Old X.md#b2"
        p = {"notes": [{"title": self.a, "text": self.ta, "complete": True, "truncated": False}],
             "bullets": {bid: {"decision": "kept", "title": self.a, "titles": [self.a]}}, "problems": []}
        sc._verify_ledger(p, {bid: sc.bullet_candidates(lit2, bt)}, self.zk, {bid: bt}, V2)
        self.assertEqual(p["bullets"][bid]["decision"], "kept")


class OkS9Unreachable(_Files):
    """CORRECT convention. Original: test_probe8_B_regress.py::S9Unreachable. The original
    does NOT call `_mark_obsolete_auto`; it counts the regex `_mark_obsolete_auto\\(` in the
    source and expects 1 (the definition). Adaptation: the definition is removed, so the
    expected count is 0. The behaviour part is unchanged."""

    def test_no_request_is_marked_obsolete_by_the_harness(self) -> None:
        self.fake()
        self.synth()
        run2 = ri.start(self.staging, "synth", self.zk)
        ri._write_json(run2 / "exchange_wanted.json", {"x" * 32: "other"})
        sc.exchange_status(self.xdir, self.staging)
        sc.exchange_list(self.xdir, staging=self.staging)
        self.assertEqual(list((self.xdir / "requests").glob("*.obsolete")), [])
        src = (ZR / "synth_call.py").read_text()
        self.assertEqual(len(re.findall(r"_mark_obsolete_auto\(", src)), 0)
        self.assertNotIn("exchange_wanted.json", src)


class OkN1Control(_cs.UnresolvedEndToEnd):
    """CORRECT convention. Original: test_probe8_C_speakers.py::UnresolvedEndToEnd::
    test_ok_N1_control_*. Adaptation: W6 no longer scrubs links, so the control's repair of
    `[[the speaker ...]]` finds nothing; the fixture link to the missing "Mara Kell" note
    stays and is dead. The adapted control replaces every link that is neither a sibling of
    the unit nor a vault note with [[Home]] (the same intent as the original)."""

    test_N1_sibling_links_are_scrubbed_into_dead_links = None  # not part of this class

    def test_ok_N1_control_with_correct_links_the_notes_are_not_quarantined(self) -> None:
        self._synth()
        outs = sorted((self.unit2 / "out").rglob("*.md"))
        titles = {p.stem for p in outs} | {p.stem for p in self.zk.rglob("*.md")}
        self.assertFalse([p for p in outs if "[[the speaker " in p.read_text()])  # W6: no scrubbed link
        for p in outs:
            t = p.read_text()
            p.write_text(re.sub(r"\[\[([^\]|#]+)([^\]]*)\]\]",
                                lambda m: m.group(0) if m.group(1).strip() in titles else "[[Home]]", t))
        ev = ri.finalize(self.staging, self.run_dir, self.unit2, [_cs.EX2], 0, self.zk)
        acts = {n["note"]: (n["action"], n.get("failure_types")) for n in ev["notes"]}
        self.assertTrue(acts)
        self.assertTrue(all(a != "quarantined" for a, _ in acts.values()), acts)


@unittest.skipUnless((REPLAY_P7 / "zr").is_dir(), "round-21 pilot-7 replay not present")
class DefectReplayTtFb3R21(unittest.TestCase):
    """DEFECT convention. Original: test_probe8_A1_new.py::ReplayTtFb3 (read the review-r20
    replay of v8). Adaptation: read the authors' round-21 replay of v9 (review-r21/p7) and
    the stub line. Defect: TTF b3 ("hold times drop ... fifteen ... ten ... five") still
    points to a note that does not carry it. v9 marks the pointer `(unverified)` and keeps
    the old bullet text in the stub (design change), so it is visible, not silent."""

    def test_b3_points_to_a_note_without_it(self) -> None:
        import stub_check
        zk = REPLAY_P7 / "vault/Video Transcripts Zettelkasten"
        stub = next((zk / PN).glob("Training To Failure Every Session*.md")).read_text()
        entries = dict(stub_check.claim_entries(stub))
        b3 = next(k for k in entries if "fifteen seconds to ten to five" in k)
        st = entries[b3]
        self.assertTrue(st.startswith("→ [["), st)
        self.assertTrue(st.endswith("(unverified)"), st)
        target = re.search(r"\[\[([^\]]+)\]\]", st).group(1)
        t = (zk / PN / f"{target}.md").read_text().lower()
        self.assertNotIn("fifteen", t)
        self.assertNotIn("15 seconds", t)


class OkW4GarbledThenGood(_Files):
    """CORRECT convention. Original: test_probe8_B_attacks.py::GarbledRepairReply (it states
    the defect; it now FAILs at `'open' != 'done-unrepaired'`). This class follows the fix to
    its end: after the garbled reply is refused, the unit waits; a good reply in the next
    pass is consumed and settles b1 in A. Never `done-unrepaired`."""

    def test_garbled_reply_waits_then_the_good_reply_settles(self) -> None:
        from tests.unit.test_run_integrity import OLD_NOTE
        from tests.unit.test_run_integrity_r13 import work
        text = OLD_NOTE.format(t="Old Garb").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath("Old Garb").write_text(text)
        rel = f"{PN}/Old Garb.md"
        state = self.staging / "repair_state.json"
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)

        def one() -> dict:
            b = sc.next_batch(self.staging, self.zk, state)
            u = self.new_unit("repair", [])
            res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                 b["prior_titles"], b["last"])
            sc.record_answers(state, b, res)
            return res
        self.assertEqual(one()["stopped"], "waiting-for-reply")
        ri.release_waiting(self.staging, "repair")
        work(self.xdir, lambda r: "I am sorry, I cannot read this transcript.\n", worker="w1")
        self.assertEqual(one().get("stopped"), "waiting-for-reply")  # refused, waits again
        self.assertEqual(json.loads(state.read_text())["notes"][rel]["status"], "open")
        ri.release_waiting(self.staging, "repair")
        good = (f"=====NOTE: {self.a} | repairs: Old Garb.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                f"=====BULLETS=====\nOld Garb.md#b1 | kept in {self.a}\n")
        self.assertEqual(work(self.xdir, lambda r: good, worker="w2"), 1)
        res = one()
        self.assertFalse(res.get("stopped"), res)
        e = json.loads(state.read_text())["notes"][rel]
        self.assertNotEqual(e["status"], "done-unrepaired")
        self.assertIn(e["bullets"]["Old Garb.md#b1"]["answers"][V5]["decision"], ("kept", "unverified"))
