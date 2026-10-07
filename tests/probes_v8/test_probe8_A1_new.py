"""Probe 8 A1 (third review, reviewer A1): attacks on the round-20 ledger verification
(`_verify_ledger`, `reopen_uncited_bullets`, `extension_loss`, `bullet_invariant`).

Convention of THIS file: the assertion states the DEFECT. PASS = the defect is present.
FAIL = the defect is not present (or the setup broke; read the failure).
Exception: a test whose name starts with `test_ok_` states the CORRECT behaviour (a
control; PASS = correct). Fake LLMs only, no network. The replay probe reads the authors'
pilot-7 replay (read-only) and is skipped when it is missing."""
from __future__ import annotations

import json
import re
import shutil
import unittest
from pathlib import Path

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5

import synth_call as sc  # noqa: E402

V2 = "vid-EXAMPLE0001"
B_FLOOR = "You do not need parallettes: planche lean presses can be done on the wrists on the floor."
B_BICEPS = "Rotate the biceps forward, not inward, in the push-up starting position."
B_TOES = "Point the toes."
B_FOUR_Q = "Keep each of the four sets under 10 to 15 reps and focus on quality: slow and controlled reps with the feeling of the muscles."


def _old_note(title: str, bullets: list[str], srcs: tuple = (V5,)) -> str:
    src = "".join(f"  - id: {v}\n    speaker: X\n" for v in srcs)
    return ("---\ntype: permanent note\nsources:\n" + src + "created: 2026-09-12\nverification: unverified\n"
            "tags: [calisthenics, x, zettelkasten, permanent-note]\n---\n\n# " + title + "\n\nLegacy claim.\n\n"
            "## Details\n\n" + "".join(f"- {b}\n" for b in bullets) + "\n## Connected Ideas\n\n- [[Other]]\n")


def _untimed(text: str) -> str:
    """A note without times: `[src: v @ t]` -> `[src: v]`, `- v @ t (sp):` -> `- v (sp):`."""
    return re.sub(r"\s*@\s*\d{1,2}:\d{2}(?::\d{2})?", "", text)


class _Ledger(_Repair):
    def repair_once(self, old: str, bullets: list[str], reply: str):
        rel = f"{PN}/{old}.md"
        self.vpath(old).write_text(_old_note(old, bullets))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        b, u, res, ev = self.run_batch(state, reply)
        return rel, state, res, ev

    def assert_silent(self, rel: str, state: Path, lost: list[str]) -> None:
        e = json.loads(state.read_text())["notes"][rel]
        for bid in lost:
            self.assertEqual(e["bullets"][bid]["answers"][V5]["decision"], "kept", bid)
        stub = self.vpath(Path(rel).stem).read_text()
        self.assertIn("status: superseded", stub)
        self.assertNotIn("superseded_pending", stub)  # nothing listed open
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        self.assertEqual(ri.repair_open_bullets(self.staging), [])
        self.assertEqual(json.loads((self.staging / "needs_operator.json").read_text())
                         if (self.staging / "needs_operator.json").is_file() else [], [])


class StemFalseAccept(_Ledger):
    """D1: timed transcript. Note A (floor, Evidence 01:16 and 01:22) is named for three
    bullets. b2 (biceps, said at 01:50) shares only `rota`+`forw` with A's 01:22 quote
    (fingers/thumb); b3 has 2 stems, so only the time window applies. A carries neither, the
    ledger takes both, the stub lists nothing, and every report is clean."""

    def test_unrelated_bullets_settle_in_a(self) -> None:
        old = "Old Lean"
        reply = (f"=====NOTE: {self.a} | repairs: {old}.md | bullets: b1,b2,b3=====\n{self.ta.rstrip()}\n=====BULLETS=====\n"
                 f"{old}.md#b1 | kept in {self.a}\n{old}.md#b2 | kept in {self.a}\n{old}.md#b3 | kept in {self.a}\n")
        rel, state, res, ev = self.repair_once(old, [B_FLOOR, B_BICEPS, B_TOES], reply)
        a_now = self.vpath(self.a).read_text().lower()
        self.assertNotIn("biceps", a_now)
        self.assertNotIn("toes", a_now)
        self.assert_silent(rel, state, [f"{old}.md#b2", f"{old}.md#b3"])


