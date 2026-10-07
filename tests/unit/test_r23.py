"""Round 23 (review-v10 Z-ids), correct-behaviour convention. Fake workers and drivers only."""
from __future__ import annotations

import json
import subprocess
import sys

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv
from tests.unit.test_r22 import _git_init, _sh
from tests.unit.test_run_integrity import OLD_NOTE, PN, ZR, ri

import synth_call as sc  # noqa: E402
import validate  # noqa: E402


class AgentModeRefused(_DriverEnv):
    """Z1: repair_loop.sh refuses agent mode (exit 2) unless ZR_REPAIR_AGENT_MODE_I_KNOW=1."""

    def test_refused(self) -> None:
        self.vpath("Old A").write_text(OLD_NOTE.format(t="Old A"))
        _git_init(self.zk)
        for extra, args in (({"ZR_SYNTH_MODE": "agent"}, []), ({}, ["--allow-other-sources"])):
            r = _sh("repair_loop.sh", dict(self._env(), **extra), *args, f"{PN}/Old A.md")
            self.assertEqual(r.returncode, 2, r.stderr[-400:])
            self.assertIn("single mode only", r.stderr)


class InPlaceRefusedInEveryMode(_Repair):
    """Z1: finalize refuses a non-stub repair write over a vault note the pipeline did not publish."""

    def test_agent_style_write_over_old_note(self) -> None:
        self.vpath("Old A").write_text(OLD_NOTE.format(t="Old A"))
        before = self.vpath("Old A").read_text()
        unit = self.new_unit("repair", [])
        p = unit / "out" / PN / "Old A.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.ta.replace(f"# {self.a}\n", "# Old A\n"))
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
        n = next(x for x in ev["notes"] if x["note"] == f"{PN}/Old A.md")
        self.assertEqual(n["action"], "quarantined")
        self.assertIn("title-equals-old", n["failure_types"])
        self.assertEqual(self.vpath("Old A").read_text(), before)


class OperatorWorkOneSource(_Repair):
    """Z3/Z5/Z7/Z8/Z9/Z15/Z17: operator_work and repair-status agree; operator-done clears."""

    def _plan(self, title: str, status: str, answers: dict) -> str:
        rel = self.old(title, 1)
        st = json.loads((self.staging / "repair_state.json").read_text()) if (self.staging / "repair_state.json").is_file() \
            else {"notes": {}, "batches": {}}
        st["notes"][rel] = {"sha": ri._sha256(self.vpath(title)), "status": status, "titles": [], "sources": ["v1"],
                            "bullets": {f"{title}.md#b1": {"text": "legacy bullet 1", "answers": answers}}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        return rel

    def test_missing_plan_note_and_unlisted_are_operator_work(self) -> None:
        rel = self._plan("Old Gone", "done", {})
        self.vpath("Old Gone").unlink()
        self._plan("Old Quiet", "done", {})
        ow = ri.operator_work(self.staging, self.zk)
        self.assertTrue([w for w in ow if "Old Gone" in w and "missing" in w], ow)
        self.assertTrue([w for w in ow if w.startswith("unlisted: Old Quiet")], ow)
        rows = {r["note"]: r for r in sc.repair_status(self.staging, self.zk)}
        self.assertEqual(rows[rel]["vault"], "missing")
        self.assertTrue(rows[rel]["operator"])

    def test_contradicts_is_listed_and_operator_done_clears(self) -> None:
        rel = self._plan("Old Contra", "done", {"v1": {"decision": "dropped", "reason": "contradicts the transcript"}})
        ow = ri.operator_work(self.staging, self.zk)
        self.assertTrue([w for w in ow if w.startswith("contradicts: Old Contra")], ow)
        res = ri.operator_done(self.staging, rel, "read: the video contradicts it")
        self.assertTrue(res["plan_closed"])
        self.assertFalse([w for w in ri.operator_work(self.staging, self.zk) if "Old Contra" in w])
        self.assertTrue((self.staging / "operator_decisions.jsonl").is_file())

    def test_waiting_target_is_not_unlisted(self) -> None:
        rel = self._plan("Old Wait", "done", {"v1": {"decision": "kept", "title": "Waiting Title"}})
        pd = self.staging / "pending-review" / "x--u" / "out" / PN
        pd.mkdir(parents=True)
        (pd / "Waiting Title.md").write_text("x")
        row = next(r for r in sc.repair_status(self.staging, self.zk) if r["note"] == rel)
        self.assertEqual((row["vault"], row["unlisted"]), ("waiting", False))

    def test_failed_queue_item_is_operator_work(self) -> None:
        q = json.loads((self.staging / "queue.json").read_text())
        q["items"][0]["stage"] = "failed"
        (self.staging / "queue.json").write_text(json.dumps(q))
        self.assertTrue([w for w in ri.operator_work(self.staging, self.zk) if w.startswith("failed:")])


class StubBoundToItsNote(_Repair):
    """Z4: a stub whose repair_archive is another note's archive fails validate."""

    def test_foreign_archive(self) -> None:
        rel = self.old("Old A", 1)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=sc.state_path(self.staging))
        other = self.staging / "retired" / "r" / "u" / PN / "Old B.md"
        other.parent.mkdir(parents=True)
        other.write_text(OLD_NOTE.format(t="Old A"))
        self.vpath(self.a).write_text(self.ta)
        import stub_check
        stub = stub_check.render(other.read_text(), "Old A", ["open: x"], [self.a], [],
                                 str(other.relative_to(self.staging)))
        self.vpath("Old A").write_text(stub)
        errs = validate.stub_errors(self.staging, self.zk, [self.vpath("Old A")])
        self.assertTrue([e for e in errs if "not an archive of this note" in e], errs)


