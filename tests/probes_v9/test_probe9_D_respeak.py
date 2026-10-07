"""Reviewer D, round 4 (frozen-v9): `respeak` and `relink` against the round-21 vault-write
rules (`_Publisher.write`, `guard_vault_write`, `pipeline_writes.json`, `stub_check`).

CONVENTION OF THIS FILE: a `test_defect_*` asserts the DEFECT (PASS = the defect is
present, FAIL = the attack found nothing). A `test_ok_*` is a control and asserts the
CORRECT behavior (PASS = correct). Fake LLMs only, no network."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from pathlib import Path

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ZR, ri, validate

import stub_check  # noqa: E402
import synth_call as sc  # noqa: E402
from tests.unit.test_synth_single import V5

U = "Unresolved Speaker In Ch Video Says Floor Presses Need No Parallettes"


def _rep(old: str, title: str, bullets: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bullets}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class _Respeak(_Repair):
    def unresolved_note(self) -> str:
        """U: a pipeline-published speaker-unresolved note of V5, written by the publisher
        (so pipeline_writes.json holds its sha)."""
        t = self.ta.replace(f"# {self.a}\n", f"# {U}\n").replace(
            "verification:", "speaker_status: unresolved\nverification:", 1)
        t = re.sub(r'(?m)^(\s+speaker:\s*).*$', r'\1"unresolved speaker, Ch video"', t, count=1)
        rel = f"{PN}/{U}.md"
        pub = ri._Publisher(self.staging, self.run_dir.name, self.new_unit("synth", [V5]), self.zk, {})
        self.assertTrue(pub.write(rel, t, "publish (test)"))
        ri.provenance.record(self.staging, "note-published", note=rel, sources=[V5], synth_worker=["w1"])
        return rel

    def respeak(self) -> list[dict]:
        out = sc.respeak(self.staging, self.zk, V5, "Sasha", apply=True, force_name=True)
        self.assertFalse([r for r in out if r.get("problem")], out)
        return out

    def pipeline_sha(self, rel: str) -> str | None:
        return (json.loads((self.staging / ri.PIPELINE_WRITES).read_text())).get(rel)

    def stub_errors(self) -> list[str]:
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        return [e for e in errors if "stub" in e.lower()]

    def ops(self) -> list[dict]:
        p = self.staging / "needs_operator.json"
        return json.loads(p.read_text()) if p.exists() else []


class RespeakBreaksStub(_Respeak):
    """The old note O carried a link `[[U]]` (a backlink that an earlier run wrote into it).
    O is repaired; its pipeline stub keeps O's old text verbatim under `## Old text`,
    including `> - [[U]]`. respeak U rewrites `[[U` in EVERY note that holds it
    (synth_call.py:3492-3500, rewrite_links 3550), the stub too, with no publisher, no
    guard and no stub_check."""

    def _stub(self) -> str:
        u_rel = self.unresolved_note()
        rel = self.old("Old Two", 2)
        p = self.vpath("Old Two")
        p.write_text(p.read_text().replace("- [[Other]]", f"- [[Other]]\n- [[{U}]] — backlink"))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.run_batch(state, _rep("Old Two", self.a, "b1", self.ta,
                                   f"Old Two.md#b1 | kept in {self.a}\nOld Two.md#b2 | dropped: not in this transcript\n"))
        sc.next_batch(self.staging, self.zk, state)
        stub = p.read_text()
        assert stub_check.CLAIMS_HEADING in stub, stub[:400]
        assert f"> - [[{U}]] — backlink" in stub
        self.assertEqual(self.stub_errors(), [])  # control: the stub is clean before respeak
        self.u_rel = u_rel
        return rel

    def test_defect_validate_stub_check_fails_after_respeak(self) -> None:
        rel = self._stub()
        self.respeak()
        stub = self.vpath("Old Two").read_text()
        self.assertNotIn(f"[[{U}]]", stub)  # the OLD text inside the stub was rewritten
        errs = self.stub_errors()
        self.assertTrue([e for e in errs if "Old Two" in e], errs)  # validate fails, and stays failing

    def test_defect_later_stub_update_refused_as_user_edit(self) -> None:
        rel = self._stub()
        self.respeak()
        # a later answer settles b2 in another published note: the stub must be rewritten
        self.vpath("Later Target").write_text(self.tb.replace(f"# {self.b}\n", "# Later Target\n"))
        st = json.loads((self.staging / "repair_state.json").read_text())
        st["notes"][rel]["bullets"]["Old Two.md#b2"]["answers"] = {"v2": {"decision": "kept", "title": "Later Target"}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        before = self.vpath("Old Two").read_text()
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        self.assertEqual(self.vpath("Old Two").read_text(), before)  # never updated again
        self.assertTrue([o for o in self.ops() if o["note"] == rel and "user edit" in o["why"]], self.ops())


class RespeakLinkerRefusedLater(_Respeak):
    def test_defect_published_linker_is_frozen_as_user_edit(self) -> None:
        """A published target A links its sibling U (Connected Ideas). respeak rewrites A
        directly (no publisher): pipeline_writes.json keeps A's old sha. Every later
        pipeline write of A (a repair extension, a backlink, a needs-repair mark) is refused
        as 'edited since the pipeline's last write (user edit?)'."""
        self.unresolved_note()
        a_rel = f"{PN}/{self.a}.md"
        pub = ri._Publisher(self.staging, self.run_dir.name, self.new_unit("synth", [V5]), self.zk, {})
        self.assertTrue(pub.write(a_rel, self.ta.replace("- [[Home]] — home map.", f"- [[Home]] — home map.\n- [[{U}]] — sibling."), "publish"))
        self.respeak()
        self.assertIn("[[Sasha", self.vpath(self.a).read_text())
        self.assertNotEqual(ri._sha256(self.vpath(self.a)), self.pipeline_sha(a_rel))
        pub2 = ri._Publisher(self.staging, self.run_dir.name, self.new_unit("repair", []), self.zk, {})
        self.assertFalse(pub2.write(a_rel, self.vpath(self.a).read_text() + "\nextension\n", "an extension"))
        self.assertTrue([o for o in self.ops() if o["note"] == a_rel and "user edit" in o["why"]])


