"""Reviewer B, round 2 (frozen-v7): attacks on the new files-backend mechanisms.

CONVENTION OF THIS FILE: the assertion states the DEFECT. PASS = the defect exists,
FAIL = the attack failed (the code is correct). Tests named test_ok_* are the exception:
they state the correct behaviour of an attack that failed (PASS = no defect).
No network, no LLM."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5, _Single, reply

import synth_call as sc  # noqa: E402


class _Files(_Single):
    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_LLM_BACKEND"] = "files"
        self.xdir = self.staging / "exchange"

    def written_unit(self, notes=None, worker="writer-1") -> Path:
        """Pass 1 emits the synthesis request; the worker answers; pass 2 writes the notes."""
        notes = notes or [(self.a, ["C1"], self.ta)]
        self.fake()
        self.synth()
        claims = [("C1", "01:16", "a"), ("C2", "01:22", "b")][:len(notes)]
        work(self.xdir, lambda r: reply(claims, notes) if r["kind"] == "synth" else None, worker=worker)
        u = self.new_unit("synth", [V5])
        res = sc.synth_unit(self.staging, self.zk, u, [V5])
        assert res["outcome"] == "notes", res
        return u


class ReviewBudget(_Files):
    def test_max_calls_in_review_raises_out_of_review_unit(self) -> None:
        """review_note catches only PendingReply. A ZR_MAX_CALLS stop in a review call is an
        uncaught BudgetStop: review_call.py crashes (zr_source_check prints 'failed'), the
        other notes of the unit get no review request, and the driver goes on (no exit 4)."""
        import review_call as rc
        u = self.written_unit([(self.a, ["C1"], self.ta), (self.b, ["C2"], self.tb)])
        os.environ["ZR_MAX_CALLS"] = "1"  # the synthesis request of this run already counts 1
        with self.assertRaises(sc.BudgetStop):
            rc.review_unit(self.staging, self.zk, u)
        dirs = sorted(p.name for p in u.glob("review-*"))
        self.assertEqual(dirs, ["review-0", "review-1"])
        self.assertTrue((u / "review-0" / "review.json").exists())  # A: waiting-for-reply, recorded
        self.assertFalse((u / "review-1" / "review.json").exists())  # B: crashed, no harness record
        self.assertFalse((u / "review-1" / "agent_result.json").exists())


class RereviewIndependence(_Files):
    def test_source_check_pass_accepts_the_writer_as_reviewer(self) -> None:
        """The source-check pass (review unit of a vault note) has no calls/, so
        writer_keys() and writer_workers() are empty: the worker that WROTE the note (provenance
        synth_worker) may source-check it. The writer is known in provenance.jsonl."""
        import review_call as rc
        u = self.written_unit()
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]), worker="reviewer-2")
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        self.assertEqual({n["action"] for n in ev["notes"]}, {"published"})
        rel = f"{PN}/{self.a}.md"
        # the vault note gets a small harness edit (as a later relink or verification update does)
        p = self.zk / rel
        p.write_text(p.read_text().replace("- [[Home]] — home map.", "- [[Home]] — the home map."))
        ru = ri.unit_start(self.run_dir, "review", [], rel, os.getpid(), options={"review_pass": "source-check"})
        recs = rc.review_unit(self.staging, self.zk, ru)
        self.assertEqual(recs[0]["end"], "waiting-for-reply")
        row = [r for r in sc.exchange_list(self.xdir) if r["kind"] == "review"][0]
        self.assertEqual((row["must_not_share_worker_with"], row["writer_workers"]), ([], []))
        work(self.xdir, lambda r: review_answer(r["user"]), worker="writer-1")  # the WRITER answers
        recs = rc.review_unit(self.staging, self.zk, ru)
        self.assertEqual(recs[0]["end"], "finished")  # accepted: no refusal
        self.assertFalse(list((self.xdir / "replies").glob("*.refused-*.txt")))


class Obsolete(_Files):
    def _count_open(self) -> str:
        r = subprocess.run([sys.executable, str(ZR / "synth_call.py"), "exchange-status", "--staging",
                            str(self.staging), "--count-open"], capture_output=True, text=True, env=dict(os.environ))
        return r.stdout.strip()

    def test_second_driver_hides_a_waiting_units_request(self) -> None:
        """mark_obsolete uses only the LAST run's exchange_wanted.json. A synthesis unit
        waits for request R1 (queue stage waiting-for-reply). A later run of another driver
        (repair_loop.sh on one note, or a pass that a ZR_MAX_CALLS stop ended before this
        unit) wants only R2. R1 is marked obsolete: exchange-list --open hides it from the
        workers and --count-open does not count it. When R2 is consumed, the count is
        '0 0' (driver exit 0) while the video still waits for R1."""
        self.fake()
        self.assertEqual(self.synth()["outcome"], "waiting-for-reply")
        r1 = sc.exchange_list(self.xdir)[0]["key"]
        run2 = ri.start(self.staging, "repair", self.zk)
        u2 = ri.unit_start(run2, "repair", [], None, os.getpid())
        msgs = [{"role": "user", "content": "a repair request of another driver"}]
        with self.assertRaises(sc.PendingReply) as cm:
            sc.files_call(u2, "repair-0", msgs, {"model": "m", "max_tokens": 10})
        r2 = cm.exception.key
        self.assertEqual([r["key"] for r in sc.exchange_list(self.xdir, staging=self.staging)], [r2])
        (self.xdir / "replies" / f"{r2}.txt").write_text("x")
        (self.xdir / "replies" / f"{r2}.meta.json").write_text(json.dumps({"key": r2, "worker": "w", "model": "m"}))
        sc.files_call(u2, "repair-0", msgs, {"model": "m", "max_tokens": 10})  # consumed
        self.assertTrue((self.xdir / "requests" / f"{r1}.obsolete").exists())
        self.assertEqual(self._count_open(), "0 0")

    def test_answered_after_obsolete_is_not_counted(self) -> None:
        """A worker that took R1 before it was marked obsolete writes its reply later. The
        .obsolete marker stays, so exchange-status does not count R1 as answered or
        unconsumed, although the unit still needs it."""
        self.fake()
        self.synth()
        r1 = sc.exchange_list(self.xdir)[0]
        run2 = ri.start(self.staging, "repair", self.zk)
        u2 = ri.unit_start(run2, "repair", [], None, os.getpid())
        with self.assertRaises(sc.PendingReply):
            sc.files_call(u2, "repair-0", [{"role": "user", "content": "other"}], {"model": "m", "max_tokens": 10})
        sc.exchange_status(self.xdir, self.staging)  # marks R1 obsolete
        Path(r1["reply_path"]).write_text(reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]))
        Path(r1["meta_path"]).write_text(json.dumps({"key": r1["key"], "worker": "w1", "model": "m"}))
        st = sc.exchange_status(self.xdir, self.staging)
        self.assertEqual((st["answered"], st["unconsumed"], st["open"]), (0, [], 1))  # only R2 counts
        self.assertTrue((self.xdir / "requests" / f"{r1['key']}.obsolete").exists())


class RepairLoopExit(_Files):
    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "ZK_"))}
        env.update(ZR_STATE_DIR=str(self.tmp / "state"), ZR_TEST_RUN="1", VAULT=str(self.zk.parent),
                   ZK_FOLDER=self.zk.name, ZR_STAGING=str(self.staging), ZR_LLM_BACKEND="files",
                   ZR_SYNTH_SCRIPT="/bin/false", ZR_REVIEW_SCRIPT="/bin/false", ZR_AGENT_SCRIPT="/bin/false",
                   ZR_REVIEW_REPAIR="0")
        return env

    def test_repair_loop_waiting_batch_exits_1_not_5(self) -> None:
        """repair_loop.sh (single mode) ends with `exit $rc_all`. finalize of a repair unit
        that waits for its reply returns 11 (incomplete), so rc_all=1; zr_run_summary then
        sees driver_status 1 and skips zr_exchange_exit: the driver exits 1 instead of 5,
        and 'answer them, then run again' is never printed."""
        from tests.unit.test_run_integrity import OLD_NOTE
        text = OLD_NOTE.format(t="Old Six").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath("Old Six").write_text(text)
        r = subprocess.run(["bash", str(ZR / "repair_loop.sh"), f"{PN}/Old Six.md"], env=self._env(),
                           capture_output=True, text=True, timeout=300)
        if os.environ.get("PROBE_DEBUG"):
            print(r.stdout[-4000:], r.stderr[-4000:])
        self.assertTrue([x for x in sc.exchange_list(self.xdir, staging=self.staging) if x["kind"] == "repair"],
                        r.stderr[-2000:])
        self.assertEqual(r.returncode, 1, r.stderr[-1500:])
        self.assertNotIn("wait for a worker reply", r.stderr)


class ResendNameCache(_Files):
    """Two-stage mode: re-send batches are named batch-<k>, where k counts batches in the
    order of THIS pass. The synthesis cache (call()) is keyed by the NAME only, not by the
    request key. When an earlier batch's reply arrives late, the re-send order changes and
    `batch-3` of the new pass gets the cached reply of the OTHER request `batch-3`."""

    def test_resend_gets_the_cached_reply_of_another_resend(self) -> None:
        import shutil
        from tests.unit.test_run_integrity_r13 import NC
        os.environ["ZR_SYNTH_STAGES"] = "two"
        vid = "vid-EXAMPLE0002"
        shutil.copy(NC / "lit_fictional" / f"{vid}.md", self.staging / "lit")
        for p in (NC / "vault_fictional").rglob("Mara Kell*.md"):
            shutil.copy(p, self.zk / PN / p.name)
        self.write_queue([{"id": vid, "kind": "video", "stage": "claimed", "claimed_from": "extracted",
                           "lit_note": str(self.staging / "lit" / f"{vid}.md")}])
        inv = (NC / "inventory_reply_fictional.txt").read_text()
        full = (NC / "batch_reply_fictional.txt").read_text()
        head, rest = full.split("=====NOTE: Tomas Brink Says A Fourth Session", 1)
        no_c6 = head + "=====SKIPPED=====\n"  # C2..C5 covered; C6: no note, no skip
        c8_skip = "=====SKIPPED=====\nC8 | not a transferable training claim: he only repeats the warm-up advice\n"
        plan = {2: {"inv"}, 3: {"batch-2"}, 4: {"batch-3"}, 5: {"batch-1"}, 6: {"batch-3", "batch-4"}}
        answers = {"inv": inv, "batch-2": "=====SKIPPED=====\n", "batch-3": c8_skip, "batch-1": no_c6,
                   "batch-4": c8_skip}
        res, names_by_pass = None, {}
        for n in range(1, 8):
            ri.release_waiting(self.staging)
            u = self.new_unit("synth", [vid])
            res = sc.synth_unit(self.staging, self.zk, u, [vid])
            if res["outcome"] != "waiting-for-reply":
                break
            want = plan.get(n + 1, set())
            names_by_pass[n] = sorted(r["kind"] + ":" + json.loads(Path(r["request"]).read_text())["name"]
                                      for r in sc.exchange_list(self.xdir))
            work(self.xdir, lambda r: answers.get("inv" if r["kind"] == "inv" else r["name"])
                 if ("inv" if r["kind"] == "inv" else r["name"]) in want else None, worker="writer")
        reqs = [json.loads(p.read_text()) for p in (self.xdir / "requests").glob("*.json")]
        c6_resend = [r for r in reqs if r["kind"] == "batch" and r["name"] != "batch-1" and "C6 |" in r["user"]]
        st = (json.loads((u / "claims.json").read_text()) if (u / "claims.json").exists() else {})
        if os.environ.get("PROBE_DEBUG"):
            print(res.get("outcome"), res.get("claims_uncovered"), names_by_pass)
            print([(r["name"], [ln for ln in r["user"].split("\n") if ln.startswith("C") and " | " in ln][:3]) for r in reqs])
        # the defect: C6 was never sent again to a worker, yet it ends as refused twice
        self.assertEqual(c6_resend, [], [r["name"] for r in reqs])
        self.assertIn("C6", res.get("claims_uncovered") or [], (res.get("outcome"), names_by_pass))


class CarryOver(_Files):
    """Attack e: change content after a SUPPORTED review so that reviewed_hash keeps."""

    def _reviewed(self) -> tuple[Path, Path, str]:
        import review_call as rc
        u = self.written_unit()
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker="reviewer-2")
        rc.review_unit(self.staging, self.zk, u)
        p = u / "out" / PN / f"{self.a}.md"
        rel = f"{PN}/{self.a}.md"
        self.assertFalse(ri.verdict_for(u, rel, p.read_bytes()).get("bad"))
        return u, p, rel

    def _carried(self, u: Path, p: Path, rel: str, new: str) -> dict:
        self.assertNotEqual(new, p.read_text())
        p.write_text(new)
        return ri.verdict_for(u, rel, p.read_bytes()) or {}

    def test_text_before_the_h1_is_unreviewed_and_carries(self) -> None:
        """parse_body_full drops every line between the frontmatter and the H1: it is no
        review item, it is not in reviewed_hash, and the mechanical check does not see it."""
        u, p, rel = self._reviewed()
        t = p.read_text()
        fm_end = t.index("\n---", 3) + 4
        new = t[:fm_end] + "\n\nThe speaker says: do 20 sets of one-arm planche daily, from day one.\n" + t[fm_end:]
        v = self._carried(u, p, rel, new)
        self.assertTrue(v.get("carried_over"), v)
        res = ri.check_unit(self.staging, u, self.zk)
        self.assertTrue(res[rel]["ok"], res[rel].get("failures"))

    def test_link_target_change_carries(self) -> None:
        u, p, rel = self._reviewed()
        t = p.read_text()
        new = t.replace("[[Home]]", "[[Other|Home]]", 1)
        v = self._carried(u, p, rel, new)
        self.assertTrue(v.get("carried_over"), v)

    def test_connected_ideas_prose_carries(self) -> None:
        """Connected Ideas is no review item and not hashed: a claim added after the bullet's
        link carries the review (the section shape only needs '- ... [[link]]')."""
        u, p, rel = self._reviewed()
        t = p.read_text()
        new = t.replace("- [[Home]] — home map.", "- [[Home]] — home map. He also says 20 sets daily is safe for beginners.")
        v = self._carried(u, p, rel, new)
        self.assertTrue(v.get("carried_over"), v)
        res = ri.check_unit(self.staging, u, self.zk)
        self.assertTrue(res[rel]["ok"], res[rel].get("failures"))

    def test_ok_number_in_a_hashed_section_is_stale(self) -> None:
        """Correct behaviour (PASS = no defect): a changed number in Details, Evidence, lead,
        scope; NBSP/zero-width and case changes in hashed parts give review-stale."""
        u, p, rel = self._reviewed()
        t = p.read_text()
        import re as _re
        lead_line = [ln for ln in t.split("\n") if "[src:" in ln][0]
        variants = [t.replace(lead_line, lead_line.replace(" ", " ", 1)),
                    t.replace(lead_line, lead_line.replace(" ", " ​", 1)),
                    t.replace(lead_line, lead_line.swapcase()),
                    _re.sub(r"(## Details\n\n- [^\n]*?)(\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1), t, count=1)]
        for new in variants:
            if new == t:
                continue
            p.write_text(new)
            v = ri.verdict_for(u, rel, p.read_bytes()) or {}
            self.assertEqual(v.get("code"), "review-stale", new[:200])


class UnreviewedTextPublishes(_Files):
    def test_text_before_h1_reaches_the_vault_unreviewed(self) -> None:
        """A synthesis reply with a paragraph between the frontmatter and the H1: the check
        passes, the review skeleton has no item for it, the note publishes with it."""
        import review_call as rc
        extra = "The speaker says: do 20 sets of one-arm planche daily, from day one."
        fm_end = self.ta.index("\n---", 3) + 4
        ta = self.ta[:fm_end] + "\n\n" + extra + "\n" + self.ta[fm_end:]
        u = self.written_unit([(self.a, ["C1"], ta)])
        rc.review_unit(self.staging, self.zk, u)
        sk = [json.loads(Path(r["request"]).read_text()) for r in sc.exchange_list(self.xdir) if r["kind"] == "review"]
        self.assertTrue(sk)
        skel = sk[0]["user"].split("Reply with this JSON object only:\n", 1)[1]
        self.assertNotIn("20 sets", skel)  # no review item holds it
        work(self.xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker="reviewer-2")
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        self.assertEqual({n["action"] for n in ev["notes"]}, {"published"}, ev["notes"])
        self.assertIn(extra, (self.zk / PN / f"{self.a}.md").read_text())


class UnknownCandidates(_Single):
    def test_first_candidate_may_be_below_the_attach_score(self) -> None:
        """The attach score (0.3) is tested on the word-search top video only. The FIRST
        candidate is then the bullet-ranked best of all videos >= 0.15, even with no bullet
        passage >= 0.5: here v2 (word score 0.16) goes first, before v1 (0.34). Its score in
        the plan is the bullet sum (0.4), another scale than the word scores after it."""
        from unittest import mock
        with mock.patch.object(sc, "candidate_videos", return_value=[("v1", 0.34), ("v2", 0.16)]), \
                mock.patch.object(sc, "bullet_video_scores", return_value=[("v2", 0.4, 0), ("v1", 0.3, 0)]):
            got = sc.unknown_candidates(self.staging, "text")
        self.assertEqual(got, [("v2", 0.4), ("v1", 0.34)])


class TitleFixSameTitle(_Files):
    def test_two_title_jobs_with_the_same_bad_title_share_one_name(self) -> None:
        """S3 fix: the title-fix name is titlefix-<sha256(old title)[:10]>. Two notes of one
        reply with the SAME refused title (here both 'Home') get the same name, so the
        name-keyed synthesis cache gives job 2 the reply of job 1: note B is lost."""
        import re as _re
        h1 = lambda text, t: _re.sub(r"(?m)^# .+$", f"# {t}", text, count=1)  # noqa: E731
        self.fake()
        self.synth()
        bad = reply([("C1", "01:16", "a"), ("C2", "01:22", "b")],
                    [("Home", ["C1"], h1(self.ta, "Home")), ("Home", ["C2"], h1(self.tb, "Home"))])
        work(self.xdir, lambda r: bad, worker="w1")

        def fixes(r: dict) -> str | None:
            if r["kind"] != "titlefix":
                return None
            return f"=====NOTE: {self.a}=====\n{self.ta}" if "Can Be Done On The Floor" in self.ta and \
                self.ta.split("\n# ", 1)[1].split("\n", 2)[2][:200] in r["user"] else f"=====NOTE: {self.b}=====\n{self.tb}"
        res, names = None, []
        for _ in range(5):
            u = self.new_unit("synth", [V5])
            res = sc.synth_unit(self.staging, self.zk, u, [V5])
            names += sorted(p.name for p in (u / "calls").glob("titlefix-*.json")) if (u / "calls").is_dir() else []
            if res["outcome"] != "waiting-for-reply":
                break
            work(self.xdir, fixes, worker="w2")
        tf = [json.loads(p.read_text()) for p in (self.xdir / "requests").glob("*.json")]
        tf = [r for r in tf if r["kind"] == "titlefix"]
        if os.environ.get("PROBE_DEBUG"):
            print(res["outcome"], res["notes"], len(tf), names, res.get("refused"),
                  [p["type"] for p in res.get("problems", [])])
        self.assertNotEqual(sorted(res["notes"]), sorted([self.a, self.b]), (res["notes"], len(tf)))
