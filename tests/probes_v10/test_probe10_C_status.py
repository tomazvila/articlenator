"""Probe 10 C (reviewer C, fifth review, frozen-v10): `synth_call.py repair-status`
(synth_call.py:340-372, read-only) against the files, and the finalize kill path.

CONVENTION OF THIS FILE: `test_defect_*` asserts the DEFECT (PASS = the defect is present;
FAIL = fixed). `test_ok_*` asserts CORRECT behavior (PASS = no defect; an attack that found
nothing). Fake LLMs only, no network."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ZR, ri

import synth_call as sc  # noqa: E402


def _tree(*roots: Path) -> dict[str, str]:
    out = {}
    for r in roots:
        for p in sorted(r.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest() + f"/{st.st_mtime_ns}"
    return out


class _S(_Repair):
    def stubbed(self, title: str = "Old S") -> tuple[str, Path]:
        rel = self.old(title, 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: {title}.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\n{title}.md#b1 | kept in {self.a}\n")
        self.run_batch(state, rep)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        self.assertIn("old_bullets:", self.vpath(title).read_text())
        return rel, state

    def rows(self) -> dict[str, dict]:
        return {r["note"]: r for r in sc.repair_status(self.staging, self.zk)}

    def cli(self) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ZR / "synth_call.py"), "repair-status", "--staging",
                               str(self.staging), "--vault", str(self.zk)], capture_output=True, text=True,
                              env=dict(os.environ))


class ReadOnly(_S):
    def test_ok_repair_status_writes_nothing(self) -> None:
        rel, _ = self.stubbed()
        before = _tree(self.staging, self.zk)
        r = self.cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Old S", r.stdout)
        self.assertEqual(_tree(self.staging, self.zk), before)

    def test_ok_good_stub_row(self) -> None:
        rel, _ = self.stubbed()
        row = self.rows()[rel]
        self.assertEqual((row["state"], row["vault"], row["open_bullets"], row["unlisted"]), ("done", "stub", 0, False))


class UserEditedStub(_S):
    """Z-C?: the vault form is decided by one regex, `^old_bullets:` in the frontmatter
    (synth_call.py:360). A stub that the user changed after the pipeline wrote it (the old
    text block deleted, a pointer line changed) is still 'stub', with no flag; the registry
    (pipeline_writes.json) and stub_check, which would show the change, are not read. The
    operator who uses repair-status as the end-of-run list sees nothing."""

    def test_defect_edited_stub_reported_as_plain_stub(self) -> None:
        rel, _ = self.stubbed()
        p = self.zk / rel
        txt = p.read_text()
        cut = "\n".join(ln for ln in txt.splitlines() if "legacy bullet" not in ln) + "\n"
        self.assertNotEqual(cut, txt)
        p.write_text(cut)  # the old text is gone from the vault
        self.assertNotEqual(ri._sha256(p), json.loads((self.staging / ri.PIPELINE_WRITES).read_text())[rel])
        row = self.rows()[rel]
        self.assertEqual(row["vault"], "stub")
        self.assertFalse(row["unlisted"])
        self.assertEqual(row["operator"], [])
        self.assertNotIn("UNLISTED", self.cli().stdout)


class RenamedOrDeletedOldNote(_S):
    """A user renames (or deletes) the old note before the repair ends. repair-status says
    vault 'missing'. `unlisted` is computed only for 'unchanged', so the row has no flag
    and no operator reason: the old text is out of the zettelkasten folder with no mark."""

    def test_defect_missing_old_note_not_flagged(self) -> None:
        rel = self.old("Old R", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        (self.zk / rel).rename(self.zk / PN / "Old R renamed.md")
        st = json.loads(state.read_text())
        st["notes"][rel]["status"] = "done"  # e.g. it closed before the rename
        state.write_text(json.dumps(st))
        row = self.rows()[rel]
        self.assertEqual(row["vault"], "missing")
        self.assertFalse(row["unlisted"])
        self.assertEqual(row["operator"], [])


class UnsupportedNoteNotListed(_S):
    """A note of the repair plan with no transcript (repair-groups `unsupported`) is marked
    `verification: unsupported` by the harness (mark-unsupported). It never enters
    repair_state.json, so repair-status (which reads only repair_state.json) has no row for
    it, although README says 'per old note of the repair plan'. The operator does not see
    that its text was never checked."""

    def test_defect_unsupported_note_has_no_row(self) -> None:
        rel = self.old("Old U", 1)
        p = self.zk / rel
        p.write_text(p.read_text().replace(f"  - id: {self.ids[0]}\n", "  - id: vid-NOTRANSCR1\n"))
        state = sc.state_path(self.staging)
        plan = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertEqual(plan["unsupported"], [rel], plan)
        u = self.new_unit("repair", [])
        ri.mark_unsupported(u, self.zk, rel, "source transcript missing")
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.assertIn("verification: unsupported", p.read_text())
        self.assertNotIn(rel, self.rows())
        self.assertNotIn("Old U", self.cli().stdout)


class KillBetweenTargetAndStubThenNextFinalize(_S):
    """README (round 22): 'The next normal finalize writes the proper stub through the
    publisher when the write guard allows it.' Kill between the target write and the stub
    write; recover (start). Found nothing: recover's recompute writes the checked stub."""

    def test_ok_next_finalize_writes_the_stub(self) -> None:
        rel = self.old("Old K", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old K.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld K.md#b1 | kept in {self.a}\n")
        b, u, res, _ = self.run_batch(state, rep, finalize=False)
        real = ri._Publisher.write
        n = {"n": 0}

        def dying(pub, rel_, text, why):
            n["n"] += 1
            if n["n"] == 2:
                raise SystemExit("killed")
            return real(pub, rel_, text, why)
        with mock.patch.object(ri._Publisher, "write", dying):
            with self.assertRaises(SystemExit):
                ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.kill_owner(u)
        ri.recover(self.staging, self.zk)
        # recover() writes the stub through the publisher (recompute_repair_stubs), no beside
        self.assertIn("old_bullets:", (self.zk / rel).read_text())
        self.assertEqual(ri.replacement_beside_old(self.staging, self.zk), [])
        self.assertEqual(sc.repair_status(self.staging, self.zk)[0]["vault"], "stub")


class QuarantinedTargetAndRefusedStub(_S):
    """Controls: a target that the review rejects (quarantined), and a stub write refused by
    the guard (a user edit of the old note after planning)."""

    def test_ok_quarantined_target_row(self) -> None:
        rel = self.old("Old Q", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Q.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Q.md#b1 | kept in {self.a}\n")
        b, u, res, _ = self.run_batch(state, rep, review=False, finalize=False)
        sha = hashlib.sha256((u / "out" / PN / f"{self.a}.md").read_text().encode()).hexdigest()
        os.environ["REJECT_SHAS"] = json.dumps([sha])
        os.environ["ZR_REVIEW_REPAIR"] = "0"
        self.addCleanup(os.environ.pop, "REJECT_SHAS", None)
        import review_call as rc
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        while sc.next_batch(self.staging, self.zk, state):
            pass
        row = self.rows()[rel]
        self.assertEqual(row["vault"], "unchanged")
        self.assertEqual(row["open_bullets"], 1, row)
        self.assertTrue(row["operator"] or row["unlisted"], row)

    def test_ok_refused_stub_row(self) -> None:
        rel = self.old("Old F", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old F.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld F.md#b1 | kept in {self.a}\n")
        b, u, res, _ = self.run_batch(state, rep, finalize=False)
        p = self.zk / rel
        p.write_text(p.read_text() + "\nUSER LINE\n")
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.assertIn("USER LINE", p.read_text())
        row = self.rows()[rel]
        self.assertEqual(row["vault"], "changed (not a pipeline stub)")
        self.assertTrue(row["operator"], row)


class PendingReplacementFlaggedUnlisted(_S):
    """The replacement of an old note waits in pending-review (no verdict yet; the next
    pass's pending step takes it). The old note is unchanged on purpose (the stub follows the
    publish). repair-status says state 'done', vault 'unchanged', UNLISTED; README tells the
    operator to 'give each such note a decision'. The pending replacement is not read
    (`waiting` is used only for bullet statuses), so a normal wait is reported as operator
    work."""

    def test_defect_waiting_replacement_reported_unlisted(self) -> None:
        rel = self.old("Old P", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old P.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld P.md#b1 | kept in {self.a}\n")
        b, u, res, ev = self.run_batch(state, rep, review=False)
        self.assertIn((f"{PN}/{self.a}.md", "pending-review"), [(n["note"], n["action"]) for n in ev["notes"]])
        sc.next_batch(self.staging, self.zk, state)
        row = self.rows()[rel]
        self.assertEqual((row["state"], row["vault"], row["unlisted"]), ("done", "unchanged", True), row)
        self.assertIn("UNLISTED", self.cli().stdout)


class EditedStubNotOperatorWork(_S):
    """The driver's exit 7 test (`operator-work`, run_integrity.py:3015) uses only
    validate.two_state_errors, which accepts any changed plan note that has `old_bullets`
    (validate.py:478-486) and never runs stub_check. `validate.py --staging` runs
    stub_errors too (validate.py:444) and reports the stub whose old text the user deleted.
    So the driver exits 0 ('no operator work is known') while validate reports an ERROR."""

    def test_defect_operator_work_misses_a_stub_error_that_validate_reports(self) -> None:
        import validate
        rel, _ = self.stubbed()
        p = self.zk / rel
        p.write_text("\n".join(ln for ln in p.read_text().splitlines() if "legacy bullet" not in ln) + "\n")
        errs = validate.stub_errors(self.staging, self.zk, [p])
        self.assertTrue(errs)                                         # validate --staging: ERROR
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])
        self.assertEqual(ri.operator_work(self.staging, self.zk), [])  # driver: exit 0
