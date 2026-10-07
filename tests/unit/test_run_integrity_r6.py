"""Round 6 tests (second real-model pilot): the single-call source-check review with a
fake LLM endpoint, link rules (self-link, link-waiting), the pilot's Packer fix-turn
sequence, cost records and the run budget, the new checker rules (scope-not-in-quote,
dropped-hedge, speaker-evidence-missing, minor loop spans) and stage times.
No network, no LLM.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.unit.test_run_integrity import (
    PN,
    RADO,
    SASA,
    ZR,
    _Env,
    agent,
    good_text,
    ri,
)

import review_call as rc  # noqa: E402
import verify_claims as vc  # noqa: E402

ROOT = ZR.parent
P2 = ROOT / "tests" / "fixtures" / "zettel_pilot2"

FAKE_LLM = r'''
import json, os, sys, time
from pathlib import Path
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
mode = os.environ.get("FAKE_REVIEW_MODE", "valid")
calls = Path(os.environ["FAKE_REVIEW_LOG"])
n = len(calls.read_text().splitlines()) if calls.exists() else 0
with open(calls, "a") as f:
    f.write(json.dumps({"keys": sorted(body), "messages": len(body["messages"]),
                        "response_format": body.get("response_format")}) + "\n")
if mode == "timeout":
    time.sleep(10)
if mode == "json-mode-refused" and body.get("response_format"):
    sys.stderr.write("HTTP 400: response_format is not supported by this model")
    sys.exit(1)
user = body["messages"][1]["content"]
sk = json.loads(user.split("Reply with this JSON object only:\n", 1)[1])
import re
marked = re.findall(r"^>>> (.*?) <<<$", user, re.M)
for it in sk["items"]:
    it["verdict"] = "supported"
    marked = item_marked(it["item"])
    it["transcript_text"] = marked[0][:120].rsplit(" ", 1)[0]
sk["scope_complete"] = True
if mode == "defect":
    sk["items"][0]["verdict"] = "changed-number"
    sk["items"][0]["problem"] = "the title changes the number"
if mode == "partial":
    sk["items"].pop()
if mode == "invalid" or (mode == "invalid-then-valid" and n == 0):
    content = "Here is my review: the note looks good overall."
else:
    content = "```json\n" + json.dumps(sk) + "\n```"
print(json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}],
                  "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "cost": 0.01}}))
'''


class _ReviewEnv(_Env):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "fake_llm.py").write_text(FAKE_LLM)
        os.environ.update(ZR_REVIEW_SCRIPT=str(self.tmp / "fake_llm.py"), FAKE_REVIEW_LOG=str(self.tmp / "calls.jsonl"),
                          ZR_REVIEW_TIMEOUT="3", ZR_REVIEW_MODEL="test/review-model")
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.rel = f"{PN}/{SASA}.md"
        q = self.queue()
        q[0]["stage"] = "synthesized"  # the agent's queue_mark claim
        self.write_queue(q)

    def review(self, mode: str) -> dict:
        os.environ["FAKE_REVIEW_MODE"] = mode
        return rc.review_note(self.staging, self.zk, self.unit, self.rel, self.unit / "out" / self.rel, ["hint one"])

    def calls(self) -> list[dict]:
        p = self.tmp / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class SingleCallReview(_ReviewEnv):
    def test_valid_verdict_publishes(self) -> None:
        rec = self.review("valid")
        self.assertEqual((rec["end"], rec["attempts"], rec["by"], rec["model"]), ("finished", 1, "harness", "test/review-model"))
        rdir = sorted(self.unit.glob("review-*"))[-1]
        for name in ("request.json", "skeleton.json", "messages.json", "reply-0.json", "verdict.json", "agent_result.json",
                     "review.json"):
            self.assertTrue((rdir / name).is_file(), name)
        self.assertEqual(json.loads((rdir / "agent_result.json").read_text())["usage"]["cost"], 0.01)
        ev = self.finalize(0)
        self.assertEqual(self.note(ev, SASA)["action"], "published")
        self.assertIn("verification: source-checked", self.vpath(SASA).read_text())
        self.assertAlmostEqual(ev["usage"]["cost"], 0.01)

    def test_request_shape(self) -> None:
        self.review("valid")
        body = json.loads((sorted(self.unit.glob("review-*"))[-1] / "messages.json").read_text())
        self.assertLessEqual({"max_tokens", "messages", "model", "response_format", "stream", "temperature"}, set(body))
        self.assertLessEqual(set(body) - {"max_tokens", "messages", "model", "response_format", "stream", "temperature"},
                             {"usage"})  # OpenRouter: usage.include
        self.assertNotIn("tools", body)
        self.assertEqual((body["max_tokens"], body["response_format"], body["model"]),
                         (12000, {"type": "json_object"}, "test/review-model"))
        sysmsg, user = body["messages"]
        self.assertEqual(sysmsg["content"], (ZR / "prompts" / "source_check.md").read_text())
        self.assertIn(f"NOTE ({self.rel}):", user["content"])
        self.assertIn("CITED PASSAGES", user["content"])
        self.assertIn("<<<NOTE", user["content"])
        self.assertIn("ITEM PASSAGES", user["content"])
        self.assertTrue(user["content"].startswith(rc.DATA_RULE))
        self.assertIn("- hint one", user["content"])
        sk = json.loads(user["content"].split("Reply with this JSON object only:\n", 1)[1])
        self.assertEqual(set(sk["items"][0]), {"item", "text", "tag", "verdict", "also", "transcript_text", "problem"})

    def test_invalid_json_then_valid(self) -> None:
        rec = self.review("invalid-then-valid")
        self.assertEqual((rec["end"], rec["attempts"]), ("finished", 2))
        self.assertEqual([c["messages"] for c in self.calls()], [2, 4])  # the error goes back to the model
        self.assertEqual(self.note(self.finalize(0), SASA)["action"], "published")

    def test_invalid_after_retries_stays_pending(self) -> None:
        rec = self.review("invalid")
        self.assertEqual((rec["end"], rec["attempts"]), ("review-invalid", 3))
        n = self.note(self.finalize(0), SASA)
        self.assertEqual((n["action"], n["pending_reason"]), ("pending-review", "review-invalid"))
        self.assertEqual(self.queue()[0]["stage"], "pending-review")  # not reset to incomplete

    def test_timeout_stays_pending(self) -> None:
        os.environ["ZR_REVIEW_TIMEOUT"] = "1"
        rec = self.review("timeout")
        self.assertEqual((rec["end"], rec["attempts"]), ("review-timeout", 1))
        n = self.note(self.finalize(0), SASA)
        self.assertEqual((n["action"], n["pending_reason"]), ("pending-review", "review-timeout"))
        self.assertEqual(list((self.staging / "quarantine").rglob("*.md")), [])
        self.assertNotEqual(self.queue()[0]["stage"], "incomplete")

    def test_partial_skeleton_is_invalid(self) -> None:
        rec = self.review("partial")
        self.assertEqual(rec["end"], "review-invalid")
        self.assertIn("missing", rec["detail"])
        self.assertEqual(self.note(self.finalize(0), SASA)["action"], "pending-review")

    def test_verdict_with_a_defect_quarantines(self) -> None:
        self.assertEqual(self.review("defect")["end"], "finished")
        n = self.note(self.finalize(0), SASA)
        self.assertEqual(n["action"], "quarantined")
        self.assertIn("review:changed-number", n["failure_types"])

    def test_json_mode_refused_falls_back_to_parsing(self) -> None:
        rec = self.review("json-mode-refused")
        self.assertEqual(rec["end"], "finished")
        self.assertEqual([c["response_format"] for c in self.calls()], [{"type": "json_object"}, None])

    def test_budget_stops_the_review(self) -> None:
        os.environ["ZR_MAX_COST_USD"] = "0.005"
        other = self.unit.parent / "spent"
        other.mkdir()
        (other / "agent_result.json").write_text(json.dumps({"usage": {"cost": 0.02}}))
        rec = self.review("valid")
        self.assertEqual(rec["end"], "budget-stop")
        self.assertEqual(self.calls(), [])
        n = self.note(self.finalize(0), SASA)
        self.assertEqual((n["action"], n["pending_reason"]), ("pending-review", "budget-stop"))
        self.assertTrue((self.run_dir / "budget-stop.json").exists())
        s = ri.summary(self.run_dir, self.staging)
        self.assertTrue(s["budget"]["stopped"])

    def test_cli_reviews_every_target(self) -> None:
        agent.write_file(str(self.vpath(RADO)), good_text(RADO))
        env = {**os.environ, "FAKE_REVIEW_MODE": "valid"}
        r = subprocess.run([sys.executable, str(ZR / "review_call.py"), "--staging", str(self.staging), "--vault",
                            str(self.zk), "--unit-dir", str(self.unit)], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.count(": finished"), 2)
        ev = self.finalize(0)
        self.assertEqual({n["action"] for n in ev["notes"]}, {"published"})

    def test_no_review_agent_in_the_drivers(self) -> None:
        text = (ZR / "run_lib.sh").read_text()
        body = text.split("zr_source_check() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("review_call.py", body)
        self.assertNotIn("zr_agent", body)
        for name in ("review_loop.sh", "parallel_review.sh", "review_worker.sh", "repair_loop.sh", "loop.sh",
                     "loop_parallel.sh", "parallel_synth.sh"):
            self.assertIn("zr_pending_reviews", (ZR / name).read_text(), name)


# --------------------------------------------------------------------------- #
# P2: links
# --------------------------------------------------------------------------- #


class LinkRules(_ReviewEnv):
    def test_self_link_fails(self) -> None:
        text = good_text().replace("## Connected Ideas\n", f"## Connected Ideas\n\n- [[{SASA}]] — this note\n")
        agent.write_file(str(self.vpath(SASA)), text)
        chk = ri.check_unit(self.staging, self.unit, self.zk)
        self.assertIn("self-link", {f["type"] for f in chk[self.rel]["failures"]})

    def test_link_to_a_sibling_of_another_unit_waits_then_publishes(self) -> None:
        other = self.new_unit()
        (other / "out" / PN).mkdir(parents=True, exist_ok=True)
        sib = "Sibling Note Of A Parallel Unit"
        (other / "out" / PN / f"{sib}.md").write_text(good_text(RADO).replace(f"# {RADO}\n", f"# {sib}\n"))
        os.environ["ZR_RUN_DIR"] = str(self.unit)
        text = good_text().replace("## Connected Ideas\n", f"## Connected Ideas\n\n- [[{sib}]] — sibling of a parallel unit\n")
        (self.unit / "out" / self.rel).write_text(text)
        chk = ri.check_unit(self.staging, self.unit, self.zk)
        self.assertEqual({f["type"] for f in chk[self.rel]["failures"]}, {"link-waiting"})
        r = subprocess.run([sys.executable, str(ZR / "run_integrity.py"), "check", "--staging", str(self.staging),
                            "--unit-dir", str(self.unit), "--vault", str(self.zk)], capture_output=True, text=True,
                           env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stdout)  # not a failure for a fix turn
        n = self.note(self.finalize(0), SASA)
        self.assertEqual((n["action"], n["pending_reason"]), ("pending-review", "link-waiting"))
        # The other unit publishes its note; the end-of-run pending step publishes this one.
        rc.review_note(self.staging, self.zk, other, f"{PN}/{sib}.md", other / "out" / PN / f"{sib}.md")
        self.assertEqual(self.note(self.finalize(0, unit=other), sib)["action"], "published")
        units = ri.pending_units(self.staging, self.run_dir, os.getpid(), "link-waiting")
        self.assertEqual(len(units), 1)
        rc.review_unit(self.staging, self.zk, Path(units[0]))
        ev = ri.finalize(self.staging, self.run_dir, Path(units[0]), [], 0, self.zk)
        self.assertEqual(self.note(ev, SASA)["action"], "published")

    def test_home_link_counts(self) -> None:
        text = good_text().replace("## Connected Ideas\n", "## Connected Ideas\n\n- [[Home]] — index\n")
        agent.write_file(str(self.vpath(SASA)), text)
        chk = ri.check_unit(self.staging, self.unit, self.zk)
        self.assertTrue(chk[self.rel]["ok"], chk[self.rel]["failures"])


# --------------------------------------------------------------------------- #
# P3: the pilot's Packer fix-turn sequence
# --------------------------------------------------------------------------- #


class PackerFixTurn(_Env):
    ids = ["vid-lkcLJc2c0NQ"]

    def test_fix_turn_clock_out_keeps_the_other_notes(self) -> None:
        shutil.copy(P2 / "lit" / "vid-lkcLJc2c0NQ.md", self.staging / "lit")
        notes = sorted((P2 / "units" / "vid-lkcLJc2c0NQ").glob("*.md"))
        self.assertEqual(len(notes), 6)
        marked = json.loads((P2 / "units" / "vid-lkcLJc2c0NQ" / "marked.json").read_text())
        for p in notes:
            agent.write_file(str(self.vpath(p.stem)), p.read_text())

        def mark(titles: list[str]) -> None:
            r = subprocess.run([sys.executable, str(ZR / "queue_mark.py"), "--ids", "vid-lkcLJc2c0NQ", "--stage",
                                "synthesized", "--notes", ";".join(titles)], env=dict(os.environ),
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        mark(marked["earlier"])  # synthesis turn: all 6
        mark(marked["notes"])  # fix turn (pilot): only the 2 fixed notes
        self.assertEqual(sorted(json.loads((self.unit / "marked.json").read_text())["notes"]), sorted(p.stem for p in notes))
        ev = self.finalize(0)
        self.assertNotIn("discarded-draft", {n["action"] for n in ev["notes"]})


# --------------------------------------------------------------------------- #
# P4 / P6: cost records, budget, base URL, stage times
# --------------------------------------------------------------------------- #


class CostAndTimes(_ReviewEnv):
    def test_turn_records_and_unit_sum(self) -> None:
        p = self.unit / "agent_result.json"
        for turn, cost in enumerate((0.165, 0.225, 0.013), 1):
            res = agent._accumulate(p, {"rc": 0, "end": "finished", "steps": 1, "elapsed_s": 10.0 * turn,
                                        "usage": {"calls": 1, "cost": cost}, "models_served": ["m"]})
            p.write_text(json.dumps(res))
        self.review("valid")
        u = ri._unit_usage(self.unit)
        self.assertAlmostEqual(u["cost"], 0.165 + 0.225 + 0.013 + 0.01)
        t = ri._unit_times(self.unit)
        self.assertEqual((t["synthesis"], t["fix_turns"]), (10.0, 50.0))
        ev = self.finalize(0)
        self.assertEqual(set(ev["times_s"]), {"synthesis", "fix_turns", "review", "finalize"})
        s = ri.summary(self.run_dir, self.staging)
        self.assertEqual(set(s["wall_time_s"]) >= {"synthesis", "fix_turns", "review", "finalize", "run"}, True)
        self.assertAlmostEqual(s["budget"]["cost_usd"], 0.413)

    def test_budget_cli_and_driver_check(self) -> None:
        (self.unit / "agent_result.json").write_text(json.dumps({"usage": {"cost": 2.0}}))
        env = {**os.environ, "ZR_MAX_COST_USD": "1"}
        r = subprocess.run([sys.executable, str(ZR / "run_integrity.py"), "budget", "--run-dir", str(self.run_dir)],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 4)
        script = (f'HERE="{ZR}"; STAGING="{self.staging}"; RUN_DIR="{self.run_dir}"; . "{ZR}/run_lib.sh"; '
                  'zr_budget_check; echo after')
        r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 4)
        self.assertNotIn("after", r.stdout)
        self.assertIn("ZR_MAX_COST_USD", r.stderr)

    def test_effective_base_url_is_recorded(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_BASE_URL"}
        r = subprocess.run(["bash", "-c", f'HERE="{ZR}"; . "{ZR}/run_lib.sh"; echo "$DEEPSEEK_BASE_URL"'],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "https://openrouter.ai/api/v1")
        self.review("valid")
        self.assertEqual(ri._unit_base_urls(self.unit), [rc.settings()["base_url"]])


# --------------------------------------------------------------------------- #
# P5: checker rules
# --------------------------------------------------------------------------- #


class FaithfulnessRules(_Env):
    def test_scope_not_in_quote(self) -> None:
        import re
        text = re.sub(r'(?m)^  equipment: .*$', '  equipment: "pull-up bar"', good_text(), count=1)
        self.assertNotEqual(text, good_text())
        p = self.tmp / f"{SASA}.md"
        p.write_text(text)
        rep = vc.verify_note(p, vc.LitIndex([self.staging / "lit"]))
        self.assertIn("scope-not-in-quote", {f["type"] for f in rep["failures"]})

    def test_dropped_hedge_is_a_hint(self) -> None:
        q = ["there is a very high probability that your front lever will get a noticeable upgrade just from that. "
             "Maybe even if you don't do any front lever at all."]
        self.assertEqual(vc.dropped_hedge("Weighted pull-ups upgrade the front lever, even without front lever training.",
                                          "SasaVenos Predicts Weighted Pull-Ups Upgrade The Front Lever", q, []), "maybe")
        self.assertIsNone(vc.dropped_hedge("Weighted pull-ups maybe upgrade the front lever.",
                                           "SasaVenos Predicts Weighted Pull-Ups Upgrade The Front Lever", q, []))
        self.assertIsNone(vc.dropped_hedge("Weighted pull-ups upgrade the front lever.",
                                           "SasaVenos Says Weighted Pull-Ups Can Upgrade The Front Lever", q, []))

    def test_packer_notes_need_speaker_evidence(self) -> None:
        lit = vc.LitIndex([P2 / "lit"])
        for p in sorted((P2 / "units" / "vid-lkcLJc2c0NQ").glob("*.md")):
            types = {f["type"] for f in vc.verify_note(p, lit)["failures"]}
            self.assertIn("speaker-evidence-missing", types, p.stem)
        p = next((P2 / "units" / "vid-lkcLJc2c0NQ").glob("David Packer Recommends Handstand*.md"))
        text = p.read_text().replace("speaker: \"David Packer\"", "speaker: \"not stated\"")
        q = self.tmp / p.name
        q.write_text(text)
        self.assertNotIn("speaker-evidence-missing", {f["type"] for f in vc.verify_note(q, lit)["failures"]})

    def test_quote_in_minor_loop_span_fails(self) -> None:
        lit = vc.LitIndex([P2 / "lit"])
        p = next((P2 / "units" / "vid-BBuB0XMVhDw").glob("SasaVenos Recommends Not Training To Failure*.md"))
        rep = vc.verify_note(p, lit)
        fails = [f for f in rep["failures"] if f["type"] == "quote-in-damaged-span"]
        self.assertTrue(fails)
        self.assertIn("minor loop lines 95-95", fails[0]["detail"])
