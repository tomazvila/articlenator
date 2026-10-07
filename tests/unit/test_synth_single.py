"""Round 9: single-call synthesis, fix and repair with a fake LLM. No network, no LLM."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.test_run_integrity import (
    FAKE_KEY,
    HONEST,
    OLD_NOTE,
    PN,
    ZR,
    _Env,
    git,
    ri,
)

import synth_call as sc  # noqa: E402
import verify_claims as vc  # noqa: E402

ROOT = ZR.parent
V5 = "vid-5zALUKd7h3g"
FICT = ROOT / "tests" / "fixtures" / "note_contract"

# The fake model: replies are taken in order from a JSON queue per call kind.
FAKE = r'''
import json, os, re, sys
from pathlib import Path
body = json.load(sys.stdin)
user = body["messages"][-1]["content"]
sysmsg = body["messages"][0]["content"]
if user.startswith("INVENTORY"):
    kind = "inventory"
elif user.startswith("BATCH "):
    kind = "batch"
elif user.startswith("CONTINUE"):
    kind = "cont"
elif user.startswith("COVERAGE"):
    kind = "coverage"
elif user.startswith("NOTE ("):
    kind = "fix"
elif "OLD NOTES TO REPAIR" in user or user.startswith("BULLETS"):
    kind = "repair"
else:
    kind = "synth"
qf = Path(os.environ["FAKE_QUEUE"])
q = json.loads(qf.read_text())
item = q[kind].pop(0)
qf.write_text(json.dumps(q))
log = Path(os.environ["FAKE_LOG"])
with open(log, "a") as f:
    f.write(json.dumps({"kind": kind, "max_tokens": body["max_tokens"], "reasoning": body.get("reasoning"),
                        "model": body["model"], "user": user, "n_messages": len(body["messages"])}) + "\n")
if isinstance(item, dict) and item.get("http"):
    sys.stderr.write(f"HTTP {item['http']}: refused"); sys.exit(1)
content, finish = (item["content"], item.get("finish", "stop")) if isinstance(item, dict) else (item, "stop")
print(json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}],
                  "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.01}}))
'''

REVIEW = r'''
import hashlib, json, os, re, sys
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
note = user.split("<<<NOTE\n", 1)[1].split("\nNOTE>>>", 1)[0]
sk = json.loads(user.split("Reply with this JSON object only:\n", 1)[1])
marked = re.findall(r"^>>> (.*?) <<<$", user, re.M)
reject = hashlib.sha256(note.encode()).hexdigest() in json.loads(os.environ.get("REJECT_SHAS", "[]"))
for k, it in enumerate(sk["items"]):
    it["verdict"] = "dropped-qualifier" if (reject and k == 0) else "supported"
    it["problem"] = "the title drops a hedge" if (reject and k == 0) else ""
    marked = item_marked(it["item"])
    it["transcript_text"] = marked[0][:100].rsplit(" ", 1)[0]
sk["scope_complete"] = True
print(json.dumps({"choices": [{"message": {"content": json.dumps(sk)}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.001}}))
'''


def honest(title_part: str) -> tuple[str, str]:
    """An honest fixture note; single mode links only to [[Home]], notes of the reply and
    candidates, so its sibling links become [[Home]]."""
    p = next(p for p in (HONEST / "notes").glob("*.md") if title_part in p.stem)
    text = re.sub(r"## Connected Ideas\n\n.*", "## Connected Ideas\n\n- [[Home]] — home map.\n", p.read_text(), flags=re.S)
    return p.stem, text


def reply(claims: list[tuple[str, str, str]], notes: list[tuple[str, list[str], str]],
          skips: list[tuple[str, str]] = ()) -> str:
    out = ["=====CLAIMS====="] + [f"{a} | {b} | {c}" for a, b, c in claims]
    for title, cl, text in notes:
        out += [f"=====NOTE: {title} | claims: {','.join(cl)}=====", text.rstrip("\n")]
    if skips:
        out += ["=====SKIPPED====="] + [f"{a} | {b}" for a, b in skips]
    return "\n".join(out) + "\n"


class _Single(_Env):
    ids = [V5]

    def setUp(self) -> None:
        super().setUp()
        shutil.copy(HONEST / "lit" / f"{V5}.md", self.staging / "lit")
        self.write_queue([{"id": V5, "kind": "video", "stage": "claimed", "lit_note": str(self.staging / "lit" / f"{V5}.md")}])
        self.unit = self.new_unit("synth", [V5])
        (self.tmp / "fake.py").write_text(FAKE)
        (self.tmp / "review.py").write_text(REVIEW)
        # The round-9 one-reply synthesis; the two-stage mode has its own tests (round 11).
        os.environ.setdefault("ZR_SYNTH_STAGES", "one")
        os.environ.update(ZR_SYNTH_SCRIPT=str(self.tmp / "fake.py"), FAKE_QUEUE=str(self.tmp / "queue.json"),
                          FAKE_LOG=str(self.tmp / "log.jsonl"), ZR_REVIEW_SCRIPT=str(self.tmp / "review.py"))
        # The agent-mode fixtures seed map notes named like the honest notes; single mode
        # refuses a title that any note of the zettelkasten has.
        for p in (self.zk / "00 Maps").glob("*.md"):
            if any(p.stem == q.stem for q in (HONEST / "notes").glob("*.md")):
                p.unlink()
        self.a, self.ta = honest("Can Be Done On The Floor")
        self.b, self.tb = honest("Four Sets Of Planche Lean")
        self.c, self.tc = honest("Slow And Controlled")

    def fake(self, **kinds: list) -> None:
        q = {k: [] for k in ("synth", "cont", "coverage", "fix", "repair", "inventory", "batch")}
        q.update(kinds)
        (self.tmp / "queue.json").write_text(json.dumps(q))

    def log(self) -> list[dict]:
        p = self.tmp / "log.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def synth(self) -> dict:
        return sc.synth_unit(self.staging, self.zk, self.unit, [V5])


# --------------------------------------------------------------------------- #
# Splitter
# --------------------------------------------------------------------------- #


class Splitter(unittest.TestCase):
    NOTE = ("---\ntype: permanent note\n---\n\n# Real Title\n\nLead.\n\n## Details\n\n- x\n\n## Evidence\n\n- e\n\n"
            "## Connected Ideas\n\n- [[Home]] — home\n")

    def test_robust_forms(self) -> None:
        raw = ("Sure, here are the notes.\r\n```markdown\r\n=====CLAIMS=====\r\nC1 | 01:50 | first claim\r\n"
               "C2 | line 12 | second claim\r\n=====NOTE: Marker Title | claims: C1=====\r\n```markdown\r\n"
               + self.NOTE.replace("\n", "\r\n") + "```\r\n=====SKIPPED=====\r\nC2 | damaged span\r\n```\r\n")
        p = sc.parse_reply(raw)
        self.assertEqual([c["id"] for c in p["claims"]], ["C1", "C2"])
        self.assertEqual(p["notes"][0]["title"], "Real Title")  # the H1 wins
        self.assertEqual(p["notes"][0]["claims"], ["C1"])
        self.assertTrue(p["notes"][0]["complete"])
        self.assertIn("title-mismatch", {x["type"] for x in p["notes"][0]["problems"]})
        self.assertEqual(p["skips"], {"C2": "damaged span"})
        self.assertIn("stray-text", {x["type"] for x in p["problems"]})
        self.assertNotIn("```", p["notes"][0]["text"])
        self.assertNotIn("\r", p["notes"][0]["text"])

    def test_truncated_last_note(self) -> None:
        raw = ("=====CLAIMS=====\nC1 | 1 | a\nC2 | 2 | b\n=====NOTE: A | claims: C1=====\n" + self.NOTE
               + "=====NOTE: B | claims: C2=====\n---\ntype: permanent note\n---\n\n# B\n\nLead")
        p = sc.parse_reply(raw, "stop")
        self.assertEqual([(n["title"], n["truncated"]) for n in p["notes"]], [("Real Title", False), ("B", True)])
        p = sc.parse_reply(raw.split("=====NOTE: B")[0], "length")
        self.assertTrue(p["notes"][-1]["truncated"])  # cut at max_tokens even if it looks complete

    def test_separator_form_drop_and_keep(self) -> None:
        p = sc.parse_reply("# Real Title\n\n=====NOTE=====\n" + self.NOTE + "=====NOTE=====\n" + self.NOTE)
        self.assertEqual(len(p["notes"]), 2)
        p = sc.parse_reply(self.NOTE + "=====NOTE=====\n" + self.NOTE)
        self.assertEqual(len(p["notes"]), 2)
        self.assertIn("note-before-first-marker", {x["type"] for x in p["problems"]})
        self.assertEqual(sc.parse_reply("=====DROP=====\nthe passage does not say it")["drop"],
                         "the passage does not say it")
        p = sc.parse_reply("=====KEEP-NEEDS-REPAIR: Old.md=====\nno supporting passage found\n"
                           "=====NOTE: New | repairs: Other.md, Third.md=====\n" + self.NOTE)
        self.assertEqual(p["keeps"], {"Old.md": "no supporting passage found"})
        self.assertEqual(p["notes"][0]["repairs"], ["Other.md", "Third.md"])


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #


class Synthesis(_Single):
    def test_request_shape_and_write(self) -> None:
        self.fake(synth=[reply([("C1", "01:16", "a"), ("C2", "01:22", "b"), ("C3", "02:00", "c")],
                               [(self.a, ["C1"], self.ta), (self.b, ["C2"], self.tb)], [("C3", "not a transferable training claim")])])
        res = self.synth()
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]))
        self.assertEqual(res["claims_uncovered"], [])
        call = self.log()[0]
        self.assertEqual((call["max_tokens"], call["model"], call["reasoning"]),
                         (24000, "anthropic/claude-sonnet-5.5", {"effort": "low"}))
        self.assertIn("<<<TRANSCRIPT", call["user"])
        self.assertIn(f"id: \"{V5}\"" if f'id: "{V5}"' in call["user"] else V5, call["user"])
        self.assertIn("EXISTING NOTES YOU MAY LINK TO", call["user"])
        self.assertTrue((self.unit / "out" / PN / f"{self.a}.md").is_file())
        self.assertEqual(json.loads((self.unit / "marked.json").read_text())["notes"], [self.a, self.b])
        claims = json.loads((self.unit / "claims.json").read_text())
        self.assertEqual(claims["coverage"]["C3"], {"skip": "not a transferable training claim"})
        self.assertEqual(self.queue()[0]["stage"], "synthesized")
        self.assertEqual(json.loads((self.unit / "agent_result.json").read_text())["end"], "finished")
        self.assertAlmostEqual(ri._unit_usage(self.unit)["cost"], 0.01)

    def test_truncated_reply_and_continuation(self) -> None:
        cut = reply([("C1", "1", "a"), ("C2", "2", "b")], [(self.a, ["C1"], self.ta)]) + \
            f"=====NOTE: {self.b} | claims: C2=====\n" + self.tb[:300]
        self.fake(synth=[{"content": cut, "finish": "length"}],
                  cont=[f"=====NOTE: {self.b} | claims: C2=====\n" + self.tb])
        res = self.synth()
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]))
        cont = [c for c in self.log() if c["kind"] == "cont"][0]
        self.assertIn("C2", cont["user"])
        self.assertIn(self.a, cont["user"])  # finished notes are named, not repeated

    def test_coverage_follow_up(self) -> None:
        self.fake(synth=[reply([("C1", "1", "a"), ("C2", "2", "b"), ("C3", "3", "c")], [(self.a, ["C1"], self.ta)])],
                  coverage=[reply([], [(self.b, ["C2"], self.tb)])])
        res = self.synth()
        self.assertEqual(res["claims_uncovered"], ["C3"])
        self.assertIn(self.b, res["notes"])
        self.assertEqual(len([c for c in self.log() if c["kind"] == "coverage"]), 1)

    def test_skip_already_covered_needs_a_candidate(self) -> None:
        self.vpath("Some Vault Note About Planche Lean Presses").write_text(OLD_NOTE.format(
            t="Some Vault Note About Planche Lean Presses"))
        self.fake(synth=[reply([("C1", "1", "a"), ("C2", "2", "b")], [(self.a, ["C1"], self.ta)],
                               [("C2", "already covered by [[Not A Candidate]]")])],
                  coverage=[reply([], [], [("C2", "already covered by [[Some Vault Note About Planche Lean Presses]]")])])
        res = self.synth()
        self.assertIn("skip-invalid", {p["type"] for p in res["problems"]})
        self.assertEqual(res["claims_uncovered"], [])

    def test_no_folding_into_vault_notes(self) -> None:
        self.vpath(self.a).write_text(self.ta)
        self.fake(synth=[reply([("C1", "1", "a")], [(self.a, ["C1"], self.ta)])])
        res = self.synth()
        self.assertEqual(res["notes"], [])
        self.assertIn("not-written", {p["type"] for p in res["problems"]})

    def test_links_only_to_candidates(self) -> None:
        # A vault note that shares no word with the transcript is not a candidate.
        self.vpath("Unrelated Zebra Note").write_text("---\ntype: permanent note\n---\n\n# Unrelated Zebra Note\n\nx\n")
        text = re.sub(r"## Connected Ideas\n\n.*", "## Connected Ideas\n\n- [[Unrelated Zebra Note]] — x\n",
                      self.ta, flags=re.S)
        self.fake(synth=[reply([("C1", "1", "a")], [(self.a, ["C1"], text)])])
        self.synth()
        chk = ri.check_unit(self.staging, self.unit, self.zk)
        self.assertIn("dead-link", {f["type"] for f in chk[f"{PN}/{self.a}.md"]["failures"]})

    def test_long_transcript_is_split(self) -> None:
        os.environ["ZR_SYNTH_MAX_INPUT_CHARS"] = "1500"
        lit = (self.staging / "lit" / f"{V5}.md").read_text()
        n = len(sc.split_parts(lit, 1500))
        self.assertGreater(n, 1)
        # Each part gets its own reply: part 1 note A, part 2 note B, later parts repeat note A.
        self.fake(synth=[reply([("C1", "1", "a")], [(self.a, ["C1"], self.ta)]),
                         reply([("C1", "2", "b")], [(self.b, ["C1"], self.tb)])]
                  + [reply([("C1", "1", "a")], [(self.a, ["C1"], self.ta)])] * (n - 2))
        res = self.synth()
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]))
        ids = {c["id"] for c in res["claims"]}
        self.assertTrue({"P1-C1", "P2-C1"} <= ids)  # claim ids are per part
        if n > 2:
            self.assertIn("duplicate-note", {p["type"] for p in res["problems"]})
        users = [c["user"] for c in self.log() if c["kind"] == "synth"]
        self.assertEqual(len(users), n)
        self.assertIn(f"PART 2 of {n}", users[1])

    def test_reasoning_refused_once(self) -> None:
        # Round 11: a model that reasons by default never gets a call without the object.
        self.fake(synth=[{"http": 400}, reply([("C1", "1", "a")], [(self.a, ["C1"], self.ta)])])
        res = self.synth()
        self.assertEqual(res["notes"], [])
        self.assertIn("refused reasoning", res["detail"])
        self.assertEqual([c["reasoning"] for c in self.log()], [{"effort": "low"}])

    def test_budget_stop_between_calls(self) -> None:
        # Round 11: the first call is checked too; it fits (about $0.027), the second
        # (spent $0.01 plus the same reservation) does not.
        os.environ["ZR_SYNTH_MAX_TOKENS"] = "1000"
        lit = (self.staging / "lit" / f"{V5}.md").read_text()
        est = sc._estimate_max_cost(sc.settings("synth"), [{"content": sc.system_text("onecall")}, {"content": lit}])
        os.environ["ZR_MAX_COST_USD"] = str(round(est + 0.005, 6))  # the first fits; $0.01 spent + the next does not
        cut = reply([("C1", "1", "a"), ("C2", "2", "b")], [(self.a, ["C1"], self.ta)]) + \
            f"=====NOTE: {self.b} | claims: C2=====\n---\n"
        self.fake(synth=[{"content": cut, "finish": "length"}], cont=["never sent"])
        res = self.synth()
        self.assertEqual(res["notes"], [self.a])
        self.assertIn("budget-stop", res["detail"])
        self.assertEqual([c["kind"] for c in self.log()], ["synth"])
        self.assertEqual(res["claims_uncovered"], ["C2"])

    def test_budget_stop_before_the_first_call(self) -> None:
        os.environ["ZR_MAX_COST_USD"] = "0.5"
        other = self.new_unit("synth", [])
        (other / "calls").mkdir()
        (other / "calls" / "synth-0.json").write_text(json.dumps({"name": "synth-0", "usage": {"calls": 1, "cost": 0.6}}))
        self.write_queue([{"id": V5, "kind": "video", "stage": "claimed", "claimed_from": "extracted",
                           "lit_note": str(self.staging / "lit" / f"{V5}.md")}])
        self.fake(synth=["never sent"])
        res = self.synth()
        self.assertEqual(res["outcome"], "budget-stop")
        self.assertEqual(self.log(), [])
        item = json.loads((self.staging / "queue.json").read_text())
        item = (item.get("items") if isinstance(item, dict) else item)[0]
        self.assertEqual(item["stage"], "extracted")  # back to its old stage, no attempt
        self.assertFalse(item.get("attempts"))

    def test_mode_selection(self) -> None:
        self.assertEqual(sc.mode_for(self.staging, [V5]), "single")
        os.environ["ZR_SYNTH_MODE"] = "agent"
        self.assertEqual(sc.mode_for(self.staging, [V5]), "agent")
        os.environ.pop("ZR_SYNTH_MODE")
        self.write_queue([{"id": "tw-1", "kind": "tweet", "stage": "claimed"}])
        self.assertEqual(sc.mode_for(self.staging, ["tw-1"]), "agent")


# --------------------------------------------------------------------------- #
# Fix calls and review repair
# --------------------------------------------------------------------------- #


class FixCalls(_Single):
    def bad(self, text: str) -> str:
        q = text.index('"', text.index("## Evidence")) + 1
        return text[:q] + "zzz " + text[q:]  # a quote that is not in the transcript

    def test_fix_call_fixes(self) -> None:
        self.fake(synth=[reply([("C1", "1", "a")], [(self.a, ["C1"], self.bad(self.ta))])],
                  fix=[f"=====NOTE: {self.a}=====\n" + self.ta])
        self.synth()
        self.assertFalse(ri.check_unit(self.staging, self.unit, self.zk)[f"{PN}/{self.a}.md"]["ok"])
        out = sc.fix_unit(self.staging, self.zk, self.unit, "mech")
        self.assertEqual(out[0]["result"], "rewritten")
        self.assertTrue(ri.check_unit(self.staging, self.unit, self.zk)[f"{PN}/{self.a}.md"]["ok"])
        fix = [c for c in self.log() if c["kind"] == "fix"][0]
        self.assertIn("CHECKER REPORT", fix["user"])
        self.assertIn("quote-mismatch", fix["user"])
        self.assertIn("TRANSCRIPT FILE(S)", fix["user"])
        self.assertIn(">>> ", fix["user"])

    def test_fix_call_drops(self) -> None:
        self.fake(synth=[reply([("C1", "1", "a")], [(self.a, ["C1"], self.bad(self.ta))])],
                  fix=["=====DROP=====\nthe passage does not support it"])
        self.synth()
        out = sc.fix_unit(self.staging, self.zk, self.unit, "mech")
        self.assertEqual(out[0]["result"], "dropped")
        self.assertFalse((self.unit / "out" / PN / f"{self.a}.md").exists())
        claims = json.loads((self.unit / "claims.json").read_text())
        self.assertTrue(claims["coverage"]["C1"]["skip"].startswith("dropped in a fix call"))
        self.assertEqual(ri.missing_listed(self.unit, self.zk), [])

    def test_at_most_two_fix_calls_per_note(self) -> None:
        bad = self.bad(self.ta)
        self.fake(synth=[reply([("C1", "1", "a")], [(self.a, ["C1"], bad)])],
                  fix=[f"=====NOTE: {self.a}=====\n" + bad] * 3)
        self.synth()
        for _ in range(3):
            sc.fix_unit(self.staging, self.zk, self.unit, "mech")
        self.assertEqual(len([c for c in self.log() if c["kind"] == "fix"]), 2)

    def test_review_rejection_single_fix_then_published(self) -> None:
        import hashlib
        import review_call as rc
        self.fake(synth=[reply([("C1", "1", "a"), ("C2", "2", "b")], [(self.a, ["C1"], self.ta), (self.b, ["C2"], self.tb)])],
                  fix=[f"=====NOTE: {self.a}=====\n" + self.ta.replace("— home map.", "— the home index.")])
        self.synth()
        os.environ["REJECT_SHAS"] = json.dumps([hashlib.sha256((self.ta if self.ta.endswith("\n") else self.ta + "\n")
                                                              .encode()).hexdigest()])
        ri.check_unit(self.staging, self.unit, self.zk)
        rc.review_unit(self.staging, self.zk, self.unit)
        out = sc.fix_unit(self.staging, self.zk, self.unit, "review")
        self.assertEqual([o["result"] for o in out], ["rewritten"])
        fix = [c for c in self.log() if c["kind"] == "fix"][0]
        self.assertIn("REJECTED REVIEW ITEMS", fix["user"])
        self.assertIn("the title drops a hedge", fix["user"])
        self.assertEqual(sc.fix_unit(self.staging, self.zk, self.unit, "review"), [])  # one per unit
        ri.check_unit(self.staging, self.unit, self.zk)
        rc.review_unit(self.staging, self.zk, self.unit)
        ev = self.finalize(0)
        self.assertEqual({n["action"] for n in ev["notes"]}, {"published"})
        idx = json.loads((self.staging / "concept-index.json").read_text())["concepts"]
        self.assertEqual(sorted(c["title"] for c in idx), sorted([self.a, self.b]))
        self.assertEqual(idx[0]["by"], "harness")
        self.assertTrue(idx[0]["gist"])


# --------------------------------------------------------------------------- #
# Repair per video
# --------------------------------------------------------------------------- #


class RepairSingle(_Single):
    kind = "repair"
    V2 = "vid-EXAMPLE0001"

    def old(self, title: str, vids: tuple[str, ...] = (V5,), bullets: int = 1) -> str:
        src = "".join(f"  - id: {v}\n    speaker: X\n" for v in vids)
        text = OLD_NOTE.format(t=title).replace("created:", f"sources:\n{src}created:")
        text = text.replace("- legacy bullet\n", "".join(f"- legacy bullet {i}\n" for i in range(1, bullets + 1)))
        self.vpath(title).write_text(text)
        return f"{PN}/{title}.md"

    def run_batch(self, b: dict, state: Path) -> tuple[Path, dict]:
        unit = self.new_unit("repair", [])
        res = sc.repair_unit(self.staging, self.zk, unit, b["vid"], b["notes"], b["unknown"], b.get("bullets"),
                             b.get("prior_titles"), b.get("last"))
        sc.record_answers(state, b, res)
        return unit, res

    def plan(self, notes: list[str]) -> Path:
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, notes, state_file=state)
        return state
    def test_repair_actions_ledger_and_publish_time_stubs(self) -> None:
        inplace, rename, split, keep = (self.old("Old In Place"), self.old("Old Rename"),
                                        self.old("Old Split", bullets=2), self.old("Old Keep"))
        mystery = self.vpath("Old Mystery")
        mystery.write_text(OLD_NOTE.format(t="Old Mystery").replace(
            "Legacy claim.", "Planche lean presses with parallettes and four sets after the planche workout. "
            + " ".join(re.sub(r"(?m)^(#+ |- )", "", vc.split_frontmatter(self.tb)[1].split("## Evidence")[0]).split())))
        groups = sc.repair_groups(self.staging, self.zk, [inplace, rename, split, keep, f"{PN}/Old Mystery.md"])
        self.assertEqual(sorted(groups["known"][0]["notes"]), sorted([inplace, rename, split, keep]))
        self.assertEqual(groups["unknown"][f"{PN}/Old Mystery.md"]["candidates"][0][0], V5)
        in_place_text = re.sub(r"^# .+$", "# Old In Place", self.ta, count=1, flags=re.M)
        rep = "\n".join([
            "=====NOTE: Old In Place | repairs: Old In Place.md | bullets: b1=====", in_place_text.rstrip(),
            f"=====NOTE: {self.a} | repairs: Old Rename.md | bullets: b1=====", self.ta.rstrip(),
            f"=====NOTE: {self.b} | repairs: Old Split.md | bullets: b1=====", self.tb.rstrip(),
            f"=====NOTE: {self.c} | repairs: Old Split.md | bullets: b2=====", self.tc.rstrip(),
            "=====KEEP-NEEDS-REPAIR: Old Keep.md=====", "no supporting passage found",
            "=====BULLETS=====", "Old In Place.md#b1 | kept in Old In Place", f"Old Rename.md#b1 | corrected in {self.a}",
            f"Old Split.md#b1 | kept in {self.b}", "Old Split.md#b2 | dropped: contradicts the transcript",
            "Old Keep.md#b1 | dropped: not in this transcript"]) + "\n"
        # One bullet is missing from the ledger: the harness asks once for the missing ids.
        self.fake(repair=[rep, "=====BULLETS=====\nOld Mystery.md#b1 | dropped: not in this transcript\n"])
        unit = self.new_unit("repair", [])
        res = sc.repair_unit(self.staging, self.zk, unit, V5, groups["known"][0]["notes"], [f"{PN}/Old Mystery.md"])
        acts = {Path(a["old"]).stem: a["action"] for a in res["applied"]}
        # X2 (round 22): a reply that keeps the old title is refused (title-equals-old): no in-place repair
        self.assertEqual(acts, {"Old In Place": "not answered", "Old Rename": "renamed", "Old Split": "split",
                                "Old Keep": "keep-needs-repair", "Old Mystery": "left as it is"})
        self.assertIn("title-equals-old", [p["type"] for p in res["problems"]])
        self.assertEqual(res["bullets_missing"], [])
        self.assertEqual({d["bullet"] for d in res["bullets_dropped"]},
                         {"Old Split.md#b2", "Old Keep.md#b1", "Old Mystery.md#b1"})
        calls = [c for c in self.log() if c["kind"] == "repair"]
        self.assertEqual(len(calls), 2)
        self.assertIn("Old Mystery.md#b1 | legacy bullet", calls[0]["user"])
        self.assertTrue(calls[1]["user"].startswith("BULLETS"))
        self.assertIn("=====OLD NOTE: Old Mystery.md (source unknown", calls[0]["user"])
        # P2: no stub before finalize; the fix call renames one target; the stub names the final title.
        self.assertFalse((unit / "out" / split).exists())
        m = json.loads((unit / "marked.json").read_text())
        (unit / "out" / PN / f"{self.c}.md").rename(unit / "out" / PN / "Renamed Target.md")
        m.setdefault("renamed", {})[self.c] = "Renamed Target"
        (unit / "marked.json").write_text(json.dumps(m))
        stubs = ri.materialize_repair_stubs(unit, self.zk)
        st = {Path(s["note"]).stem: s for s in stubs}
        self.assertTrue(st["Old Split"]["stub"])
        self.assertEqual(sorted(st["Old Split"]["targets"]), sorted([self.b, "Renamed Target"]))
        self.assertNotIn("Old In Place", st)
        keep_before = self.vpath("Old Keep").read_text()
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
        a = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertNotIn("Old Keep", a)  # X2: the old note is never edited in place (no status line)
        self.assertEqual(self.vpath("Old Keep").read_text(), keep_before)
        self.assertEqual(ev["repair_unclaimed"], [f"{PN}/Old Mystery.md"])
        self.assertEqual({a[t] for t in (self.a, self.b)}, {"pending-review"})

    def test_dropped_target_and_no_target_left(self) -> None:
        rename = self.old("Old Rename")
        unit = self.new_unit("repair", [])
        rep = (f"=====NOTE: {self.a} | repairs: Old Rename.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Rename.md#b1 | kept in {self.a}\n")
        self.fake(repair=[rep])
        sc.repair_unit(self.staging, self.zk, unit, V5, [rename], [])
        (unit / "out" / PN / f"{self.a}.md").unlink()  # a fix call dropped it
        m = json.loads((unit / "marked.json").read_text())
        m["deleted"] = [self.a]
        (unit / "marked.json").write_text(json.dumps(m))
        self.assertEqual(ri.materialize_repair_stubs(unit, self.zk)[0]["why"], "no target note is left")
        self.assertFalse((unit / "out" / rename).exists())  # the old note stays as it is

    def test_multi_source_note_carries_bullets_to_next_video(self) -> None:
        shutil.copy(FICT / "lit_fictional" / f"{self.V2}.md", self.staging / "lit")
        multi = self.old("Old Multi", vids=(V5, self.V2), bullets=2)
        state = self.plan([multi])
        b1 = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual((b1["vid"], b1["notes"], b1["bullets"][multi]), (V5, [multi], ["Old Multi.md#b1", "Old Multi.md#b2"]))
        self.assertFalse(b1["last"][multi])
        rep = (f"=====NOTE: {self.a} | repairs: Old Multi.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Multi.md#b1 | kept in {self.a}\nOld Multi.md#b2 | dropped: not in this transcript\n")
        self.fake(repair=[rep, "=====KEEP-NEEDS-REPAIR: Old Multi.md=====\nnot here either\n=====BULLETS=====\n"
                               "Old Multi.md#b2 | dropped: not in this transcript\n"])
        u1, _res1 = self.run_batch(b1, state)
        self.assertFalse(json.loads((u1 / "repair_map.json").read_text())[multi]["final"])
        st = ri.materialize_repair_stubs(u1, self.zk)[0]
        self.assertTrue(st["stub"])  # round 11: a stub with superseded_pending, never the old note beside
        self.assertIn("superseded_pending", (u1 / "out" / multi).read_text())
        b2 = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual((b2["vid"], b2["bullets"][multi]), (self.V2, ["Old Multi.md#b2"]))
        self.assertTrue(b2["last"][multi])
        self.assertEqual(b2["prior_titles"][multi], [self.a])
        u2, _res2 = self.run_batch(b2, state)
        user = [c for c in self.log() if c["kind"] == "repair"][-1]["user"]
        self.assertIn("Old Multi.md#b2 | legacy bullet 2", user)
        self.assertNotIn("Old Multi.md#b1 |", user)
        self.assertIn(f"[[{self.a}]]", user)
        self.assertEqual(json.loads((u2 / "repair_map.json").read_text())[multi]["targets"], [self.a])
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][multi]
        self.assertEqual((e["status"], e["unsupported_bullets"]), ("done", ["Old Multi.md#b2"]))

    def test_batches_are_capped_and_notes_are_read_again(self) -> None:
        notes = [self.old(f"Old Cap {i}", bullets=2) for i in range(5)]
        os.environ.update(ZR_REPAIR_MAX_NOTES="2", ZR_REPAIR_MAX_BULLETS="3")
        state = self.plan(notes)
        # M4: a note repaired after the plan was made is not sent again.
        self.vpath("Old Cap 0").write_text(self.ta)
        b = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual(b["notes"], [notes[1], notes[2]])
        self.assertEqual(sum(len(x) for x in b["bullets"].values()), 3)  # the second note is split
        self.assertEqual(b["bullets"][notes[2]], ["Old Cap 2.md#b1"])
        self.assertFalse(b["last"][notes[2]])
        again = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual(again["key"], b["key"])  # a running batch is handed out again (resume)

    def test_unknown_note_goes_to_next_candidate_after_left_or_keep(self) -> None:
        p = self.vpath("Old Mystery")
        p.write_text(OLD_NOTE.format(t="Old Mystery").replace("Legacy claim.", "x"))
        rel = f"{PN}/Old Mystery.md"
        state = sc.state_path(self.staging)
        entry = {"sha": ri._sha256(p), "status": "open", "pos": 0, "titles": [], "kind": "unknown",
                 "sources": [V5, self.V2], "bullets": {"Old Mystery.md#b1": {"text": "legacy bullet", "answers": {}}}}
        state.write_text(json.dumps({"notes": {rel: entry}, "batches": {}}))
        b = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual((b["vid"], b["unknown"]), (V5, [rel]))
        sc.record_answers(state, b, {"applied": [{"old": rel, "action": "left as it is"}]})
        b = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual(b["vid"], self.V2)  # H: the next candidate in the same run
        sc.record_answers(state, b, {"applied": [{"old": rel, "action": "left as it is"}]})
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        self.assertEqual(json.loads(state.read_text())["notes"][rel]["status"], "operator")
    def test_reply_problems_are_merged(self) -> None:
        keep = self.old("Old Keep")
        rep = (f"=====NOTE: {self.a} | repairs: Not An Old Note.md=====\n{self.ta.rstrip()}\n"
               "=====KEEP-NEEDS-REPAIR: Unknown.md=====\nx\n=====KEEP-NEEDS-REPAIR: Old Keep.md=====\nno passage\n"
               "=====BULLETS=====\nOld Keep.md#b1 | dropped: not in this transcript\nnonsense line\n")
        self.fake(repair=[rep])
        unit = self.new_unit("repair", [])
        res = sc.repair_unit(self.staging, self.zk, unit, V5, [keep], [])
        types = {p["type"] for p in res["problems"]}
        self.assertTrue({"repairs-unknown-note", "keep-unknown-note", "bullet-line-unparsed"} <= types)
        self.assertEqual(json.loads((unit / "claims.json").read_text())["problems"], res["problems"])

    def test_truncated_repair_reply_is_continued(self) -> None:
        rename = self.old("Old Rename")
        cut = f"=====NOTE: {self.a} | repairs: Old Rename.md | bullets: b1=====\n" + self.ta[:200]
        rest = (f"=====NOTE: {self.a} | repairs: Old Rename.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                f"=====BULLETS=====\nOld Rename.md#b1 | kept in {self.a}\n")
        self.fake(repair=[{"content": cut, "finish": "length"}], cont=[rest])
        unit = self.new_unit("repair", [])
        res = sc.repair_unit(self.staging, self.zk, unit, V5, [rename], [])
        self.assertEqual(res["written"], [self.a])
        self.assertEqual([c["kind"] for c in self.log()], ["repair", "cont"])

    def test_unknown_source_without_match_goes_to_operator(self) -> None:
        self.vpath("Old Lonely").write_text(OLD_NOTE.format(t="Old Lonely").replace("Legacy claim.", "Zebra quilting."))
        g = sc.repair_groups(self.staging, self.zk, [f"{PN}/Old Lonely.md"])
        self.assertEqual([o["note"] for o in g["operator"]], [f"{PN}/Old Lonely.md"])
        self.assertEqual(g["unknown"], {})


# --------------------------------------------------------------------------- #
# End to end: loop.sh in single mode, two fictional videos
# --------------------------------------------------------------------------- #


class SingleModeEndToEnd(unittest.TestCase):
    def test_loop(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="single-e2e-"))
        try:
            stg, zk = tmp / "stg", tmp / "vault" / "ZK"
            shutil.copytree(FICT / "lit_fictional", stg / "lit")
            shutil.copytree(FICT / "vault_fictional", zk)
            contract = (ZR / "NOTE_CONTRACT.md").read_text()
            ex = {sec: re.search(r"### " + re.escape(sec) + r".*?```markdown\n(.*?)```", contract, re.S).group(1)
                  for sec in ("14.1", "14.2")}
            t1, t2 = (re.search(r"^# (.+)$", ex[k], re.M).group(1) for k in ("14.1", "14.2"))
            q = {"synth": [reply([("C1", "02:10", "rows three pieces"), ("C2", "02:30", "a story")],
                                 [(t1, ["C1"], ex["14.1"])], [("C2", "not a transferable training claim")]),
                           reply([("C1", "01:40", "48 hours")], [(t2, ["C1"], ex["14.2"])])],
                 "cont": [], "coverage": [], "fix": [], "repair": []}
            (tmp / "queue.json").write_text(json.dumps(q))
            (tmp / "fake.py").write_text(FAKE)
            (tmp / "review.py").write_text(REVIEW)
            (stg / "queue.json").write_text(json.dumps({"version": 1, "items": [
                {"id": v, "kind": "video", "stage": "extracted", "lit_note": str(stg / "lit" / f"{v}.md")}
                for v in ("vid-EXAMPLE0001", "vid-EXAMPLE0002")]}))
            for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                        ["add", "-A"], ["commit", "-qm", "init"]):
                git(zk, *cmd)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ZR_", "ZK_"))}
            env.update(ZR_STATE_DIR=str(tmp / "state"), ZR_TEST_RUN="1", VAULT=str(tmp / "vault"), ZK_FOLDER="ZK",
                       ZR_STAGING=str(stg), ZR_SYNTH_STAGES="one", ZR_SYNTH_SCRIPT=str(tmp / "fake.py"), ZR_REVIEW_SCRIPT=str(tmp / "review.py"),
                       FAKE_QUEUE=str(tmp / "queue.json"), FAKE_LOG=str(tmp / "log.jsonl"), DEEPSEEK_API_KEY=FAKE_KEY,
                       ZR_AGENT_SCRIPT="/bin/false", MAX_ITERS="5", MAX_STALL="1", SYNTH_BACKOFF="0",
                       AGENTS_FILE=str(ZR / "AGENTS_transcript.md"))
            r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            if os.environ.get("E2E_DEBUG"):
                print(r.stdout[-5000:], r.stderr[-3000:])
            pub = {p.stem for p in (zk / PN).glob("*.md")}
            self.assertIn(t1, pub)
            self.assertIn(t2, pub)
            items = {it["id"]: it for it in json.loads((stg / "queue.json").read_text())["items"]}
            self.assertEqual({it["stage"] for it in items.values()}, {"synthesized"})
            self.assertEqual(sum(it.get("synth_attempts", 0) for it in items.values()), 0)
            units = sorted(stg.glob("runs/*/units/*"))
            self.assertEqual(len(units), 2)
            claims = json.loads((units[0] / "claims.json").read_text())
            self.assertEqual(claims["coverage"]["C2"], {"skip": "not a transferable training claim"})
            self.assertFalse(list(stg.glob("runs/*/units/*/tools.jsonl")))  # no agent ran
            idx = {c["title"]: c for c in json.loads((stg / "concept-index.json").read_text())["concepts"]}
            self.assertLessEqual({t1, t2}, set(idx))
            committed = git(zk, "log", "--name-only", "--format=").stdout
            self.assertIn(f"{t1}.md", committed)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ReviewInputRound9(unittest.TestCase):
    def test_adjacent_sentences(self) -> None:
        t = "Intro here. He said the before part. The quoted words are here. Then the after part. Is it? End."
        p = t.find("The quoted")
        a, b = vc.quote_sentences(t, p, p + 10)
        a2, b2 = vc.adjacent_sentences(t, a, b)
        self.assertEqual(t[a2:b2], "He said the before part. The quoted words are here. Then the after part.")
        q = t.find("End")
        a, b = vc.quote_sentences(t, q, q + 3)
        self.assertEqual(vc.adjacent_sentences(t, a, b)[0], a)  # the question before is the other voice

    def test_hedge_in_another_clause(self) -> None:
        q = ["let's say i'm doing everything else perfect but i'm relaxing my core, i can't hold the planche"]
        self.assertIsNone(vc.dropped_hedge("If the core is relaxed you cannot hold the planche.",
                                           "X Requires Squeezing The Core For Planche", q, []))
        q2 = ["let's say three minutes of rest between the attempts"]
        self.assertEqual(vc.dropped_hedge("Rest three minutes between attempts.", "X Rests Three Minutes", q2,
                                          [(3.0,)]), "let's say")

    def test_defaults_and_prices(self) -> None:
        import pricing
        import review_call as rc
        self.assertEqual(rc.DEFAULT_REVIEW_MODEL, "anthropic/claude-sonnet-5.5")
        self.assertEqual(pricing.price("anthropic/claude-sonnet-5.5"), (2.0, 10.0))
        self.assertEqual(sc.settings()["model"], "anthropic/claude-sonnet-5.5")
        lib = (ZR / "run_lib.sh").read_text()
        for line in ('ZR_SYNTH_MODE="${ZR_SYNTH_MODE:-single}"', 'ZR_REVIEW_MODEL="${ZR_REVIEW_MODEL:-anthropic/claude-sonnet-5.5}"'):
            self.assertIn(line, lib)


class BuiltPrompts(_Single):
    def test_prompts_are_built_and_hashed(self) -> None:
        for kind in ("synth", "fix", "repair"):
            text = sc.system_text(kind)
            self.assertNotIn("<!-- include", text, kind)
            self.assertNotIn("<!-- single-only", text, kind)
        meta = json.loads((self.run_dir / "run.json").read_text())
        self.assertLessEqual({"synth_single.md", "fix_single.md", "repair_single.md", "source_check.md"},
                             set(meta["prompt_sha256"]))

    def test_b_fixture_reply_parses(self) -> None:
        p = sc.parse_reply((FICT / "single_reply_fictional.txt").read_text())
        self.assertEqual((len(p["claims"]), len(p["notes"]), len(p["skips"])), (7, 5, 1))
        self.assertTrue(all(n["complete"] for n in p["notes"]))

    def test_counted_adjective_is_not_a_quantity(self) -> None:
        q = vc.extract_quantities("Requires 20 Seconds Two-Armed On The Edge Before One-Arm Hangs")
        self.assertEqual([x.values for x in q], [(20.0,)])
