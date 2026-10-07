"""Probe 9 B (reviewer B, round 4): paths that bypass `_Publisher.write` or the guard.

Convention of THIS file: every test asserts the DEFECT. PASS = the defect is present.
FAIL = the defect is gone (or the setup changed). Fake LLMs only, no network."""
from __future__ import annotations

import json
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri, validate

import synth_call as sc  # noqa: E402


def _errors(t: _Repair) -> list[str]:
    errors, _w, _n = validate.check(t.zk, None, str(t.staging))
    return errors


class RefusedStubHealedByRecover(_Repair):
    """X-B1: the publisher refuses a stub (stub_check fails: an indented line under
    `## Details` before the first bullet; render drops it, old_parts keeps it). The old note
    stays beside its published target; validate flags it. The next `recover` (every `start`
    calls it) runs heal_beside WITHOUT a publisher: no guard, no stub_check, the old-format
    `repair_stub` replaces the old note. Old bullets and lines are gone from the vault, and
    validate is clean."""

    def test_refused_stub_is_replaced_by_unchecked_heal_stub(self) -> None:
        rel = self.old("Old Indent", 2)
        p = self.vpath("Old Indent")
        p.write_text(p.read_text().replace("## Details\n\n", "## Details\n\n  An indented intro line.\n"))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Indent.md | bullets: b1, b2=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Indent.md#b1 | kept in {self.a}\nOld Indent.md#b2 | kept in {self.a}\n")
        self.run_batch(state, rep)
        # after the finalize: refused, old note unchanged, validate reports it
        self.assertIn("An indented intro line.", p.read_text())
        self.assertNotIn("status: superseded", p.read_text())
        no = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertTrue(any("stub check" in x.get("why", "") for x in no), no)
        self.assertTrue(any("Old Indent" in e for e in _errors(self)))
        # the next start: recover -> heal_beside(pub=None)
        ri.recover(self.staging, self.zk)
        stub = p.read_text()
        self.assertIn("status: superseded", stub)
        self.assertNotIn("old_bullets:", stub)
        self.assertNotIn("legacy bullet 1", stub)
        self.assertNotIn("An indented intro line.", stub)
        self.assertNotIn("Legacy claim.", stub)
        self.assertEqual([e for e in _errors(self) if "Old Indent" in e], [])  # validate clean


class InPlaceRepairDropsOldText(_Repair):
    """X-B2: a reply that names the OLD title as its new note ("repaired in place") makes
    finalize publish a contract note over the old note with a plain (non-stub) write: no
    stub, no stub_check, no `old_bullets`. A bullet that the reply drops and every other old
    body line leave the vault; validate is clean."""

    def test_in_place_publish_removes_old_bullets_and_validate_is_clean(self) -> None:
        rel = self.old("Old Inp", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        inp = self.ta.replace(f"# {self.a}\n", "# Old Inp\n")
        rep = (f"=====NOTE: Old Inp | repairs: Old Inp.md | bullets: b1=====\n{inp.rstrip()}\n"
               f"=====BULLETS=====\nOld Inp.md#b1 | kept in Old Inp\n"
               f"Old Inp.md#b2 | dropped: not in this transcript\n")
        _b, _u, _res, ev = self.run_batch(state, rep)
        now = self.vpath("Old Inp").read_text()
        self.assertNotIn("status: superseded", now)   # not a stub
        self.assertNotIn("legacy bullet 1", now)       # old bullets are not in the vault
        self.assertNotIn("legacy bullet 2", now)
        self.assertNotIn("Legacy claim.", now)
        self.assertEqual([e for e in _errors(self) if "Old Inp" in e], [])  # validate clean


class InPlaceAndSplitBothPublished(_Repair):
    """X-B5: "in place and split", both versions publish. The old title now holds a new
    contract note, and replacement_beside_old() reads it as the OLD note beside its
    published sibling: validate reports "old note unchanged beside its published
    replacement" for a note that is not the old note, and needs_operator.json lists it.
    The old bullets themselves are not in the vault (no stub), as in X-B2."""

    def test_in_place_split_gives_a_false_beside_error_and_no_old_text(self) -> None:
        rel = self.old("Old Inp", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        inp = self.ta.replace(f"# {self.a}\n", "# Old Inp\n")
        rep = (f"=====NOTE: Old Inp | repairs: Old Inp.md | bullets: b1=====\n{inp.rstrip()}\n"
               f"=====NOTE: {self.b} | repairs: Old Inp.md | bullets: b2=====\n{self.tb.rstrip()}\n"
               f"=====BULLETS=====\nOld Inp.md#b1 | kept in Old Inp\nOld Inp.md#b2 | kept in {self.b}\n")
        self.run_batch(state, rep)
        now = self.vpath("Old Inp").read_text()
        self.assertTrue(self.vpath(self.b).is_file())
        self.assertNotIn("status: superseded", now)
        self.assertNotIn("legacy bullet 1", now)
        self.assertNotIn("legacy bullet 2", now)
        errs = [e for e in _errors(self) if "Old Inp" in e]
        self.assertTrue(any("unchanged beside its published replacement" in e for e in errs), errs)
