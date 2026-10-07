"""Probe 8 A2 (reviewer A2, round 3): lifecycle attacks on the round-18..20 repair bookkeeping.

Convention of THIS file: the assertion states the DEFECT. PASS = the defect is present.
FAIL = the defect is not present (or the setup broke; read the failure). Fake LLMs only."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5

import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402


def _rep(old: str, title: str, bids: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bids}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


def _details_add(text: str, line: str) -> str:
    return text.replace("## Details\n\n", f"## Details\n\n- {line} [src: {V5} @ 01:16]\n", 1)


class _Base(_Repair):
    def summary_clean(self) -> bool:
        no = ri._read_json(self.staging / "needs_operator.json", []) or []
        return not ri.bullet_invariant(self.staging, self.zk) and not ri.repair_open_bullets(self.staging) and not no

    def two_batches(self, old: str, first_finalize: bool = True):
        """Old note with 2 bullets, 1 bullet per call (same video): call 1 writes A for b1."""
        os.environ["ZR_REPAIR_MAX_BULLETS"] = "1"
        rel = self.old(old, 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.run_batch(state, _rep(old, self.a, "b1", self.ta, f"{old}.md#b1 | kept in {self.a}\n"),
                       finalize=first_finalize)
        return rel, state


class ExtensionOverwritesUserEdit(_Base):
    """F1: call 2 extends the PUBLISHED target A (a prior title). apply_repair records a base
    sha only for the OLD notes, not for A. The user edits A while unit 2 runs (or while A's
    extension waits in pending review). Finalize overwrites A with no conflict-stale-base;
    the user's text is only in retired/; needs_operator.json and the invariant say nothing."""

    def test_user_edit_of_published_target_is_overwritten(self) -> None:
        rel, state = self.two_batches("Old Ue")
        a_ext = _details_add(self.ta, "Second legacy point, now from the video.")
        b2, u2, res2, _ = self.run_batch(state, _rep("Old Ue", self.a, "b1,b2", a_ext,
                                                     f"Old Ue.md#b2 | kept in {self.a}\n"), finalize=False)
        self.assertIn(self.a, res2["written"])
        pa = self.vpath(self.a)
        pa.write_text(pa.read_text().replace("## Details\n\n", f"## Details\n\n- USER WROTE THIS BY HAND. [src: {V5} @ 01:22]\n", 1))
        ev = ri.finalize(self.staging, self.run_dir, u2, [], 0, self.zk)
        n = [x for x in ev["notes"] if x["note"] == f"{PN}/{self.a}.md"][0]
        self.assertEqual(n["action"], "published", n)
        self.assertNotIn("conflict-stale-base", n["failure_types"])
        self.assertNotIn("USER WROTE THIS BY HAND.", pa.read_text())  # the edit is gone from the vault
        self.assertFalse((self.staging / "needs_operator.json").exists())
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])


class RenamedReplacementBesideOld(_Base):
    """F2: a fix call renames the replacement A -> A2, and the user edits the old note during
    the unit (the harness stub fails conflict-stale-base). A2 publishes. replacement_beside_old
    reads the repair_map / note-repair titles (A), never the renamed A2, so the edited old note
    stays beside its published replacement with NO needs_operator.json entry; recompute skips
    it (R15 guard) and the invariant is clean (b1 settled in A2)."""

    def test_old_note_beside_renamed_replacement_is_not_reported(self) -> None:
        rel = self.old("Old Rn", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        b, u, res, _ = self.run_batch(state, _rep("Old Rn", self.a, "b1", self.ta, f"Old Rn.md#b1 | kept in {self.a}\n"),
                                      review=False, finalize=False)
        a2 = "Floor Presses Need No Parallettes Renamed"
        self.fake(fix=[f"=====NOTE: {a2}=====\n" + self.ta.replace(f"# {self.a}\n", f"# {a2}\n")])
        sc._apply_fix(self.staging, self.zk, u, f"{PN}/{self.a}.md",
                      sc.parse_reply(f"=====NOTE: {a2}=====\n" + self.ta.replace(f"# {self.a}\n", f"# {a2}\n")))
        self.assertEqual(json.loads((u / "marked.json").read_text())["renamed"], {self.a: a2})
        rc.review_unit(self.staging, self.zk, u)
        p = self.vpath("Old Rn")
        p.write_text(p.read_text() + "\nUSER EDIT DURING THE UNIT.\n")
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        acts = {Path(n["note"]).stem: n for n in ev["notes"]}
        self.assertEqual(acts[a2]["action"], "published", ev["notes"])
        self.assertIn("conflict-stale-base", acts["Old Rn"]["failure_types"])
        self.assertNotIn("status: superseded", p.read_text())  # old note beside A2
        sc.next_batch(self.staging, self.zk, state)
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)  # a later pass
        self.assertEqual(ri.replacement_beside_old(self.staging, self.zk), [])
        self.assertFalse((self.staging / "needs_operator.json").exists())
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        self.assertEqual(ri.repair_open_bullets(self.staging), [])


