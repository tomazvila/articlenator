"""Round 22 (review-v9 X-ids and pilot-9 points), correct-behaviour convention. Fake
workers and drivers only; no network."""
from __future__ import annotations

import json
import subprocess
import sys

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv
from tests.unit.test_run_integrity import OLD_NOTE, PN, ZR, ri

import stub_check  # noqa: E402
import synth_call as sc  # noqa: E402
import validate  # noqa: E402


def _rep(old: str, title: str, bullets: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bullets}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


def _sh(script: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(ZR / script), *args], env=env, capture_output=True, text=True, timeout=600)


def _git_init(zk) -> None:
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                ["add", "-A"], ["commit", "-qm", "init", "--allow-empty"]):
        subprocess.run(["git", "-C", str(zk), *cmd], capture_output=True, text=True)


class ExitCodes(_DriverEnv):
    """Item 6: 7 = finished, but operator work remains (printed); 1 = an unknown backend."""

    def setUp(self) -> None:
        super().setUp()
        _git_init(self.zk)  # round 23 (Z20): the preflight needs a git vault

    def _nolit(self) -> str:
        text = OLD_NOTE.format(t="Old Nolit").replace("created:", "sources:\n  - id: vid-NOTRANSCR1\n    speaker: X\ncreated:")
        self.vpath("Old Nolit").write_text(text)
        return f"{PN}/Old Nolit.md"

    def test_operator_work_exits_7_and_prints_it(self) -> None:
        rel = self._nolit()
        _git_init(self.zk)
        (self.staging / "needs_operator.json").write_text(json.dumps([{"note": f"{PN}/Old X.md", "why": "target rejected"}]))
        r = _sh("repair_loop.sh", self._env(), rel)
        self.assertEqual(r.returncode, 7, r.stderr[-800:])
        self.assertIn("operator work remains", r.stderr)
        self.assertIn("Old X", r.stderr)

    def test_no_transcript_note_is_marked_exits_0(self) -> None:
        # Round 24 (the owner's "mark"): the note without a transcript gets only the three mark
        # keys (body identical); that is the chosen outcome, not operator work: exit 0
        rel = self._nolit()
        before = self.vpath("Old Nolit").read_text()
        _git_init(self.zk)
        r = _sh("repair_loop.sh", self._env(), rel)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertTrue(ri.is_unsupported_mark(before, self.vpath("Old Nolit").read_text()))

    def test_unknown_backend_is_an_error_in_every_driver(self) -> None:
        env = dict(self._env(), ZR_LLM_BACKEND="filez")
        for script in ("loop.sh", "repair_loop.sh"):
            r = _sh(script, env, self._nolit()) if script == "repair_loop.sh" else _sh(script, env)
            self.assertEqual(r.returncode, 1, (script, r.stderr[-400:]))
        env = dict(self._env(), ZR_LLM_BACKEND="files ")  # X13: normalized, refused in review_loop.sh
        self.assertIn("is not supported", _sh("review_loop.sh", env).stderr)


class Preflight(_Repair):
    """Item 4: repair-preflight lists what makes a run unsafe; exit 1, `--force` exits 0."""

    def _pf(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ZR / "synth_call.py"), "repair-preflight", "--staging", str(self.staging),
                               "--vault", str(self.zk), *extra], capture_output=True, text=True)

    def test_findings(self) -> None:
        self.assertIn("not in a git repository", self._pf().stdout)  # Z20
        _git_init(self.zk)
        self.assertEqual(self._pf().returncode, 0, self._pf().stdout)
        self.vpath("Why Planche Hurts?").write_text(OLD_NOTE.format(t="Why Planche Hurts?"))
        _git_init(self.zk)  # committed: only the name and stub findings remain
        self.vpath("Old Legacy").write_text(ri.repair_stub("Old Legacy", [self.a]))
        r = self._pf()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Why Planche Hurts?.md", r.stdout)
        self.assertIn("legacy stub: Old Legacy.md", r.stdout)
        self.assertEqual(self._pf("--force").returncode, 0)


