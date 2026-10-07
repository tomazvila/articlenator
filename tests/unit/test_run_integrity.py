"""Tests for run integrity: shadow folder, mechanical check, source-check review before
publication, pending reviews, quarantine, recovery, secrets, helpers, git, registry.

Each class reproduces findings of the code reviews (round 1: B1-B9; round 2: X1-X4, M-a..M-i).
Runs under pytest and under `python -m unittest`. No network, no LLM.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ZR = ROOT / "zettel_ralph"
FIX = ROOT / "tests" / "fixtures" / "zettel_verify"
HONEST = ROOT / "tests" / "fixtures" / "zettel_honest"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

import provenance  # noqa: E402
import run_integrity as ri  # noqa: E402
import secret_scan  # noqa: E402
import validate  # noqa: E402

_spec = importlib.util.spec_from_file_location("deepseek_agent_ri", ZR / "deepseek_agent.py")
agent = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(agent)

SASA = "SasaVenos Recommends No More Than 30 To 40 Seconds Of Banded Skill Volume Per Session After His Own Maltese Trial"
RADO = "Radoslav Radev Advises One, Maximum Two Failure Workouts At Four Workouts Per Week"
VID = "vid-krqBQjUydGY"
PN = "01 Permanent Notes"
OLD_NOTE = ("---\ntype: permanent note\ncreated: 2026-09-12\nverification: unverified\n"
            "tags: [calisthenics, x, zettelkasten, permanent-note]\n---\n\n# {t}\n\nLegacy claim.\n\n"
            "## Details\n\n- legacy bullet\n\n## Connected Ideas\n\n- [[Other]]\n")
FAKE_KEY = "sk-or-v1-FAKESECRET0123456789"


def good_text(title: str = SASA) -> str:
    return (FIX / "notes" / f"{title}.md").read_text(encoding="utf-8")


def bad_text() -> str:
    t = good_text()
    assert "He did around 10 sets of holds per week." in t
    return t.replace("He did around 10 sets of holds per week.", "He did around 10 sets of holds each.")


def seed_link_targets(zk: Path) -> None:
    """The fixture notes link to notes that are not part of the fixtures. A dead link
    fails (round 4), so the targets exist as map notes."""
    import re as _re
    for p in list((FIX / "notes").glob("*.md")) + list((HONEST / "notes").glob("*.md")):
        for t in _re.findall(r"\[\[([^\]|#]+)", p.read_text()):
            target = zk / "00 Maps" / f"{t.strip()}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"---\ntype: map note\n---\n\n# {t.strip()}\n", encoding="utf-8")


def git(zk: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(zk), *a], capture_output=True, text=True)


class _Env(unittest.TestCase):
    kind = "synth"
    ids = [VID]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ri-"))
        self.staging = self.tmp / "staging"
        shutil.copytree(FIX / "lit", self.staging / "lit")
        self.zk = self.tmp / "vault" / "ZK"
        (self.zk / PN).mkdir(parents=True)
        (self.zk / "Other.md").write_text("---\ntype: map note\n---\n\n# Other\n", encoding="utf-8")
        seed_link_targets(self.zk)
        self.write_queue([{"id": v, "kind": "video", "stage": "claimed", "worker": 0,
                           "lit_note": str(self.staging / "lit" / f"{v}.md")} for v in self.ids] or
                         [{"id": VID, "kind": "video", "stage": "synthesized", "lit_note": "x"}])
        self._old_env = dict(os.environ)
        for k in ("ZR_LIT_DIRS", "ZR_AGENT", "ZR_AGENT_READONLY", "ZR_CONTENT"):
            os.environ.pop(k, None)
        os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
        self.run_dir = ri.start(self.staging, "synth", self.zk)
        self.unit = self.new_unit()

    def new_unit(self, kind: str | None = None, ids: list[str] | None = None, review: str | None = None) -> Path:
        ids = self.ids if ids is None else ids
        unit = ri.unit_start(self.run_dir, kind or self.kind, ids, review, os.getpid())
        os.environ.update({
            "ZR_STAGING": str(self.staging), "VAULT": str(self.tmp / "vault"), "ZK_DIR": str(self.zk),
            "ZR_RUN_DIR": str(unit), "ZR_SHADOW_DIR": str(unit / "out"), "ZR_UNIT": unit.name,
            "ZR_UNIT_IDS": ",".join(ids), "ZR_RUN_ID": self.run_dir.name, "ZR_UNIT_KIND": kind or self.kind,
        })
        return unit

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_queue(self, items: list[dict]) -> None:
        self.staging.mkdir(parents=True, exist_ok=True)
        (self.staging / "queue.json").write_text(json.dumps({"version": 1, "items": items}), encoding="utf-8")

    def queue(self) -> list[dict]:
        return json.loads((self.staging / "queue.json").read_text(encoding="utf-8"))["items"]

    def vpath(self, title: str) -> Path:
        return self.zk / PN / f"{title}.md"

    def approve(self, title: str, unit: Path | None = None, verdict: str = "supported", scope_complete=True,
                rc: int = 0, payload: dict | None = None, source: Path | None = None) -> Path:
        """Run the harness review steps (review-begin, the review agent's verdict file,
        review-end) as zr_source_check does. The first item gets `verdict`."""
        unit = unit or self.unit
        rel = title if "/" in title else f"{PN}/{title}.md"
        src = source or (unit / "out" / rel)
        rdir = ri.review_begin(unit, rel, src.read_bytes())
        if payload is None:
            payload = json.loads((rdir / "skeleton.json").read_text())
            for k, it in enumerate(payload["items"]):
                it["verdict"] = verdict if k == 0 else "supported"
            payload["scope_complete"] = scope_complete
        (rdir / "verdict.json").write_text(json.dumps(payload))
        (rdir / "agent_result.json").write_text(json.dumps({"rc": rc, "end": "finished" if rc == 0 else "error"}))
        ri.review_end(rdir, rc)
        return rdir

    def finalize(self, rc: int = 0, unit: Path | None = None, **kw) -> dict:
        unit = unit or self.unit
        info = json.loads((unit / "unit.json").read_text())
        return ri.finalize(self.staging, self.run_dir, unit, info["ids"], rc, self.zk, **kw)

    def note(self, ev: dict, title_or_rel: str) -> dict:
        rel = title_or_rel if "/" in title_or_rel else f"{PN}/{title_or_rel}.md"
        return next(n for n in ev["notes"] if n["note"] == rel)


# --------------------------------------------------------------------------- #
# X1: secrets
# --------------------------------------------------------------------------- #


class Secrets(_Env):
    def test_no_variable_expansion_in_paths(self) -> None:
        out, _ = agent.run_tool({"function": {"name": "read_file", "arguments": json.dumps({"path": "$DEEPSEEK_API_KEY"})}})
        self.assertNotIn(FAKE_KEY, out)
        out, _ = agent.run_tool({"function": {"name": "write_file", "arguments": json.dumps(
            {"path": str(self.zk / PN / "${DEEPSEEK_API_KEY}.md"), "content": "x"})}})
        self.assertNotIn(FAKE_KEY, out)
        self.assertFalse(any(FAKE_KEY in p.name for p in self.unit.rglob("*")))

    def test_allowed_variables_are_replaced(self) -> None:
        r = agent.run_command("python3", ["validate.py", "--vault", "$ZK_DIR", "--squeeze"])
        self.assertEqual(r["returncode"], 0, r)

    def test_write_with_a_secret_is_refused(self) -> None:
        with self.assertRaises(agent.AgentError) as ctx:
            agent.write_file(str(self.vpath("Leak")), "text " + FAKE_KEY)
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        with self.assertRaises(agent.AgentError):
            agent.write_file(str(self.staging / "STATE.md"), "key sk-or-v1-abcdefgh12345678")

    def test_secret_in_a_shadow_file_is_quarantined(self) -> None:
        p = self.unit / "out" / PN / f"{SASA}.md"
        p.parent.mkdir(parents=True)
        p.write_text(good_text().replace("the other side", "x") + f"\n{FAKE_KEY}\n")
        self.approve(SASA)
        ev = self.finalize(0)
        self.assertIn("secret-in-note", self.note(ev, SASA)["failure_types"])
        self.assertFalse(self.vpath(SASA).exists())
        q = Path(self.note(ev, SASA)["quarantine_path"]).read_text()
        self.assertNotIn(FAKE_KEY, q)

    def test_helper_environment_has_no_secrets(self) -> None:
        seen = {}
        real = agent.subprocess.run

        def fake_run(cmd, **kw):
            seen.update(kw["env"])
            return real(cmd, **kw)
        agent.subprocess.run = fake_run
        try:
            agent.run_command("python3", ["index_rebuild.py"])
        finally:
            agent.subprocess.run = real
        self.assertNotIn("DEEPSEEK_API_KEY", seen)
        self.assertEqual(seen.get("ZR_AGENT"), "1")
        self.assertEqual(secret_scan.scrub(f"a {FAKE_KEY} b"), "a [redacted] b")

    def test_index_query_index_flag_refused(self) -> None:
        for args in (["index_query.py", "x", "--index", "/etc/passwd"], ["index_query.py", "x", "--vault", "/tmp"],
                     ["validate.py", "--vault", "/home"]):
            with self.assertRaises(agent.AgentError):
                agent.run_command("python3", args)


# --------------------------------------------------------------------------- #
# Shadow writes and helper arguments (B1-B3, M-c, M-g tool side)
# --------------------------------------------------------------------------- #


class ShadowWrites(_Env):
    def test_vault_write_goes_to_shadow(self) -> None:
        agent.write_file(str(self.vpath("A Note")), "new")
        self.assertFalse(self.vpath("A Note").exists())
        self.assertIn("new", agent.read_file(str(self.vpath("A Note")))["content"])

    def test_refusals(self) -> None:
        stg = self.staging
        for p in [stg / "provenance.jsonl", stg / "queue.json", stg / "lit" / "x.md", stg / "runs" / "x.md",
                  stg / "Provenance.jsonl", stg / "LIT" / "v.md", stg / "disagreements.jsonl",
                  ZR / "run_integrity.py", self.tmp / "vault" / "Other Zettelkasten" / "U.md",
                  Path(str(self.zk).replace("/ZK", "/zk")) / PN / "X.md",
                  # Round 5 (B1): no agent writes a verdict location except its own review.
                  stg / "review-verdicts" / "A.json", self.unit / "review-0" / "verdict.json",
                  self.unit / "review-0" / "review.json", self.unit / "check.json"]:
            with self.assertRaises(agent.AgentError, msg=str(p)):
                agent.write_file(str(p), "x")
        for p in [stg / "STATE.md", stg / "repair" / "A.json"]:
            agent.write_file(str(p), "{}")

    def test_read_only_review_agent_cannot_write_notes(self) -> None:
        """M1: a review agent writes exactly one file, its verdict, and runs only
        read-only helpers."""
        rdir = self.unit / "review-0"
        os.environ.update({"ZR_AGENT_READONLY": "1", "ZR_VERDICT_FILE": str(rdir / "verdict.json")})
        for p in (self.vpath("A Note"), self.staging / "STATE.md", self.staging / "repair" / "A.json",
                  rdir / "review.json", self.unit / "review-1" / "verdict.json"):
            with self.assertRaises(agent.AgentError, msg=str(p)):
                agent.write_file(str(p), "x")
        agent.write_file(str(rdir / "verdict.json"), "{}")
        for args in (["note_link.py", "--from", f"{PN}/A.md", "--to", f"{PN}/B.md", "--kind", "related"],
                     ["queue_mark.py", "--ids", VID, "--stage", "synthesized"],
                     ["index_add.py", "--title", "X"]):
            with self.assertRaises(agent.AgentError, msg=args):
                agent.run_command("python3", args)
        self.assertEqual(agent.run_command("python3", ["validate.py", "--help"])["returncode"], 0)


class HelperArguments(_Env):
    def test_abbreviation_out_and_paths_are_refused(self) -> None:
        note = str(self.vpath("X"))
        for args in (["verify_claims.py", note, "--wr"], ["verify_claims.py", note, "--write"],
                     ["verify_claims.py", note, "--out", str(self.staging / "provenance.jsonl")],
                     ["verify_claims.py", "/home/deploy/.ssh/id_rsa"], ["verify_claims.py", "--", "--write", note],
                     ["index_add.py", "--title", "X", "--file", "../../etc/passwd"],
                     ["note_link.py", "--from", f"{PN}/A.md", "--to", "Private Journal.md", "--kind", "related"],
                     ["queue_mark.py", "--id", VID]):
            with self.assertRaises(agent.AgentError, msg=args):
                agent.run_command("python3", args)

    def test_queue_mark_owns_only_its_unit_and_skip_records(self) -> None:
        env = dict(os.environ)

        def run(*a: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(ZR / "queue_mark.py"), *a], env=env, capture_output=True, text=True)
        self.assertEqual(run("--ids", "vid-other", "--stage", "skipped").returncode, 2)
        self.assertEqual(run("--ids", VID, "--skip-passage", "it also works", "--reason", "anecdote").returncode, 0)
        self.assertEqual(json.loads((self.staging / "skips.jsonl").read_text().splitlines()[-1])["id"], VID)

    def test_documented_commands_work_under_the_agent(self) -> None:
        """M-c: every helper command line of AGENTS_transcript.md, prompts/*.md and
        NOTE_CONTRACT.md, filled with real values, runs under the agent's allowlist."""
        agent.write_file(str(self.vpath(SASA)), good_text())
        agent.write_file(str(self.vpath(RADO)), good_text(RADO))
        rel = f"{PN}/{SASA}.md"
        lit = f"{os.environ['ZR_STAGING']}/lit"
        cmds = [
            ["validate.py", "--vault", "$ZK_DIR", "--squeeze"],
            ["verify_claims.py", str(self.vpath(SASA)), "--lit", "$ZR_STAGING/lit"],
            ["verify_claims.py", rel, "--lit", "$ZR_STAGING/lit"],
            ["verify_claims.py", str(self.unit / "out" / rel), "--lit", lit],
            ["index_query.py", "claim", "--speaker", "SasaVenos", "--skill", "s", "--level", "l", "--equipment", "e",
             "--basis", "general rule", "--modality", "recommendation"],
            ["index_query.py", "claim", "--kind", "tweet"],
            ["index_add.py", "--title", SASA, "--file", rel, "--gist", "g", "--tags", "calisthenics",
             "--source", VID, "--speaker", "SasaVenos", "--skill", "s", "--level", "l", "--equipment", "e",
             "--basis", "speaker's own practice", "--modality", "recommendation"],
            ["note_link.py", "--from", f"{PN}/{RADO}.md", "--to", rel, "--kind", "disagreement"],
            ["note_link.py", "--from", str(self.vpath(SASA)), "--to", f"{PN}/Old.md", "--kind", "supersedes-candidate"],
            ["note_link.py", "--from", rel, "--to", f"{PN}/Other Note.md", "--kind", "related"],
            ["queue_mark.py", "--ids", VID, "--skip-passage", "the first eight words", "--at", "01:00", "--reason", "x"],
            ["queue_mark.py", "--ids", VID, "--stage", "synthesized", "--notes", SASA],
            ["queue_mark.py", "--ids", VID, "--stage", "skipped", "--reason", "why"],
        ]
        for c in cmds:
            r = agent.run_command("python3", c)
            self.assertEqual(r["returncode"], 0, (c, r["stdout"][-400:], r["stderr"][-400:]))
            self.assertNotIn("internal-error", r["stdout"], c)
        os.environ["ZR_UNIT_KIND"] = "repair"
        r = agent.run_command("python3", ["index_add.py", "--title", SASA, "--file", rel, "--repair"])
        self.assertIn(r["returncode"], (0, 3), r)


# --------------------------------------------------------------------------- #
# X4: review before publication; pending reviews; M-a retry
# --------------------------------------------------------------------------- #


class ReviewBeforePublish(_Env):
    def test_supported_published_bad_verdict_quarantined_missing_pending(self) -> None:
        h = sorted((HONEST / "notes").glob("Radoslav Radev*5*.md"))
        texts = {p.stem: p.read_text() for p in (HONEST / "notes").glob("*.md")
                 if "id: vid-5zALUKd7h3g" in p.read_text()}
        self.assertEqual(len(texts), 3, h)
        shutil.copy(HONEST / "lit" / "vid-5zALUKd7h3g.md", self.staging / "lit")
        self.write_queue([{"id": "vid-5zALUKd7h3g", "kind": "video", "stage": "synthesized", "lit_note": "x"}])
        unit = self.new_unit("synth", ["vid-5zALUKd7h3g"])
        titles = sorted(texts)
        for t in titles:
            agent.write_file(str(self.vpath(t)), texts[t])
        self.approve(titles[0], unit)
        self.approve(titles[1], unit, verdict="changed-modality")
        ev = self.finalize(0, unit)
        self.assertEqual(self.note(ev, titles[0])["action"], "published")
        self.assertIn("verification: source-checked", self.vpath(titles[0]).read_text())
        self.assertEqual(self.note(ev, titles[1])["action"], "quarantined")
        self.assertIn("review:changed-modality", self.note(ev, titles[1])["failure_types"])
        self.assertEqual(self.note(ev, titles[2])["action"], "pending-review")
        self.assertFalse(self.vpath(titles[2]).exists())
        # Next run: the pending note becomes a unit, gets its review, and is published.
        units = ri.pending_units(self.staging, self.run_dir, os.getpid())
        self.assertEqual(len(units), 1)
        pu = Path(units[0])
        self.approve(titles[2], pu)
        ev2 = ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        self.assertEqual(self.note(ev2, titles[2])["action"], "published")
        self.assertIn("verification: source-checked", self.vpath(titles[2]).read_text())

    def test_quote_checked_is_never_published_for_video(self) -> None:
        q = self.queue()
        q[0]["stage"] = "synthesized"  # the agent called queue_mark.py
        self.write_queue(q)
        agent.write_file(str(self.vpath(SASA)), good_text())
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "pending-review")
        self.assertEqual(self.queue()[0]["stage"], "pending-review")

    def test_quarantine_does_not_resynthesize_the_item(self) -> None:
        """Round 8: a finished unit is synthesized; quarantined notes are counted, and more
        than half quarantined gives `needs-attention` (not `incomplete`)."""
        q = self.queue()
        q[0]["stage"] = "synthesized"
        self.write_queue(q)
        agent.write_file(str(self.vpath("Bad Copy")), bad_text().replace(f"# {SASA}", "# Bad Copy"))
        self.finalize(0)
        item = self.queue()[0]
        self.assertEqual(item["stage"], "needs-attention")
        self.assertEqual((item["notes_quarantined"], item["notes_published"]), (1, 0))
        self.assertEqual(item.get("synth_attempts", 0), 0)

    def test_check_report_for_fix_turns(self) -> None:
        agent.write_file(str(self.vpath(SASA)), bad_text())
        res = ri.check_unit(self.staging, self.unit, self.zk)
        self.assertFalse(res[f"{PN}/{SASA}.md"]["ok"])
        agent.write_file(str(self.vpath(SASA)), good_text())  # the fix turn
        res = ri.check_unit(self.staging, self.unit, self.zk)
        self.assertTrue(res[f"{PN}/{SASA}.md"]["needs_review"])

    def test_review_context_is_90_seconds(self) -> None:
        self.vpath(RADO).write_text(good_text(RADO))
        text = ri.passages(self.staging, self.zk, f"{PN}/{RADO}.md")
        self.assertIn("context 00:20 to 03:20", text)


# --------------------------------------------------------------------------- #
# Publication rules: paths, names, renames, repair transaction (B1, B2, X2, M-g)
# --------------------------------------------------------------------------- #


class Publish(_Env):
    def test_commit_only_published_paths_and_lock_outside_vault(self) -> None:
        git(self.zk, "init", "-q")
        git(self.zk, "config", "user.email", "t@example.invalid")
        git(self.zk, "config", "user.name", "t")
        (self.zk / "untracked by the run.md").write_text("x")
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.approve(SASA)
        ev = self.finalize(0, commit_message="test commit")
        self.assertTrue(ev["commit"]["ok"])
        files = [c for c in git(self.zk, "show", "--name-only", "--format=").stdout.split("\n") if c]
        self.assertEqual(files, [f"{PN}/{SASA}.md"])
        self.assertFalse(any(p.name.startswith(".zr") for p in self.zk.rglob("*")))
        self.assertTrue(ri.lock_file(self.staging, self.zk).exists())

    def test_failed_commit_is_reported_and_retried(self) -> None:
        git(self.zk, "init", "-q")
        git(self.zk, "config", "user.email", "t@example.invalid")
        git(self.zk, "config", "user.name", "t")
        (self.zk / ".git" / "index.lock").write_text("")
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.approve(SASA)
        ev = self.finalize(0, commit_message="m")
        self.assertFalse(ev["commit"]["ok"])
        self.assertEqual(len(ri.summary(self.run_dir, self.staging)["commit_failed"]), 1)
        (self.zk / ".git" / "index.lock").unlink()
        run2 = ri.start(self.staging, "synth", self.zk)
        self.assertTrue(json.loads((run2 / "run.json").read_text())["commits_retried"][0]["ok"])
        self.assertIn(f"{PN}/{SASA}.md", git(self.zk, "log", "--name-only", "--format=").stdout)

    def test_paths_outside_the_allowlist(self) -> None:
        for p in (self.zk / "02 Examples" / "E.md", self.zk / "Top.md", self.zk.parent / "Vault Root.md"):
            with self.assertRaises(agent.AgentError, msg=str(p)):
                agent.write_file(str(p), good_text())  # round 5: outside the read and write roots
        for p in (self.zk / PN / "sub" / "N.md", self.zk / PN / "U.MD"):
            agent.write_file(str(p), good_text())
        ev = self.finalize(0)
        self.assertTrue(all(n["action"] == "quarantined" for n in ev["notes"]), ev["notes"])

    def test_unsafe_and_duplicate_names(self) -> None:
        self.vpath(SASA).write_text(good_text())
        for name in (SASA.replace("Recommends", "Rеcommends"), SASA + ".", SASA.replace(" ", " ", 1), SASA.lower()):
            p = self.unit / "out" / PN / f"{name}.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(good_text().replace(f"# {SASA}", f"# {name}"))
        ev = self.finalize(0)
        acts = {n["note"]: (n["action"], n["failure_types"]) for n in ev["notes"]}
        self.assertTrue(all(a == "quarantined" for a, _ in acts.values()), acts)
        self.assertIn("duplicate-name", acts[f"{PN}/{SASA.lower()}.md"][1])
        self.assertEqual(len(list((self.zk / PN).glob("*.md"))), 1)

    def test_failed_rename_keeps_the_original(self) -> None:
        self.vpath("P2 Existing").write_text(OLD_NOTE.format(t="P2 Existing"))
        agent.move_file(str(self.vpath("P2 Existing")), str(self.vpath("P2 Renamed")))
        agent.append_file(str(self.vpath("P2 Renamed")), "\n- invented 77 reps per set\n")
        ev = self.finalize(0)
        self.assertTrue(self.vpath("P2 Existing").exists())
        self.assertIn("Legacy claim.", self.vpath("P2 Existing").read_text())
        self.assertFalse(ev["removals"][0]["applied"])

    def test_good_rename_leaves_a_stub_and_an_archive(self) -> None:
        old = "SasaVenos Old Title"
        self.vpath(old).write_text(good_text().replace(f"# {SASA}", f"# {old}"))
        agent.move_file(str(self.vpath(old)), str(self.vpath(SASA)))
        self.approve(SASA)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "published")
        stub = self.vpath(old).read_text()
        self.assertIn(f'superseded_by: "[[{SASA}]]"', stub)
        archived = list((self.staging / "retired").rglob(f"{old}.md"))
        self.assertEqual(len(archived), 1)
        self.assertTrue(any(r["event"] == "note-archived" for r in provenance.read_records(self.staging)))


class RepairTransaction(_Env):
    kind = "repair"

    def test_stub_without_published_target_never_overwrites(self) -> None:
        """X2: the replacement fails, so the stub is not published and the old note stays."""
        self.vpath("Old Claim").write_text(OLD_NOTE.format(t="Old Claim"))
        agent.move_file(str(self.vpath("Old Claim")), str(self.vpath(SASA)))
        agent.write_file(str(self.vpath(SASA)), bad_text())
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, "Old Claim")["action"], "quarantined")
        self.assertIn("stub-target-not-published", self.note(ev, "Old Claim")["failure_types"])
        self.assertIn("Legacy claim.", self.vpath("Old Claim").read_text())

    def test_good_repair_publishes_stub_and_archives_old(self) -> None:
        self.vpath("Old Claim").write_text(OLD_NOTE.format(t="Old Claim"))
        agent.move_file(str(self.vpath("Old Claim")), str(self.vpath(SASA)))
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.approve(SASA)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, "Old Claim")["action"], "published")
        self.assertIn("superseded_by", self.vpath("Old Claim").read_text())
        self.assertTrue(list((self.staging / "retired").rglob("Old Claim.md")))

    def test_rename_chain_ends_with_last_name_and_stub(self) -> None:
        self.vpath("A Old").write_text(OLD_NOTE.format(t="A Old"))
        agent.move_file(str(self.vpath("A Old")), str(self.vpath("B Middle")))
        agent.move_file(str(self.vpath("B Middle")), str(self.vpath(SASA)))
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.approve(SASA)
        ev = self.finalize(0)
        self.assertFalse(self.vpath("B Middle").exists())
        self.assertTrue(self.vpath(SASA).exists())
        self.assertIn(f"[[{SASA}]]", self.vpath("A Old").read_text())
        self.assertEqual({n["note"] for n in ev["notes"]}, {f"{PN}/A Old.md", f"{PN}/{SASA}.md"})


# --------------------------------------------------------------------------- #
# Two workers (B5), ends, queue
# --------------------------------------------------------------------------- #


class TwoWorkers(_Env):
    def test_second_unit_with_stale_base_is_quarantined(self) -> None:
        unit_a = self.unit
        agent.write_file(str(self.vpath(SASA)), good_text())
        unit_b = self.new_unit()
        agent.write_file(str(self.vpath(SASA)), good_text().replace("this is his opinion", "this is his own opinion"))
        self.approve(SASA, unit_a)
        self.approve(SASA, unit_b)
        self.finalize(0, unit=unit_a)
        published = self.vpath(SASA).read_text()
        ev_b = self.finalize(0, unit=unit_b)
        self.assertIn("conflict-stale-base", self.note(ev_b, SASA)["failure_types"])
        self.assertEqual(self.vpath(SASA).read_text(), published)

    def test_finalize_twice_is_refused(self) -> None:
        self.finalize(0)
        with self.assertRaises(ri.HarnessError):
            self.finalize(0)


class EndsAndQueue(_Env):
    def test_limit_keeps_passing_notes_and_marks_incomplete(self) -> None:
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.approve(SASA)
        (self.unit / "agent_result.json").write_text(json.dumps({"rc": 3, "end": "limit"}))
        ev = self.finalize(3)
        self.assertEqual((ev["end"], self.note(ev, SASA)["action"]), ("limit", "published"))
        self.assertEqual(self.queue()[0]["stage"], "incomplete")

    def test_sigkill_is_killed(self) -> None:
        self.assertEqual(self.finalize(137)["end"], "killed")

    def test_transient_error_counts_no_attempt(self) -> None:
        (self.unit / "agent.log").write_text("deepseek_agent error: DeepSeek API HTTP 429: rate limit")
        self.finalize(1)
        self.assertNotIn("synth_attempts", self.queue()[0])

    def test_precheck_holds_degraded_and_runs_partial(self) -> None:
        import re as _re
        lit = self.staging / "lit" / f"{VID}.md"
        lit.write_text(_re.sub(r'asr_quality: "?ok"?', 'asr_quality: "partial"', lit.read_text(), count=1))
        self.assertEqual(ri.precheck(self.staging, self.run_dir, [VID]), [VID])
        lit.write_text(lit.read_text().replace('asr_quality: "partial"', 'asr_quality: "degraded"'))
        self.assertEqual(ri.precheck(self.staging, self.run_dir, [VID]), [])
        self.assertEqual(self.queue()[0]["stage"], "held")
        self.assertTrue(self.queue()[0]["hold_reasons"])

    def test_missing_queue_is_a_clear_error(self) -> None:
        (self.staging / "queue.json").unlink()
        with self.assertRaises(ri.HarnessError):
            ri.start(self.staging, "synth", self.zk)


# --------------------------------------------------------------------------- #
# Recovery (X3, M-d)
# --------------------------------------------------------------------------- #


class Recovery(_Env):
    def test_owner_is_the_driver_process_and_live_units_are_left_alone(self) -> None:
        """X3 with the real run_lib.sh: the owner pid is the driver's $$, not a subshell."""
        script = self.tmp / "driver.sh"
        script.write_text(f'''set -u
HERE="{ZR}"; STAGING="{self.staging}"; ZK_DIR="{self.zk}"; RUN_DIR="{self.run_dir}"
. "$HERE/run_lib.sh"
u="$(zr_new_unit synth {VID} "" drv)"
printf '%s\\n%s\\n' "$$" "$u" > "{self.tmp}/unit.txt"
sleep 60
''')
        proc = subprocess.Popen(["bash", str(script)], start_new_session=True)
        try:
            for _ in range(100):
                if (self.tmp / "unit.txt").exists() and len((self.tmp / "unit.txt").read_text().splitlines()) == 2:
                    break
                time.sleep(0.1)
            pid, unit = (self.tmp / "unit.txt").read_text().splitlines()
            unit = Path(unit)
            info = json.loads((unit / "unit.json").read_text())
            self.assertEqual(int(info["owner_pid"]), proc.pid)
            self.assertEqual(int(pid), proc.pid)
            (unit / "out" / PN).mkdir(parents=True)
            (unit / "out" / PN / f"{SASA}.md").write_text(good_text())
            self.approve(SASA, unit)
            run2 = ri.start(self.staging, "synth", self.zk)
            self.assertEqual(json.loads((run2 / "run.json").read_text())["recovered_units"], [])
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        run3 = ri.start(self.staging, "synth", self.zk)
        rec = [r for r in json.loads((run3 / "run.json").read_text())["recovered_units"] if r["unit"] == str(unit)][0]
        self.assertEqual(rec["published"], [])  # a crashed unit's output is not trusted
        self.assertIn(f"{PN}/{SASA}.md", rec["quarantined"])
        self.assertFalse(self.vpath(SASA).exists())
        self.assertEqual(self.queue()[0]["stage"], "incomplete")

    def test_pid_reuse_is_detected(self) -> None:
        self.assertFalse(ri.owner_alive({"owner_pid": os.getpid(), "owner_start": "1"}))
        self.assertTrue(ri.owner_alive({"owner_pid": os.getpid(), "owner_start": ri.proc_start(os.getpid())}))

    def test_crash_after_publish_keeps_the_note_and_commits_it(self) -> None:
        """M-d: the note was renamed into the vault, then the process died."""
        git(self.zk, "init", "-q")
        git(self.zk, "config", "user.email", "t@example.invalid")
        git(self.zk, "config", "user.name", "t")
        text = good_text()
        (self.unit / "out" / PN).mkdir(parents=True)
        (self.unit / "out" / PN / f"{SASA}.md").write_text(text)
        published = text.replace("verification: unverified", "verification: source-checked")
        sha = ri._sha256_bytes(published.encode())
        (self.unit / "publish.jsonl").write_text(json.dumps({"phase": "intent", "rel": f"{PN}/{SASA}.md", "sha256": sha}) + "\n")
        self.vpath(SASA).write_text(published)
        info = json.loads((self.unit / "unit.json").read_text())
        info["owner_pid"] = 999999999
        (self.unit / "unit.json").write_text(json.dumps(info))
        run2 = ri.start(self.staging, "synth", self.zk)
        rec = json.loads((run2 / "run.json").read_text())["recovered_units"][0]
        self.assertEqual(rec["published"], [f"{PN}/{SASA}.md"])
        self.assertTrue(self.vpath(SASA).exists())
        events = [r["event"] for r in provenance.read_records(self.staging) if r.get("note") == f"{PN}/{SASA}.md"]
        self.assertNotIn("note-quarantined", events)
        self.assertIn(f"{PN}/{SASA}.md", git(self.zk, "log", "--name-only", "--format=").stdout)


# --------------------------------------------------------------------------- #
# Review of vault notes (M-f), registry (B6, M-i), links, text units, repair lists
# --------------------------------------------------------------------------- #


class Review(_Env):
    kind = "review"
    ids: list[str] = []

    def review_unit(self, rel: str) -> Path:
        u = self.new_unit("review", [], rel)
        (u / "started").write_text("0")
        return u

    def test_review_never_removes_a_published_note(self) -> None:
        self.vpath(SASA).write_text(good_text())
        rel = f"{PN}/{SASA}.md"
        for verdict, complete in (("changed-modality", True), ("supported", False)):
            u = self.review_unit(rel)
            self.approve(rel, u, verdict=verdict, scope_complete=complete, source=self.vpath(SASA))
            ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk, review_note=rel)
            self.assertTrue(ev["review"]["needs_repair"])
            self.assertTrue(self.vpath(SASA).exists())
            self.assertIn("verification: needs-repair", self.vpath(SASA).read_text())
        self.assertIn(rel, ri.repair_candidates(self.staging, self.zk)[-1]["notes"])

    def test_transcript_in_other_staging_found_or_reported(self) -> None:
        self.vpath(SASA).write_text(good_text())
        rel = f"{PN}/{SASA}.md"
        other = self.tmp / "staging_other" / "lit"
        other.mkdir(parents=True)
        shutil.move(str(self.staging / "lit" / f"{VID}.md"), other / f"{VID}.md")
        ev = ri.finalize(self.staging, self.run_dir, self.review_unit(rel), [], 0, self.zk, review_note=rel)
        self.assertTrue(ev["review"]["transcript_missing"])
        self.assertEqual(self.vpath(SASA).read_text(), good_text())

    def test_backlinks_only_inside_permanent_notes(self) -> None:
        (self.zk / "Private Journal.md").write_text("# Private Journal\n\nprivate\n")
        self.vpath("Old Other").write_text(OLD_NOTE.format(t="Old Other"))
        unit = self.new_unit("synth", [VID])
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.approve(SASA, unit)
        with open(self.staging / "disagreements.jsonl", "a") as f:
            for to in (f"{PN}/Old Other.md", "Private Journal.md"):
                f.write(json.dumps({"from": f"{PN}/{SASA}.md", "to": to, "kind": "related", "unit": unit.name}) + "\n")
        ev = ri.finalize(self.staging, self.run_dir, unit, [VID], 0, self.zk)
        self.assertEqual([lk["applied"] for lk in ev["links"]], [True, False])
        self.assertNotIn(SASA, (self.zk / "Private Journal.md").read_text())
        self.assertIn(f"[[{SASA}]]", self.vpath("Old Other").read_text())


class Registry(unittest.TestCase):
    def test_two_stagings_need_the_registry(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            zk = tmp / "vault" / "ZK"
            (zk / PN).mkdir(parents=True)
            stg = []
            for name in ("staging_a", "staging_b"):
                s = tmp / name
                (s / "lit").mkdir(parents=True)
                (s / "queue.json").write_text('{"items": []}')
                stg.append(s)
            ri.start(stg[0], "synth", zk)
            with self.assertRaises(ri.HarnessError) as ctx:
                ri.start(stg[1], "synth", zk)
            self.assertIn("registry", str(ctx.exception))
            for s in stg:
                ri.registry_add(zk, s)
            ri.start(stg[1], "synth", zk)
            self.assertIn("staging_a/lit", (tmp / "lit_dirs.txt").read_text())
        finally:
            shutil.rmtree(tmp)


class TextUnits(_Env):
    ids = ["tw-1"]

    def test_tweet_note_publishes_with_old_rules_and_no_review(self) -> None:
        self.write_queue([{"id": "tw-1", "kind": "tweet", "stage": "claimed", "lit_note": "x"}])
        note = ("---\ntype: permanent note\ncreated: 2026-01-01\ntags: [ai, agents-design]\n---\n\n"
                "# Agents Need Small Tools\n\nSmall tools help.\n\n## Details\n\n- one\n\n## Connected Ideas\n\n- [[Other]] — map\n")
        agent.write_file(str(self.vpath("Agents Need Small Tools")), note)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, "Agents Need Small Tools")["action"], "published")


class ValidateIndex(unittest.TestCase):
    def test_vault_is_read_once_per_index(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            for i in range(5):
                (tmp / f"N{i}.md").write_text(f"---\ntype: map note\n---\n\n# N{i}\n")
            calls = []
            orig = validate.load_vault
            validate.load_vault = lambda root: calls.append(root) or orig(root)
            try:
                idx = validate.VaultIndex(tmp)
                for i in range(5):
                    validate.check(tmp, [Path(f"N{i}.md")], index=idx)
            finally:
                validate.load_vault = orig
            self.assertEqual(len(calls), 1)
        finally:
            shutil.rmtree(tmp)


class Round4Pilot(_Env):
    """Findings of the real-model pilot."""

    def test_abandoned_draft_is_discarded_not_quarantined(self) -> None:
        q = self.queue()
        q[0]["stage"] = "synthesized"
        self.write_queue(q)
        agent.write_file(str(self.vpath("Old Draft")), bad_text().replace(f"# {SASA}", "# Old Draft"))
        agent.write_file(str(self.vpath(SASA)), good_text())
        r = subprocess.run([sys.executable, str(ZR / "queue_mark.py"), "--ids", VID, "--stage", "synthesized",
                            "--notes", SASA], env=dict(os.environ), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.approve(SASA)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, "Old Draft")["action"], "discarded-draft")
        self.assertEqual(self.note(ev, SASA)["action"], "published")
        self.assertEqual(self.queue()[0]["stage"], "synthesized")  # no retry, no attempt
        self.assertNotIn("synth_attempts", self.queue()[0])
        s = ri.summary(self.run_dir, self.staging)
        self.assertEqual((s["notes_quarantined"], len(s["discarded_drafts"])), (0, 1))

    def test_without_a_note_list_drafts_stay_quarantined(self) -> None:
        agent.write_file(str(self.vpath("Old Draft")), bad_text().replace(f"# {SASA}", "# Old Draft"))
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, "Old Draft")["action"], "quarantined")

    def test_delete_draft_tool(self) -> None:
        agent.write_file(str(self.vpath("Draft")), "x")
        agent.delete_draft(str(self.vpath("Draft")))
        self.assertFalse((self.unit / "out" / PN / "Draft.md").exists())
        self.vpath("In Vault").write_text("x")
        with self.assertRaises(agent.AgentError):
            agent.delete_draft(str(self.vpath("In Vault")))

    def test_limit_with_no_output_is_named(self) -> None:
        (self.unit / "agent_result.json").write_text(json.dumps(
            {"rc": 3, "end": "limit", "detail": "DeepSeek response hit max_tokens before finishing (DEEPSEEK_MAX_TOKENS=12000)."}))
        ev = self.finalize(3)
        self.assertEqual(ev["end"], "limit-no-output")
        s = ri.summary(self.run_dir, self.staging)
        self.assertEqual(s["runs_ended_by_limit_no_output"], 1)
        self.assertIn("DEEPSEEK_MAX_TOKENS", s["hints"][0])

    def test_dead_link_fails(self) -> None:
        agent.write_file(str(self.vpath(SASA)), good_text().replace("## Connected Ideas\n",
                                                                     "## Connected Ideas\n\n- [[No Such Note Anywhere]] — x\n"))
        self.approve(SASA)
        ev = self.finalize(0)
        self.assertIn("dead-link", self.note(ev, SASA)["failure_types"])

    def test_link_kind_rules(self) -> None:
        self.vpath(RADO).write_text(good_text(RADO))
        agent.write_file(str(self.vpath(SASA)), good_text())
        env = dict(os.environ)

        def link(src: str, dst: str, kind: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(ZR / "note_link.py"), "--from", f"{PN}/{src}.md",
                                   "--to", f"{PN}/{dst}.md", "--kind", kind], env=env, capture_output=True, text=True)
        self.assertEqual(link(SASA, RADO, "supersedes-candidate").returncode, 2)  # target has sources
        self.assertEqual(link(SASA, RADO, "disagreement").returncode, 2)  # no ## Disagreement in SASA
        self.assertEqual(link(SASA, RADO, "related").returncode, 0)
        self.approve(SASA)
        ev = self.finalize(0)
        self.assertTrue(ev["links"][0]["applied"])
        self.assertIn(f"- Related newer note: [[{SASA}]]", self.vpath(RADO).read_text())

    def test_usage_and_checker_hash_are_recorded(self) -> None:
        (self.unit / "agent_result.json").write_text(json.dumps({"rc": 0, "end": "finished", "usage": {
            "calls": 2, "prompt_tokens": 1000, "completion_tokens": 300, "reasoning_tokens": 200, "cost": 0.01}}))
        (self.unit / "review-0").mkdir()
        (self.unit / "review-0" / "agent_result.json").write_text(json.dumps({"usage": {"calls": 1, "cost": 0.005}}))
        self.finalize(0)
        s = ri.summary(self.run_dir, self.staging)
        self.assertEqual((s["usage"]["calls"], s["usage"]["cost"]), (3, 0.015))
        self.assertIn("verify_claims.py", s["checker_sha256"])
        agent._add_usage({"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.1,
                          "completion_tokens_details": {"reasoning_tokens": 1}})
        self.assertEqual(agent.RUN_STATE["usage"]["reasoning_tokens"], 1)

    def test_tool_results_are_logged_without_secrets(self) -> None:
        agent.write_file(str(self.staging / "STATE.md"), "fine")
        agent.run_tool({"function": {"name": "read_file", "arguments": json.dumps({"path": str(self.staging / "STATE.md")})}})
        last = json.loads((self.unit / "tools.jsonl").read_text().splitlines()[-1])
        self.assertIn("fine", last["result"])
        self.assertLessEqual(len(last["result"]), 2010)

    def test_help_is_allowed_and_dash_c_names_the_helpers(self) -> None:
        for h in ("verify_claims.py", "index_add.py", "queue_mark.py", "index_query.py", "note_link.py", "validate.py"):
            r = agent.run_command("python3", [h, "--help"])
            self.assertEqual(r["returncode"], 0, h)
        with self.assertRaises(agent.AgentError) as ctx:
            agent.run_command("python3", ["-c", "print(1)"])
        self.assertIn("verify_claims.py", str(ctx.exception))

    def test_verdict_file_with_small_deviations(self) -> None:
        agent.write_file(str(self.vpath(SASA)), good_text())
        # The reviewer fills the skeleton; letter case, spaces and extra keys do not matter.
        sk = ri.review_skeleton(f"{PN}/{SASA}.md", (self.unit / "out" / PN / f"{SASA}.md").read_text())
        for k, it in enumerate(sk["items"]):
            it["verdict"] = ("Supported", "SUPPORTED", " supported ")[k % 3]
            it["extra"] = 1
        sk["scope_complete"] = "true"
        self.approve(SASA, payload=sk)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "published")

    def test_unusable_review_stays_pending_and_is_never_quarantined(self) -> None:
        """Round 6 (pilot 2): a review without a usable verdict leaves the note pending
        (never `review-unusable` quarantine, the item is not sent back to synthesis). After
        ZR_MAX_PENDING_CYCLES (5) cycles the folder stays for the operator."""
        q = self.queue()
        q[0]["stage"] = "synthesized"
        self.write_queue(q)
        agent.write_file(str(self.vpath(SASA)), good_text())
        sk = ri.review_skeleton(f"{PN}/{SASA}.md", (self.unit / "out" / PN / f"{SASA}.md").read_text())
        for it in sk["items"]:
            it["verdict"] = "looks fine"
        sk["scope_complete"] = True
        self.approve(SASA, payload=sk)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "pending-review")
        self.assertIn("unknown verdict", self.note(ev, SASA)["review_unusable"])
        for _cycle in range(2, ri.MAX_PENDING_CYCLES + 1):
            units = ri.pending_units(self.staging, self.run_dir, os.getpid())
            self.assertEqual(len(units), 1)
            ev = ri.finalize(self.staging, self.run_dir, Path(units[0]), [], 0, self.zk)
            self.assertEqual(self.note(ev, SASA)["action"], "pending-review")
            self.assertEqual(self.queue()[0]["stage"], "pending-review")
        self.assertEqual(ri.pending_units(self.staging, self.run_dir, os.getpid()), [])
        self.assertEqual(len(ri._operator_pending(self.staging)), 1)
        self.assertEqual(list((self.staging / "quarantine").rglob("*.md")), [])


class ShellScripts(unittest.TestCase):
    SCRIPTS = ("loop.sh", "loop_parallel.sh", "parallel_synth.sh", "parallel_review.sh", "parallel_worker.sh",
               "review_loop.sh", "run_lib.sh", "worker.sh", "review_worker.sh", "review_parallel.sh", "run.sh",
               "run_thdxr_6mo.sh", "run_transcripts.sh", "run_transcripts_parallel.sh", "repair_loop.sh")

    def test_parse_no_whole_vault_add_no_direct_agent_call(self) -> None:
        for name in self.SCRIPTS:
            text = (ZR / name).read_text(encoding="utf-8")
            self.assertEqual(subprocess.run(["bash", "-n", str(ZR / name)]).returncode, 0, name)
            self.assertNotIn("add -A\n", text, name)
            if name != "run_lib.sh":
                for line in text.splitlines():
                    if "deepseek_agent.py" in line and not line.lstrip().startswith("#"):
                        self.assertNotRegex(line, r"python3?\s|AGENT\[@\]", f"{name}: {line}")
        self.assertNotIn("BASHPID", (ZR / "run_lib.sh").read_text())

    def test_driver_defaults(self) -> None:
        lib = (ZR / "run_lib.sh").read_text()
        self.assertIn('DEEPSEEK_MAX_TOKENS="${DEEPSEEK_MAX_TOKENS:-32000}"', lib)
        self.assertIn('AGENT_TIMEOUT="${AGENT_TIMEOUT:-1800}"', lib)
        for name in self.SCRIPTS:
            text = (ZR / name).read_text()
            self.assertNotIn("12000", text, name)
            self.assertNotIn("16000", text, name)
            self.assertNotIn("v4.1-flash", text, name)

    def test_wrappers_stop_before_review_after_failed_synthesis(self) -> None:
        for name in ("run.sh", "run_thdxr_6mo.sh", "run_transcripts.sh", "run_transcripts_parallel.sh"):
            text = (ZR / name).read_text(encoding="utf-8")
            self.assertRegex(text, r"exit \"\$(brc|prc)\"", name)


FAKE_AGENT = r'''
import json, os, re, sys
from pathlib import Path
sys.path.insert(0, os.environ["HERE"])
import deepseek_agent as da
prompt = sys.stdin.read()
pn = Path(os.environ["ZK_DIR"]) / "01 Permanent Notes"
fix = Path(os.environ["FIX_NOTES"])
mode = os.environ.get("FAKE_MODE", "main")
def tool(name, **kw):
    out, _ = da.run_tool({"function": {"name": name, "arguments": json.dumps(kw)}})
    return json.loads(out)
if "source-check" in prompt:
    m = re.search(r"exactly this file: \\?\n?(\S.*verdict\.json)", prompt)
    title = re.search(r'single note: "[^"]*/01 Permanent Notes/([^"]+)\.md"', prompt, re.I).group(1)
    roles = json.loads(os.environ["ROLES"])
    role = roles.get(title, "A")
    if mode == "approve-all":
        role = "A"
    if role != "C":
        v = "supported" if role == "A" else "changed-modality"
        sk = json.loads(prompt.split("Skeleton:\n", 1)[1].split("\nHints from", 1)[0])
        for k, it in enumerate(sk["items"]):
            it["verdict"] = v if k == 0 else "supported"
        sk["scope_complete"] = True
        tool("write_file", path=m.group(1).strip(), content=json.dumps(sk))
    Path(os.environ["ZR_RUN_DIR"], "agent_result.json").write_text(json.dumps({"rc": 0, "end": "finished"}))
    sys.exit(0)
if "FIX TURN" in prompt:
    tool("write_file", path=str(pn / "Fixable.md"), content=(fix / "fixable_good.md").read_text())
    sys.exit(0)
if "vid-5zALUKd7h3g" in prompt and mode == "main":
    for p in sorted(fix.glob("v5_*.md")):
        t = p.read_text()
        tool("write_file", path=str(pn / (re.search(r"^# (.+)$", t, re.M).group(1) + ".md")), content=t)
    tool("write_file", path=str(pn / "Fixable.md"), content=(fix / "fixable_bad.md").read_text())
    tool("run_command", command="python3", args=["queue_mark.py", "--ids", "vid-5zALUKd7h3g", "--stage", "synthesized"])
elif "tw-1" in prompt:
    note = ("---\ntype: permanent note\ncreated: 2026-01-01\ntags: [ai, agents-design]\n---\n\n"
            "# Agents Need Small Tools\n\nSmall tools help.\n\n## Details\n\n- one\n\n## Connected Ideas\n\n- [[Other]] — map\n")
    tool("write_file", path=str(pn / "Agents Need Small Tools.md"), content=note)
    tool("run_command", command="python3", args=["queue_mark.py", "--ids", "tw-1", "--stage", "synthesized"])
'''


FAKE_REVIEW_LLM = r'''
import json, os, re, sys
body = json.load(sys.stdin)

def item_marked(name, user=None):
    import re as _re
    user = user if user is not None else body["messages"][1]["content"]
    imap = {}
    for m in _re.finditer(r"^- (\S+) -> (.*)$", user.split("ITEM PASSAGES", 1)[-1].split("CHECKER HINTS", 1)[0], _re.M):
        imap[m.group(1)] = [int(x) for x in _re.findall(r"EVIDENCE (\d+)", m.group(2))]
    blocks = {}
    starts = [(int(m.group(1)), m.start()) for m in _re.finditer(r"(?m)^\[EVIDENCE (\d+)", user)]
    for k, (n_, s_) in enumerate(starts):
        blocks[n_] = user[s_:(starts[k + 1][1] if k + 1 < len(starts) else len(user))]
    out = [x for n_ in imap.get(name, []) for x in _re.findall(r"^>>> (.*?) <<<$", blocks.get(n_, ""), _re.M)]
    return out or _re.findall(r"^>>> (.*?) <<<$", user, _re.M)
user = body["messages"][1]["content"]
title = re.search(r"^NOTE \(01 Permanent Notes/(.+)\.md\):", user, re.M).group(1)
sk = json.loads(user.split("Reply with this JSON object only:\n", 1)[1])
role = json.loads(os.environ.get("ROLES", "{}")).get(title, "A")
if os.environ.get("FAKE_MODE") == "approve-all":
    role = "A"
if role == "C":
    content = "I could not decide."  # no JSON: invalid, retried, then pending
else:
    marked = re.findall(r"^>>> (.*?) <<<$", user, re.M)
    for k, it in enumerate(sk["items"]):
        it["verdict"] = "changed-modality" if (role == "B" and k == 0) else "supported"
        marked = item_marked(it["item"], user)
        it["transcript_text"] = marked[0][:120].rsplit(" ", 1)[0] if marked else ""
    sk["scope_complete"] = True
    content = json.dumps(sk)
print(json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}],
                  "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.001}}))
'''


class LoopEndToEnd(unittest.TestCase):
    """loop.sh with a fake agent that uses the real tool functions: mechanical check with a
    fix turn, source-check review (supported / changed-modality / no verdict), pending
    review in the next run, a tweet unit, and git commits of published paths only."""

    def test_loop(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="e2e-"))
        try:
            stg, zk = tmp / "stg", tmp / "vault" / "ZK"
            (stg / "lit").mkdir(parents=True)
            shutil.copy(HONEST / "lit" / "vid-5zALUKd7h3g.md", stg / "lit")
            (zk / PN).mkdir(parents=True)
            (zk / "Other.md").write_text("---\ntype: map note\n---\n\n# Other\n")
            seed_link_targets(zk)
            notes = tmp / "notes"
            notes.mkdir()
            v5 = sorted(p for p in (HONEST / "notes").glob("*.md") if "id: vid-5zALUKd7h3g" in p.read_text())
            roles = {}
            for role, p in zip("ABC", v5):
                shutil.copy(p, notes / f"v5_{role}.md")
                roles[p.stem] = role
            fixable = v5[0].read_text().replace(f"# {v5[0].stem}", "# Fixable")
            (notes / "fixable_good.md").write_text(fixable)
            ev_start = fixable.index("## Evidence")
            q = fixable.index('"', ev_start) + 1
            (notes / "fixable_bad.md").write_text(fixable[:q] + "zzz " + fixable[q:])  # quote not in transcript
            roles["Fixable"] = "A"
            (tmp / "fake.py").write_text(FAKE_AGENT)
            (tmp / "fake_review.py").write_text(FAKE_REVIEW_LLM)
            (stg / "queue.json").write_text(json.dumps({"version": 1, "items": [
                {"id": "vid-5zALUKd7h3g", "kind": "video", "stage": "extracted", "lit_note": str(stg / "lit" / "vid-5zALUKd7h3g.md")},
                {"id": "tw-1", "kind": "tweet", "stage": "extracted", "lit_note": "x"}]}))
            for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
                git(zk, *cmd)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "ZK_"))}
            env.update(ZR_STATE_DIR=str(tmp / "state"), ZR_TEST_RUN="1", ZR_SYNTH_MODE="agent")  # never ~/.local/state
            env.update(VAULT=str(tmp / "vault"), ZK_FOLDER="ZK", ZR_STAGING=str(stg), ZR_AGENT_SCRIPT=str(tmp / "fake.py"),
                       FIX_NOTES=str(notes), DEEPSEEK_API_KEY=FAKE_KEY, MAX_ITERS="6", MAX_STALL="1",
                       SYNTH_BACKOFF="0", AGENT_TIMEOUT="60", ROLES=json.dumps(roles),
                       AGENTS_FILE=str(ZR / "AGENTS_transcript.md"), ZR_REVIEW_SCRIPT=str(tmp / "fake_review.py"))
            r1 = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            if os.environ.get("E2E_DEBUG"):
                Path(os.environ["E2E_DEBUG"]).write_text(r1.stdout + "\n=====\n" + r1.stderr)
            pub = {p.stem for p in (zk / PN).glob("*.md")}
            a, b, c = (p.stem for p in v5)
            # Round 18: "Fixable" is a copy of note a under another title; the narrowed merge
            # only REPORTS it (possible_duplicates), both publish.
            self.assertIn(a, pub)
            self.assertIn("Fixable", pub)  # fixed in the fix turn, then reviewed
            self.assertIn("Agents Need Small Tools", pub)
            self.assertNotIn(b, pub)  # review: changed-modality
            self.assertNotIn(c, pub)  # invalid review reply after the retries: pending review
            self.assertIn("verification: source-checked", (zk / PN / f"{a}.md").read_text())
            possible = json.loads((stg / "possible_duplicates.json").read_text())
            self.assertTrue(any({p["a"], p["b"]} == {f"{PN}/{a}.md", f"{PN}/Fixable.md"} for p in possible))
            self.assertEqual(len(list((stg / "pending-review").rglob(f"{c}.md"))), 1)
            summ = json.loads(sorted(stg.glob("runs/*/summary.json"))[-1].read_text())
            self.assertTrue(summ["notes_pending_review"])
            self.assertEqual(set(summ["pending_reasons"]), {"review-invalid"}, summ["notes_pending_review"])
            self.assertEqual(json.loads(next(stg.glob("runs/*/units/*/review-*/agent_result.json")).read_text())["kind"],
                             "review-call")
            self.assertIn("review:changed-modality", summ["verification_failures_by_type"])
            # Next run: the pending note gets its review and is published.
            env["FAKE_MODE"] = "approve-all"
            subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            self.assertTrue((zk / PN / f"{c}.md").exists())
            committed = git(zk, "log", "--name-only", "--format=").stdout
            self.assertNotIn("Other.md", committed)
            self.assertIn(f"{c}.md", committed)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
