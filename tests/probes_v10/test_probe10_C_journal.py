"""Probe 10 C (reviewer C, fifth review, frozen-v10): the write journal
(`pipeline_writes.journal.jsonl`) and `reconcile_pipeline_writes` (run_integrity.py:1357, the
X10 fix), with kills at each step of `_Publisher.write`.

CONVENTION OF THIS FILE: a test named `test_defect_*` asserts the DEFECT (PASS = the defect
is present; FAIL = fixed). A test named `test_ok_*` asserts CORRECT behavior (PASS = no
defect; an attack that found nothing). Fake LLMs only, no network."""
from __future__ import annotations

import json
import shutil
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri


def _journal(stg):
    p = stg / ri.PIPELINE_JOURNAL
    return [json.loads(x) for x in p.read_text().splitlines()] if p.is_file() else []


def _reg(stg):
    return json.loads((stg / ri.PIPELINE_WRITES).read_text()) if (stg / ri.PIPELINE_WRITES).is_file() else {}


class _J(_Repair):
    def pub(self):
        return ri._Publisher(self.staging, self.run_dir.name, self.new_unit("repair", []), self.zk, {})

    def more(self, text: str, line: str) -> str:
        out = text.replace("\n## Details\n", f"\n## Details\n\n- {line}\n", 1)
        self.assertNotEqual(out, text)
        return out


