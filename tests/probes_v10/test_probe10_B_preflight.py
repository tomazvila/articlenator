"""Probe 10 B (reviewer B, round 5): `repair-preflight` and its `--force`.
Fake LLMs only, no network.

Convention of THIS file: every test asserts the DEFECT. PASS = the defect is present.
FAIL = the defect is gone (or the setup changed)."""
from __future__ import annotations

import os
import subprocess
import sys

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import OLD_NOTE, PN, ZR, ri, validate

import synth_call as sc  # noqa: E402


def _git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)


class PreflightIgnoresKnownViolations(_Repair):
    """Z-B10: the preflight does not run the two-state checks (two_state_errors,
    stub_errors, replacement_beside_old, operator_work). A vault that a previous run left in
    a violated state (here: a plan note changed in place, not a stub, which validate
    --staging reports) gives `preflight: 0 finding(s)` and exit 0: the next repair run
    starts on it without a word."""

    def test_violation_from_previous_run_passes_preflight(self) -> None:
        rel = self.old("Old Pf", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        p = self.zk / rel
        p.write_text(p.read_text().replace("- legacy bullet 2\n", ""))
        self.assertTrue(any("Old Pf" in e for e in validate.two_state_errors(self.staging, self.zk)))
        self.assertEqual(sc.repair_preflight(self.staging, self.zk), [])


class PreflightForceLeavesNoRecord(_Repair):
    """Z-B11: `--force` (ZR_PREFLIGHT_FORCE=1 in repair_loop.sh) skips no check: it prints
    every finding and exits 0. The findings are written only to stdout: no staging file,
    no provenance record, nothing in the run summary or operator_work. After the run, no
    file shows that a run started on (for example) legacy stubs or a dirty vault."""

    def test_forced_findings_are_not_recorded(self) -> None:
        self.vpath("Legacy Stub").write_text("---\nstatus: superseded\nsuperseded_by: \"[[X]]\"\n---\n\n# Legacy Stub\n")
        before = {p.relative_to(self.staging) for p in self.staging.rglob("*") if p.is_file()}
        env = dict(os.environ)
        r = subprocess.run([sys.executable, str(ZR / "synth_call.py"), "repair-preflight", "--staging",
                            str(self.staging), "--vault", str(self.zk), "--force"], capture_output=True, text=True,
                           env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("legacy stub: Legacy Stub.md", r.stdout)
        after = {p.relative_to(self.staging) for p in self.staging.rglob("*") if p.is_file()}
        self.assertEqual(after - before, set())
        self.assertNotIn("Legacy", " ".join(ri.operator_work(self.staging, self.zk)))


class PreflightGitScopeIsTheWholeRepo(_Repair):
    """Z-B12 (false positive, environment-dependent): the dirty-vault check runs
    `git -C <zettelkasten> status --porcelain` with no path limit. When the git repository
    is the Obsidian vault (ZK_DIR = $VAULT/$ZK_FOLDER) or a parent folder, a change OUTSIDE
    the zettelkasten (a daily note, `.obsidian/workspace.json`) blocks every repair run."""

    def test_change_outside_zettelkasten_blocks(self) -> None:
        vault = self.zk.parent
        self.assertEqual(_git(vault, "init", "-q").returncode, 0)
        _git(vault, "-c", "user.email=x@y", "-c", "user.name=x", "add", "-A")
        _git(vault, "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-q", "-m", "snap")
        self.assertEqual(_git(self.zk, "status", "--porcelain", "--", ".").stdout.strip(), "")
        (vault / ".obsidian").mkdir(exist_ok=True)
        (vault / ".obsidian" / "workspace.json").write_text("{}")  # outside the zettelkasten
        found = sc.repair_preflight(self.staging, self.zk)
        self.assertTrue(any(f.startswith("git: the vault has uncommitted changes") for f in found), found)
