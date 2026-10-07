"""Probe 10 A (fifth review, frozen-v10): a quarantined (review-invalid) repair target, the
rename chain, the final `contradicts` drop, and "never cut" review passages.

CONVENTION OF THIS FILE (mixed, by prefix):
- `test_ok_*`     asserts the CORRECT behaviour (PASS = correct).
- `test_defect_*` asserts the DEFECT (PASS = the defect is present).
Fake LLMs and fake workers only; no network."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from tests.probes_v9.test_probe9_D_attacks import _Drv, _rep
from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_run_integrity_r13 import work
from tests.unit.test_synth_single import V5, _Single

import synth_call as sc  # noqa: E402

V2 = "vid-OTHERVIDEO1"


def unusable_answer(user: str) -> str:
    sk = json.loads(user.split("Reply with this JSON object only:\n", 1)[1])
    for it in sk["items"]:
        it.update(verdict="transcript-missing", problem="cut", transcript_text="")
    sk["scope_complete"] = True
    return json.dumps(sk)


class QuarantinedTargetBullets(_Drv):
    """A repair target whose reviewer answers transcript-missing every time: after the
    second unusable reply it is quarantined `review-invalid`. Its old note's bullet."""

    def test_ok_bullet_of_review_invalid_target_is_open_listed_and_exit_7(self) -> None:
        rel = self._old("Old Inv")
        before = self.vpath("Old Inv").read_bytes()
        rep = _rep("Old Inv", self.a, "b1", self.ta, f"Old Inv.md#b1 | kept in {self.a}\n")
        codes = []
        for _ in range(10):
            r = self.run_rl(rel)
            codes.append(r.returncode)
            if r.returncode not in (5, 7) or (r.returncode == 7 and not sc.exchange_list(self.xdir, only_open=True)
                                               and not list(self.staging.glob("pending-review/*--*/meta.json"))):
                break
            if os.environ.get("PROBE_DEBUG"):
                print("PASS", r.returncode, [(x["kind"], x["key"][:6], x["unit"][-30:]) for x in sc.exchange_list(self.xdir, only_open=True)],
                      json.loads((self.staging / "repair_state.json").read_text())["notes"][rel]["status"],
                      r.stderr[-300:].replace("\n", " | "))
            work(self.xdir, lambda q: rep if q["kind"] == "repair" else (
                unusable_answer(q["user"]) if q["kind"] == "review" else None),
                worker=lambda q: "w-rep" if q["kind"] == "repair" else "w-rev")
        e = json.loads((self.staging / "repair_state.json").read_text())["notes"][rel]
        quar = [json.loads(p.read_text()).get("failure_types") for p in self.staging.glob("quarantine/**/*.reason.json")]
        rows = sc.repair_status(self.staging, self.zk)
        if os.environ.get("PROBE_DEBUG"):
            print(codes, e, quar, self.ops(), rows, r.stderr[-1500:])
        self.assertTrue(any("review-invalid" in (q or []) for q in quar), (codes, quar))
        self.assertEqual(self.vpath("Old Inv").read_bytes(), before)
        self.assertFalse(self.vpath(self.a).is_file())
        self.assertEqual(e["status"], "needs-repair", e)
        self.assertTrue([o for o in self.ops() if o.get("note") == rel])
        self.assertEqual(rows[0]["open_bullets"], 1, rows)
        self.assertEqual(codes[-1], 7, codes)


