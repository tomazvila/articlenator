"""Reviewer B, round 2 (frozen-v7): the 4 B probes that the authors marked SUPERSEDED,
adapted minimally to the new meta rule, plus a stricter form of the S1 loop probe.

CONVENTION OF THIS FILE: the assertion states the CORRECT behaviour (the same convention
as the original B probes). PASS = fixed, FAIL = defect. No network, no LLM."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from tests.unit.test_run_integrity import ZR, ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5, _Single, reply
import unittest

from tests.probes_v6 import test_probe_B_loop as _bl

import synth_call as sc  # noqa: E402


class Superseded(_Single):
    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_LLM_BACKEND"] = "files"
        self.xdir = self.staging / "exchange"

    def _answer(self, i: int, worker) -> str:
        msgs = [{"role": "user", "content": f"u{i}"}]
        s = {"model": "m", "max_tokens": 10}
        with self.assertRaises(sc.PendingReply) as cm:
            sc.files_call(self.unit, f"synth-p{i}-0", msgs, s)
        key = cm.exception.key
        (self.xdir / "replies" / f"{key}.txt").write_text("x")
        # the only change to the original probe: a string `model` in the meta
        (self.xdir / "replies" / f"{key}.meta.json").write_text(json.dumps({"key": key, "worker": worker, "model": "m"}))
        try:
            sc.files_call(self.unit, f"synth-p{i}-0", msgs, s)
        except sc.PendingReply:
            pass
        return key

    def test_meta_worker_list_with_model(self) -> None:
        """Original: test_meta_worker_list_crashes_every_later_review."""
        k = self._answer(1, ["w1"])
        sc.writer_workers(self.unit)  # no TypeError
        self.assertTrue(list((self.xdir / "replies").glob(f"{k}.refused-*.json")))

    def test_meta_worker_int_and_str_with_model(self) -> None:
        """Original: test_meta_worker_int_and_str_crash_sorted."""
        k1 = self._answer(1, 7)
        self._answer(2, "w1")
        self.assertEqual(sc.writer_workers(self.unit), ["w1"])
        self.assertTrue(list((self.xdir / "replies").glob(f"{k1}.refused-*.json")))

    def test_reply_survives_a_crash_after_consume_with_meta(self) -> None:
        """Original: test_ok_reply_survives_a_crash_after_consume."""
        msgs = [{"role": "user", "content": "u"}]
        s = {"model": "m", "max_tokens": 10}
        with self.assertRaises(sc.PendingReply) as cm:
            sc.files_call(self.unit, "synth-p1-0", msgs, s)
        key = cm.exception.key
        (self.xdir / "replies" / f"{key}.txt").write_text("hello\n")
        (self.xdir / "replies" / f"{key}.meta.json").write_text(json.dumps({"key": key, "worker": "w", "model": "m"}))
        self.assertEqual(sc.files_call(self.unit, "synth-p1-0", msgs, s)[0], "hello\n")
        self.assertEqual(sc.files_call(self.unit, "synth-p1-0", msgs, s)[0], "hello\n")

    def test_refusal_is_visible_new_file_name(self) -> None:
        """Original: test_refusal_is_not_visible_in_exchange_list (only the glob changed)."""
        self.fake()
        self.synth()
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="writer-1")
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])
        import review_call as rc
        rc.review_unit(self.staging, self.zk, u)
        work(self.xdir, lambda r: review_answer(r["user"]), worker="writer-1")
        rc.review_unit(self.staging, self.zk, u)
        rows = [r for r in sc.exchange_list(self.xdir) if r["kind"] == "review"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(list((self.xdir / "replies").glob("*.refused-*.txt")))
        self.assertIn("writer-1", json.dumps(rows[0]))


class LoopRightReason(unittest.TestCase):
    def test_S1_refusal_then_independent_reviewer_publishes(self) -> None:
        """The original S1 probe passes also when nothing publishes. Here: the same
        worker's review is refused (refusal file), a second worker's review publishes both
        notes, and provenance names different writer and reviewer workers (S11)."""
        tmp = Path(tempfile.mkdtemp(prefix="probe7B-"))
        try:
            stg, zk, env, replies = _bl.LoopProbes._setup(self, tmp)

            def ans(r: dict) -> str | None:
                if r["kind"] == "synth":
                    return next(v for k, v in replies.items() if k in r["user"])
                if r["kind"] == "review":
                    return review_answer(r["user"])
                return None
            codes = []
            for n in range(7):
                r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
                codes.append(r.returncode)
                if r.returncode != 5:
                    break
                # pass 1 and 2: one worker for everything; later passes: a second reviewer
                work(stg / "exchange", ans, worker="same" if n < 2 else (lambda q: "rev2" if q["kind"] == "review" else "same"))
            xdir = stg / "exchange"
            self.assertTrue(list((xdir / "replies").glob("*.refused-*.txt")), codes)
            pubs = [json.loads(x) for x in (stg / "provenance.jsonl").read_text().splitlines() if '"note-published"' in x]
            self.assertEqual(codes[-1], 0, codes)
            self.assertEqual(len(pubs), 2, codes)
            self.assertEqual({(tuple(p.get("synth_worker") or []), p.get("review_worker")) for p in pubs},
                             {(("same",), "rev2")})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
