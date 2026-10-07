"""Reviewer C, fourth review (frozen-v9): new attacks on the round-21 changes (stub design,
pipeline-write guard, count-open, pending cycles).

CONVENTION OF THIS FILE: the assertion states the DEFECT. PASS = the defect is present.
FAIL = the attack found nothing (or the setup broke; read the reason). A test whose name
starts with `test_ok_` is a control that states the CORRECT behaviour (PASS = correct).
No network, no LLM; fake models and fake workers only."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.probes_v8.test_probe8_A1_new import B_BICEPS, B_FLOOR, B_TOES, _Ledger
from tests.probes_v8.test_probe8_B_attacks import _Files
from tests.unit.test_run_integrity import PN, ZR, ri
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5
from tests.probes_v6 import test_probe_B_loop as _bl

import stub_check  # noqa: E402
import synth_call as sc  # noqa: E402


# --------------------------------------------------------------------------- #
# X-C1 / X-C2: a pending folder at the cycle limit that waits for a reply
# --------------------------------------------------------------------------- #


class StuckFolderThatWaits(unittest.TestCase):
    def _loop(self, reason: str, cycles: int, runs: int = 2) -> tuple[list[int], Path]:
        tmp = Path(tempfile.mkdtemp(prefix="probe9C-x1-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stg, _zk, env, _ = _bl.LoopProbes._setup(self, tmp)
        q = json.loads((stg / "queue.json").read_text())
        for it in q["items"]:
            it["stage"] = "synthesized"
        (stg / "queue.json").write_text(json.dumps(q))
        pdir = stg / "pending-review" / "20261004T000000Z-1--u"
        (pdir / "out").mkdir(parents=True)
        (pdir / "meta.json").write_text(json.dumps({"pending_cycles": cycles, "notes": [f"{PN}/X.md"],
                                                    "reasons": {f"{PN}/X.md": reason}}))
        codes = [subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True,
                                timeout=300).returncode for _ in range(runs)]
        return codes, pdir

    def test_X_C1_folder_at_the_limit_waiting_for_a_reply_exits_5_for_ever(self) -> None:
        """synth_call.py:3664-3682 + run_lib.sh:128-139. count-open counts a pending folder
        whose reason is `waiting-for-reply` as a wait, and since W7 prints at least 1 while
        anything waits. `open > 0` is tested before `stuck > 0`, so the driver exits 5, not
        6. pending_units() skips the folder (cycle limit), so nothing ever consumes the
        reply: every pass exits 5. A dispatcher that loops on 5 never stops and the operator
        never sees exit 6 (pilot 8 point 4 shape: the fifth unit stored the folder at cycle 5
        while it waited for a review reply)."""
        codes, pdir = self._loop("waiting-for-reply", ri.MAX_PENDING_CYCLES, runs=3)
        self.assertEqual(codes, [5, 5, 5])
        self.assertTrue(pdir.is_dir())

    def test_ok_control_folder_at_the_limit_with_another_reason_exits_6(self) -> None:
        codes, _ = self._loop("review-rejected", ri.MAX_PENDING_CYCLES, runs=1)
        self.assertEqual(codes, [6])

    def test_X_C2_max_pending_cycles_1_makes_every_new_folder_stuck(self) -> None:
        """run_integrity.py:3398 and synth_call.py:3678 read `pending_cycles or 1`: a folder
        stored at cycle 0 counts as cycle 1. With ZR_MAX_PENDING_CYCLES=1 a fresh folder that
        waits for its first review reply is never taken again; with X-C1 the driver exits 5
        on every pass while the answered reply is never consumed."""
        tmp = Path(tempfile.mkdtemp(prefix="probe9C-x2-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stg, zk, env, replies = _bl.LoopProbes._setup(self, tmp)
        q = json.loads((stg / "queue.json").read_text())
        q["items"] = [it for it in q["items"] if it["id"] == "vid-EXAMPLE0001"]
        (stg / "queue.json").write_text(json.dumps(q))
        env.update(ZR_MAX_PENDING_CYCLES="1")

        def ans(r: dict) -> str | None:
            if r["kind"] == "synth":
                return next(v for k, v in replies.items() if k in r["user"])
            if r["kind"] == "review":
                return review_answer(r["user"])
            return None
        codes = []
        for _ in range(6):
            r = subprocess.run(["bash", str(ZR / "loop.sh")], env=env, capture_output=True, text=True, timeout=300)
            codes.append(r.returncode)
            if r.returncode != 5:
                break
            work(stg / "exchange", ans, worker=lambda req: "reviewer-2" if req["kind"] == "review" else "writer-1")
        metas = [json.loads(p.read_text()) for p in stg.glob("pending-review/*--*/meta.json")]
        self.assertEqual(codes, [5] * 6)
        self.assertEqual([m.get("pending_cycles") for m in metas], [0])
        self.assertEqual(sc.exchange_status(stg / "exchange", stg)["open"], 0)  # the reply is there
        self.assertFalse(list((zk / PN).glob("Ivo Marsh*.md")))  # never published


# --------------------------------------------------------------------------- #
# X-C3: respeak changes pipeline-written notes without recording the write
# --------------------------------------------------------------------------- #


class RespeakTripsTheWriteGuard(_Files):
    def test_X_C3_link_rewrite_by_respeak_is_taken_for_a_user_edit(self) -> None:
        """synth_call.py:3487-3503 (respeak) and :3556-3562 (rewrite_links) write vault files
        directly; they do not call record_pipeline_write. guard_vault_write
        (run_integrity.py:1327-1345) then sees a sha that differs from the pipeline's last
        write and refuses every later pipeline write of that note as 'edited since the
        pipeline's last write (user edit?)'. Here: a published target T links to an
        unresolved note; respeak renames it and rewrites T's link; the next extension of T
        is refused and needs_operator.json gets a false 'user edit' entry. The same happens
        to a stub whose status line or superseded_by names the renamed note."""
        unres = "Unresolved Speaker In Ch Video Says Floor Presses Need No Parallettes"
        t_un = self.ta.replace(f"# {self.a}\n", f"# {unres}\n").replace(
            "verification:", "speaker_status: unresolved\nverification:", 1)
        t_un = re.sub(r'(?m)^(\s+speaker:\s*).*$', r'\1"unresolved speaker, Ch video"', t_un, count=1)
        self.vpath(unres).write_text(t_un)
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{unres}.md", sources=[V5],
                             synth_worker=["writer-1"])
        t_rel = f"{PN}/{self.b}.md"
        t_text = self.tb.replace("## Connected Ideas\n", f"## Connected Ideas\n\n- [[{unres}]] — same session.\n", 1)
        pub = ri._Publisher(self.staging, "r1", self.new_unit("synth", [V5]), self.zk, {})
        self.assertTrue(pub.write(t_rel, t_text, "publish"))
        out = sc.respeak(self.staging, self.zk, V5, "Sasha", apply=True, force_name=True)
        self.assertFalse(out[0].get("problem"), out)
        self.assertIn(self.b + ".md", out[0].get("links_rewritten") or [])
        ext = (self.zk / t_rel).read_text().replace("## Evidence", "Extended lead line.\n\n## Evidence", 1)
        pub2 = ri._Publisher(self.staging, "r2", self.new_unit("repair", []), self.zk, {})
        self.assertFalse(pub2.write(t_rel, ext, "an extension"))
        ops = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertTrue([o for o in ops if o["note"] == t_rel and "user edit" in o["why"]], ops)


# --------------------------------------------------------------------------- #
# X-C4: every finalize archives every unchanged stub again
# --------------------------------------------------------------------------- #


def _rep(old: str, title: str, bullets: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bullets}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class StubArchivedEveryFinalize(_Repair):
    def test_X_C4_unchanged_stub_is_archived_at_every_finalize(self) -> None:
        """run_integrity.py:2650-2656 builds the short legacy stub (repair_stub) and compares
        it with the rendered stub in the vault; they always differ, so pub.write() runs.
        write() archives the current file (run_integrity.py:1532-1534) BEFORE it renders the
        stub and sees that nothing changed. Result: one retired/ copy, one `note-archived`
        record and one `recomputed` entry per stub per finalize, with no change."""
        rel = self.old("Old Arc", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.run_batch(state, _rep("Old Arc", self.a, "b1", self.ta,
                                   f"Old Arc.md#b1 | kept in {self.a}\nOld Arc.md#b2 | dropped: not in this transcript\n"))
        sc.next_batch(self.staging, self.zk, state)
        stub = self.vpath("Old Arc").read_text()
        self.assertIn(stub_check.CLAIMS_HEADING, stub)

        def n_arch() -> int:
            return len([r for r in ri.provenance.read_records(self.staging)
                        if r.get("event") == "note-archived" and r.get("note") == rel])
        before = n_arch()
        for _ in range(3):
            ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        self.assertEqual(self.vpath("Old Arc").read_text(), stub)  # unchanged
        self.assertEqual(n_arch() - before, 3)


# --------------------------------------------------------------------------- #
# W1-W3 design question: a wrong "-> [[A]]" without a mark
# --------------------------------------------------------------------------- #


class WrongPointerUnmarked(_Ledger):
    def test_W10_unrelated_bullets_get_a_plain_pointer_to_a(self) -> None:
        """The stub carries every old bullet verbatim (design change; control below), but
        for b2 (biceps) and b3 (toes) it shows a plain `-> [[A]]` (no `(unverified)`), and A
        holds neither text. Nothing lists them for the operator."""
        old = "Old Lean"
        reply = (f"=====NOTE: {self.a} | repairs: {old}.md | bullets: b1,b2,b3=====\n{self.ta.rstrip()}\n=====BULLETS=====\n"
                 f"{old}.md#b1 | kept in {self.a}\n{old}.md#b2 | kept in {self.a}\n{old}.md#b3 | kept in {self.a}\n")
        rel, state, res, ev = self.repair_once(old, [B_FLOOR, B_BICEPS, B_TOES], reply)
        entries = dict(stub_check.claim_entries(self.vpath(old).read_text()))
        a_now = self.vpath(self.a).read_text().lower()
        self.assertNotIn("biceps", a_now)
        self.assertNotIn("toes", a_now)
        self.assertEqual(entries[B_BICEPS], f"→ [[{self.a}]]")
        self.assertEqual(entries[B_TOES], f"→ [[{self.a}]]")
        self.assertEqual(ri.repair_open_bullets(self.staging), [])

    def test_ok_old_text_is_in_the_stub_verbatim(self) -> None:
        old = "Old Lean2"
        reply = (f"=====NOTE: {self.a} | repairs: {old}.md | bullets: b1,b2,b3=====\n{self.ta.rstrip()}\n=====BULLETS=====\n"
                 f"{old}.md#b1 | kept in {self.a}\n{old}.md#b2 | kept in {self.a}\n{old}.md#b3 | kept in {self.a}\n")
        rel, state, res, ev = self.repair_once(old, [B_FLOOR, B_BICEPS, B_TOES], reply)
        entries = [e[0] for e in stub_check.claim_entries(self.vpath(old).read_text())]
        self.assertEqual(entries, [B_FLOOR, B_BICEPS, B_TOES])
