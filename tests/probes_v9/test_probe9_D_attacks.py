"""Reviewer D, round 4 (frozen-v9): do the drivers signal work that is NOT done? Real
repair_loop.sh / loop.sh runs with fake workers between passes (files backend), plus
count-open from live state.

CONVENTION OF THIS FILE: a `test_defect_*` asserts the DEFECT (PASS = the defect is
present, FAIL = the attack found nothing). A `test_ok_*` is a control (PASS = correct).
No network, no LLM."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from tests.unit.test_run_integrity import OLD_NOTE, PN, ZR, ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv, count_open
from tests.probes_v6.test_probe_A_crash import _Repair

import stub_check  # noqa: E402
import synth_call as sc  # noqa: E402


def _rep(old: str, title: str, bullets: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bullets}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class _Drv(_DriverEnv):
    def _old(self, title: str, n: int = 1) -> str:
        text = OLD_NOTE.format(t=title).replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        text = text.replace("- legacy bullet\n", "".join(f"- legacy bullet {i}\n" for i in range(1, n + 1)))
        self.vpath(title).write_text(text)
        return f"{PN}/{title}.md"

    def run_rl(self, *notes: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(ZR / "repair_loop.sh"), *notes], env=self._env(), capture_output=True,
                              text=True, timeout=600)

    def ops(self) -> list[dict]:
        p = self.staging / "needs_operator.json"
        return json.loads(p.read_text()) if p.exists() else []


class NeedsRepairExit0(_Drv):
    def test_defect_keep_needs_repair_closes_done_elsewhere_unsupported_exit_0(self) -> None:
        """The worker answers KEEP-NEEDS-REPAIR and, asked for the ledger, 'dropped: damaged
        transcript' (the only planned video). apply_repair marks the OLD note
        `verification: needs-repair` in the vault, so next_batch sees the note changed
        (`_still_old` False) and closes it `done-elsewhere` with EVERY bullet in
        `unsupported_bullets` (synth_call.py next_batch, the done-elsewhere branch does not
        use classify_bullets): the damaged-transcript bullet that pilot 8 keeps OPEN is
        recorded unsupported. needs_operator.json stays empty; the summary keys disagree
        (`repair_bullets`: open/damaged transcript; `repair_bullets_open`: unsupported).
        repair_loop.sh exits 0."""
        rel = self._old("Old Keep")
        codes = []
        for _ in range(4):
            r = self.run_rl(rel)
            codes.append(r.returncode)
            if r.returncode != 5:
                break
            work(self.xdir, lambda q: ("=====KEEP-NEEDS-REPAIR: Old Keep.md=====\nThe passage is garbled.\n"
                                       if q["kind"] == "repair" else (
                                           "=====BULLETS=====\nOld Keep.md#b1 | dropped: damaged transcript\n"
                                           if q["kind"] == "ledger" else None)), worker="w-rep")
        e = json.loads((self.staging / "repair_state.json").read_text())["notes"][rel]
        summ = json.loads(sorted((self.staging / "runs").glob("*/summary.json"))[-1].read_text())
        if os.environ.get("PROBE_DEBUG"):
            print(codes, e, summ.get("notes_for_operator"))
        self.assertEqual(e["bullets"]["Old Keep.md#b1"]["answers"][V5]["reason"], "damaged transcript")
        self.assertEqual(e["status"], "done-elsewhere")
        self.assertEqual(e["unsupported_bullets"], ["Old Keep.md#b1"])
        self.assertEqual(self.ops(), [])
        self.assertIn("damaged transcript", summ["repair_bullets"]["bullets_open"])
        self.assertEqual(summ["repair_bullets_open"][0]["unsupported"], ["Old Keep.md#b1"])
        self.assertEqual(codes[-1], 0, codes)


class UserEditHealedExit0(_Drv):
    def test_defect_user_edit_between_passes_overwritten_by_heal_and_exit_0(self) -> None:
        """Pass 1: repair request. Pass 2: the reply is consumed, the target waits for its
        review. Between pass 2 and 3 (allowed: README checklist item 2) the user edits the
        old note. Pass 3: the target publishes; the stub is refused (the guard sees the user
        edit), and the old note stays beside its replacement (needs_operator.json). Pass 4:
        `start` -> recover() -> heal_beside() WITHOUT a publisher (no guard, no stub_check)
        writes the OLD-FORMAT stub over the user-edited old note: the user edit and the old
        bullet leave the vault (only retired/heal-*/ keeps them). The summary lists a
        bullet_invariant_violation, and repair_loop.sh exits 0. Same root cause as reviewer
        B's RefusedStubHealedByRecover; here the trigger is a plain user edit."""
        rel = self._old("Old Edit")
        rep = _rep("Old Edit", self.a, "b1", self.ta, f"Old Edit.md#b1 | kept in {self.a}\n")
        codes, edited = [], False
        for _ in range(6):
            r = self.run_rl(rel)
            codes.append(r.returncode)
            if r.returncode != 5:
                break
            kinds = {x["kind"] for x in sc.exchange_list(self.xdir, staging=self.staging)}
            if "review" in kinds and not edited:
                p = self.vpath("Old Edit")
                p.write_text(p.read_text().replace("Legacy claim.", "Legacy claim. USER EDIT."))
                edited = True
            work(self.xdir, lambda q: rep if q["kind"] == "repair" else (
                review_answer(q["user"]) if q["kind"] == "review" else None),
                worker=lambda q: "w-rep" if q["kind"] == "repair" else "w-rev")
        if os.environ.get("PROBE_DEBUG"):
            print(codes, self.ops(), r.stdout[-3000:])
        self.assertTrue(edited)
        self.assertTrue(self.vpath(self.a).is_file())  # the target is published
        now = self.vpath("Old Edit").read_text()
        self.assertNotIn("USER EDIT", now)  # the user edit left the vault
        self.assertNotIn("legacy bullet 1", now)  # and the old bullet too
        self.assertNotIn(stub_check.CLAIMS_HEADING, now)  # an old-format stub, never checked
        heal = [x for x in (self.staging / "provenance.jsonl").read_text().splitlines() if "note-stub-healed" in x]
        self.assertTrue(heal)
        self.assertTrue([q for q in (self.staging / "retired").rglob("Old Edit*.md") if "USER EDIT" in q.read_text()])
        summ = json.loads(sorted((self.staging / "runs").glob("*/summary.json"))[-1].read_text())
        self.assertTrue(summ["bullet_invariant_violations"])
        self.assertEqual(codes[-1], 0, codes)


