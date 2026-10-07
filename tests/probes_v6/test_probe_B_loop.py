"""Reviewer B probes: loop.sh with the files backend. Each test asserts the CORRECT
behavior; a failing test proves a defect. No network, no LLM."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.test_run_integrity import FAKE_KEY, PN, ZR, git
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import FICT, reply

import synth_call as sc  # noqa: E402


class LoopProbes(unittest.TestCase):
    def _setup(self, tmp: Path) -> tuple[Path, Path, dict, dict]:
        stg, zk = tmp / "stg", tmp / "vault" / "ZK"
        shutil.copytree(FICT / "lit_fictional", stg / "lit")
        shutil.copytree(FICT / "vault_fictional", zk)
        contract = (ZR / "NOTE_CONTRACT.md").read_text()
        ex = {sec: re.search(r"### " + re.escape(sec) + r".*?```markdown\n(.*?)```", contract, re.S).group(1)
              for sec in ("14.1", "14.2")}
        t1, t2 = (re.search(r"^# (.+)$", ex[k], re.M).group(1) for k in ("14.1", "14.2"))
        replies = {"vid-EXAMPLE0001": reply([("C1", "02:10", "rows three pieces"), ("C2", "02:30", "a story")],
                                            [(t1, ["C1"], ex["14.1"])], [("C2", "not a transferable training claim")]),
                   "vid-EXAMPLE0002": reply([("C1", "01:40", "48 hours")], [(t2, ["C1"], ex["14.2"])])}
        (stg / "queue.json").write_text(json.dumps({"version": 1, "items": [
            {"id": v, "kind": "video", "stage": "extracted", "lit_note": str(stg / "lit" / f"{v}.md")}
            for v in replies]}))
        for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                    ["add", "-A"], ["commit", "-qm", "init"]):
            git(zk, *cmd)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "ZK_"))}
        env.update(ZR_STATE_DIR=str(tmp / "state"), ZR_TEST_RUN="1", VAULT=str(tmp / "vault"), ZK_FOLDER="ZK",
                   ZR_STAGING=str(stg), ZR_SYNTH_STAGES="one", ZR_LLM_BACKEND="files", DEEPSEEK_API_KEY=FAKE_KEY,
                   ZR_SYNTH_SCRIPT="/bin/false", ZR_REVIEW_SCRIPT="/bin/false",
                   ZR_AGENT_SCRIPT="/bin/false", MAX_ITERS="8", MAX_STALL="2", SYNTH_BACKOFF="0",
                   AGENTS_FILE=str(ZR / "AGENTS_transcript.md"))
        return stg, zk, env, replies

    def test_one_worker_writes_and_reviews_through_loop_sh(self) -> None:
        """In the driver flow the review reply always arrives in a later pass and is
        consumed in a PENDING unit, which has no calls/: refuse_workers and
        must_not_share_worker_with are empty, so the writer reviews its own note."""
        tmp = Path(tempfile.mkdtemp(prefix="probeB-"))
        try:
            stg, zk, env, replies = self._setup(tmp)

            def ans(r: dict) -> str | None:
                if r["kind"] == "synth":
                    return next(v for k, v in replies.items() if k in r["user"])
                if r["kind"] == "review":
                    return review_answer(r["user"])
                return None
            codes = []
            for _ in range(5):
                r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
                codes.append(r.returncode)
                if r.returncode != 5:
                    break
                work(stg / "exchange", ans, worker="same-worker")
            xdir = stg / "exchange"
            reviews = [json.loads(p.read_text()) for p in (xdir / "requests").glob("*.json")]
            reviews = [q for q in reviews if q["kind"] == "review"]
            self.assertTrue(reviews, codes)
            pubs = [json.loads(x) for x in (stg / "provenance.jsonl").read_text().splitlines()
                    if '"note-published"' in x] if (stg / "provenance.jsonl").exists() else []
            refused = list((xdir / "replies").glob("*.refused-same-worker.txt"))
            self.assertTrue(refused or not pubs,
                            f"exit codes {codes}; published {len(pubs)} note(s) with synth_worker/review_worker "
                            f"{[(p.get('synth_worker'), p.get('review_worker')) for p in pubs]}; "
                            f"review must_not_share_worker_with: {[q['must_not_share_worker_with'] for q in reviews]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_budget_stop_exit_4_is_replaced_by_5(self) -> None:
        """ZR_MAX_CALLS=1 and two videos: the driver hits the budget (exit 4), and the EXIT
        trap (zr_exchange_exit) turns it into exit 5. The same trap hides exit 2 and 3."""
        tmp = Path(tempfile.mkdtemp(prefix="probeB-"))
        try:
            stg, zk, env, _ = self._setup(tmp)
            env["ZR_MAX_CALLS"] = "1"
            r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            self.assertIn("budget:", r.stderr)
            summ = json.loads(next((stg / "runs").glob("*/summary.json")).read_text())
            self.assertEqual((r.returncode, summ["budget"]["stopped"]), (4, True),
                             f"budget stop reported as exit {r.returncode}; summary budget {summ['budget']}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ok_max_calls_is_per_run_not_per_pass_chain(self) -> None:
        """ZR_MAX_CALLS counts per run folder; every pass is a new run, so the counter
        starts again (README: "limits the requests that one run writes")."""
        tmp = Path(tempfile.mkdtemp(prefix="probeB-"))
        try:
            stg, zk, env, _ = self._setup(tmp)
            env["ZR_MAX_CALLS"] = "1"
            for _ in range(2):
                subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(sc.exchange_status(stg / "exchange")["open"], 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
