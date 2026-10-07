"""Round 21, pilot 8 points (correct-behaviour convention). Fake LLMs only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5

import review_call as rc  # noqa: E402
import stub_check  # noqa: E402
import synth_call as sc  # noqa: E402


def _rep(old: str, parts: list[tuple[str, str, str]], ledger: str) -> str:
    out = "".join(f"=====NOTE: {t} | repairs: {old}.md | bullets: {b}=====\n{text.rstrip()}\n" for t, b, text in parts)
    return out + f"=====BULLETS=====\n{ledger}"


class RejectedTargetStaysOpen(_Repair):
    """Point 1 (Intensity, TTF b18/b20, Unassisted b3): a rejected target never makes its
    bullets `unsupported`."""

    def _reject(self, *texts: str) -> None:
        os.environ["REJECT_SHAS"] = json.dumps([hashlib.sha256((t if t.endswith("\n") else t + "\n").encode()).hexdigest()
                                                for t in texts])
        os.environ["ZR_REVIEW_REPAIR"] = "0"

    def test_only_target_rejected_note_needs_repair_and_the_operator_gets_the_paths(self) -> None:
        rel = self.old("Old Int", 2)
        before = self.vpath("Old Int").read_text()
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        b, u, res, _ = self.run_batch(state, _rep("Old Int", [(self.a, "b1,b2", self.ta)],
                                                  f"Old Int.md#b1 | kept in {self.a}\nOld Int.md#b2 | kept in {self.a}\n"),
                                      review=False, finalize=False)
        self._reject((u / "out" / PN / f"{self.a}.md").read_text())
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["status"], "needs-repair")
        self.assertEqual(e.get("unsupported_bullets"), [])
        self.assertEqual(sorted(e["rejected_bullets"]), ["Old Int.md#b1", "Old Int.md#b2"])
        self.assertEqual(self.vpath("Old Int").read_text(), before)  # unchanged in the vault
        ops = [o for o in json.loads((self.staging / "needs_operator.json").read_text()) if o["note"] == rel]
        self.assertTrue(ops and ops[0]["state"] == "needs-repair", ops)
        self.assertEqual([x["id"] for x in ops[0]["bullets"]], ["Old Int.md#b1", "Old Int.md#b2"])
        self.assertTrue(ops[0]["quarantined_targets"], ops)
        # a later run plans it again (W15: needs-repair)
        out = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertIn({"note": rel, "state": "needs-repair"}, out.get("replanned", []))

    def test_rejected_bullet_beside_a_published_target_is_open_in_the_stub(self) -> None:
        rel = self.old("Old Ttf", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        b, u, res, _ = self.run_batch(state, _rep("Old Ttf", [(self.a, "b1", self.ta), (self.b, "b2", self.tb)],
                                                  f"Old Ttf.md#b1 | kept in {self.a}\nOld Ttf.md#b2 | kept in {self.b}\n"),
                                      review=False, finalize=False)
        self._reject((u / "out" / PN / f"{self.b}.md").read_text())
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        sc.next_batch(self.staging, self.zk, state)
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e.get("unsupported_bullets"), [])
        entries = dict(stub_check.claim_entries(self.vpath("Old Ttf").read_text()))
        self.assertTrue(entries["legacy bullet 2"].startswith("open: target rejected"), entries)
        counts = ri.repair_bullet_counts(self.staging, self.zk)
        self.assertEqual(counts["bullets_open"].get("target rejected"), ["Old Ttf.md#b2"])


class BulletCounts(_Repair):
    """Point 3: one class per bullet; the classes add up to the old bullets."""

    def test_classes_add_up(self) -> None:
        rel = self.old("Old Cnt", 3)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.run_batch(state, _rep("Old Cnt", [(self.a, "b1", self.ta)],
                                   f"Old Cnt.md#b1 | kept in {self.a}\nOld Cnt.md#b2 | dropped: not in this transcript\n"
                                   "Old Cnt.md#b3 | dropped: contradicts the transcript\n"))
        sc.next_batch(self.staging, self.zk, state)
        c = ri.repair_bullet_counts(self.staging, self.zk)
        self.assertEqual(c["counts"]["total"], 3)
        self.assertEqual(c["counts"]["settled"] + c["counts"]["unverified"], 1)
        self.assertEqual(c["bullets_unsupported"], ["Old Cnt.md#b2"])
        self.assertEqual(list(c["bullets_dropped"]), ["contradicts the transcript"])
        self.assertEqual(sum(v for k, v in c["counts"].items() if k != "total"), 3)


class LedgerFollowsRename(_Repair):
    """Point 2 (Nose b1): the ledger names a title that a fix renamed; the check follows it."""

    def test_renamed_title_is_followed(self) -> None:
        a2 = "Renamed Floor Note"
        self.vpath(a2).write_text(self.ta.replace(f"# {self.a}\n", f"# {a2}\n"))
        bt = "Planche lean presses can be done on the floor without parallettes on the wrists."
        p = {"notes": [], "problems": [], "bullets": {"O.md#b1": {"decision": "kept", "title": self.a}}}
        cands = {"O.md#b1": [{"at": "01:20", "text": "x"}]}
        sc._verify_ledger(p, cands, self.zk, {"O.md#b1": bt}, V5, {self.a: a2})
        self.assertEqual(p["bullets"]["O.md#b1"]["decision"], "kept")
        p2 = {"notes": [], "problems": [], "bullets": {"O.md#b1": {"decision": "kept", "title": self.a}}}
        sc._verify_ledger(p2, cands, self.zk, {"O.md#b1": bt}, V5, {})
        self.assertEqual(p2["bullets"]["O.md#b1"]["decision"], "unverified")  # without the map: no file


class Relink(_Repair):
    """Point 6b: offline relink (dry run by default; exact recorded line only)."""

    def _setup(self) -> Path:
        x = self.vpath("Linker X")
        line = f"- {self.a} — waits."
        x.write_text(f"---\ntype: permanent note\n---\n\n# Linker X\n\nBody.\n\n## Connected Ideas\n\n{line}\n")
        (self.staging / "unlinked.json").write_text(json.dumps([
            {"note": f"{PN}/Linker X.md", "target": self.a, "at": "t", "line": line,
             "link": f"[[{self.a}]]", "plain": self.a}]))
        return x

    def test_dry_run_then_apply(self) -> None:
        x = self._setup()
        before = x.read_text()
        out = ri.relink(self.staging, self.zk)  # target not published yet
        self.assertEqual(out["restored"], [])
        self.assertIn("not a published", out["skipped"][0]["why"])
        self.vpath(self.a).write_text(self.ta)
        out = ri.relink(self.staging, self.zk)  # dry run
        self.assertEqual(len(out["restored"]), 1)
        self.assertEqual(x.read_text(), before)
        out = ri.relink(self.staging, self.zk, apply=True)
        self.assertIn(f"- [[{self.a}]] — waits.", x.read_text())
        self.assertTrue(list(self.staging.glob("retired/relink-*/**/Linker X.md")))  # archived first
        self.assertEqual(json.loads((self.staging / "unlinked.json").read_text()), [])

    def test_changed_line_is_skipped(self) -> None:
        x = self._setup()
        self.vpath(self.a).write_text(self.ta)
        x.write_text(x.read_text().replace("— waits.", "— the user changed this."))
        out = ri.relink(self.staging, self.zk, apply=True)
        self.assertEqual(out["restored"], [])
        self.assertIn("recorded line found 0 times", out["skipped"][0]["why"])


class ChannelNameScrub(_Repair):
    """Point 7: "sthenics_ says" in the lead of an unresolved note becomes "The speaker"."""

    def test_channel_handle_in_the_lead(self) -> None:
        text = ("---\ntype: permanent note\nsources:\n  - id: vid-X\n    speaker: \"sthenics_\"\nverification: unverified\n"
                "---\n\n# Sthenics_ Says Rest Two Minutes\n\nsthenics_ says to rest two minutes. [src: vid-X @ 01:00]\n\n"
                "## Details\n\n- Sthenics says it again. [src: vid-X @ 01:00]\n")
        n = {"title": "Sthenics_ Says Rest Two Minutes", "text": text, "claims": []}
        u = sc.unresolve_note(n, "sthenics_", {"lit_speakers": [], "found": [], "channel": "sthenics_"})
        body = u["text"].split("\n---\n", 1)[1]
        self.assertNotIn("sthenics", body.split("# ", 1)[1].split("\n", 1)[1].lower())
        self.assertIn("The speaker says to rest two minutes.", u["text"])
        self.assertTrue(u["title"].startswith("Unresolved Speaker In "))


class DuplicateReport(_Repair):
    """Point 8: two notes of one video and speaker, same cited times, near-equal lead, other
    numbers: listed in possible_duplicates (report only, never merged)."""

    def test_report_pair(self) -> None:
        a = {"speaker": (("x",), ""), "key": "a", "vids": {V5}, "nums": {(2.0, "times")}, "skill": "s", "modality": "option",
             "times": {(V5, 267)}, "lead": {"push", "limit", "maximum", "two", "workouts", "four", "week"}}
        b = dict(a, key="b", nums={(2.0, "times"), (50.0, "percent")}, modality="recommendation",
                 times={(V5, 276)}, lead={"push", "limit", "maximum", "two", "workouts", "four", "week", "effort"})
        self.assertFalse(ri.same_note(a, b))
        self.assertTrue(ri.report_pair(a, b))
        self.assertFalse(ri.report_pair(a, dict(b, speaker=(("y",), ""))))