class RefusedWriteRecordedAsPublished(_Repair):
    def test_defect_refused_stub_is_recorded_as_published(self) -> None:
        """run_integrity.py:3159: finalize ignores the return value of `pub.write`. A stub
        that the stub check refuses gets entry action `published` and a `note-published`
        provenance record, and the unit is `unit_ok` (finalize exit 0). The summary counts
        it as published; only needs_operator.json says otherwise."""
        rel = self.old("Old Ref", 1)
        before = self.vpath("Old Ref").read_text()
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        with mock.patch.object(stub_check, "check", return_value=["simulated: old line not carried"]):
            _b, _u, _res, ev = self.run_batch(state, _rep("Old Ref", self.a, "b1", self.ta,
                                                          f"Old Ref.md#b1 | kept in {self.a}\n"))
        self.assertEqual(self.vpath("Old Ref").read_text(), before)  # not written
        entry = next(n for n in ev["notes"] if n["note"] == rel)
        if os.environ.get("PROBE_DEBUG"):
            print(entry, ev["unit_ok"])
        self.assertEqual(entry["action"], "published")
        self.assertTrue(ri.last_published(self.staging, rel))
        self.assertTrue(ev["unit_ok"])


class OtherDriversWaitNeverConsumed(_Drv):
    def test_defect_repair_loop_exits_5_forever_for_an_answered_synth_reply(self) -> None:
        """A synthesis video waits (queue stage waiting-for-reply) and its worker has
        ANSWERED. Only loop.sh consumes that reply. repair_loop.sh (which never releases or
        consumes queue items, W19 fix) exits 5 with 'N request(s) wait for a worker reply'
        on every run, while no request is open: a dispatcher that reruns the driver on
        exit 5 loops without end."""
        from tests.unit.test_synth_single import reply
        self.fake()
        self.assertEqual(self.synth()["outcome"], "waiting-for-reply")
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="w1")
        self.assertEqual(sc.exchange_status(self.xdir, self.staging)["open"], 0)
        text = OLD_NOTE.format(t="Old Nolit").replace("created:", "sources:\n  - id: vid-NOTRANSCR1\n    speaker: X\ncreated:")
        self.vpath("Old Nolit").write_text(text)
        codes, msgs = [], []
        for _ in range(3):
            r = self.run_rl(f"{PN}/Old Nolit.md")
            codes.append(r.returncode)
            msgs.append("wait for a worker reply" in r.stderr)
        self.assertEqual(codes, [5, 5, 5])
        self.assertEqual(msgs, [True, True, True])
        self.assertEqual(len(sc.exchange_status(self.xdir, self.staging)["unconsumed"]), 1)