class UntimedNoCheck(_Ledger):
    """D2 (decisive): an UNTIMED transcript (pilot 7: vid-krqBQjUydGY, vid-I29haShgQhk,
    vid-RcrIYne-Yrg). Candidate passages are `line N`, `at_seconds` gives None, and
    `_verify_ledger` skips the bullet (`if not ctimes: continue`). "kept in A" for a bullet
    that A does not carry settles it; no `cited` is recorded, so no later check reopens it."""

    def setUp(self) -> None:
        super().setUp()
        lit = self.staging / "lit" / f"{V5}.md"
        t = lit.read_text().replace("timestamps: true", "timestamps: false")
        lit.write_text(re.sub(r"(?m)^\[\d{1,2}:\d{2}\]\s*", "", t))

    def test_unrelated_bullet_settles_without_any_check(self) -> None:
        old = "Old Untimed"
        ta = _untimed(self.ta)
        reply = (f"=====NOTE: {self.a} | repairs: {old}.md | bullets: b1,b2=====\n{ta.rstrip()}\n=====BULLETS=====\n"
                 f"{old}.md#b1 | kept in {self.a}\n{old}.md#b2 | kept in {self.a}\n")
        rel, state, res, ev = self.repair_once(old, [B_FLOOR, B_FOUR_Q], reply)
        acts = {Path(n["note"]).stem: n.get("action") for n in ev["notes"]}
        self.assertEqual(acts.get(self.a), "published", ev["notes"])
        self.assertNotIn("cited", res["bullets"][f"{old}.md#b2"])
        self.assertNotIn("four sets", self.vpath(self.a).read_text().lower())
        self.assert_silent(rel, state, [f"{old}.md#b2"])

    def test_ok_control_timed_transcript_opens_b2(self) -> None:
        """Control (correct behaviour): the same reply on the TIMED transcript leaves b2 open."""
        lit = self.staging / "lit" / f"{V5}.md"
        shutil.copy(Path(__file__).resolve().parents[1] / "fixtures/zettel_honest/lit" / f"{V5}.md", lit)
        old = "Old Timed"
        reply = (f"=====NOTE: {self.a} | repairs: {old}.md | bullets: b1,b2=====\n{self.ta.rstrip()}\n=====BULLETS=====\n"
                 f"{old}.md#b1 | kept in {self.a}\n{old}.md#b2 | kept in {self.a}\n")
        rel, state, res, ev = self.repair_once(old, [B_FLOOR, B_FOUR_Q], reply)
        self.assertEqual(res["bullets"][f"{old}.md#b2"]["decision"], "unverified")
        self.assertIn(f"bullet: {old}.md#b2", self.vpath(old).read_text())


