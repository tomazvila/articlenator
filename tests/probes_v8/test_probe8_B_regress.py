"""Reviewer B, round 3 (frozen-v8): the v7 B attacks that now FAIL, re-stated as the
CORRECT behaviour, so that a FAIL of the old probe is shown to be a fix and not a broken
setup. Plus V1 (files-backend view) and V39.

CONVENTION OF THIS FILE: the assertion states the CORRECT behaviour. PASS = fixed,
FAIL = defect. No network, no LLM; fake workers only."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5, _Single, reply
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv, _Files, _h1, count_open

import synth_call as sc  # noqa: E402


class V16ReviewBudget(_Files):
    def test_max_calls_stop_in_a_review_is_recorded_and_sticky(self) -> None:
        import review_call as rc
        u = self.written_unit([(self.a, ["C1"], self.ta), (self.b, ["C2"], self.tb)])
        os.environ["ZR_MAX_CALLS"] = "1"
        recs = rc.review_unit(self.staging, self.zk, u)  # no exception
        ends = sorted(r["end"] for r in recs)
        self.assertEqual(ends, ["budget-stop", "waiting-for-reply"], recs)
        self.assertTrue((u / "review-1" / "review.json").exists())
        self.assertTrue((self.run_dir / "budget-stop.json").exists())  # zr_budget_check exits 4
        self.assertTrue(ri.budget_status(self.run_dir)["over"])


class V13RereviewRefusesTheWriter(_Files):
    def test_source_check_pass_refuses_the_provenance_writer(self) -> None:
        import review_call as rc
        u = self.written_unit()
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]), worker="reviewer-2")
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        rel = f"{PN}/{self.a}.md"
        p = self.zk / rel
        p.write_text(p.read_text().replace("- [[Home]] — home map.", "- [[Home]] — the home map."))
        ru = ri.unit_start(self.run_dir, "review", [], rel, os.getpid(), options={"review_pass": "source-check"})
        self.assertEqual(rc.review_unit(self.staging, self.zk, ru)[0]["end"], "waiting-for-reply")
        work(self.xdir, lambda r: review_answer(r["user"]), worker="writer-1")
        self.assertEqual(rc.review_unit(self.staging, self.zk, ru)[0]["end"], "waiting-for-reply")
        self.assertTrue(list((self.xdir / "replies").glob("*.refused-*.txt")))
        work(self.xdir, lambda r: review_answer(r["user"]), worker="reviewer-3")
        self.assertEqual(rc.review_unit(self.staging, self.zk, ru)[0]["end"], "finished")


class V14ListNotHidden(_Files):
    def test_a_later_run_of_another_driver_does_not_hide_the_request(self) -> None:
        self.fake()
        self.synth()
        r1 = sc.exchange_list(self.xdir)[0]["key"]
        run2 = ri.start(self.staging, "repair", self.zk)
        u2 = ri.unit_start(run2, "repair", [], None, os.getpid())
        try:
            sc.files_call(u2, "repair-0", [{"role": "user", "content": "other"}], {"model": "m", "max_tokens": 10})
        except sc.PendingReply:
            pass
        keys = [r["key"] for r in sc.exchange_list(self.xdir, staging=self.staging)]
        self.assertIn(r1, keys)
        self.assertFalse((self.xdir / "requests" / f"{r1}.obsolete").exists())


class V2RepairLoopExit5(_DriverEnv):
    def test_waiting_repair_batch_exits_5(self) -> None:
        from tests.unit.test_run_integrity import OLD_NOTE
        text = OLD_NOTE.format(t="Old Six").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath("Old Six").write_text(text)
        r = subprocess.run(["bash", str(ZR / "repair_loop.sh"), f"{PN}/Old Six.md"], env=self._env(),
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 5, r.stderr[-1500:])
        self.assertIn("wait for a worker reply", r.stderr)


class V1FilesBackendWaits(_Files):
    def test_a_batch_that_waits_four_passes_uses_the_late_reply(self) -> None:
        from tests.unit.test_run_integrity import OLD_NOTE
        text = OLD_NOTE.format(t="Old Late").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath("Old Late").write_text(text)
        rel = f"{PN}/Old Late.md"
        state = self.staging / "repair_state.json"
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        for _ in range(4):
            ri.release_waiting(self.staging)
            b = sc.next_batch(self.staging, self.zk, state)
            self.assertIsNotNone(b)
            u = self.new_unit("repair", [])
            res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                 b["prior_titles"], b["last"])
            sc.record_answers(state, b, res)
            self.assertEqual(res["stopped"], "waiting-for-reply")
        st = json.loads(state.read_text())
        self.assertEqual([x["status"] for x in st["batches"].values()], ["waiting"])
        self.assertEqual(st["notes"][rel]["status"], "open")
        rep = (f"=====NOTE: {self.a} | repairs: Old Late.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Late.md#b1 | kept in {self.a}\n")
        work(self.xdir, lambda r: rep, worker="w1")
        ri.release_waiting(self.staging)
        b = sc.next_batch(self.staging, self.zk, state)
        u = self.new_unit("repair", [])
        res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                             b["prior_titles"], b["last"])
        sc.record_answers(state, b, res)
        self.assertEqual(res["written"], [self.a])
        st = json.loads(state.read_text())
        self.assertNotIn("failed", [x["status"] for x in st["batches"].values()])


class V6ResendCovered(_Files):
    def test_c6_is_sent_again_and_covered(self) -> None:
        """The v7 ResendNameCache scenario (late reply changes the re-send order)."""
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
        c6_note = "=====NOTE: Tomas Brink Says A Fourth Session" + rest
        no_c6 = head + "=====SKIPPED=====\n"
        c8_skip = "=====SKIPPED=====\nC8 | not a transferable training claim: he only repeats the warm-up advice\n"
        plan = {2: {"inv"}, 3: {"batch-2"}, 4: {"batch-3"}, 5: {"batch-1"}, 6: {"batch-3", "batch-4"}}
        answers = {"inv": inv, "batch-2": "=====SKIPPED=====\n", "batch-3": c8_skip, "batch-1": no_c6,
                   "batch-4": c8_skip}
        res = None
        for n in range(1, 12):
            ri.release_waiting(self.staging)
            u = self.new_unit("synth", [vid])
            res = sc.synth_unit(self.staging, self.zk, u, [vid])
            if res["outcome"] != "waiting-for-reply":
                break
            want = plan.get(n + 1)

            def ans(r: dict) -> str | None:
                nm = "inv" if r["kind"] == "inv" else r["name"]
                if want is not None:
                    if nm not in want:
                        return None
                    if r["kind"] == "batch" and nm != "batch-1" and "C6 |" in r["user"]:
                        return c6_note  # the re-send of C6 gets a note for C6
                    return answers.get(nm)
                # later passes: answer every open request; a C6 re-send gets the C6 note
                return c6_note if "C6 |" in r["user"] else (c8_skip if "C8 |" in r["user"] else "=====SKIPPED=====\n")
            work(self.xdir, ans, worker="writer")
        reqs = [json.loads(p.read_text()) for p in (self.xdir / "requests").glob("*.json")]
        c6_resend = [r for r in reqs if r["kind"] == "batch" and r["name"] != "batch-1" and "C6 |" in r["user"]]
        self.assertTrue(c6_resend)
        self.assertNotIn("C6", res.get("claims_uncovered") or [], res.get("outcome"))


class V15SameBadTitle(_Files):
    def test_two_notes_with_the_same_bad_title_both_publish(self) -> None:
        self.fake()
        self.synth()
        bad = reply([("C1", "01:16", "a"), ("C2", "01:22", "b")],
                    [("Home", ["C1"], _h1(self.ta, "Home")), ("Home", ["C2"], _h1(self.tb, "Home"))])
        work(self.xdir, lambda r: bad, worker="w1")
        marker_a = self.ta.split("\n# ", 1)[1].split("\n", 2)[2][:200]

        def fixes(r: dict) -> str | None:
            if r["kind"] != "titlefix":
                return None
            return f"=====NOTE: {self.a}=====\n{self.ta}" if marker_a in r["user"] else f"=====NOTE: {self.b}=====\n{self.tb}"
        res, per_pass = None, []
        for _ in range(4):
            u = self.new_unit("synth", [V5])
            res = sc.synth_unit(self.staging, self.zk, u, [V5])
            per_pass.append(len([x for x in sc.exchange_list(self.xdir) if x["kind"] == "titlefix"]))
            if res["outcome"] != "waiting-for-reply":
                break
            work(self.xdir, fixes, worker="w2")
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]))
        self.assertEqual(per_pass[0], 2)  # V39: both title-fix requests in the first pass


class V39TwoBadTitlesOnePass(_Files):
    def test_two_different_bad_titles_go_out_in_one_pass(self) -> None:
        self.fake()
        self.synth()
        bad = reply([("C1", "01:16", "a"), ("C2", "01:22", "b")],
                    [("Home", ["C1"], _h1(self.ta, "Home")), ("00 Maps", ["C2"], _h1(self.tb, "00 Maps"))])
        work(self.xdir, lambda r: bad, worker="w1")
        u = self.new_unit("synth", [V5])
        res = sc.synth_unit(self.staging, self.zk, u, [V5])
        self.assertEqual(res["outcome"], "waiting-for-reply")
        self.assertEqual(len([x for x in sc.exchange_list(self.xdir) if x["kind"] == "titlefix"]), 2)


class V17TextBeforeH1(_Files):
    def test_text_before_h1_is_refused_by_the_check(self) -> None:
        extra = "The speaker says: do 20 sets of one-arm planche daily, from day one."
        fm_end = self.ta.index("\n---", 3) + 4
        ta = self.ta[:fm_end] + "\n\n" + extra + "\n" + self.ta[fm_end:]
        u = self.written_unit([(self.a, ["C1"], ta)])
        res = ri.check_unit(self.staging, u, self.zk)
        rel = f"{PN}/{self.a}.md"
        self.assertFalse(res[rel]["ok"])
        self.assertIn("text-before-h1", {f["type"] for f in res[rel]["failures"]})
        ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        self.assertFalse((self.zk / rel).exists())


class V37FirstCandidate(_Single):
    def test_first_candidate_passes_the_attach_score(self) -> None:
        from unittest import mock
        with mock.patch.object(sc, "candidate_videos", return_value=[("v1", 0.34), ("v2", 0.16)]), \
                mock.patch.object(sc, "bullet_video_scores", return_value=[("v2", 0.4, 0), ("v1", 0.3, 0)]):
            got = sc.unknown_candidates(self.staging, "text")
        self.assertEqual(got[0][0], "v1")


class S9Unreachable(_Files):
    def test_no_request_is_marked_obsolete_by_the_harness(self) -> None:
        self.fake()
        self.synth()
        run2 = ri.start(self.staging, "synth", self.zk)
        ri._write_json(run2 / "exchange_wanted.json", {"x" * 32: "other"})
        sc.exchange_status(self.xdir, self.staging)
        sc.exchange_list(self.xdir, staging=self.staging)
        self.assertEqual(list((self.xdir / "requests").glob("*.obsolete")), [])
        src = (ZR / "synth_call.py").read_text()
        self.assertEqual(len(re.findall(r"_mark_obsolete_auto\(", src)), 1)  # only its definition
