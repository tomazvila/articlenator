"""Round 13: the files backend (sub-agent workers). A fake worker answers the request
files between passes. No network, no LLM."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Callable

from tests.unit.test_run_integrity import FAKE_KEY, PN, ZR, git, ri
from tests.unit.test_synth_single import FICT, V5, _Single, reply

import synth_call as sc  # noqa: E402

NC = ZR.parent / "tests" / "fixtures" / "note_contract"


def review_answer(user: str, reject: Callable[[str], bool] = lambda note: False) -> str:
    """A reviewer that fills the skeleton: every item supported with words of its own
    marked passage (or the first item rejected when `reject(note)`)."""
    note = user.split("<<<NOTE\n", 1)[1].split("\nNOTE>>>", 1)[0]
    sk = json.loads(user.split("Reply with this JSON object only:\n", 1)[1])
    imap = {m.group(1): [int(x) for x in re.findall(r"EVIDENCE (\d+)", m.group(2))]
            for m in re.finditer(r"^- (\S+) -> (.*)$", user.split("ITEM PASSAGES", 1)[-1].split("CHECKER HINTS", 1)[0],
                                 re.M)}
    starts = [(int(m.group(1)), m.start()) for m in re.finditer(r"(?m)^\[EVIDENCE (\d+)", user)]
    blocks = {n: user[s:(starts[k + 1][1] if k + 1 < len(starts) else len(user))] for k, (n, s) in enumerate(starts)}
    bad = reject(note)
    for k, it in enumerate(sk["items"]):
        marked = [x for n in imap.get(it["item"], []) for x in re.findall(r"^>>> (.*?) <<<$", blocks.get(n, ""), re.M)] \
            or re.findall(r"^>>> (.*?) <<<$", user, re.M)
        it["verdict"] = "dropped-qualifier" if (bad and k == 0) else "supported"
        it["problem"] = "the title drops a hedge" if (bad and k == 0) else ""
        it["transcript_text"] = marked[0][:100].rsplit(" ", 1)[0] if marked else ""
    sk["scope_complete"] = True
    return json.dumps(sk)


def work(xdir: Path, answer: Callable[[dict], str | None], worker: str | Callable[[dict], str] = "w1") -> int:
    """The fake worker: answers every open request whose answer() is not None."""
    n = 0
    for r in sc.exchange_list(xdir, only_open=True):
        req = json.loads(Path(r["request"]).read_text())
        text = answer(req)
        if text is None:
            continue
        Path(req["reply_path"]).write_text(text)
        w = worker(req) if callable(worker) else worker
        Path(req["meta_path"]).write_text(json.dumps({"key": req["key"], "worker": w, "model": "claude-sonnet-sub"}))
        n += 1
    return n


class ReplyHygieneAndRefusals(_Single):
    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_LLM_BACKEND"] = "files"

    def test_fence_and_bom_are_stripped_and_nothing_else(self) -> None:
        self.assertEqual(sc.reply_text("﻿```markdown\n=====NOTE: A=====\nx\n```\n"), "=====NOTE: A=====\nx\n")
        inner = "=====NOTE: A=====\n```\ncode\n```\n"
        self.assertEqual(sc.reply_text(inner), inner)

    def test_waiting_unit_then_reply_and_no_duplicate_request(self) -> None:
        self.fake()
        res = self.synth()
        self.assertEqual(res["outcome"], "waiting-for-reply")
        xdir = self.staging / "exchange"
        reqs = sc.exchange_list(xdir)
        self.assertEqual(len(reqs), 1)
        req = json.loads(Path(reqs[0]["request"]).read_text())
        self.assertEqual((req["kind"], req["unit"]), ("synth", self.unit.name))
        self.assertIn("<<<TRANSCRIPT", req["user"])
        self.assertTrue(Path(reqs[0]["system_md"]).read_text().strip())
        self.assertFalse(list((self.unit / "out").rglob("*.md")))  # nothing written while waiting
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        self.assertEqual(ev["notes"], [])
        item = json.loads((self.staging / "queue.json").read_text())["items"][0]
        self.assertEqual(item["stage"], "waiting-for-reply")
        self.assertFalse(item.get("synth_attempts"))
        # A second pass without a reply writes no new request.
        u2 = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u2, [V5])
        self.assertEqual(len(list((xdir / "requests").glob("*.json"))), 1)
        # The worker answers; the next pass consumes it (cost 0, backend files, worker recorded).
        work(xdir, lambda r: "```\n" + reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]) + "```\n")
        ri.release_waiting(self.staging)
        u3 = self.new_unit("synth", [V5])
        res = sc.synth_unit(self.staging, self.zk, u3, [V5])
        self.assertEqual(res["notes"], [self.a])
        rec = json.loads((u3 / "calls" / "synth-p1-0.json").read_text())
        self.assertEqual((rec["backend"], rec["worker"], rec["usage"]["cost"]), ("files", "w1", 0.0))
        self.assertEqual(sc.exchange_status(xdir)["open"], 0)

    def test_exchange_cli(self) -> None:
        import sys
        self.fake()
        self.synth()
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)  # the item now waits
        env = dict(os.environ)
        r = subprocess.run([sys.executable, str(ZR / "synth_call.py"), "exchange-list", "--staging", str(self.staging),
                            "--open", "--json"], capture_output=True, text=True, env=env)
        rows = json.loads(r.stdout)
        self.assertEqual([x["kind"] for x in rows], ["synth"])
        for k in ("key", "request", "system_md", "user_md", "reply_path", "words", "must_not_share_worker_with"):
            self.assertIn(k, rows[0])
        r = subprocess.run([sys.executable, str(ZR / "synth_call.py"), "exchange-status", "--staging", str(self.staging),
                            "--count-open"], capture_output=True, text=True, env=env)
        self.assertEqual(r.stdout.strip(), "1 0")  # open (+ needed answered), folders at the cycle limit

    def test_mismatched_meta_and_orphan_reply_are_refused(self) -> None:
        self.fake()
        self.synth()
        xdir = self.staging / "exchange"
        req = sc.exchange_list(xdir)[0]
        Path(req["reply_path"]).write_text(reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]))
        Path(req["meta_path"]).write_text(json.dumps({"key": "0" * 32, "worker": "w1"}))
        (xdir / "replies" / ("f" * 32 + ".txt")).write_text("orphan")
        u2 = self.new_unit("synth", [V5])
        res = sc.synth_unit(self.staging, self.zk, u2, [V5])
        self.assertEqual(res["outcome"], "waiting-for-reply")  # the reply of another key is never taken
        st = sc.exchange_status(xdir)
        self.assertEqual(st["orphans"], ["f" * 32])
        self.assertTrue(st["refused"])

    def test_review_reply_of_the_writer_is_refused(self) -> None:
        self.fake()
        self.synth()
        xdir = self.staging / "exchange"
        work(xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="writer-1")
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])
        import review_call as rc
        rec = rc.review_unit(self.staging, self.zk, u)[0]
        self.assertEqual(rec["end"], "waiting-for-reply")
        rreq = [json.loads(Path(r["request"]).read_text()) for r in sc.exchange_list(xdir) if r["kind"] == "review"][0]
        self.assertTrue(rreq["must_not_share_worker_with"])
        work(xdir, lambda r: review_answer(r["user"]), worker="writer-1")  # the same worker: refused
        rec = rc.review_unit(self.staging, self.zk, u)[0]
        self.assertEqual(rec["end"], "waiting-for-reply")
        self.assertTrue(list((xdir / "replies").glob("*.refused-1.txt")))
        work(xdir, lambda r: review_answer(r["user"]), worker="reviewer-2")
        rec = rc.review_unit(self.staging, self.zk, u)[0]
        self.assertEqual(rec["end"], "finished")
        ev = ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        self.assertEqual({n["action"] for n in ev["notes"]}, {"published"})
        pub = [r for r in ri.provenance.read_records(self.staging) if r.get("event") == "note-published"][-1]
        self.assertEqual((pub["synth_worker"], pub["review_worker"], pub["review_model"]),
                         (["writer-1"], "reviewer-2", "claude-sonnet-sub"))

    def test_max_calls_limits_emitted_requests_and_no_cost_limit(self) -> None:
        os.environ.update(ZR_MAX_CALLS="0", ZR_MAX_COST_USD="0.0000001")
        self.fake()
        self.assertEqual(self.synth()["outcome"], "waiting-for-reply")  # no budget use for a request
        os.environ["ZR_MAX_CALLS"] = "1"
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])  # the same key: not a new request
        self.assertEqual(len(sc.exchange_list(self.staging / "exchange")), 1)

    def test_secrets_never_reach_a_request(self) -> None:
        (self.staging / "lit" / f"{V5}.md").write_text((self.staging / "lit" / f"{V5}.md").read_text() + "\n" + FAKE_KEY)
        self.fake()
        with self.assertRaises(ri.HarnessError):
            self.synth()
        self.assertFalse((self.staging / "exchange" / "requests").is_dir()
                         and list((self.staging / "exchange" / "requests").glob("*.json")))


class TwoStagePasses(_Single):
    """The two-stage synthesis of the fictional video reaches publication through passes."""

    def test_passes_to_publication(self) -> None:
        os.environ.update(ZR_LLM_BACKEND="files", ZR_SYNTH_STAGES="two")
        vid = "vid-EXAMPLE0002"
        shutil.copy(NC / "lit_fictional" / f"{vid}.md", self.staging / "lit")
        for p in (NC / "vault_fictional").rglob("Mara Kell*.md"):  # the link target of B's fixture
            shutil.copy(p, self.zk / PN / p.name)
        self.write_queue([{"id": vid, "kind": "video", "stage": "claimed", "claimed_from": "extracted",
                           "lit_note": str(self.staging / "lit" / f"{vid}.md")}])
        xdir = self.staging / "exchange"
        answers = {"inv": (NC / "inventory_reply_fictional.txt").read_text(),
                   "batch": (NC / "batch_reply_fictional.txt").read_text()}
        last = "=====SKIPPED=====\nC8 | not a transferable training claim: he only repeats the warm-up advice\n"

        def ans(r: dict) -> str | None:
            if r["kind"] == "batch":
                return answers["batch"] if "BATCH 1 of" in r["user"] else last
            return answers.get(r["kind"])
        res, keys_seen = None, []
        for _pass in range(5):
            ri.release_waiting(self.staging)
            u = self.new_unit("synth", [vid])
            res = sc.synth_unit(self.staging, self.zk, u, [vid])
            keys_seen.append(len(list((xdir / "requests").glob("*.json"))))
            if res["outcome"] != "waiting-for-reply":
                break
            work(xdir, ans, worker="writer")
        self.assertEqual(res["outcome"], "notes")
        if os.environ.get("E2E_DEBUG"):
            print(json.dumps(json.loads((u / "claims.json").read_text())["problems"], indent=1)[:3000])
        self.assertEqual(len(res["notes"]), 4)
        self.assertEqual(keys_seen, sorted(keys_seen))  # requests only grow, one per key
        self.assertEqual(sc.exchange_status(xdir)["answered"], keys_seen[-1])
        import review_call as rc
        rc.review_unit(self.staging, self.zk, u)
        work(xdir, lambda r: review_answer(r["user"]) if r["kind"] == "review" else None, worker="reviewer")
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [vid], 0, self.zk)
        acts = {Path(n["note"]).stem[:30]: (n["action"], n["failure_types"]) for n in ev["notes"]}
        self.assertEqual(sum(1 for n in ev["notes"] if n["action"] == "published"), 4, acts)


class LoopPasses(unittest.TestCase):
    """loop.sh with the files backend: exit 5 while requests wait; the passes reach the
    same final state as a live run (both fictional videos published, no attempts)."""

    def test_loop_passes_and_exit_code(self) -> None:
        from tests.unit.test_synth_single import FAKE  # noqa: F401 - not used: no script call
        tmp = Path(tempfile.mkdtemp(prefix="files-e2e-"))
        try:
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

            def ans(r: dict) -> str | None:
                if r["kind"] == "synth":
                    return next(v for k, v in replies.items() if k in r["user"])
                if r["kind"] == "review":
                    return review_answer(r["user"])
                return None
            codes = []
            for _pass in range(5):
                r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
                codes.append(r.returncode)
                if os.environ.get("E2E_DEBUG"):
                    print(r.stdout[-3000:], r.stderr[-3000:])
                if r.returncode != 5:
                    break
                self.assertIn("wait for a worker reply", r.stderr)
                work(stg / "exchange", ans, worker=lambda q: "reviewer" if q["kind"] == "review" else "writer")
            self.assertEqual(codes[0], 5)
            self.assertEqual(codes[-1], 0, codes)
            pub = {p.stem for p in (zk / PN).glob("*.md")}
            self.assertLessEqual({t1, t2}, pub)
            items = {it["id"]: it for it in json.loads((stg / "queue.json").read_text())["items"]}
            self.assertEqual({it["stage"] for it in items.values()}, {"synthesized"})
            self.assertEqual(sum(it.get("synth_attempts", 0) for it in items.values()), 0)
            st = sc.exchange_status(stg / "exchange")
            self.assertEqual((st["open"], st["orphans"]), (0, []))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RepairPasses(_Single):
    """The round-11 end-to-end repair (6 bullets, two videos, a split, note B rejected)
    through passes of the files backend: the same final state."""
    kind = "repair"
    V2 = "vid-EXAMPLE0001"

    def test_repair_through_passes(self) -> None:
        import review_call as rc
        from tests.unit.test_run_integrity import OLD_NOTE
        os.environ["ZR_LLM_BACKEND"] = "files"
        os.environ["ZR_REVIEW_REPAIR"] = "0"
        shutil.copy(NC / "lit_fictional" / f"{self.V2}.md", self.staging / "lit")
        src = "".join(f"  - id: {v}\n    speaker: X\n" for v in (V5, self.V2))
        text = OLD_NOTE.format(t="Old Six").replace("created:", f"sources:\n{src}created:").replace(
            "- legacy bullet\n", "".join(f"- legacy bullet {i}\n" for i in range(1, 7)))
        self.vpath("Old Six").write_text(text)
        rel = f"{PN}/Old Six.md"
        state = sc.state_path(self.staging)
        xdir = self.staging / "exchange"
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        ledger1 = "\n".join([f"Old Six.md#b1 | kept in {self.a}", f"Old Six.md#b2 | corrected in {self.a}",
                             f"Old Six.md#b3 | kept in {self.b}"] +
                            [f"Old Six.md#b{i} | dropped: not in this transcript" for i in (4, 5, 6)])
        rep1 = (f"=====NOTE: {self.a} | repairs: Old Six.md | bullets: b1,b2=====\n{self.ta.rstrip()}\n"
                f"=====NOTE: {self.b} | repairs: Old Six.md | bullets: b3=====\n{self.tb.rstrip()}\n"
                f"=====BULLETS=====\n{ledger1}\n")
        rep2 = (f"=====NOTE: {self.c} | repairs: Old Six.md | bullets: b4=====\n{self.tc.rstrip()}\n"
                f"=====BULLETS=====\nOld Six.md#b3 | dropped: not in this transcript\nOld Six.md#b4 | kept in {self.c}\n"
                "Old Six.md#b5 | dropped: not in this transcript\nOld Six.md#b6 | dropped: not in this transcript\n")
        b_text = self.tb

        def ans(r: dict) -> str | None:
            if r["kind"] == "repair":
                return rep1 if V5 in r["user"].split("OLD NOTES TO REPAIR")[0] else rep2
            if r["kind"] == "review":
                return review_answer(r["user"], reject=lambda note: note.strip() == b_text.strip())
            return None
        passes, run = 0, self.run_dir
        while passes < 12:
            passes += 1
            ri.release_waiting(self.staging)
            b = sc.next_batch(self.staging, self.zk, state)
            if b is None:
                break
            u = self.new_unit("repair", [])
            res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                 b["prior_titles"], b["last"])
            sc.record_answers(state, b, res)
            if res.get("stopped") == "waiting-for-reply":
                work(xdir, ans, worker="writer")
                continue
            while [r for r in rc.review_unit(self.staging, self.zk, u) if r["end"] == "waiting-for-reply"]:
                work(xdir, ans, worker="reviewer")
            ri.finalize(self.staging, run, u, [], 0, self.zk)
        stub = self.vpath("Old Six").read_text()
        self.assertIn(f"[[{self.c}]]", stub)
        self.assertIn(f"[[{self.a}]]", stub)
        self.assertNotIn(f"[[{self.b}]]", stub)
        self.assertEqual(ri.replacement_beside_old(self.staging, self.zk), [])
        e = json.loads(state.read_text())["notes"][rel]
        # round 20: the "kept in" claims for b3 and b4 name notes that cite no time near their
        # passages: the claims are unverified, so both stay open for the operator (never settled)
        # pilot 8 (round 21): b3 (named note B rejected) is target-rejected, open, never unsupported
        self.assertEqual(sorted(e["unsupported_bullets"] + e.get("unverified_bullets", []) + e.get("rejected_bullets", [])),
                         ["Old Six.md#b3", "Old Six.md#b4", "Old Six.md#b5", "Old Six.md#b6"])
        st = sc.exchange_status(xdir)
        self.assertEqual((st["open"], st["orphans"]), (0, []))
        # one request per key: 2 repair calls and the reviews of the written notes
        kinds = [json.loads(p.read_text())["kind"] for p in (xdir / "requests").glob("*.json")]
        self.assertEqual(kinds.count("repair"), 2)
        self.assertEqual(len(set(p.stem for p in (xdir / "requests").glob("*.json"))), len(kinds))
