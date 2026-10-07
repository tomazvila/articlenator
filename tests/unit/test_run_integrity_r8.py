"""Round 8 tests (third real-model pilot): targeted repair turn after a review
rejection, queue and index bookkeeping, pending-step fixes, fix-turn input, review
token limit, new checker rules, review hints, full helper logs. No network, no LLM.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.unit.test_run_integrity import (
    FAKE_KEY,
    HONEST,
    PN,
    RADO,
    SASA,
    VID,
    ZR,
    _Env,
    agent,
    git,
    good_text,
    ri,
    seed_link_targets,
)
from tests.unit.test_run_integrity_r6 import _ReviewEnv

import review_call as rc  # noqa: E402
import verify_claims as vc  # noqa: E402

V5 = "vid-5zALUKd7h3g"
D_TITLE = "Radoslav Radev Repeats That Planche Lean Presses Work Without Parallettes"

# The synthesis agent writes 4 notes (index_add for each, one clock-out); the review
# repair turn rewrites the two rejected notes (C fixed, D not).
E2E_AGENT = r'''
import json, os, re, sys
from pathlib import Path
sys.path.insert(0, os.environ["HERE"])
import deepseek_agent as da
prompt = sys.stdin.read()
pn = Path(os.environ["ZK_DIR"]) / "01 Permanent Notes"
notes = json.loads(os.environ["E2E_NOTES"])
def tool(name, **kw):
    out, _ = da.run_tool({"function": {"name": name, "arguments": json.dumps(kw)}})
    return json.loads(out)
if prompt.startswith("REVIEW REPAIR TURN"):
    for title in notes["rejected"]:
        if f"NOTE: 01 Permanent Notes/{title}.md" in prompt:
            tool("write_file", path=str(pn / f"{title}.md"), content=notes["write"][title] + "\n")  # the rewrite
    Path(os.environ["E2E_LOG"]).write_text(prompt)
    sys.exit(0)
for title, text in notes["write"].items():
    tool("write_file", path=str(pn / f"{title}.md"), content=text)
    lead = re.search(r"^# .+\n\n(.+)$", text, re.M).group(1)
    tool("run_command", command="python3", args=["index_add.py", "--title", title, "--file",
         f"01 Permanent Notes/{title}.md", "--gist", lead[:100], "--tags", "calisthenics,planche-lean",
         "--source", "vid-5zALUKd7h3g", "--speaker", "Radoslav Radev"])
tool("run_command", command="python3", args=["queue_mark.py", "--ids", "vid-5zALUKd7h3g", "--stage", "synthesized",
     "--notes", ";".join(notes["write"])])
'''

E2E_REVIEW = r'''
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
note = user.split("<<<NOTE\n", 1)[1].split("\nNOTE>>>", 1)[0]
sk = json.loads(user.split("Reply with this JSON object only:\n", 1)[1])
marked = re.findall(r"^>>> (.*?) <<<$", user, re.M)
roles = json.loads(os.environ["E2E_ROLES"])
role = roles.get(title, "ok")
reject = role == "always" or (role == "once" and not note.endswith("\n\n"))
for k, it in enumerate(sk["items"]):
    it["verdict"] = "dropped-qualifier" if (reject and k == 0) else "supported"
    it["problem"] = "the title drops 'you can'" if (reject and k == 0) else ""
    marked = item_marked(it["item"])
    it["transcript_text"] = marked[0][:100].rsplit(" ", 1)[0]
sk["scope_complete"] = True
print(json.dumps({"choices": [{"message": {"content": json.dumps(sk)}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.001}}))
'''


class ReviewRepairEndToEnd(unittest.TestCase):
    """4 notes: 2 supported, 2 rejected -> one targeted repair turn -> 1 fixed and
    published, 1 rejected again and quarantined; the queue item is synthesized, the
    video is not synthesized again, and the index holds only published notes."""

    def test_loop(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="r8e2e-"))
        try:
            stg, zk = tmp / "stg", tmp / "vault" / "ZK"
            (stg / "lit").mkdir(parents=True)
            shutil.copy(HONEST / "lit" / f"{V5}.md", stg / "lit")
            (zk / PN).mkdir(parents=True)
            seed_link_targets(zk)
            v5 = sorted(p for p in (HONEST / "notes").glob("*.md") if f"id: {V5}" in p.read_text())
            texts = {p.stem: p.read_text() for p in v5}
            a, b, c = sorted(texts)
            base = texts[[t for t in texts if "Floor" in t][0]]
            texts[D_TITLE] = re.sub(r"^# .+$", f"# {D_TITLE}", base, count=1, flags=re.M)
            (stg / "queue.json").write_text(json.dumps({"version": 1, "items": [
                {"id": V5, "kind": "video", "stage": "extracted", "lit_note": str(stg / "lit" / f"{V5}.md")}]}))
            (stg / "concept-index.json").write_text('{"version":1,"concepts":[],"mocs":[]}')
            for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
                git(zk, *cmd)
            (tmp / "agent.py").write_text(E2E_AGENT)
            (tmp / "review.py").write_text(E2E_REVIEW)
            rejected = [c, D_TITLE]
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "ZK_"))}
            env.update(ZR_STATE_DIR=str(tmp / "state"), ZR_TEST_RUN="1", ZR_SYNTH_MODE="agent", VAULT=str(tmp / "vault"), ZK_FOLDER="ZK",
                       ZR_STAGING=str(stg), ZR_AGENT_SCRIPT=str(tmp / "agent.py"),
                       ZR_REVIEW_SCRIPT=str(tmp / "review.py"), DEEPSEEK_API_KEY=FAKE_KEY, MAX_ITERS="4",
                       MAX_STALL="1", SYNTH_BACKOFF="0", AGENT_TIMEOUT="60", AGENTS_FILE=str(ZR / "AGENTS_transcript.md"),
                       E2E_NOTES=json.dumps({"write": texts, "rejected": rejected}), E2E_LOG=str(tmp / "repair.txt"),
                       E2E_ROLES=json.dumps({c: "once", D_TITLE: "always"}))
            r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            if os.environ.get("E2E_DEBUG"):
                print(r.stdout[-5000:], r.stderr[-3000:])
            pub = {p.stem for p in (zk / PN).glob("*.md")}
            self.assertEqual({a, b, c} & pub, {a, b, c})
            self.assertNotIn(D_TITLE, pub)
            msg = (tmp / "repair.txt").read_text()
            self.assertTrue(msg.startswith("REVIEW REPAIR TURN"))
            for part in (f"NOTE: {PN}/{c}.md", f"NOTE: {PN}/{D_TITLE}.md", "TRANSCRIPT FILE(S):", "REJECTED ITEMS:",
                         "Problem: the title drops 'you can'", "Transcript words:", ">>> "):
                self.assertIn(part, msg, part)
            self.assertNotIn(f"NOTE: {PN}/{a}.md", msg)
            units = list(stg.glob("runs/*/units/*"))
            self.assertEqual(len(units), 1)  # no second synthesis of the video
            ev = [json.loads(x) for x in next(stg.glob("runs/*/events.jsonl")).read_text().splitlines()]
            notes = {Path(n["note"]).stem: n for n in ev[-1]["notes"]}
            self.assertIn("review-rejected-twice", notes[D_TITLE]["failure_types"])
            item = json.loads((stg / "queue.json").read_text())["items"][0]
            self.assertEqual((item["stage"], item["notes_published"], item["notes_quarantined"]), ("synthesized", 3, 1))
            self.assertEqual(item.get("synth_attempts", 0), 0)
            idx = {x["title"] for x in json.loads((stg / "concept-index.json").read_text())["concepts"]}
            self.assertEqual(idx, {a, b, c})
            # The reviews: 4 first calls + 2 after the repair turn; the passed notes are not reviewed again.
            reviews = list(units[0].glob("review-*/review.json"))
            self.assertEqual(len(reviews), 6)
            summ = json.loads(next(stg.glob("runs/*/summary.json")).read_text())
            self.assertEqual([Path(n["note"]).stem for n in summ["notes_rejected_twice"]], [D_TITLE])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 1c: index rollback
# --------------------------------------------------------------------------- #


class IndexRollback(_Env):
    def test_quarantined_and_folded_entries(self) -> None:
        self.vpath(RADO).write_text(good_text(RADO))
        idx = {"version": 1, "mocs": [], "concepts": [
            {"title": SASA, "file": f"{PN}/{SASA}.md", "sources": [{"id": VID}]},
            {"title": RADO, "file": f"{PN}/{RADO}.md", "sources": [{"id": "vid-new"}, {"id": "vid-old"}]},
            {"title": "Other", "file": f"{PN}/Other.md"}]}
        (self.staging / "concept-index.json").write_text(json.dumps(idx))
        (self.staging / "provenance.json").write_text(json.dumps({f"{PN}/{SASA}.md": [VID], f"{PN}/Other.md": ["x"]}))
        done = ri.index_rollback(self.staging, self.zk, [f"{PN}/{SASA}.md", f"{PN}/{RADO}.md"])
        self.assertEqual(done, sorted([f"{PN}/{SASA}.md", f"{PN}/{RADO}.md"]))
        out = json.loads((self.staging / "concept-index.json").read_text())["concepts"]
        self.assertEqual([c["title"] for c in out], [RADO, "Other"])
        rado = out[0]
        fm, _, _ = vc.split_frontmatter(good_text(RADO))
        self.assertEqual([s["id"] for s in rado["sources"]], vc._source_ids(fm))  # back to the vault file
        self.assertNotIn("vid-new", [s["id"] for s in rado["sources"]])
        self.assertNotIn(f"{PN}/{SASA}.md", json.loads((self.staging / "provenance.json").read_text()))

    def test_quarantine_at_finalize_rolls_back(self) -> None:
        (self.staging / "concept-index.json").write_text(json.dumps(
            {"version": 1, "mocs": [], "concepts": [{"title": "Bad Copy", "file": f"{PN}/Bad Copy.md"}]}))
        from tests.unit.test_run_integrity import bad_text
        agent.write_file(str(self.vpath("Bad Copy")), bad_text().replace(f"# {SASA}", "# Bad Copy"))
        ev = self.finalize(0)
        self.assertEqual(ev["index_rolled_back"], [f"{PN}/Bad Copy.md"])
        self.assertEqual(json.loads((self.staging / "concept-index.json").read_text())["concepts"], [])

    def test_quarantine_folder_is_not_readable(self) -> None:
        q = self.staging / "quarantine" / "r" / "u" / PN
        q.mkdir(parents=True)
        (q / "X.md").write_text("x")
        with self.assertRaises(agent.AgentError):
            agent.read_file(str(q / "X.md"))


# --------------------------------------------------------------------------- #
# 2: pending step
# --------------------------------------------------------------------------- #


class PendingStep(_ReviewEnv):
    def test_pending_unit_is_not_an_attempt_even_from_claimed(self) -> None:
        self.review("invalid")
        self.finalize(0)
        q = self.queue()
        q[0]["stage"] = "claimed"  # the parallel claim that the budget stop left behind
        self.write_queue(q)
        u = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        item = self.queue()[0]
        self.assertEqual(item.get("synth_attempts", 0), 0)
        self.assertNotIn(item["stage"], ("incomplete", "failed", "claimed"))

    def test_dead_link_in_pending_note_is_dropped(self) -> None:
        self.review("invalid")
        self.finalize(0)
        # The pending copy links a draft that was renamed later (the pilot's protein note).
        pend = next((self.staging / "pending-review").glob(f"*/out/{self.rel}"))
        pend.write_text(re.sub(r"## Connected Ideas\n\n.*", "## Connected Ideas\n\n- [[A Renamed Draft]] — sibling\n",
                               pend.read_text(), flags=re.S))
        u = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        os.environ["FAKE_REVIEW_MODE"] = "valid"
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        n = self.note(ev, SASA)
        self.assertEqual((n["action"], n["links_dropped"]), ("published", ["A Renamed Draft"]))
        self.assertIn("[[Home]]", self.vpath(SASA).read_text())

    def test_rereview_command(self) -> None:
        self.review("valid")
        self.finalize(0)
        os.environ["FAKE_REVIEW_MODE"] = "defect"
        r = subprocess.run([sys.executable, str(ZR / "run_integrity.py"), "rereview", "--staging", str(self.staging),
                            "--vault", str(self.zk), "--note", self.rel, "--model", "test/other-model"],
                           env=dict(os.environ), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["review"]["end"], out["review"]["model"], out["bad"]),
                         ("finished", "test/other-model", ["changed-number"]))
        self.assertIn("verification: source-checked", self.vpath(SASA).read_text())  # the vault does not change

    def test_every_owner_driver_releases_all_claims(self) -> None:
        lib = (ZR / "run_lib.sh").read_text()
        self.assertIn("--release-all", lib.split("zr_run_summary() {", 1)[1].split("\n}", 1)[0])
        for name in ("loop.sh", "loop_parallel.sh", "parallel_synth.sh", "parallel_worker.sh", "repair_loop.sh",
                     "review_loop.sh", "parallel_review.sh", "review_worker.sh"):
            self.assertIn("trap 'zr_run_summary' EXIT", (ZR / name).read_text(), name)


# --------------------------------------------------------------------------- #
# 3: fix turn input
# --------------------------------------------------------------------------- #


class FixTurnInput(_Env):
    def test_drafts_leave_and_report_has_lit_path_and_passages(self) -> None:
        from tests.unit.test_run_integrity import bad_text
        agent.write_file(str(self.vpath(SASA)), bad_text())
        agent.write_file(str(self.vpath("Abandoned Draft")), bad_text().replace(f"# {SASA}", "# Abandoned Draft"))
        subprocess.run([sys.executable, str(ZR / "queue_mark.py"), "--ids", VID, "--stage", "synthesized", "--notes", SASA],
                       env=dict(os.environ), capture_output=True, text=True, check=True)
        gone = ri.discard_drafts(self.staging, self.unit, self.zk)
        self.assertEqual([g["note"] for g in gone], [f"{PN}/Abandoned Draft.md"])
        self.assertFalse((self.unit / "out" / PN / "Abandoned Draft.md").exists())
        self.assertTrue(Path(gone[0]["path"]).is_file())
        rep = ri.fix_report(self.staging, self.unit, self.zk)
        self.assertIn(f"NOTE: {PN}/{SASA}.md", rep)
        self.assertNotIn("Abandoned Draft", rep)
        self.assertIn(str(self.staging / "lit" / f"{VID}.md"), rep)
        self.assertIn("changed-period", rep)
        self.assertIn(">>> ", rep)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, "Abandoned Draft")["action"], "discarded-draft")

    def test_fix_turn_limit_ends_cleanly(self) -> None:
        (self.unit / "agent_result.json").write_text(json.dumps({"rc": 3, "end": "limit", "detail": "max_tokens"}))
        self.assertEqual(ri.classify_end(self.unit, 0)[0], "limit")
        (self.unit / "fix-turn-limit.json").write_text("{}")
        self.assertEqual(ri.classify_end(self.unit, 0)[0], "finished")
        lib = (ZR / "run_lib.sh").read_text()
        self.assertIn('DEEPSEEK_MAX_TOKENS="${ZR_FIX_MAX_TOKENS:-20000}"', lib)


# --------------------------------------------------------------------------- #
# 4: review token limit
# --------------------------------------------------------------------------- #


LENGTH_LLM = r'''
import json, os, sys
from pathlib import Path
body = json.load(sys.stdin)
log = Path(os.environ["LEN_LOG"])
with open(log, "a") as f:
    f.write(json.dumps({"reasoning": body.get("reasoning"), "last": body["messages"][-1]["content"][:80],
                        "max_tokens": body["max_tokens"]}) + "\n")
n = len(log.read_text().splitlines())
if os.environ.get("LEN_REFUSE") and body.get("reasoning"):
    sys.stderr.write("HTTP 400: unknown parameter"); sys.exit(1)
if n == 1:
    print(json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {"cost": 0.002}}))
    sys.exit(0)
import io
sys.stdin = io.StringIO(json.dumps(body))
exec(open(os.environ["FAKE_OK"]).read())
'''


class ReviewLength(_ReviewEnv):
    def _run(self, **env: str) -> list[dict]:
        (self.tmp / "len.py").write_text(LENGTH_LLM)
        os.environ.update(ZR_REVIEW_SCRIPT=str(self.tmp / "len.py"), LEN_LOG=str(self.tmp / "len.jsonl"),
                          FAKE_OK=str(self.tmp / "fake_llm.py"), **env)
        rec = self.review("valid")
        self.assertEqual(rec["end"], "finished")
        return [json.loads(x) for x in (self.tmp / "len.jsonl").read_text().splitlines()]

    def test_length_stop_retries_with_json_only_line(self) -> None:
        calls = self._run()
        self.assertEqual(calls[0]["max_tokens"], 12000)
        self.assertEqual(calls[1]["last"], rc.LENGTH_RETRY_LINE[:80])
        self.assertIsNone(calls[0]["reasoning"])  # a model outside ZR_SYNTH_REASONING_MODELS gets none
        self.assertIsNone(calls[1]["reasoning"])  # round 11: the same setting as the first call
        self.assertEqual(calls[1]["max_tokens"], 18000)  # and 1.5 x max_tokens

    def test_reasoning_setting_and_refusal(self) -> None:
        calls = self._run(ZR_REVIEW_RETRY_REASONING='{"effort": "low"}')
        self.assertEqual(calls[1]["reasoning"], {"effort": "low"})
        (self.tmp / "len.jsonl").unlink()
        calls = self._run(ZR_REVIEW_RETRY_REASONING='{"effort": "low"}', LEN_REFUSE="1")
        self.assertEqual([c["reasoning"] for c in calls], [None, {"effort": "low"}, None])


# --------------------------------------------------------------------------- #
# 5: checker additions
# --------------------------------------------------------------------------- #


class CheckerRound8(unittest.TestCase):
    def test_ellipsis_cue(self) -> None:
        gap = " or that's actually what evo you know penikioni yeah yeah that's what he got me to do and i was like "
        self.assertTrue(vc.ellipsis_cue(gap, ["SasaVenos", "David Packer"], {"David Packer"}))
        self.assertTrue(vc.ellipsis_cue(" but you should not do that ", [], set()))
        self.assertTrue(vc.ellipsis_cue(" according to my coach ", [], set()))
        self.assertTrue(vc.ellipsis_cue(" and then she says it ", ["A"], {"A"}))
        self.assertIsNone(vc.ellipsis_cue(" and then you hold it for a bit and ", [], set()))
        self.assertIsNone(vc.ellipsis_cue(" david and i ", ["David Packer"], {"David Packer"}))

    def test_speaker_evidence_is_claim(self) -> None:
        q = ["i don't recommend giant band at all i only recommend like band yeah an"]
        self.assertTrue(vc.speaker_evidence_is_claim(q[0], q, "Lead.", ["SasaVenos", "David Packer"]))
        self.assertFalse(vc.speaker_evidence_is_claim("so how would you go from zero to the front lever", q, "Lead.",
                                                      ["SasaVenos", "David Packer"]))
        self.assertTrue(vc.speaker_evidence_is_claim("he trains front lever three times per week", [],
                                                     "He trains front lever three times per week.", ["X"]))

    def test_title_drops_frame(self) -> None:
        lead = vc.extract_quantities("if you train four times per week only one maximum two to go to a failure")
        title = vc.extract_quantities("At Most One Or Two Failure Sessions Per Week")
        self.assertTrue(vc.title_drops_frame(title[-1], lead))
        ok = vc.extract_quantities("Only One Maximum Two Failure Workouts At Four Workouts Per Week")
        self.assertIsNone(vc.title_drops_frame(ok[0], lead))

    def test_example_hedge_not_hinted_when_marked(self) -> None:
        q = ["let's say three minutes of rest between the attempts"]
        self.assertEqual(vc.dropped_hedge("Rest three minutes between attempts.", "X Rests Three Minutes", q, [(3.0,)]),
                         "let's say")
        self.assertIsNone(vc.dropped_hedge("Rest three minutes between attempts.", "X Rests Three Minutes", q, [(3.0,)],
                                           example_marked=True))


class PilotThreeNotes(unittest.TestCase):
    """The pilot-3 defects that these rules catch, on copies of the real notes."""

    P3 = Path(__file__).resolve().parents[1] / "fixtures" / "zettel_pilot3"

    def test_p13_and_speaker_evidence(self) -> None:
        lit = vc.LitIndex([self.P3 / "lit"])
        types = {p.stem[:40]: {f["type"] for f in vc.verify_note(p, lit)["failures"]}
                 for p in (self.P3 / "notes").glob("*.md")}
        p13 = next(t for k, t in types.items() if k.startswith("David Packer Recommends Six To Eight"))
        self.assertIn("ellipsis-hides-attribution", p13)
        claim_ev = [k for k, t in types.items() if "speaker-evidence-is-claim" in t]
        self.assertEqual(len(claim_ev), 4, claim_ev)


# --------------------------------------------------------------------------- #
# 6 / 7: review input and helper logs
# --------------------------------------------------------------------------- #


class ReviewInput(_Env):
    def test_lead_maps_to_all_evidence(self) -> None:
        text = good_text()
        items = ri.review_items(text)
        imap = ri.item_passages(text, items)
        n_ev = len([i for i in items if i["item"].startswith("evidence-")])
        self.assertEqual(imap["lead"], list(range(1, n_ev + 1)))
        self.assertEqual(imap["title"], list(range(1, n_ev + 1)))
        self.assertEqual(imap["evidence-1"], [1])

    def test_hint_types(self) -> None:
        self.assertEqual(set(ri.REVIEW_HINT_TYPES),
                         {"low-quote-support", "added-qualifier", "dropped-hedge", "title-drops-frame"})
        for t in ri.REVIEW_HINT_TYPES:
            self.assertNotIn("quote", t.replace("low-quote-support", ""))

    def test_helper_results_are_kept_in_full(self) -> None:
        agent.write_file(str(self.vpath(SASA)), good_text())
        agent.run_command("python3", ["verify_claims.py", f"{PN}/{SASA}.md", "--lit", str(self.staging / "lit")])
        rec = json.loads((self.unit / "helper_results.jsonl").read_text().splitlines()[-1])
        self.assertEqual(rec["helper"], "verify_claims.py")
        self.assertIsInstance(rec["stdout"], dict)
        self.assertIn("notes", rec["stdout"])
        self.assertNotIn(FAKE_KEY, (self.unit / "helper_results.jsonl").read_text())


class ScopeTranscriptText(unittest.TestCase):
    def test_scope_item_may_list_values(self) -> None:
        toks = rc._tokens("when i started to learn planche my advice for you guys is to squeeze the core")
        self.assertTrue(rc.transcript_text_ok("planche; my advice for you guys; when i started", toks, "scope"))
        self.assertTrue(rc.transcript_text_ok("planche, squeeze the core", toks, "scope"))
        self.assertFalse(rc.transcript_text_ok("planche; front lever on rings", toks, "scope"))
        self.assertFalse(rc.transcript_text_ok("planche; my advice for you guys; when i started", toks, "details-1"))
        self.assertIsNone(rc.validate({"items": [{"item": "scope", "verdict": "supported", "transcript_text": ""}],
                                       "scope_complete": True}, ["scope"], "any passage"))