class GenericCitationSurvivesFix(_Ledger):
    """D3: `cited` holds EVERY Evidence item within 60 s that shares 2 stems, also a generic
    neighbour. A later fix that removes the item that really carried the bullet keeps one
    generic item, so `reopen_uncited_bullets` keeps the bullet settled. (Pilot-7 replay:
    "Training To Failure ..." b3, see ReplayTtFb3.)"""

    def test_fix_removes_the_carrying_item(self) -> None:
        lit = (self.staging / "lit" / f"{V5}.md").read_text()
        bid = "Old Q.md#b1"
        p = {"notes": [{"title": self.b, "text": self.tb, "complete": True, "truncated": False}],
             "bullets": {bid: {"decision": "kept", "title": self.b, "titles": [self.b]}}, "problems": []}
        sc._verify_ledger(p, {bid: sc.bullet_candidates(lit, B_FOUR_Q)}, self.zk, {bid: B_FOUR_Q})
        cited = p["bullets"][bid]["cited"]
        self.assertIn([V5, 430], cited)  # 07:10: the item that carries "10 to 15 reps ... quality"
        self.assertGreater(len(cited), 1)  # ... and generic neighbours (06:37 / 06:53: "four sets ... each")
        st = {"notes": {f"{PN}/Old Q.md": {"status": "done", "titles": [self.b], "sources": [V5], "pos": 0,
                                            "bullets": {bid: {"text": B_FOUR_Q, "answers": {V5: p["bullets"][bid]}}}}}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        fixed = re.sub(r"(?m)^- .*@ 07:10.*\n", "", self.tb)  # a fix drops the 07:10 Details and Evidence lines
        self.assertNotIn("07:10", fixed)
        self.assertNotIn("15", fixed.split("## Evidence")[0].split("---", 2)[2])
        out = ri.reopen_uncited_bullets(self.staging, self.zk, {self.b: fixed})
        self.assertEqual(out, {})
        a = json.loads((self.staging / "repair_state.json").read_text())["notes"][f"{PN}/Old Q.md"]["bullets"][bid]
        self.assertEqual(a["answers"][V5]["decision"], "kept")


class CrossVideoEvidence(_Ledger):
    """D4: `_verify_ledger` ignores the Evidence item's video: an item of video 1 near the
    TIME of a candidate passage of video 2 verifies a video-2 answer."""

    def test_other_video_item_counts(self) -> None:
        shutil.copy(Path(__file__).resolve().parents[1] / "fixtures/note_contract/lit_fictional" / f"{V2}.md",
                    self.staging / "lit")
        lit2 = (self.staging / "lit" / f"{V2}.md").read_text()
        bt = "Row steady pieces."
        bid = "Old X.md#b2"
        cands = sc.bullet_candidates(lit2, bt)
        p = {"notes": [{"title": self.a, "text": self.ta, "complete": True, "truncated": False}],
             "bullets": {bid: {"decision": "kept", "title": self.a, "titles": [self.a]}}, "problems": []}
        sc._verify_ledger(p, {bid: cands}, self.zk, {bid: bt})
        self.assertEqual(p["bullets"][bid]["decision"], "kept")
        self.assertTrue(all(v == V5 for v, _ in p["bullets"][bid]["cited"]))  # only video-1 items


class UntimedTagsBlind(_Ledger):
    """D5 (V7 remainder): untimed Evidence items all give the tag (video, None). A new version
    that drops every untimed Evidence item but one passes `extension_loss`, and the
    invariant's "lost Evidence" check sees nothing."""

    def test_rewrite_keeps_one_item(self) -> None:
        ta = _untimed(self.ta)
        rel = f"{PN}/{self.a}.md"
        self.vpath(self.a).write_text(ta)
        ri.provenance.record(self.staging, "note-published", note=rel, sources=[V5], bullets=["Old U.md#b1"],
                             evidence_tags=sorted([v, t] for v, t in ri._evidence_tags(ta)))
        ev_lines = [ln for ln in ta.split("\n") if ln.startswith(f"- {V5} (")]
        self.assertEqual(len(ev_lines), 2)
        new = ta.replace(ev_lines[1] + "\n", "")
        self.assertIsNone(ri.extension_loss(self.staging, rel, new))
        self.vpath(self.a).write_text(new)
        st = {"notes": {f"{PN}/Old U.md": {"status": "done", "titles": [self.a], "sources": [V5], "pos": 0,
                                            "bullets": {"Old U.md#b1": {"text": "x", "answers": {V5: {
                                                "decision": "kept", "title": self.a, "titles": [self.a]}}}}}}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        self.vpath("Old U").write_text(ri.repair_stub("Old U", [self.a]))
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])


REPLAY = Path("/tmp/claude-1000/-home-deploy/8cef0e1f-75a1-4258-8f75-cea6749f2270/scratchpad/pipefix/review-r20")


@unittest.skipUnless((REPLAY / "zr/staging_radoslav_radev/repair_state.json").is_file(), "replay missing")
class ReplayTtFb3(unittest.TestCase):
    """D3 on real data (authors' pilot-7 replay, current code): old note "Training To Failure
    ..." b3 ("hold times drop ... from fifteen seconds to ten to five") is `corrected` in
    "Radoslav Radev Advises Never Going To Failure Every Single Workout", cited at 230 / 236
    / 252. A pending-unit fix removed the 04:12 (252) Details and Evidence lines; the
    published note holds no "15 seconds", the stub does not list b3, and the invariant is
    clean."""

    def test_b3_settled_in_a_note_without_it(self) -> None:
        stg = REPLAY / "zr/staging_radoslav_radev"
        zk = REPLAY / "vault/Video Transcripts Zettelkasten"
        st = json.loads((stg / "repair_state.json").read_text())
        rel, e = next((k, v) for k, v in st["notes"].items() if "Training To Failure" in k)
        bid = next(b for b in e["bullets"] if b.endswith("#b3"))
        a = e["bullets"][bid]["answers"]["vid-e4gjJgvWViQ"]
        self.assertEqual(a["decision"], "corrected")
        self.assertIn(["vid-e4gjJgvWViQ", 252], a["cited"])
        t = (zk / PN / f"{a['title']}.md").read_text()
        self.assertNotIn("04:12", t)
        self.assertNotIn("15 seconds", t)
        self.assertNotIn(f"bullet: {bid}", (zk / rel).read_text())
        import os
        import tempfile
        os.environ.setdefault("ZR_STATE_DIR", tempfile.mkdtemp())
        self.assertEqual([x for x in ri.bullet_invariant(stg, zk) if bid in (x.get("bullets") or [])], [])