class ExtendKeepsTagsDropsText(_Base):
    """F3 (V7 remainder): extension_loss and reopen_uncited compare only Evidence TAGS. Call 2
    rewrites A with A's Evidence section byte-equal but with B's lead and Details (call 1's
    text of A is gone). A publishes, b1 stays settled in A, and the summary is clean."""

    def test_extension_keeps_evidence_drops_text(self) -> None:
        rel, state = self.two_batches("Old Kt")
        lead_a = [ln for ln in self.ta.split("\n") if "[src:" in ln][0]
        ev_a = self.ta.split("## Evidence\n\n", 1)[1].split("\n## Connected Ideas", 1)[0].strip("\n")
        ext = self.tb.replace(f"# {self.b}\n", f"# {self.a}\n").replace(
            "\n## Connected Ideas", "\n" + ev_a + "\n\n## Connected Ideas", 1).replace("\n\n\n", "\n\n")
        _b2, _u2, _r2, ev2 = self.run_batch(state, _rep("Old Kt", self.a, "b1,b2", ext,
                                                        f"Old Kt.md#b2 | kept in {self.a}\n"))
        n = [x for x in ev2["notes"] if x["note"] == f"{PN}/{self.a}.md"][0]
        self.assertEqual(n["action"], "published", n)
        self.assertNotIn(lead_a, self.vpath(self.a).read_text())
        sc.next_batch(self.staging, self.zk, state)
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["bullets"]["Old Kt.md#b1"]["answers"][V5]["decision"], "kept")
        self.assertTrue(self.summary_clean(), (ri.bullet_invariant(self.staging, self.zk), ri.repair_open_bullets(self.staging)))


class FixDropsClaimKeepsEvidence(_Base):
    """F3b: the same gap inside one unit. The review fix removes the Details line that carried
    b1 (for example, the review judged it an overclaim) and keeps the Evidence item. The
    `cited` tag is still in A, so reopen_uncited_bullets keeps b1 `kept`; the summary is clean
    although no published text states b1 any more."""

    def test_fix_removes_details_line_only(self) -> None:
        rel = self.old("Old Fx", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        _b, u, _res, _ = self.run_batch(state, _rep("Old Fx", self.a, "b1", self.ta, f"Old Fx.md#b1 | kept in {self.a}\n"),
                                        review=False, finalize=False)
        a = json.loads(state.read_text())["notes"][rel]["bullets"]["Old Fx.md#b1"]["answers"][V5]
        self.assertTrue(a.get("cited"), a)
        det = [ln for ln in self.ta.split("\n") if ln.startswith("- He says you don't need parallettes")][0]
        fixed = self.ta.replace(det + "\n", "", 1)
        r = sc._apply_fix(self.staging, self.zk, u, f"{PN}/{self.a}.md", sc.parse_reply(f"=====NOTE: {self.a}=====\n" + fixed))
        self.assertEqual(r["result"], "rewritten")
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.assertNotIn(det, self.vpath(self.a).read_text())
        sc.next_batch(self.staging, self.zk, state)
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["bullets"]["Old Fx.md#b1"]["answers"][V5]["decision"], "kept")
        self.assertTrue(self.summary_clean())