class TitleMdSuffix(_Repair):
    """Z19: `<Old>.md` as a title is refused like `<Old>`."""

    def test_md_suffix(self) -> None:
        unit = self.new_unit("repair", [])
        self.assertIn(".md", sc._title_equals_old(self.zk, unit, "Some Title.md", set()))


class Refusals(_Repair):
    """Z16: after ZR_MAX_REFUSALS (3) refusals of one key the request is obsolete and listed."""

    def test_third_refusal(self) -> None:
        xdir = self.staging / "exchange"
        (xdir / "requests").mkdir(parents=True)
        (xdir / "replies").mkdir(parents=True)
        key = "a" * 32
        (xdir / "requests" / f"{key}.json").write_text(json.dumps({"key": key, "unit": "u", "name": "review-x"}))
        for _ in range(3):
            (xdir / "replies" / f"{key}.txt").write_text("bad")
            sc.refuse_reply(xdir, key, "the same worker wrote the note", "w1")
        self.assertTrue((xdir / "requests" / f"{key}.obsolete").is_file())
        ops = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertTrue([o for o in ops if key in o["note"]])


class JournalEdgeCases(_Repair):
    """Z13: a user edit after a killed write is seen by the guard; the journal empties."""

    def test_kill_then_user_edit(self) -> None:
        rel = f"{PN}/{self.a}.md"
        self.vpath(self.a).write_text(self.ta)
        intent = ri._sha256(self.vpath(self.a))
        (self.staging / ri.PIPELINE_JOURNAL).write_text(json.dumps({"rel": rel, "sha256": intent}) + "\n")
        self.vpath(self.a).write_text(self.ta + "\nUSER EDIT\n")  # after the kill
        ri.reconcile_pipeline_writes(self.staging, self.zk)
        self.assertTrue(ri.guard_vault_write(self.staging, self.zk, rel, self.ta))  # refused

    def test_journal_empties_after_a_write(self) -> None:
        pub = ri._Publisher(self.staging, "r", self.new_unit("repair", []), self.zk, {})
        self.assertTrue(pub.write(f"{PN}/{self.a}.md", self.ta, "test"))
        self.assertFalse((self.staging / ri.PIPELINE_JOURNAL).exists())


class PreflightScope(_Repair):
    """Z20: the git check covers only the zettelkasten folder; two-state errors are findings."""

    def test_change_outside_the_folder_does_not_block(self) -> None:
        root = self.zk.parent
        for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                    ["add", "-A"], ["commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(root), *cmd], capture_output=True)
        (root / "Other.md").write_text("user work outside the zettelkasten")
        r = subprocess.run([sys.executable, str(ZR / "synth_call.py"), "repair-preflight", "--staging",
                            str(self.staging), "--vault", str(self.zk)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