class TwoState(_Repair):
    """X2/X3: validate --staging reports a changed non-stub repair note and a stub without
    old_bullets; X1: recover() never writes an old note (list only)."""

    def test_changed_plan_note_and_legacy_stub_are_errors(self) -> None:
        rel = self.old("Old Two", 1)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=sc.state_path(self.staging))
        p = self.vpath("Old Two")
        p.write_text(p.read_text().replace("legacy bullet 1", "edited in place"))
        errs = validate.two_state_errors(self.staging, self.zk)
        self.assertTrue([e for e in errs if "neither a stub nor a clean unsupported mark" in e], errs)
        p.write_text(ri.repair_stub("Old Two", [self.a]))
        self.vpath(self.a).write_text(self.ta)
        errs = validate.two_state_errors(self.staging, self.zk)
        self.assertTrue([e for e in errs if "without old_bullets" in e], errs)

    def test_recover_lists_but_never_writes(self) -> None:
        rel = self.old("Old Beside", 1)
        before = self.vpath("Old Beside").read_text()
        self.vpath(self.a).write_text(self.ta)
        ri.provenance.record(self.staging, "note-repair", note=rel, new=[self.a])
        ri.recover(self.staging, self.zk)
        self.assertEqual(self.vpath("Old Beside").read_text(), before)
        ops = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertIn("old note beside its published replacement", [o["why"] for o in ops])
        self.assertFalse(list(self.staging.glob("retired/heal-*")))

    def test_in_place_title_is_refused(self) -> None:
        rel = self.old("Old Inplace", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        text = self.ta.replace(f"# {self.a}\n", "# Old Inplace\n")
        _b, _u, res, _ = self.run_batch(state, _rep("Old Inplace", "Old Inplace", "b1", text,
                                                    "Old Inplace.md#b1 | kept in Old Inplace\n"), finalize=False)
        self.assertIn("title-equals-old", [x["type"] for x in res["problems"]])


class GuardJournal(_Repair):
    """X10: a write whose registry update was lost (kill) is completed from the journal."""

    def test_reconcile(self) -> None:
        rel = f"{PN}/{self.a}.md"
        self.vpath(self.a).write_text(self.ta)
        sha = ri._sha256(self.vpath(self.a))
        (self.staging / ri.PIPELINE_JOURNAL).write_text(json.dumps({"rel": rel, "sha256": sha}) + "\n")
        self.assertEqual(ri.reconcile_pipeline_writes(self.staging, self.zk), [rel])
        self.assertEqual(json.loads((self.staging / ri.PIPELINE_WRITES).read_text())[rel], sha)


class ShapesAndArchives(_Repair):
    """X4: a first line `---` without YAML is text; X6: archive search by exact paths."""

    def test_rule_first_line_is_carried(self) -> None:
        old = "---\nA horizontal rule first, then text.\n---\n\n# T\n\n## Details\n\n- b1 claim text here\n"
        self.assertEqual(stub_check.split(old)[0], "")
        stub = stub_check.render(old, "T", ["open: x"], [], [], "a")
        self.assertIn("> A horizontal rule first, then text.", stub)
        self.assertEqual(stub_check.check(old, stub, None), [])

    def test_glob_characters_in_names(self) -> None:
        for name, other in (("Why Planche Hurts?", "Why Planche Hurts!"), ("Note [draft]", "Note d")):
            for n, body in ((name, "THE RIGHT TEXT"), (other, "ANOTHER NOTE")):
                p = self.staging / "retired" / "r1" / "u1" / PN / f"{n}.md"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(OLD_NOTE.format(t=n).replace("Legacy claim.", body))
            got = ri.archived_old_text(self.staging, f"{PN}/{name}.md")
            self.assertIn("THE RIGHT TEXT", got or "", name)


class Pilot9(_Repair):
    """Pilot-9 points 1a (passages never cut), 3 (contradicts drop is final), 9 (repair-status)."""

    def test_passages_keep_every_marked_passage(self) -> None:
        self.vpath(self.a).write_text(self.ta)
        full = ri.passages(self.staging, self.zk, f"{PN}/{self.a}.md")
        small = ri.passages(self.staging, self.zk, f"{PN}/{self.a}.md", max_chars=len(full) // 3)
        self.assertNotIn("passages cut", small)
        marked = [ln for ln in full.split("\n") if ln.startswith(">>> ")]
        self.assertTrue(marked)
        for ln in marked:
            self.assertIn(ln, small)

    def test_contradicts_drop_is_final(self) -> None:
        e = {"sources": ["v1", "v2", "v3"], "status": "done",
             "bullets": {"O.md#b1": {"text": "x", "answers": {"v1": {"decision": "dropped", "reason": "not in this transcript"},
                                                             "v2": {"decision": "dropped", "reason": "contradicts the transcript"}}}}}
        s, _p, _w = ri._bullet_status(self.zk, e, "O.md#b1", set())
        self.assertTrue(s.startswith("dropped: contradicts the transcript"), s)
        self.assertEqual(ri.classify_bullets(e)["unsupported"], ["O.md#b1"])

    def test_repair_status_lists_unchanged_unlisted_notes(self) -> None:
        rel = self.old("Old Quiet", 1)
        st = {"notes": {rel: {"sha": ri._sha256(self.vpath("Old Quiet")), "status": "done", "titles": [], "sources": ["v"],
                              "bullets": {"Old Quiet.md#b1": {"text": "legacy bullet 1", "answers": {}}}}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        rows = sc.repair_status(self.staging, self.zk)
        self.assertEqual((rows[0]["vault"], rows[0]["unlisted"]), ("unchanged", True))