class RespeakRenamedNoteUnprotected(_Respeak):
    def test_ok_control_user_edit_of_pipeline_note_is_refused(self) -> None:
        u_rel = self.unresolved_note()
        p = self.zk / u_rel
        p.write_text(p.read_text() + "\nUSER LINE\n")
        pub = ri._Publisher(self.staging, self.run_dir.name, self.new_unit("repair", []), self.zk, {})
        self.assertFalse(pub.write(u_rel, self.ta, "an extension"))

    def test_defect_user_edit_of_renamed_note_is_overwritten(self) -> None:
        """respeak writes the renamed note with os.replace (synth_call.py:3488-3491) and
        records no pipeline_writes.json entry for the new path. A user edit of the renamed
        note is then overwritten by the next pipeline write (only an archive copy keeps it)."""
        self.unresolved_note()
        out = self.respeak()
        new_rel = f"{PN}/{out[0]['to']}"
        self.assertIsNone(self.pipeline_sha(new_rel))
        p = self.zk / new_rel
        p.write_text(p.read_text() + "\nUSER LINE\n")
        pub = ri._Publisher(self.staging, self.run_dir.name, self.new_unit("repair", []), self.zk, {})
        self.assertTrue(pub.write(new_rel, self.ta, "an extension"))
        self.assertNotIn("USER LINE", p.read_text())


class RespeakNoLock(_Respeak):
    def test_defect_respeak_runs_while_a_driver_holds_the_locks(self) -> None:
        """respeak takes neither the driver lock (.driver.lock), nor the vault lock that
        finalize holds, nor state_lock for repair_state.json. The README only says 'do not'.
        Here both locks are held by 'a driver'; respeak --apply still renames the note and
        rewrites repair_state.json at once."""
        import fcntl
        self.unresolved_note()
        drv = open(self.staging / ".driver.lock", "a")
        fcntl.flock(drv, fcntl.LOCK_EX)
        done = {}

        def go() -> None:
            done["out"] = subprocess.run(
                [sys.executable, str(ZR / "synth_call.py"), "respeak", "--staging", str(self.staging), "--vault",
                 str(self.zk), "--video", V5, "--name", "Sasha", "--force-name", "--apply"],
                capture_output=True, text=True, timeout=60)
        with ri.vault_lock(self.staging, self.zk):
            th = threading.Thread(target=go)
            th.start()
            th.join(30)
            finished_inside = not th.is_alive()
        th.join(60)
        fcntl.flock(drv, fcntl.LOCK_UN)
        drv.close()
        self.assertTrue(finished_inside, "respeak waited for the lock")
        self.assertEqual(done["out"].returncode, 0, done["out"].stderr[-800:])
        self.assertFalse((self.zk / PN / f"{U}.md").exists())