class RenameChain(_Single):
    def _marked(self, run: str, unit: str, renamed: dict) -> None:
        d = self.staging / "runs" / run / "units" / unit
        d.mkdir(parents=True, exist_ok=True)
        (d / "marked.json").write_text(json.dumps({"renamed": renamed}))

    def test_defect_rename_back_loop_resolves_to_a_title_that_is_gone(self) -> None:
        """run_integrity.py _rename_chain/load_renames: the renames of every unit are merged
        into ONE map keyed by title, without time order. A -> B (fix 1) then B -> A (fix 2):
        an answer that names B (written between the two fixes) resolves to B, which no
        longer exists; the true final title is A."""
        self._marked("20261001T000000Z-1", "u1", {"Title A": "Title B"})
        self._marked("20261002T000000Z-1", "u2", {"Title B": "Title A"})
        ri.load_renames(self.staging)
        self.assertEqual(ri._rename_chain(self.zk, "Title A"), "Title A")
        self.assertEqual(ri._rename_chain(self.zk, "Title B"), "Title B")  # wrong: B was renamed to A

    def test_defect_reused_title_resolves_to_the_old_rename(self) -> None:
        """A note T was renamed T -> T2 in an early unit. Later a NEW note is published
        under the free title T. A ledger answer `kept in T` (the new note) now resolves to
        T2, a different note: the stub status points to the wrong note."""
        self._marked("20261001T000000Z-1", "u1", {"Reused Title": "Renamed Title"})
        ri.load_renames(self.staging)
        self.assertEqual(ri._rename_chain(self.zk, "Reused Title"), "Renamed Title")

    def test_ok_long_chain_ends(self) -> None:
        self._marked("20261001T000000Z-1", "u1", {f"T{i}": f"T{i + 1}" for i in range(200)} | {"T200": "T0"})
        ri.load_renames(self.staging)
        self.assertIn(ri._rename_chain(self.zk, "T5"), {f"T{i}" for i in range(201)})


