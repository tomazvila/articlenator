"""Probe 8 A2 (reviewer A2, round 3): rename_in_state, extend-lost-evidence, needs_operator,
and the merge in repair units.

Convention of THIS file: the assertion states the DEFECT. PASS = the defect is present.
FAIL = the defect is not present (or the setup broke; read the failure). Fake LLMs only.
Exception: `MergeNotInRepair.test_ok_*` states the CORRECT behavior (PASS = no defect)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5

import synth_call as sc  # noqa: E402


def _rep(old: str, title: str, bids: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bids}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class RenameInStateIsGlobal(_Repair):
    """S1: rename_in_state(renamed) rewrites EVERY answer of EVERY old note whose title is a
    key of the unit's `renamed` map, and the map is applied again at each finalize of the unit
    (a pending unit carries marked.json). Unit 1 (old note O1) renamed A -> A2 and waits. A
    later unit publishes a NEW note titled A for old note O2 (A is free in the vault). When
    unit 1's pending unit finalizes, O2's answer moves to A2: O2's stub is rewritten to [[A2]]
    (the link to A, which holds O2's bullet, is gone), and the summary is clean."""

    def test_title_reuse_after_rename(self) -> None:
        a2 = "Floor Presses Need No Parallettes Renamed"
        o2 = "Old Second"
        rel2 = f"{PN}/{o2}.md"
        self.vpath(self.a).write_text(self.ta)  # the NEW note A of O2, published
        self.vpath(a2).write_text(self.ta.replace(f"# {self.a}\n", f"# {a2}\n"))  # O1's renamed note
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5],
                             bullets=[f"{o2}.md#b1"], evidence_tags=sorted([v, t] for v, t in ri._evidence_tags(self.ta)))
        self.vpath(o2).write_text(ri.repair_stub(o2, [self.a]))
        st = {"notes": {rel2: {"sha": "x", "status": "done", "titles": [self.a], "pos": 0, "sources": [V5],
                               "bullets": {f"{o2}.md#b1": {"text": "a", "answers": {V5: {"decision": "kept", "title": self.a}}}},
                               "unsupported_bullets": []}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        u = self.new_unit("repair", [])  # unit 1's pending unit: marked.json travels with it
        (u / "marked.json").write_text(json.dumps({"notes": [a2], "renamed": {self.a: a2}, "by": "harness"}))
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        e = json.loads((self.staging / "repair_state.json").read_text())["notes"][rel2]
        self.assertEqual(e["bullets"][f"{o2}.md#b1"]["answers"][V5]["title"], a2)  # O2's answer moved
        stub = self.vpath(o2).read_text()
        self.assertNotIn(f"[[{self.a}]]", stub)
        self.assertIn(f"[[{a2}]]", stub)
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])
        self.assertEqual(ri.repair_open_bullets(self.staging), [])


class ExtendCorrectionRefused(_Repair):
    """S2: call 1 publishes A with an Evidence time 01:15 for the 01:16 passage. Call 2 (same
    video) extends A and corrects the time to 01:16. extension_loss refuses the corrected
    version (extend-lost-evidence) and keeps the published one; b2 (carried only by the new
    version) is reopened. Any later correction of a tag in A is refused the same way: a wrong
    tag in a published repair note can never be corrected by the pipeline."""

    def test_tag_correction_is_refused(self) -> None:
        os.environ["ZR_REPAIR_MAX_BULLETS"] = "1"
        rel = self.old("Old Tag", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        wrong = self.ta.replace("@ 01:16", "@ 01:15")
        _b1, _u1, _r1, ev1 = self.run_batch(state, _rep("Old Tag", self.a, "b1", wrong, f"Old Tag.md#b1 | kept in {self.a}\n"))
        n1 = [x for x in ev1["notes"] if x["note"] == f"{PN}/{self.a}.md"][0]
        self.assertEqual(n1["action"], "published", n1)
        fixed = self.ta.replace("## Details\n\n", f"## Details\n\n- He says the floor version works too. [src: {V5} @ 01:16]\n", 1)
        _b2, _u2, _r2, ev2 = self.run_batch(state, _rep("Old Tag", self.a, "b1,b2", fixed, f"Old Tag.md#b2 | kept in {self.a}\n"))
        n2 = [x for x in ev2["notes"] if x["note"] == f"{PN}/{self.a}.md"][0]
        self.assertIn("extend-lost-evidence", n2["failure_types"], n2)
        self.assertIn("@ 01:15", self.vpath(self.a).read_text())  # the wrong tag stays published


class NeedsOperatorNeverCleared(_Repair):
    """S3: needs_operator.json is append-only (run_integrity.py:2364-2373; no code removes an
    entry). After the operator resolves the case (here: writes the stub by hand), every later
    summary still lists it, so `needs_operator` never becomes clean again."""

    def test_entry_stays_after_the_operator_fixed_it(self) -> None:
        rel = self.old("Old Op", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        b, u, res, _ = self.run_batch(state, _rep("Old Op", self.a, "b1", self.ta, f"Old Op.md#b1 | kept in {self.a}\n"),
                                      finalize=False)
        p = self.vpath("Old Op")
        p.write_text(p.read_text() + "\nUSER EDIT.\n")
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.assertTrue(ri._read_json(self.staging / "needs_operator.json", []))
        p.write_text(ri.repair_stub("Old Op", [self.a]))  # the operator resolves it
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        self.assertEqual(ri.replacement_beside_old(self.staging, self.zk), [])
        self.assertTrue(ri._read_json(self.staging / "needs_operator.json", []))  # still listed


class MergeNotInRepair(_Repair):
    """S4: the round-18 merge must be unreachable in repair units."""

    def _results(self) -> tuple[list, dict]:
        lead_old = [ln for ln in self.ta.split("\n") if "[src:" in ln][0]
        lead = "Radoslav Radev says planche lean presses take 10 seconds on the floor. [src: vid-5zALUKd7h3g @ 01:16]"
        t1 = self.ta.replace(lead_old, lead, 1).replace(f"# {self.a}\n", "# Radoslav Radev Says Lean Presses Take 10 Seconds\n")
        t2 = self.ta.replace(lead_old, lead, 1).replace(f"# {self.a}\n", "# Says Lean Presses Take 10 Seconds\n")
        res = [{"rel": f"{PN}/{t}.md", "data": x.encode(), "stub": False, "existed_before": False, "entry": {}}
               for t, x in (("Radoslav Radev Says Lean Presses Take 10 Seconds", t1), ("Says Lean Presses Take 10 Seconds", t2))]
        return res, {r["rel"]: "publish" for r in res}

    def test_ok_synth_merges_this_pair(self) -> None:  # setup control
        res, dec = self._results()
        self.assertTrue(ri.merge_duplicates(self.staging, self.zk, self.new_unit("synth", []), res, dec, "synth"))

    def test_repair_merges_this_pair(self) -> None:  # DEFECT form: FAIL = no merge in repair
        os.environ.pop("ZR_MERGE_IN_REPAIR", None)
        res, dec = self._results()
        self.assertTrue(ri.merge_duplicates(self.staging, self.zk, self.new_unit("repair", []), res, dec, "repair"))