class RespeakPendingSibling(_Respeak):
    def test_defect_pending_note_link_to_renamed_note_not_rewritten(self) -> None:
        """A note that waits in pending-review/ links `[[U]]`. respeak rewrites only vault
        files (zk.rglob): the pending copy keeps `[[U]]`, which no longer resolves when it
        publishes."""
        self.unresolved_note()
        pdir = self.staging / "pending-review" / "20261004T000000Z-1--u" / "out" / PN
        pdir.mkdir(parents=True)
        (pdir / f"{self.b}.md").write_text(self.tb.replace("- [[Home]] — home map.", f"- [[{U}]] — sibling."))
        self.respeak()
        self.assertIn(f"[[{U}]]", (pdir / f"{self.b}.md").read_text())
        self.assertFalse((self.zk / PN / f"{U}.md").exists())


class RespeakWritesOutside(_Respeak):
    def test_ok_name_with_path_signs_refused(self) -> None:
        """CONTROL: a name with '/' or '..' never leaves the zettelkasten folder."""
        self.unresolved_note()
        out = sc.respeak(self.staging, self.zk, V5, "../../Evil", apply=True, force_name=True)
        self.assertTrue(all(r.get("problem") for r in out), out)
        self.assertFalse(list(self.zk.parent.rglob("*Evil*")))


# --------------------------------------------------------------------------- #
# relink
# --------------------------------------------------------------------------- #


class _Relink(_Respeak):
    def seed(self, note_text: str | None = None) -> tuple[str, str]:
        """Note B (pipeline write) whose link to T was unlinked; T is published now."""
        t_title = "Later Target"
        b_rel = f"{PN}/{self.b}.md"
        line = "- Later Target — sibling."
        text = note_text or self.tb.replace("- [[Home]] — home map.", f"- [[Home]] — home map.\n{line}")
        pub = ri._Publisher(self.staging, self.run_dir.name, self.new_unit("synth", [V5]), self.zk, {})
        self.assertTrue(pub.write(b_rel, text, "publish"))
        pub.write(f"{PN}/{t_title}.md", self.ta.replace(f"# {self.a}\n", f"# {t_title}\n"), "publish")
        (self.staging / ri.UNLINKED).write_text(json.dumps([{
            "note": b_rel, "target": t_title, "at": "x", "line": line, "link": f"[[{t_title}]]", "plain": t_title}]))
        return b_rel, t_title

    def cli(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ZR / "synth_call.py"), "relink", "--staging", str(self.staging),
                               "--vault", str(self.zk), *extra], capture_output=True, text=True, timeout=60)


class RelinkControls(_Relink):
    def test_ok_dry_run_by_default_writes_nothing(self) -> None:
        b_rel, _t = self.seed()
        before = (self.zk / b_rel).read_text()
        r = self.cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["apply"], False)
        self.assertEqual((self.zk / b_rel).read_text(), before)
        self.assertEqual(len(json.loads((self.staging / ri.UNLINKED).read_text())), 1)

    def test_ok_apply_archives_and_records_sha(self) -> None:
        b_rel, t = self.seed()
        before = (self.zk / b_rel).read_text()
        r = self.cli("--apply")
        self.assertEqual(json.loads(r.stdout)["restored"], [{"note": b_rel, "target": t}])
        self.assertIn(f"[[{t}]]", (self.zk / b_rel).read_text())
        arch = list((self.staging / "retired").rglob(Path(b_rel).name))
        self.assertTrue(arch and arch[0].read_text() == before)
        self.assertEqual(self.pipeline_sha(b_rel), ri._sha256(self.zk / b_rel))

    def test_ok_user_edit_refused(self) -> None:
        b_rel, _t = self.seed()
        p = self.zk / b_rel
        p.write_text(p.read_text() + "\nUSER LINE\n")
        r = self.cli("--apply")
        self.assertEqual(json.loads(r.stdout)["restored"], [])
        self.assertIn("USER LINE", p.read_text())