class ContradictsIsFinalAndSilent(_Single):
    """A `dropped: contradicts the transcript` answer of ONE video ends the bullet: the
    other planned videos are not asked (_pending_bullets), classify_bullets counts it
    `unsupported`, and nothing reports it to the operator: no needs_operator entry, 0 open
    bullets in repair-status, no operator_work line (so exit 0, not 7). Only the stub line
    `dropped: ...` and the README step 9 (read the stubs) show it. No drop check is made
    for a contradicts drop (synth_call.py ~3115 checks only `not in this transcript`)."""

    def test_defect_contradicts_from_one_video_is_not_reported(self) -> None:
        rel = f"{PN}/Old Contra.md"
        self.vpath("Old Contra").write_text("---\ntype: permanent note\n---\n\n# Old Contra\n\n## Details\n\n"
                                             "- Planche lean presses can be done on the floor without parallettes.\n"
                                             "- Four sets of planche lean presses after the workout are an option.\n")
        self.vpath(self.a).write_text(self.ta)
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md")
        e = {"sha": ri._sha256(self.vpath("Old Contra")), "status": "open", "pos": 0, "kind": "known",
             "sources": [V5, V2], "titles": [self.a],
             "bullets": {"Old Contra.md#b1": {"text": "Planche lean presses can be done on the floor without parallettes.",
                                             "answers": {V5: {"decision": "kept", "title": self.a}}},
                         "Old Contra.md#b2": {"text": "Four sets of planche lean presses after the workout are an option.",
                                             "answers": {V5: {"decision": "dropped",
                                                              "reason": "contradicts the transcript"}}}}}
        (self.staging / "repair_state.json").write_text(json.dumps({"notes": {rel: e}, "batches": {}}))
        self.assertEqual(sc._pending_bullets(e, V2), [])            # V2 is never asked
        sc._close(self.staging, rel, e)
        st = {"notes": {rel: e}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        self.assertEqual(e["unsupported_bullets"], ["Old Contra.md#b2"])
        self.assertEqual(e["status"], "done")
        s, _p, _w = ri._bullet_status(self.zk, e, "Old Contra.md#b2", set())
        self.assertTrue(s.startswith("dropped: contradicts"), s)
        self.assertFalse((self.staging / "needs_operator.json").exists() and
                         json.loads((self.staging / "needs_operator.json").read_text()))
        rows = sc.repair_status(self.staging, self.zk)
        self.assertEqual(rows[0]["open_bullets"], 0)
        self.assertFalse([x for x in ri.operator_work(self.staging, None) if "Old Contra" in x])


class PassagesNeverCut(_Single):
    def test_ok_every_marked_line_survives_and_defect_size_is_unbounded(self) -> None:
        """No marked passage is dropped (ok). Note: the result is not bounded by max_chars
        at all any more (size is recorded, not judged here)."""
        rel = f"{PN}/{self.a}.md"
        self.vpath(self.a).write_text(self.ta)
        full = ri.passages(self.staging, self.zk, rel, max_chars=10 ** 6)
        small = ri.passages(self.staging, self.zk, rel, max_chars=50)
        want = [ln for ln in full.splitlines() if ln.startswith((">>", "- "))]
        self.assertTrue(want)
        for ln in want:
            self.assertIn(ln, small)
        self.assertGreater(len(small), 50)


class PreflightWithoutGit(_Single):
    def test_defect_vault_without_git_passes_the_git_check(self) -> None:
        """synth_call.repair_preflight: `git status --porcelain` fails outside a git work
        tree (returncode 128), and only a NON-EMPTY output with returncode 0 is a finding.
        A vault that is not a git repository (no snapshot and no restore possible, README
        checklist steps 2 and 10) passes the preflight."""
        r = __import__("subprocess").run(["git", "-C", str(self.zk), "rev-parse", "--is-inside-work-tree"],
                                         capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "the fixture vault is in git; probe not applicable")
        found = sc.repair_preflight(self.staging, self.zk)
        self.assertFalse([f for f in found if f.startswith("git")], found)


class AgentModeInPlace(_Single):
    """X2 rest: the agent-mode repair path (repair_loop.sh with ZR_SYNTH_MODE != single, or
    ZR_ALLOW_OTHER_SOURCES=1) still runs. The title-equals-old refusal lives only in the
    single-mode apply_repair. A repair unit whose shadow holds a contract note at the OLD
    path (the agent ignored "never write_file at the old path")."""

    def test_defect_agent_in_place_write_replaces_old_note_without_stub(self) -> None:
        import review_call as rc
        import validate
        old = "Old Ag"
        text = ("---\ntype: permanent note\ncreated: 2026-09-12\nverification: unverified\n---\n\n# Old Ag\n\n"
                "Lead.\n\n## Details\n\n- legacy bullet 1\n- legacy bullet 2\n\n## Connected Ideas\n\n- [[Home]] — home.\n")
        self.vpath(old).write_text(text)
        rel = f"{PN}/{old}.md"
        (self.staging / "repair_state.json").write_text(json.dumps(
            {"notes": {rel: {"sha": ri._sha256(self.vpath(old)), "status": "open", "sources": [V5], "pos": 0,
                             "kind": "known", "bullets": {}}}, "batches": {}}))
        u = self.new_unit("repair", [])
        inp = self.ta.replace(f"# {self.a}\n", f"# {old}\n")
        (u / "out" / PN).mkdir(parents=True, exist_ok=True)
        (u / "out" / PN / f"{old}.md").write_text(inp)
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        now = self.vpath(old).read_text()
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        if os.environ.get("PROBE_DEBUG"):
            print([(n["note"], n.get("action"), n.get("failure_types")) for n in ev["notes"]], errors)
        self.assertNotIn("legacy bullet 1", now)       # the old text left the vault
        self.assertNotIn("old_bullets", now)           # no stub
        self.assertTrue([e for e in errors if old in e], errors)  # (validate does see it)


class NoTranscriptNoteSingleMode(_Drv):
    def test_defect_no_transcript_old_note_is_rewritten_in_place_with_status_lines(self) -> None:
        """mark-unsupported (single mode, a note whose source has no transcript) rewrites the
        OLD note in place: `verification: unsupported` + `unsupported_reason`. The body is
        kept, but this is a third state beside "unchanged" and "stub" (contract section 16
        says two states only); validate then reports `unsupported-outside-repair` for it."""
        from tests.unit.test_run_integrity import OLD_NOTE
        import validate
        text = OLD_NOTE.format(t="Old Nolit").replace("created:", "sources:\n  - id: vid-NOTRANSCR1\n    speaker: X\ncreated:")
        self.vpath("Old Nolit").write_text(text)
        r = self.run_rl(f"{PN}/Old Nolit.md")
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        if os.environ.get("PROBE_DEBUG"):
            print(r.returncode, self.vpath("Old Nolit").read_text(), errors, self.ops(), r.stderr[-800:])
        now = self.vpath("Old Nolit").read_text()
        self.assertNotEqual(now, text)
        self.assertIn("verification: unsupported", now)
        self.assertNotIn("old_bullets", now)
