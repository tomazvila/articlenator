"""Probe 10 A (fifth review): the new review-invalid path (pilot 9, round 22), which has no
dedicated test. Fake workers / fake review script only; no network, no LLM.

Convention: every `test_defect_*` assertion states the DEFECT (PASS = defect present).
Every `test_ok_*` assertion states the CORRECT behaviour (PASS = correct)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import REVIEW, V5, _Single, reply
from tests.probes_v6 import test_probe_B_loop as _bl

import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402


def unusable_answer(user: str) -> str:
    """A well-formed reply that the reviewer could not use: every item transcript-missing."""
    sk = json.loads(user.split("Reply with this JSON object only:\n", 1)[1])
    for it in sk["items"]:
        it["verdict"] = "transcript-missing"
        it["problem"] = "the passage is cut"
        it["transcript_text"] = ""
    sk["scope_complete"] = True
    return json.dumps(sk)


def _loop_chain(test: unittest.TestCase, answer_review, max_cycles: str = "5", rounds: int = 14):
    tmp = Path(tempfile.mkdtemp(prefix="probe10A-"))
    test.addCleanup(shutil.rmtree, tmp, True)
    stg, zk, env, replies = _bl.LoopProbes._setup(test, tmp)
    q = json.loads((stg / "queue.json").read_text())
    q["items"] = [it for it in q["items"] if it["id"] == "vid-EXAMPLE0001"]
    (stg / "queue.json").write_text(json.dumps(q))
    env.update(ZR_MAX_PENDING_CYCLES=max_cycles, MAX_ITERS="30")
    n_rev = {"n": 0}

    def ans(r: dict) -> str | None:
        if r["kind"] == "synth":
            return next(v for k, v in replies.items() if k in r["user"])
        if r["kind"] == "review":
            n_rev["n"] += 1
            return answer_review(n_rev["n"], r["user"])
        return None
    codes, outs = [], []
    for _ in range(rounds):
        r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
        codes.append(r.returncode)
        outs.append(r.stdout[-3000:] + r.stderr[-3000:])
        if r.returncode not in (5, 7):
            break
        if r.returncode == 7 and not sc.exchange_list(stg / "exchange", only_open=True) and \
                not list(stg.glob("pending-review/*--*/meta.json")):
            break
        work(stg / "exchange", ans, worker=lambda req: "reviewer-2" if req["kind"] == "review" else "writer-1")
    return codes, outs, stg, zk, n_rev["n"]


def _quarantined(stg: Path) -> dict[str, list[str]]:
    out = {}
    for p in stg.glob("quarantine/**/*.reason.json"):
        out[p.name] = json.loads(p.read_text()).get("failure_types")
    return out


def _published(stg: Path) -> list[str]:
    p = stg / "provenance.jsonl"
    return [json.loads(x)["note"] for x in p.read_text().splitlines() if '"note-published"' in x] if p.exists() else []


class FilesUnusableEveryTime(unittest.TestCase):
    """Files backend through loop.sh: the reviewer answers transcript-missing every time."""

    def test_ok_quarantined_review_invalid_and_driver_ends(self) -> None:
        codes, outs, stg, zk, n = _loop_chain(self, lambda k, u: unusable_answer(u))
        q = _quarantined(stg)
        self.assertTrue(any("review-invalid" in (v or []) for v in q.values()), (codes, q, n))
        self.assertNotEqual(codes[-1], 5, codes)

    def test_defect_moved_aside_requests_stay_open_for_ever(self) -> None:
        """review_call.py:468 moves the unusable reply aside with refuse_reply(), which makes
        the SAME request key open again for a worker. The next pass asks with a NEW key
        (review_attempt), so nobody ever consumes the reopened key. Measured: the worker
        writes 3 review replies for 2 used ones; after the quarantine and exit 0, one review
        request is still open and the third reply (to the reopened first key) is never used: the key already has a
        consumed record, so exchange-status does not even list it as unconsumed. held_reply_keys()
        also counts these keys as held (stale agent_result.json of finalized units)."""
        codes, outs, stg, zk, n = _loop_chain(self, lambda k, u: unusable_answer(u))
        xdir = stg / "exchange"
        open_reviews = [r for r in sc.exchange_list(xdir, only_open=True) if r["kind"] == "review"]
        held = ri.held_reply_keys(stg)
        orphans = [r["key"] for r in open_reviews if r["key"] not in held]
        if os.environ.get("PROBE_DEBUG"):
            print("CODES", codes, "N", n)
            for o in outs:
                print("----PASS\n", o[-1500:])
            print("QUAR", _quarantined(stg), "PUB", _published(stg))
            print("INV", (stg / "review_invalid.json").read_text() if (stg / "review_invalid.json").exists() else None)
            print("OPEN", [(r["key"], r["unit"]) for r in open_reviews], "HELD", held)
            print("PENDING", [str(p) for p in stg.glob("pending-review/*")])
            for f in sorted((stg / "exchange" / "replies").glob("*")):
                print("R", f.name)
            for f in sorted((stg / "exchange" / "consumed").glob("*")):
                print("C", f.name, f.read_text()[:200])
        self.assertEqual(codes[-1], 0, codes)                     # "nothing left for workers"
        self.assertFalse(list(stg.glob("pending-review/*--*")))   # nobody waits
        self.assertGreaterEqual(len(open_reviews), 1, codes)      # ... but a review request is open
        self.assertEqual(n, 3, n)                                 # the worker answered a reopened key
        st0 = sc.exchange_status(xdir, stg)
        self.assertGreaterEqual(st0["open"], 1, st0)             # exchange-status: work for workers
        # held_reply_keys() still names the open key as held (stale agent_result.json of a
        # finalized unit), although no unit or folder waits for it
        self.assertTrue({r["key"] for r in open_reviews} & held, (open_reviews, held))


class FilesUnusableOnceThenGood(unittest.TestCase):
    def test_ok_one_unusable_reply_then_a_good_one_publishes(self) -> None:
        codes, outs, stg, zk, n = _loop_chain(
            self, lambda k, u: unusable_answer(u) if k == 1 else review_answer(u))
        self.assertTrue(_published(stg), (codes, _quarantined(stg), n))
        self.assertFalse(any("review-invalid" in (v or []) for v in _quarantined(stg).values()))


class UsableBadVerdictCountedAsInvalid(unittest.TestCase):
    """parse_verdict_usable (run_integrity.py ~888) and _parse_verdict (~903) disagree:
    scope_complete false, or one transcript-missing item next to a defect verdict, is a
    USABLE bad verdict for finalize, but review_call counts it as unusable, moves the reply
    aside (the request opens again) and adds to review_invalid.json."""

    def _obj(self) -> dict:
        return {"items": [{"item": "title", "verdict": "dropped-qualifier", "also": []},
                          {"item": "C1", "verdict": "transcript-missing", "also": []}], "scope_complete": False}

    def test_defect_bad_verdict_is_counted_as_unusable(self) -> None:
        obj = self._obj()
        tmp = Path(tempfile.mkdtemp(prefix="probe10A-v-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        vf = tmp / "verdict.json"
        vf.write_text(json.dumps(obj))
        fin = ri._parse_verdict(vf, ["title", "C1"])
        self.assertTrue(fin.get("bad") and not fin.get("unusable"), fin)  # finalize: a usable rejection
        self.assertIsNotNone(ri.parse_verdict_usable(obj))  # review_call: "unusable" -> counted, moved aside


FAKE_UNUSABLE_REVIEW = REVIEW.replace(
    'it["verdict"] = "dropped-qualifier" if (reject and k == 0) else "supported"',
    'it["verdict"] = "transcript-missing"').replace(
    'it["transcript_text"] = marked[0][:100].rsplit(" ", 1)[0]', 'it["transcript_text"] = ""')


class OpenrouterUnusableHasNoCounter(_Single):
    """The openrouter path (review_call.py:481-529) writes an unusable verdict like a good
    one and never calls record_review_invalid, so the review-invalid quarantine of finalize
    (run_integrity.py:3242) never fires: the note waits pending, one paid review call per
    pass, until the cycle limit (exit 6 / operator re-arm)."""

    def test_defect_openrouter_unusable_never_counts_and_never_quarantines(self) -> None:
        assert FAKE_UNUSABLE_REVIEW != REVIEW
        (self.tmp / "review.py").write_text(FAKE_UNUSABLE_REVIEW)
        os.environ["ZR_LLM_BACKEND"] = "openrouter"
        self.fake(synth=[reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)])])
        res = self.synth()
        self.assertEqual(res["outcome"], "notes", res)
        rc.review_unit(self.staging, self.zk, self.unit)
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        calls = 1
        for _ in range(3):
            units = ri.pending_units(self.staging, self.run_dir, os.getpid())
            if not units:
                break
            for u in units:
                rc.review_unit(self.staging, self.zk, Path(u))
                calls += 1
                ri.finalize(self.staging, self.run_dir, Path(u), [], 0, self.zk)
        self.assertFalse((self.staging / ri.REVIEW_INVALID).exists())
        self.assertFalse(any("review-invalid" in (v or []) for v in _quarantined(self.staging).values()))
        self.assertTrue(list(self.staging.glob("pending-review/*--*/meta.json")))
        self.assertGreaterEqual(calls, 4)


if __name__ == "__main__":
    unittest.main()
