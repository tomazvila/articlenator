"""Round 5 tests: review provenance (who wrote the verdict), verdict coverage, read roots,
note lists, pending units, driver lock, repair candidates and sources, the code-tree
guard, spoken runs, limit words and the minor items. No network, no LLM.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.unit.test_run_integrity import (
    OLD_NOTE,
    PN,
    RADO,
    SASA,
    VID,
    ZR,
    _Env,
    agent,
    good_text,
    ri,
)

import secret_scan  # noqa: E402
import verify_claims as vc  # noqa: E402

ROOT = ZR.parent


def fill(rdir: Path, verdict: str = "supported", **over) -> dict:
    sk = json.loads((rdir / "skeleton.json").read_text())
    for it in sk["items"]:
        it["verdict"] = verdict
    sk["scope_complete"] = True
    sk.update(over)
    return sk


# --------------------------------------------------------------------------- #
# B1: a verdict counts only from a harness-recorded review of exactly this text
# --------------------------------------------------------------------------- #


class WhoWroteTheVerdict(_Env):
    def setUp(self) -> None:
        super().setUp()
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.rel = f"{PN}/{SASA}.md"
        self.shadow = self.unit / "out" / self.rel

    def test_recorded_review_publishes(self) -> None:
        rdir = self.approve(SASA)
        rec = json.loads((rdir / "review.json").read_text())
        self.assertEqual((rec["by"], rec["note"], rec["agent_rc"]), ("harness", self.rel, 0))
        self.assertEqual(self.note(self.finalize(0), SASA)["action"], "published")
        self.assertIn("verification: source-checked", self.vpath(SASA).read_text())

    def test_verdict_without_harness_record_is_ignored(self) -> None:
        """A verdict file that no recorded review left (the synthesis agent, a copy, an
        old review-verdicts/ file) does not count: the note waits for a review."""
        rdir = ri.review_begin(self.unit, self.rel, self.shadow.read_bytes())
        (rdir / "verdict.json").write_text(json.dumps(fill(rdir)))  # no review-end record
        old = self.staging / "review-verdicts" / f"{self.unit.name}--{SASA}.json"
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps(fill(rdir)))
        self.assertEqual(self.note(self.finalize(0), SASA)["action"], "pending-review")
        self.assertFalse(self.vpath(SASA).exists())

    def test_note_changed_after_review(self) -> None:
        # Round 16 (6c): a change outside the reviewed content (here one more newline) keeps
        # the supported review; a change of the reviewed content needs a new review.
        self.approve(SASA)
        self.shadow.write_text(good_text() + "\n")
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "published")

    def test_reviewed_content_changed_after_review(self) -> None:
        self.approve(SASA)
        text = good_text()
        self.assertIn("He did around 10 sets of holds per week.", text)
        self.shadow.write_text(text.replace("He did around 10 sets of holds per week.",
                                            "He did around 10 sets of holds per week, always."))
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "pending-review")
        self.assertIn("changed after its review", self.note(ev, SASA)["review_unusable"])

    def test_verdict_changed_after_review_end(self) -> None:
        rdir = self.approve(SASA, verdict="changed-modality")
        (rdir / "verdict.json").write_text(json.dumps(fill(rdir)))  # all supported now
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "pending-review")
        self.assertIn("changed after the review ended", self.note(ev, SASA)["review_unusable"])

    def test_review_agent_that_failed_does_not_count(self) -> None:
        self.approve(SASA, rc=3)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "pending-review")
        self.assertIn("ended by limit", self.note(ev, SASA)["review_unusable"])

    def test_review_of_another_unit_does_not_count(self) -> None:
        """Same title, same text, other unit: no matching across units."""
        other = self.new_unit()
        (other / "out" / PN).mkdir(parents=True)
        (other / "out" / self.rel).write_bytes(self.shadow.read_bytes())
        self.approve(SASA, unit=other)
        os.environ["ZR_RUN_DIR"] = str(self.unit)
        self.assertEqual(self.note(self.finalize(0), SASA)["action"], "pending-review")

    def test_review_record_names_the_note(self) -> None:
        """A review of note A in this unit does not count for note B."""
        agent.write_file(str(self.vpath(RADO)), good_text(RADO))
        self.approve(SASA)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, RADO)["action"], "pending-review")

    def test_synthesis_agent_cannot_write_review_folders(self) -> None:
        for p in (self.unit / "review-0" / "verdict.json", self.unit / "review-0" / "review.json",
                  self.unit / "review-0" / "request.json"):
            with self.assertRaises(agent.AgentError, msg=str(p)):
                agent.write_file(str(p), "{}")


# --------------------------------------------------------------------------- #
# M2: verdict coverage
# --------------------------------------------------------------------------- #


class VerdictCoverage(_Env):
    def setUp(self) -> None:
        super().setUp()
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.rel = f"{PN}/{SASA}.md"

    def test_skeleton_lists_every_item(self) -> None:
        text = good_text()
        items = [it["item"] for it in ri.review_items(text)]
        body = vc.parse_body_full(vc.split_frontmatter(text)[1])
        n_det = len(body.sections["Details"].bullets())
        n_ev = len(body.sections["Evidence"].bullets())
        self.assertEqual(items[:3], ["title", "scope", "lead"])
        self.assertEqual([i for i in items if i.startswith("details-")], [f"details-{k}" for k in range(1, n_det + 1)])
        self.assertEqual([i for i in items if i.startswith("evidence-")], [f"evidence-{k}" for k in range(1, n_ev + 1)])
        sk = ri.review_skeleton(self.rel, text)
        self.assertEqual(set(sk["items"][0]), {"item", "text", "tag", "verdict", "also", "transcript_text", "problem"})
        self.assertIn("scope_complete", sk)

    def _run(self, mutate) -> dict:
        rdir = ri.review_begin(self.unit, self.rel, (self.unit / "out" / self.rel).read_bytes())
        sk = fill(rdir)
        mutate(sk)
        (rdir / "verdict.json").write_text(json.dumps(sk))
        (rdir / "agent_result.json").write_text(json.dumps({"rc": 0, "end": "finished"}))
        ri.review_end(rdir, 0)
        return self.note(self.finalize(0), SASA)

    def test_missing_item_is_pending(self) -> None:
        n = self._run(lambda sk: sk["items"].pop())
        self.assertEqual(n["action"], "pending-review")
        self.assertIn("missing", n["review_unusable"])

    def test_empty_verdict_is_pending(self) -> None:
        def m(sk):
            sk["items"][-1]["verdict"] = ""
        self.assertEqual(self._run(m)["action"], "pending-review")

    def test_item_not_a_dict_is_pending(self) -> None:
        def m(sk):
            sk["items"][0] = "supported"
        self.assertEqual(self._run(m)["action"], "pending-review")

    def test_scope_complete_missing_is_pending(self) -> None:
        self.assertEqual(self._run(lambda sk: sk.pop("scope_complete"))["action"], "pending-review")

    def test_bad_verdict_on_evidence_item_quarantines(self) -> None:
        def m(sk):
            next(it for it in sk["items"] if it["item"].startswith("evidence-"))["verdict"] = "changed-number"
        n = self._run(m)
        self.assertEqual(n["action"], "quarantined")
        self.assertIn("review:changed-number", n["failure_types"])

    def test_also_with_a_defect_quarantines(self) -> None:
        def m(sk):
            sk["items"][0]["also"] = ["dropped-qualifier"]
        self.assertEqual(self._run(m)["action"], "quarantined")

    def test_scope_incomplete_quarantines(self) -> None:
        self.assertEqual(self._run(lambda sk: sk.update(scope_complete=False))["action"], "quarantined")


# --------------------------------------------------------------------------- #
# B2: read roots and the scrubber
# --------------------------------------------------------------------------- #


class ReadRoots(_Env):
    def test_vault_root_parent_escape_symlink_and_other_zettelkasten(self) -> None:
        vault = self.tmp / "vault"
        (vault / "Diary.md").write_text("private")
        (vault / "Other Zettelkasten").mkdir()
        (vault / "Other Zettelkasten" / "N.md").write_text("other")
        (self.zk / PN / "Link.md").symlink_to(vault / "Diary.md")
        (self.zk / "00 Maps" / "dirlink").symlink_to(vault / "Other Zettelkasten")
        for p in (vault / "Diary.md", f"{self.zk}/../Diary.md", self.zk / PN / "Link.md",
                  self.zk / "00 Maps" / "dirlink" / "N.md", vault / "Other Zettelkasten" / "N.md",
                  self.staging / "queue.json", self.staging / "provenance.jsonl", Path.home() / ".bashrc"):
            with self.assertRaises(agent.AgentError, msg=str(p)):
                agent.read_file(str(p))
        (self.zk / "00 Maps" / "Map.md").write_text("# Map\n")
        self.assertIn("Map", agent.read_file(str(self.zk / "00 Maps" / "Map.md"))["content"])
        with self.assertRaises(agent.AgentError):
            agent.read_file(str(self.zk / "Other.md"))  # a zettelkasten root file other than Home.md

    def test_list_and_find_show_only_readable_entries(self) -> None:
        (self.zk / "03 Reviews").mkdir()
        (self.zk / "03 Reviews" / "R.md").write_text("r")
        (self.zk / "Home.md").write_text("# Home\n")
        names = {e["name"] if isinstance(e, dict) else str(e) for e in agent.list_dir(str(self.zk))["entries"]}
        self.assertNotIn("03 Reviews", " ".join(names))
        found = json.dumps(agent.find_files(str(self.zk), "*.md"))
        self.assertNotIn("R.md", found)

    def test_scrubber_word_value_patterns(self) -> None:
        for raw in ("password: hunter2hunter", "api key = abcdef123456", "Token: zzzzzzzz9", "bearer abcdefghijklmnop",
                    "client secret: s3cr3tvalue"):
            self.assertIn("[redacted]", secret_scan.scrub(f"x {raw} y"), raw)
            self.assertTrue(secret_scan.find_secrets(f"x {raw} y"), raw)
        for fine in ("a token based approach", "the secret is good form", "my-sk-1234 slug", "task-force-ab12cd34ef56gh78ij90kl"):
            self.assertEqual(secret_scan.scrub(fine), fine)

    def test_secret_env_names(self) -> None:
        for name in ("OPENAI_API_KEY", "MY_KEY", "GH_TOKEN", "KEY_FILE_PASSWORD", "DB_PASSWD", "AWS_SECRET_ACCESS_KEY"):
            self.assertTrue(secret_scan.SECRET_NAME.search(name), name)
        for name in ("KEYBOARD", "MONKEY_COUNT"):
            self.assertFalse(secret_scan.SECRET_NAME.search(name), name)


# --------------------------------------------------------------------------- #
# M3: cumulative note list; drafts
# --------------------------------------------------------------------------- #


class NoteList(_Env):
    def mark(self, *a: str) -> None:
        r = subprocess.run([sys.executable, str(ZR / "queue_mark.py"), "--ids", VID, "--stage", "synthesized", *a],
                           env=dict(os.environ), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def listed(self) -> list[str]:
        return json.loads((self.unit / "marked.json").read_text())["notes"]

    def test_union_and_replace(self) -> None:
        self.mark("--notes", "A")
        self.mark("--notes", "B;A")
        self.assertEqual(self.listed(), ["A", "B"])
        self.mark("--notes", "C", "--notes-replace")
        self.assertEqual(self.listed(), ["C"])

    def test_edit_of_existing_note_is_never_a_draft(self) -> None:
        self.vpath("Old Other").write_text(OLD_NOTE.format(t="Old Other"))
        agent.write_file(str(self.vpath(SASA)), good_text())
        agent.write_file(str(self.vpath("Old Other")), OLD_NOTE.format(t="Old Other").replace("Legacy", "Edited"))
        self.mark("--notes", SASA)
        self.approve(SASA)
        ev = self.finalize(0)
        self.assertNotEqual(self.note(ev, "Old Other")["action"], "discarded-draft")

    def test_missing_listed_note_leaves_the_item_incomplete(self) -> None:
        agent.write_file(str(self.vpath(SASA)), good_text())
        agent.write_file(str(self.vpath(RADO)), good_text(RADO))
        self.mark("--notes", f"{SASA};Never Written")
        self.approve(SASA)
        self.approve(RADO)
        ev = self.finalize(0)
        self.assertEqual(ev["end"], "incomplete-output")
        self.assertEqual(self.queue()[0]["stage"], "incomplete")
        self.assertNotEqual(self.note(ev, RADO)["action"], "discarded-draft")


# --------------------------------------------------------------------------- #
# M5: a pending unit keeps its kind and options
# --------------------------------------------------------------------------- #


class PendingKeepsKind(_Env):
    kind = "repair"

    def test_pending_repair_stays_repair(self) -> None:
        self.unit = self.new_unit("repair", [])
        info = json.loads((self.unit / "unit.json").read_text())
        info["options"] = {"allow_other_sources": True}
        (self.unit / "unit.json").write_text(json.dumps(info))
        self.vpath("Old Claim").write_text(OLD_NOTE.format(t="Old Claim"))
        agent.move_file(str(self.vpath("Old Claim")), str(self.vpath(SASA)))
        agent.write_file(str(self.vpath(SASA)), good_text())
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "pending-review")
        units = ri.pending_units(self.staging, self.run_dir, os.getpid())
        u = Path(units[0])
        info = json.loads((u / "unit.json").read_text())
        self.assertEqual((info["kind"], info["pending"], info["options"]), ("repair", True, {"allow_other_sources": True}))
        self.approve(SASA, unit=u)
        ev2 = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.assertEqual(self.note(ev2, SASA)["action"], "published")
        self.assertEqual(self.note(ev2, "Old Claim")["action"], "published")  # the stub follows (repair rules)

    def test_repair_loop_reviews_pending_notes(self) -> None:
        self.assertIn("zr_pending_reviews", (ZR / "repair_loop.sh").read_text())


# --------------------------------------------------------------------------- #
# M8: one driver per staging; atomic serial claim
# --------------------------------------------------------------------------- #


class DriverLock(unittest.TestCase):
    def test_two_drivers_on_one_staging(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="lock-"))
        try:
            script = (f'HERE="{ZR}"; STAGING="{tmp}"; . "{ZR}/run_lib.sh"; zr_driver_lock; '
                      'echo locked; sleep "${HOLD:-0}"')
            first = subprocess.Popen(["bash", "-c", script], env={**os.environ, "HOLD": "5"},
                                     stdout=subprocess.PIPE, text=True)
            self.assertEqual(first.stdout.readline().strip(), "locked")
            second = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
            self.assertEqual(second.returncode, 75)
            self.assertIn("another driver runs on staging", second.stderr)
            first.wait(timeout=20)
            third = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
            self.assertEqual(third.returncode, 0, third.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_every_driver_takes_the_lock(self) -> None:
        text = (ZR / "run_lib.sh").read_text()
        self.assertIn("zr_driver_lock\n", text.split("zr_run_start() {", 1)[1].split("\n}", 1)[0])
        for name in ("loop.sh", "loop_parallel.sh", "parallel_worker.sh", "parallel_synth.sh", "repair_loop.sh",
                     "review_loop.sh", "parallel_review.sh", "review_worker.sh"):
            self.assertIn("zr_run_start", (ZR / name).read_text(), name)

    def test_queue_next_claims_atomically(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="qn-"))
        try:
            items = [{"id": f"v{i}", "kind": "video", "stage": "extracted", "lit_note": "x"} for i in range(3)]
            (tmp / "queue.json").write_text(json.dumps({"items": items}))
            env = {**os.environ, "ZR_STAGING": str(tmp)}

            def run(*a: str) -> dict:
                return json.loads(subprocess.run([sys.executable, str(ZR / "queue_next.py"), *a], env=env,
                                                 capture_output=True, text=True, check=True).stdout)
            ids = [run()["ids"][0], run()["ids"][0]]
            self.assertEqual(ids, ["v0", "v1"])
            q = json.loads((tmp / "queue.json").read_text())["items"]
            self.assertEqual([it["stage"] for it in q], ["claimed", "claimed", "extracted"])
            self.assertEqual(run("--release")["released"], ["v0", "v1"])
            q = json.loads((tmp / "queue.json").read_text())["items"]
            self.assertEqual({it["stage"] for it in q}, {"extracted"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# M9 / M11: repair candidates and known sources
# --------------------------------------------------------------------------- #


class RepairLists(_Env):
    def test_candidates_only_old_format_and_needs_repair_once(self) -> None:
        self.vpath("Old A").write_text(OLD_NOTE.format(t="Old A"))
        self.vpath("Checked").write_text(good_text().replace("verification: unverified", "verification: source-checked")
                                         if "verification: unverified" in good_text() else good_text())
        self.vpath("Stub").write_text('---\ntype: permanent note\nstatus: superseded\nsuperseded_by: "[[Old A]]"\n---\n\n# Stub\n\n[[Old A]]\n')
        self.vpath("Broken").write_text(OLD_NOTE.format(t="Broken").replace("verification: unverified", "verification: needs-repair"))
        notes = [f"{PN}/{t}.md" for t in ("Old A", "Checked", "Stub", "Broken")]
        self.write_queue([{"id": VID, "kind": "video", "stage": "synthesized", "transcript_replaced_at": "x",
                           "published_notes": notes},
                          {"id": "vid-two", "kind": "video", "stage": "synthesized", "transcript_replaced_at": "x",
                           "published_notes": notes}])
        out = ri.repair_candidates(self.staging, self.zk)
        listed = [n for g in out for n in g["notes"]]
        self.assertEqual(sorted(listed), sorted([f"{PN}/Old A.md", f"{PN}/Broken.md"]))
        self.assertEqual(len(listed), len(set(listed)))
        excl = {e["note"]: e["state"] for e in out[0]["excluded"]}
        self.assertEqual(excl[f"{PN}/Stub.md"], "stub")

    def _git_vault(self) -> None:
        def g(*a):
            subprocess.run(["git", "-C", str(self.zk), *a], check=True, capture_output=True,
                           env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        g("init", "-q")
        self.vpath("Old A").write_text(OLD_NOTE.format(t="Old A"))
        g("add", "-A")
        g("commit", "-q", "-m", "zettel: initial notes (12 from krqBQjUydGY)")
        self.vpath("Old B").write_text(OLD_NOTE.format(t="Old B"))
        g("add", "-A")
        g("commit", "-q", "-m", "zettel: synthesis iter 2 (23 done)")

    def test_known_sources_from_git_history(self) -> None:
        self._git_vault()
        res = ri.known_sources_build(self.staging, self.zk)
        self.assertEqual(res["notes"][f"{PN}/Old A.md"]["sources"], [VID])
        self.assertEqual(res["notes"][f"{PN}/Old B.md"]["sources"], [])
        self.assertEqual(ri.repair_sources(self.staging, f"{PN}/Old A.md", self.zk), [VID])

    def test_missing_transcript_defaults_to_unsupported(self) -> None:
        self._git_vault()
        ri.known_sources_build(self.staging, self.zk)
        (self.staging / "lit" / f"{VID}.md").unlink()
        plan = ri.repair_plan(self.staging, self.zk, f"{PN}/Old A.md")
        self.assertTrue(plan["mark_unsupported"])
        self.assertEqual(plan["reason"], "source transcript missing")
        self.assertFalse(ri.repair_plan(self.staging, self.zk, f"{PN}/Old A.md", allow_other=True)["mark_unsupported"])
        unit = self.new_unit("repair", [])
        ri.mark_unsupported(unit, self.zk, f"{PN}/Old A.md", "source transcript missing")
        before = self.vpath("Old A").read_text()
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
        # Z1/Z11 (round 23): repair never writes over an old note that the pipeline did not
        # publish; the driver lists the note instead (note-unsupported), the old note stays
        self.assertEqual(self.note(ev, "Old A")["action"], "quarantined", ev["notes"])
        self.assertIn("title-equals-old", self.note(ev, "Old A")["failure_types"])
        self.assertEqual(self.vpath("Old A").read_text(), before)

    def test_evidence_from_other_videos_is_refused_without_flag(self) -> None:
        self.vpath("Old A").write_text(OLD_NOTE.format(t="Old A").replace("created:", "sources:\n  - id: vid-gone\n    speaker: X\ncreated:"))
        for allow in (False, True):
            unit = self.new_unit("repair", [])
            if allow:
                info = json.loads((unit / "unit.json").read_text())
                info["options"] = {"allow_other_sources": True}
                (unit / "unit.json").write_text(json.dumps(info))
            agent.write_file(str(self.vpath("Old A")), good_text().replace(f"# {SASA}", "# Old A"))
            ck = ri._Checker(self.staging, unit, self.zk, "repair", set())
            res = ck.check_file(unit / "out" / PN / "Old A.md", {})
            types = {f["type"] for f in res["failures"]}
            self.assertEqual("other-source-evidence" in types, not allow, types)


# --------------------------------------------------------------------------- #
# M10: the code tree stays unchanged
# --------------------------------------------------------------------------- #


class CodeTree(unittest.TestCase):
    def test_start_refuses_a_staging_in_the_code_tree(self) -> None:
        self.assertEqual(os.environ.get("ZR_TEST_RUN"), "1")
        with self.assertRaises(ri.HarnessError):
            ri.start(ZR / "staging_test_should_not_exist", "review")
        self.assertFalse((ZR / "staging_test_should_not_exist").exists())

    def test_state_is_outside_the_code_tree(self) -> None:
        self.assertNotIn(str(ZR), str(ri.state_dir()))
        tmp = Path(tempfile.mkdtemp(prefix="st-"))
        try:
            self.assertEqual(ri.lock_file(tmp / "a", tmp / "zk"), ri.lock_file(tmp / "b", tmp / "zk"))
            self.assertTrue(str(ri.lock_file(tmp / "a", tmp / "zk")).startswith(str(ri.state_dir())))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_guard_hash_sees_a_new_file(self) -> None:
        from tests.unit.conftest import tree_hash
        tmp = Path(tempfile.mkdtemp(prefix="th-"))
        try:
            (tmp / "a.txt").write_text("a")
            h1 = tree_hash(tmp)
            (tmp / "b.txt").write_text("b")
            self.assertNotEqual(h1, tree_hash(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# M6 / M7 and number minors
# --------------------------------------------------------------------------- #


def verdict(claim: str, quote: str) -> str:
    return vc.match_quantity(vc.extract_quantities(claim)[-1], vc.extract_quantities(quote))[0]


class SpokenRuns(unittest.TestCase):
    def test_range_needs_every_value(self) -> None:
        self.assertEqual(verdict("6 to 10 seconds", "6, 7, 8, 9 seconds, 10 seconds"), "supported")
        self.assertEqual(verdict("6 to 10 seconds", "6, 7, 8, 9 or 10 seconds"), "supported")
        self.assertEqual(verdict("6 to 10 seconds", "6 to 10 seconds"), "supported")
        self.assertEqual(verdict("50 or 60 push-ups", "50 60 push-ups"), "supported")
        self.assertEqual(verdict("3 to 4 sets", "3 4 sets"), "supported")

    def test_widened_narrowed_or_skipping_fails(self) -> None:
        self.assertNotEqual(verdict("50 to 60 push-ups", "50 60 push-ups"), "supported")
        self.assertNotEqual(verdict("6 to 10 seconds", "6, 8, 10 seconds"), "supported")
        self.assertNotEqual(verdict("5 to 10 seconds", "6, 7, 8, 9 or 10 seconds"), "supported")
        self.assertNotEqual(verdict("6 to 9 seconds", "6, 7, 8, 9 or 10 seconds"), "supported")
        self.assertNotEqual(verdict("5 or 7 seconds", "5 to 7 seconds"), "supported")


class LimitWords(unittest.TestCase):
    def test_limit_as_noun_or_adjective_is_not_a_qualifier(self) -> None:
        for t in ("only one limit session at four sessions per week", "go to your limit in 2 sessions",
                  "limit sessions at four sessions per week"):
            self.assertTrue(all("upper" not in q.quals for q in vc.extract_quantities(t)), t)

    def test_limit_as_verb_is_a_qualifier(self) -> None:
        for t in ("limit your sets to 3 per session", "capped at 3 sets", "Limits Banded Volume To 30 Seconds"):
            self.assertIn("upper", vc.extract_quantities(t)[0].quals, t)


class NumberMinors(unittest.TestCase):
    def test_years_are_not_merged(self) -> None:
        q = vc.extract_quantities("In 2024 5 athletes did it")
        self.assertEqual([x.values for x in q], [(2024.0,), (5.0,)])
        self.assertEqual(len(vc.extract_quantities("in 2023 2024 it changed")), 2)

    def test_quantities_missing_ignores_labels(self) -> None:
        for t in ("rule number one is to lower slowly to the 90 degree position", "step 3 is the hardest",
                  "the 3rd set is the hardest", "In 2024 he changed the plan"):
            self.assertEqual(vc.counted_quantities(t), [], t)
        self.assertEqual(len(vc.counted_quantities("hold 90 degrees for 10 seconds")), 2)

    def test_duplicate_frontmatter_key_fails(self) -> None:
        self.assertEqual(vc.duplicate_keys("---\na: 1\nunsupported_reason: x\nunsupported_reason: y\n---\n"),
                         ["unsupported_reason"])


# --------------------------------------------------------------------------- #
# Minor items of finalize and the drivers
# --------------------------------------------------------------------------- #


class FinalizeMinors(_Env):
    def test_unsafe_name_only_for_new_notes(self) -> None:
        name = "Old Note With A Colon: Inside"
        self.vpath(name).write_text(OLD_NOTE.format(t=name))
        self.assertIsNotNone(ri._name_problem(name))
        unit = self.new_unit("repair", [])
        agent.write_file(str(self.vpath(name)), OLD_NOTE.format(t=name).replace("Legacy claim.", "Legacy claim, edited."))
        ck = ri._Checker(self.staging, unit, self.zk, "repair", set())
        res = ck.check_file(unit / "out" / PN / f"{name}.md", {})
        self.assertNotIn("unsafe-name", {f["type"] for f in res["failures"]})

    def test_repair_drops_dead_old_links(self) -> None:
        text = good_text().replace("## Connected Ideas\n", "## Connected Ideas\n\n- [[Gone Note]] — old link\n")
        self.vpath(SASA).write_text(good_text())
        unit = self.new_unit("repair", [])
        agent.write_file(str(self.vpath(SASA)), text)
        self.approve(SASA, unit=unit)
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
        n = self.note(ev, SASA)
        # Z1 (round 23): no in-place write over a vault note that the pipeline did not publish
        self.assertEqual(n["action"], "quarantined", n)
        self.assertIn("title-equals-old", n["failure_types"])
        self.assertEqual(self.vpath(SASA).read_text(), good_text())

    def test_repair_that_does_nothing_is_not_success(self) -> None:
        unit = self.new_unit("repair", [])
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
        self.assertTrue(ev["no_change"])
        self.assertFalse(ev["unit_ok"])

    def test_precheck_names_unknown_ids(self) -> None:
        with self.assertRaises(ri.HarnessError) as ctx:
            ri.precheck(self.staging, self.run_dir, ["vid-not-in-queue"])
        self.assertIn("vid-not-in-queue", str(ctx.exception))

    def test_source_check_pass_needs_a_verdict(self) -> None:
        self.vpath(SASA).write_text(good_text())
        rel = f"{PN}/{SASA}.md"
        unit = ri.unit_start(self.run_dir, "review", [], rel, os.getpid(), options={"review_pass": "source-check"})
        self.assertEqual([t["rel"] for t in ri.review_targets(self.staging, unit, self.zk)], [rel])
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk, review_note=rel)
        self.assertTrue(ev["review_no_verdict"])
        unit2 = ri.unit_start(self.run_dir, "review", [], rel, os.getpid(), options={"review_pass": "source-check"})
        self.approve(rel, unit=unit2, source=self.vpath(SASA))
        ev2 = ri.finalize(self.staging, self.run_dir, unit2, [], 0, self.zk, review_note=rel)
        self.assertFalse(ev2["review_no_verdict"])
        self.assertTrue(ev2["review"]["source_checked"])

    def test_review_loop_final_validate_is_not_strict(self) -> None:
        text = (ZR / "review_loop.sh").read_text()
        self.assertNotIn("--strict", text)
        for name in ("review_loop.sh", "parallel_review.sh", "review_worker.sh", "run_lib.sh"):
            self.assertNotIn("review-verdicts", (ZR / name).read_text(), name)

    def test_agent_result_accumulates_over_fix_turns(self) -> None:
        p = self.unit / "agent_result.json"
        r1 = agent._accumulate(p, {"rc": 0, "end": "finished", "steps": 3, "elapsed_s": 1.0,
                                   "usage": {"calls": 2, "cost": 0.5}, "models_served": ["m1"]})
        p.write_text(json.dumps(r1))
        r2 = agent._accumulate(p, {"rc": 0, "end": "finished", "steps": 2, "elapsed_s": 2.0,
                                   "usage": {"calls": 1, "cost": 0.25}, "models_served": ["m2"]})
        p.write_text(json.dumps(r2))
        r3 = agent._accumulate(p, {"rc": 3, "end": "limit", "steps": 1, "elapsed_s": 1.0,
                                   "usage": {"calls": 1, "cost": 0.25}, "models_served": ["m1"]})
        self.assertEqual(r3["usage"], {"calls": 4, "cost": 1.0})
        self.assertEqual((r3["steps"], r3["elapsed_s"], r3["end"]), (6, 4.0, "limit"))
        self.assertEqual(len(r3["turns"]), 2)
        self.assertEqual(r3["models_served"], ["m1", "m2"])

    def test_passages_use_the_occurrence_near_the_marker(self) -> None:
        lit = self.staging / "lit" / "vid-twice.md"
        lit.write_text("---\nid: vid-twice\nspeakers: [A]\n---\n\n[00:10] the same words here\n"
                       + "".join(f"[{m:02d}:00] filler line {m}\n" for m in range(1, 9))
                       + "[09:00] the same words here and later context\n")
        note = self.tmp / "n.md"
        note.write_text("---\ntype: permanent note\n---\n\n# N\n\nx\n\n## Evidence\n\n"
                        '- vid-twice @ 09:00 (A): "the same words here"\n')
        out = ri.passages(self.staging, self.zk, f"{PN}/N.md", note_file=note)
        self.assertIn("@ 09:00", out)


class SelfCheck(_Env):
    def test_self_check_runs_the_finalize_checks(self) -> None:
        """M7: the documented self-check command (same command line as before) fails on
        what finalize fails on: a dead link and a note without `## Connected Ideas`."""
        lit = str(self.staging / "lit")
        agent.write_file(str(self.vpath(SASA)), good_text())
        ok = agent.run_command("python3", ["verify_claims.py", f"{PN}/{SASA}.md", "--lit", lit])
        self.assertEqual(ok["returncode"], 0, ok["stdout"][-1500:])
        dead = good_text().replace("## Connected Ideas\n", "## Connected Ideas\n\n- [[No Such Note]] — x\n")
        agent.write_file(str(self.vpath(SASA)), dead)
        bad = agent.run_command("python3", ["verify_claims.py", f"{PN}/{SASA}.md", "--lit", lit])
        self.assertEqual(bad["returncode"], 1)
        self.assertIn("dead-link", bad["stdout"])
        cut = good_text().split("## Connected Ideas")[0]
        agent.write_file(str(self.vpath(SASA)), cut)
        bad = agent.run_command("python3", ["verify_claims.py", f"{PN}/{SASA}.md", "--lit", lit])
        self.assertEqual(bad["returncode"], 1)
        self.assertIn("validate", bad["stdout"])


class HomeAndLinks(_Env):
    def test_start_creates_home(self) -> None:
        self.assertTrue((self.zk / "Home.md").is_file())
        self.assertIn("# Home", (self.zk / "Home.md").read_text())
        before = (self.zk / "Home.md").read_text()
        ri.start(self.staging, "synth", self.zk)
        self.assertEqual((self.zk / "Home.md").read_text(), before)

    def test_home_and_same_unit_links_are_not_dead(self) -> None:
        text = good_text().replace("## Connected Ideas\n", f"## Connected Ideas\n\n- [[Home]] — index\n- [[{RADO}]] — same unit\n")
        agent.write_file(str(self.vpath(SASA)), text)
        agent.write_file(str(self.vpath(RADO)), good_text(RADO))
        ck = ri._Checker(self.staging, self.unit, self.zk, "synth", {"video"})
        res = ck.check_file(self.unit / "out" / PN / f"{SASA}.md", {})
        self.assertNotIn("dead-link", {f["type"] for f in res["failures"]})


class TwoTargetStub(_Env):
    kind = "repair"

    def test_stub_publishes_only_with_all_targets(self) -> None:
        for approve_both in (False, True):
            self.vpath("Split Old").write_text(OLD_NOTE.format(t="Split Old"))
            for t in (SASA, RADO):
                if self.vpath(t).exists():
                    self.vpath(t).unlink()
            unit = self.new_unit("repair", [])
            agent.move_file(str(self.vpath("Split Old")), str(self.vpath(SASA)))
            agent.write_file(str(self.vpath(SASA)), good_text())
            agent.write_file(str(self.vpath(RADO)), good_text(RADO))
            stub = unit / "out" / PN / "Split Old.md"
            agent.replace_in_file(str(self.vpath("Split Old")), f'"[[{SASA}]]"', f'"[[{SASA}]]; [[{RADO}]]"')
            agent.replace_in_file(str(self.vpath("Split Old")), f"[[{SASA}]].", f"[[{SASA}]] and [[{RADO}]].")
            self.assertIn(f"; [[{RADO}]]", stub.read_text())
            self.approve(SASA, unit=unit)
            if approve_both:
                self.approve(RADO, unit=unit)
            ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
            want = "published" if approve_both else "pending-review"
            self.assertEqual(self.note(ev, "Split Old")["action"], want, ev["notes"])
            if not approve_both:
                for p in (self.staging / "pending-review").glob("*"):
                    shutil.rmtree(p)


if __name__ == "__main__":
    unittest.main()