class TargetRejectedNeedsRepairExit0(_Drv):
    def test_defect_needs_repair_after_rejected_target_exit_0(self) -> None:
        """The repair target is rejected by its source-check review (review fix off). The
        bullet's answer becomes target-rejected; the note closes `needs-repair` with an
        entry in needs_operator.json. repair_loop.sh ends with exit 0 (no code for
        'operator work left')."""
        rel = self._old("Old Rej")
        rep = _rep("Old Rej", self.a, "b1", self.ta, f"Old Rej.md#b1 | kept in {self.a}\n")
        codes = []
        for _ in range(8):
            r = self.run_rl(rel)
            codes.append(r.returncode)
            if r.returncode != 5:
                break
            work(self.xdir, lambda q: rep if q["kind"] == "repair" else (
                review_answer(q["user"], reject=lambda n: True) if q["kind"] == "review" else None),
                worker=lambda q: "w-rep" if q["kind"] == "repair" else "w-rev")
        e = json.loads((self.staging / "repair_state.json").read_text())["notes"][rel]
        if os.environ.get("PROBE_DEBUG"):
            print(codes, e, self.ops(), r.stdout[-2500:])
        self.assertEqual(e["status"], "needs-repair")
        self.assertTrue([o for o in self.ops() if o.get("note") == rel and o.get("state") == "needs-repair"])
        self.assertEqual(codes[-1], 0, codes)


class MarkObsoleteHeldKeys(_Drv):
    """W36 fix (`_key_held`) for the waits that repair_loop.sh leaves: a repair batch and a
    pending-review folder of a repair target."""

    def _mark(self, key: str) -> subprocess.CompletedProcess:
        import sys
        return subprocess.run([sys.executable, str(ZR / "synth_call.py"), "exchange-status", "--staging",
                               str(self.staging), "--count-open", "--mark-obsolete", key],
                              capture_output=True, text=True, env=dict(os.environ))

    def test_ok_repair_batch_key_is_held(self) -> None:
        rel = self._old("Old Held")
        self.assertEqual(self.run_rl(rel).returncode, 5)
        k = sc.exchange_list(self.xdir, staging=self.staging)[0]["key"]
        r = self._mark(k)
        self.assertIn("held by a waiting unit", r.stderr)

    def test_defect_pending_review_key_not_held(self) -> None:
        """The target waits in pending-review/ for its source-check review. The operator marks
        that review key obsolete: `_key_held` looks at runs/*/units/*/agent_result.json,
        runs/*/units/*/review-*/agent_result.json and pending-review/*--*/agent_result.json;
        if none of them names the key the mark is accepted, the request leaves
        exchange-list --open (workers never see it), and count-open stays >= 1
        (max(n_open, 1)): every later pass exits 5 and nothing can answer."""
        rel = self._old("Old Held")
        rep = _rep("Old Held", self.a, "b1", self.ta, f"Old Held.md#b1 | kept in {self.a}\n")
        self.assertEqual(self.run_rl(rel).returncode, 5)
        work(self.xdir, lambda q: rep if q["kind"] == "repair" else None, worker="w-rep")
        self.assertEqual(self.run_rl(rel).returncode, 5)
        rev = [x for x in sc.exchange_list(self.xdir, staging=self.staging) if x["kind"] == "review"]
        self.assertEqual(len(rev), 1)
        r = self._mark(rev[0]["key"])
        if os.environ.get("PROBE_DEBUG"):
            print(r.stdout, r.stderr)
        self.assertNotIn("held by a waiting unit", r.stderr)
        self.assertEqual(sc.exchange_list(self.xdir, staging=self.staging), [])
        codes = [self.run_rl(rel).returncode for _ in range(2)]
        self.assertEqual(codes, [5, 5])
        self.assertEqual(sc.exchange_list(self.xdir, staging=self.staging), [])  # nothing to answer