class KillAfterReplaceThenUserEdit(_J):
    """Z-C1. A kill after os.replace and before record_pipeline_write (the window that X10
    closes). The user then edits the note before the next run (README checklist step 3 bans
    this only during the run; a crash ends the run). reconcile sees file sha != journal sha
    and drops the entry; the registry keeps NO entry for the note. guard_vault_write then has
    no `last` sha, so the next pipeline write of the note overwrites the user edit."""

    def test_defect_published_target_user_edit_overwritten(self) -> None:
        rel = f"{PN}/{self.a}.md"
        p1 = self.pub()
        with mock.patch.object(ri, "record_pipeline_write", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                p1.write(rel, self.ta, "publish target")
        self.assertEqual(self.vpath(self.a).read_text(), self.ta)          # the write reached the vault
        self.assertNotIn(rel, _reg(self.staging))                         # no registry entry
        user = self.ta.replace("- [[Home]] — home map.", "- [[Home]] — home map. MY OWN NOTE")
        self.vpath(self.a).write_text(user)                               # user edit between runs
        self.kill_owner(p1.unit_dir)
        ri.recover(self.staging, self.zk)                                  # = run_integrity start
        self.assertNotIn(rel, _reg(self.staging))                         # reconcile skipped it
        self.assertFalse((self.staging / ri.PIPELINE_JOURNAL).exists())   # and deleted the journal
        p3 = self.pub()
        ok = p3.write(rel, self.more(self.ta, "a later extend"), "extend")
        self.assertTrue(ok, p3.refused)                                    # NOT refused ...
        self.assertNotIn("MY OWN NOTE", self.vpath(self.a).read_text())   # ... the user edit is gone

    def test_ok_without_the_kill_the_same_user_edit_is_refused(self) -> None:
        rel = f"{PN}/{self.a}.md"
        self.assertTrue(self.pub().write(rel, self.ta, "publish target"))
        self.vpath(self.a).write_text(self.ta.replace("- [[Home]] — home map.", "- [[Home]] — MY OWN NOTE"))
        ri.recover(self.staging, self.zk)
        p3 = self.pub()
        self.assertFalse(p3.write(rel, self.more(self.ta, "a later extend"), "extend"))
        self.assertIn("user edit", p3.refused[0]["why"])


class StaleJournalAdoptsUserRevert(_J):
    """Z-C2. The journal is emptied only at the next `start` (recover). After a NORMAL run it
    still holds every write of the run. Two writes of one note in one run (sha A, then sha B;
    for example a stub that a later unit updates, or an extended target). The user reverts the
    note to version A (e.g. `git checkout <interim commit> -- note`, the restore of README
    checklist step 9 at an interim commit). At the next start reconcile finds file sha == A
    (a journal entry) and registry B != A, and records A: the user's revert becomes "the
    pipeline's last write", and the next pipeline write overwrites it without a refusal."""

    def test_defect_reverted_note_is_recorded_as_pipeline_write(self) -> None:
        rel = f"{PN}/{self.a}.md"
        text_b = self.more(self.ta, "version B bullet")
        self.assertTrue(self.pub().write(rel, self.ta, "A"))
        self.assertTrue(self.pub().write(rel, text_b, "B"))
        self.assertEqual(len(_journal(self.staging)), 2)                  # the journal is not closed per write
        self.vpath(self.a).write_text(self.ta)                            # the user goes back to version A
        p_chk = self.pub()                                                # control: before start, refused
        self.assertFalse(p_chk.write(rel, self.more(self.ta, "C"), "C"))
        ri.recover(self.staging, self.zk)                                  # next start
        self.assertEqual(_reg(self.staging)[rel], ri._sha256(self.vpath(self.a)))  # A adopted
        p3 = self.pub()
        self.assertTrue(p3.write(rel, self.more(self.ta, "version C bullet"), "C"), p3.refused)


class KillBetweenCopyAndReplace(_J):
    """Z-C3. A kill after `shutil.copyfile(tmp, staged)` and before `os.replace`: the staged
    file `.<name>.zr-publish.tmp` stays IN THE VAULT folder. Nothing removes it (no code
    names the pattern except the writers). The vault git state is dirty, so the next
    repair_loop.sh preflight refuses the run (exit 1, 'uncommitted changes')."""

    def test_defect_stray_temp_file_stays_in_the_vault(self) -> None:
        rel = f"{PN}/{self.a}.md"
        p1 = self.pub()
        with mock.patch.object(ri.os, "replace", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                p1.write(rel, self.ta, "publish")
        self.kill_owner(p1.unit_dir)
        ri.recover(self.staging, self.zk)
        stray = list((self.zk / PN).glob(".*.zr-publish.tmp"))
        self.assertEqual([p.name for p in stray], [f".{self.a}.md.zr-publish.tmp"])
        self.assertFalse(self.vpath(self.a).exists())
        self.assertTrue(list((self.staging / ".zr-publish-tmp").glob("*.tmp")))  # staging temp too


class KillAtEachStepNoLoss(_J):
    """Controls: kills at the other steps (before the journal entry, after the journal entry,
    after the temp write, after os.replace with no user edit, during reconcile) and a journal
    entry for a deleted file. Each ends with a consistent registry and no false refusal."""

    def _kill(self, target, rel, text):
        p = self.pub()
        with mock.patch.object(*target, side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                p.write(rel, text, "w")
        self.kill_owner(p.unit_dir)
        ri.recover(self.staging, self.zk)
        return p

    def test_ok_kill_at_journal_append(self) -> None:
        rel = f"{PN}/{self.a}.md"
        self._kill((ri, "_append"), rel, self.ta)
        self.assertFalse(self.vpath(self.a).exists())
        self.assertTrue(self.pub().write(rel, self.ta, "again"))

    def test_ok_kill_after_replace_no_edit_then_rewrite(self) -> None:
        rel = f"{PN}/{self.a}.md"
        self._kill((ri, "record_pipeline_write"), rel, self.ta)
        self.assertEqual(_reg(self.staging).get(rel), ri._sha256(self.vpath(self.a)))
        self.assertTrue(self.pub().write(rel, self.more(self.ta, "x"), "again"))

    def test_ok_journal_entry_for_deleted_file(self) -> None:
        rel = f"{PN}/{self.a}.md"
        p = self.pub()
        with mock.patch.object(ri, "record_pipeline_write", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                p.write(rel, self.ta, "w")
        self.vpath(self.a).unlink()
        self.kill_owner(p.unit_dir)
        ri.recover(self.staging, self.zk)
        self.assertNotIn(rel, _reg(self.staging))
        self.assertFalse((self.staging / ri.PIPELINE_JOURNAL).exists())

    def test_ok_kill_during_reconcile_is_idempotent(self) -> None:
        rel = f"{PN}/{self.a}.md"
        p = self.pub()
        with mock.patch.object(ri, "record_pipeline_write", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                p.write(rel, self.ta, "w")
        real = ri._write_json
        with mock.patch.object(ri, "_write_json", side_effect=SystemExit("killed in reconcile")):
            with self.assertRaises(SystemExit):
                ri.reconcile_pipeline_writes(self.staging, self.zk)
        self.assertTrue((self.staging / ri.PIPELINE_JOURNAL).exists())   # journal kept
        self.assertIs(ri._write_json, real)
        self.kill_owner(p.unit_dir)
        ri.recover(self.staging, self.zk)
        self.assertEqual(_reg(self.staging).get(rel), ri._sha256(self.vpath(self.a)))


class TwoUnitsOneJournal(_J):
    """Two units, both killed after os.replace of different notes; one note is then edited
    by the user. The other gets its registry entry; the edited one gets none (Z-C1 again)."""

    def test_defect_second_unit_note_unprotected(self) -> None:
        ra, rb = f"{PN}/{self.a}.md", f"{PN}/{self.b}.md"
        for rel, text in ((ra, self.ta), (rb, self.tb)):
            p = self.pub()
            with mock.patch.object(ri, "record_pipeline_write", side_effect=SystemExit("killed")):
                with self.assertRaises(SystemExit):
                    p.write(rel, text, "w")
            self.kill_owner(p.unit_dir)
        self.vpath(self.b).write_text(self.tb + "\nUSER LINE\n")
        ri.recover(self.staging, self.zk)
        reg = _reg(self.staging)
        self.assertIn(ra, reg)
        self.assertNotIn(rb, reg)
        p3 = self.pub()
        self.assertTrue(p3.write(rb, self.more(self.tb, "pipeline extend"), "extend"), p3.refused)
        self.assertNotIn("USER LINE", self.vpath(self.b).read_text())