class RelinkNotCommitted(_Relink):
    def test_defect_relink_apply_leaves_the_vault_git_dirty(self) -> None:
        """relink --apply writes through the publisher but never commits (respeak and every
        finalize commit their paths): the changed note stays uncommitted in the vault repo,
        and a later `git checkout`/snapshot restore drops it silently."""
        def git(*a: str) -> str:
            return subprocess.run(["git", "-C", str(self.zk), *a], capture_output=True, text=True).stdout
        b_rel, _t = self.seed()
        for a in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                  ["add", "-A"], ["commit", "-qm", "init"]):
            git(*a)
        self.assertEqual(git("status", "--porcelain").strip(), "")
        r = self.cli("--apply")
        self.assertTrue(json.loads(r.stdout)["restored"])
        self.assertIn(Path(b_rel).name, git("status", "--porcelain"))


class RelinkIntoStub(_Relink):
    def test_defect_relink_of_a_stub_reports_restored_but_changes_nothing(self) -> None:
        """B later became an old note that repair replaced (a pipeline stub). The unlinked
        line is a Details bullet, kept verbatim in the stub's claims list, so relink finds
        it once and calls the publisher. The publisher re-renders every stub from the OLD
        text (render_pipeline_stub), so the link is not added, but write() returns True:
        relink reports 'restored' and removes the unlinked.json entry. The link is lost."""
        line = "- Later Target is the next step."
        rel = self.old("Old Rel", 1)
        p = self.vpath("Old Rel")
        p.write_text(p.read_text().replace("- legacy bullet 1", line))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.run_batch(state, _rep("Old Rel", self.a, "b1", self.ta, f"Old Rel.md#b1 | kept in {self.a}\n"))
        sc.next_batch(self.staging, self.zk, state)
        stub = p.read_text()
        self.assertIn(stub_check.CLAIMS_HEADING, stub)
        self.assertEqual(stub.count(line + "\n"), 1)
        pub = ri._Publisher(self.staging, self.run_dir.name, self.new_unit("synth", [V5]), self.zk, {})
        pub.write(f"{PN}/Later Target.md", self.tb.replace(f"# {self.b}\n", "# Later Target\n"), "publish")
        (self.staging / ri.UNLINKED).write_text(json.dumps([{
            "note": rel, "target": "Later Target", "at": "x", "line": line, "link": "[[Later Target]]",
            "plain": "Later Target"}]))
        r = self.cli("--apply")
        out = json.loads(r.stdout)
        self.assertEqual(out["restored"], [{"note": rel, "target": "Later Target"}], out)
        self.assertNotIn("[[Later Target]]", p.read_text())
        self.assertEqual(json.loads((self.staging / ri.UNLINKED).read_text()), [])


class RespeakRereviewW22(_Respeak):
    def test_ok_writer_refused_after_respeak(self) -> None:
        """CONTROL (W22 re-check, v8 RespeakRenamedRereview adapted with --force-name): the
        writer of the note is refused as the reviewer of the renamed note."""
        import os
        import review_call as rc
        from tests.unit.test_run_integrity_r13 import review_answer, work
        os.environ["ZR_LLM_BACKEND"] = "files"
        self.unresolved_note()
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{U}.md", sources=[V5],
                             synth_worker=["writer-1"], review_worker="reviewer-2")
        out = self.respeak()
        new_rel = f"{PN}/{out[0]['to']}"
        run2 = ri.start(self.staging, "review", self.zk)
        ru = ri.unit_start(run2, "review", [], new_rel, os.getpid(), options={"review_pass": "source-check"})
        self.assertEqual(rc.review_unit(self.staging, self.zk, ru)[0]["end"], "waiting-for-reply")
        xdir = self.staging / "exchange"
        work(xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker=" Writer-1")
        self.assertEqual(rc.review_unit(self.staging, self.zk, ru)[0]["end"], "waiting-for-reply")
        self.assertTrue(list((xdir / "replies").glob("*.refused-*.txt")))
