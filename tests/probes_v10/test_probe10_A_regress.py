"""Probe 10 A (fifth review, frozen-v10): the v9 probes whose SETUP changed, adapted
minimally, plus checks of the round-22 fixes X1, X2, X3, X6, X11, X14 by behaviour.

CONVENTION OF THIS FILE (mixed, by prefix):
- `test_ok_*`     asserts the CORRECT behaviour (PASS = fixed / correct).
- `test_defect_*` asserts the DEFECT (PASS = the defect is present).
Fake LLMs and fake workers only; no network."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.probes_v9.test_probe9_D_attacks import _Drv, _rep
from tests.probes_v9.test_probe9_D_exitcodes import _crash_env, _run
from tests.probes_v6 import test_probe_B_loop as _bl
import shutil
import tempfile
import unittest
from tests.unit.test_run_integrity import OLD_NOTE, PN, ZR, ri, validate
from tests.unit.test_run_integrity_r13 import review_answer, work
from tests.unit.test_synth_single import V5

import stub_check  # noqa: E402
import synth_call as sc  # noqa: E402


def _errors(t, title: str) -> list[str]:
    errors, _w, _n = validate.check(t.zk, None, str(t.staging))
    return [e for e in errors if title in e]


def _prov(staging: Path, event: str) -> list[dict]:
    p = staging / "provenance.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if f'"{event}"' in x] if p.exists() else []


# --------------------------------------------------------------------------- #
# X1: recover() never writes; the refused stub stays for the operator
# --------------------------------------------------------------------------- #


class X1RefusedStubNotHealed(_Repair):
    """Adapted from test_probe9_B_bypass.RefusedStubHealedByRecover: the v9 trigger (an
    indented line before the first bullet) is no longer refused (X18), so the refusal is
    forced with a stub_check.check that reports a problem during the finalize."""

    def test_ok_recover_leaves_old_note_lists_it_and_next_finalize_writes_checked_stub(self) -> None:
        rel = self.old("Old Indent", 2)
        p = self.vpath("Old Indent")
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        before = p.read_bytes()
        rep = (f"=====NOTE: {self.a} | repairs: Old Indent.md | bullets: b1, b2=====\n{self.ta.rstrip()}\n"
               f"=====BULLETS=====\nOld Indent.md#b1 | kept in {self.a}\nOld Indent.md#b2 | kept in {self.a}\n")
        with mock.patch.object(stub_check, "check", return_value=["simulated refusal"]):
            self.run_batch(state, rep)
        self.assertEqual(p.read_bytes(), before)
        self.assertTrue(self.vpath(self.a).is_file())
        self.assertTrue(_errors(self, "Old Indent"))           # validate reports it
        ri.recover(self.staging, self.zk)
        self.assertEqual(p.read_bytes(), before)               # recover wrote nothing
        self.assertEqual(_prov(self.staging, "note-stub-healed"), [])
        no = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertTrue(any(x.get("note") == rel and "beside" in x.get("why", "") for x in no), no)
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = p.read_text()
        self.assertIn("old_bullets: 2", stub)                  # the proper, checked stub
        arch = ri.archived_old_text(self.staging, rel)
        self.assertEqual(stub_check.check(arch, stub, self.zk), [])
        self.assertEqual(_errors(self, "Old Indent"), [])


class X1UserEditBetweenPasses(_Drv):
    """Adapted from test_probe9_D_attacks.UserEditHealedExit0 (same flow)."""

    def test_ok_user_edit_kept_no_heal_and_exit_is_not_0(self) -> None:
        rel = self._old("Old Edit")
        rep = _rep("Old Edit", self.a, "b1", self.ta, f"Old Edit.md#b1 | kept in {self.a}\n")
        codes, edited, outs = [], False, []
        for _ in range(6):
            r = self.run_rl(rel)
            codes.append(r.returncode)
            outs.append(r.stdout[-800:] + r.stderr[-800:])
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
            print(codes, self.ops(), outs[-1])
        self.assertTrue(edited)
        self.assertIn("USER EDIT", self.vpath("Old Edit").read_text())
        self.assertEqual(_prov(self.staging, "note-stub-healed"), [])
        self.assertNotEqual(codes[-1], 0, (codes, outs[-1]))


# --------------------------------------------------------------------------- #
# X2: no in-place repair
# --------------------------------------------------------------------------- #


class X2NoInPlace(_Repair):
    def test_ok_old_title_refused_old_note_unchanged_bullets_open_for_operator(self) -> None:
        rel = self.old("Old Inp", 2)
        before = self.vpath("Old Inp").read_bytes()
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        inp = self.ta.replace(f"# {self.a}\n", "# Old Inp\n")
        rep = (f"=====NOTE: Old Inp | repairs: Old Inp.md | bullets: b1=====\n{inp.rstrip()}\n"
               f"=====BULLETS=====\nOld Inp.md#b1 | kept in Old Inp\n"
               f"Old Inp.md#b2 | dropped: not in this transcript\n")
        _b, _u, res, _ev = self.run_batch(state, rep)
        sc.next_batch(self.staging, self.zk, state)
        self.assertEqual(self.vpath("Old Inp").read_bytes(), before)
        self.assertIn("title-equals-old", {x["type"] for x in res["problems"]})
        e = json.loads(state.read_text())["notes"][rel]
        no = json.loads((self.staging / "needs_operator.json").read_text())
        if os.environ.get("PROBE_DEBUG"):
            print(e["status"], e.get("open_bullets"), no)
        self.assertIn("Old Inp.md#b1", e.get("open_bullets") or [])
        self.assertTrue(any(x.get("note") == rel for x in no), no)

    def test_ok_in_place_and_split_gives_a_checked_stub(self) -> None:
        rel = self.old("Old Inp", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        inp = self.ta.replace(f"# {self.a}\n", "# Old Inp\n")
        rep = (f"=====NOTE: Old Inp | repairs: Old Inp.md | bullets: b1=====\n{inp.rstrip()}\n"
               f"=====NOTE: {self.b} | repairs: Old Inp.md | bullets: b2=====\n{self.tb.rstrip()}\n"
               f"=====BULLETS=====\nOld Inp.md#b1 | kept in Old Inp\nOld Inp.md#b2 | kept in {self.b}\n")
        self.run_batch(state, rep)
        now = self.vpath("Old Inp").read_text()
        self.assertIn("old_bullets: 2", now)
        self.assertIn("legacy bullet 1", now)
        lines = now.splitlines()
        i = lines.index("- legacy bullet 1")
        self.assertTrue(lines[i + 1].strip().startswith("- open:"), lines[i + 1])
        self.assertEqual(_errors(self, "Old Inp"), [])


class X2KeepNeedsRepair(_Drv):
    """Adapted from test_probe9_D_attacks.NeedsRepairExit0 (same worker answers)."""

    def test_ok_keep_leaves_note_unchanged_lists_it_and_exits_7(self) -> None:
        rel = self._old("Old Keep")
        before = self.vpath("Old Keep").read_bytes()
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
        if os.environ.get("PROBE_DEBUG"):
            print(codes, e, self.ops())
        self.assertEqual(self.vpath("Old Keep").read_bytes(), before)
        self.assertEqual(e["status"], "operator")
        self.assertTrue([o for o in self.ops() if o.get("note") == rel])
        self.assertEqual(codes[-1], 7, codes)


# --------------------------------------------------------------------------- #
# X3: legacy stubs
# --------------------------------------------------------------------------- #


class X3LegacyStub(_Repair):
    def _legacy(self, title: str = "Old Two") -> str:
        rel = self.old(title, 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.run_batch(state, _rep(title, self.a, "b1", self.ta,
                                   f"{title}.md#b1 | kept in {self.a}\n{title}.md#b2 | dropped: not in this transcript\n"))
        e = json.loads(state.read_text())["notes"][rel]
        v = ri._repair_view(self.staging, self.zk, e)
        legacy = ri.repair_stub(title, v["published"], [f"bullet: {b}" for b in v["open"]])
        self.vpath(title).write_text(legacy)
        (self.staging / ri.PIPELINE_WRITES).unlink(missing_ok=True)
        return rel

    def test_ok_legacy_stub_upgraded_and_clean(self) -> None:
        rel = self._legacy()
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = self.vpath("Old Two").read_text()
        self.assertIn("old_bullets: 2", stub)
        self.assertIn("legacy bullet 2", stub)
        self.assertEqual(_errors(self, "Old Two"), [])

    def test_ok_legacy_stub_without_archive_is_an_error_and_a_preflight_finding(self) -> None:
        rel = self._legacy()
        shutil.rmtree(self.staging / "retired")
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        self.assertNotIn("old_bullets", self.vpath("Old Two").read_text())
        self.assertTrue(any("old_bullets" in e for e in _errors(self, "Old Two")), _errors(self, "Old Two"))
        self.assertTrue(any("legacy stub" in f for f in sc.repair_preflight(self.staging, self.zk)))


# --------------------------------------------------------------------------- #
# X6: exact-path archive lookup, planned sha first
# --------------------------------------------------------------------------- #


class X6ArchiveLookup(_Repair):
    def _arch(self, run: str, unit: str, rel: str, text: str) -> Path:
        p = self.staging / "retired" / run / unit / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_ok_planned_sha_wins_over_newer_archive(self) -> None:
        rel = f"{PN}/Why Planche Hurts?.md"
        old = "---\ntype: x\n---\n# Why Planche Hurts?\n\nPLANNED TEXT\n"
        self._arch("20261001T000000Z-1", "u1", rel, old)
        self._arch("20261003T000000Z-1", "u2", rel, old.replace("PLANNED", "LATER USER"))
        self._arch("20261003T000000Z-1", "u2", f"{PN}/Why Planche Hurts!.md", "OTHER")
        (self.staging / "repair_state.json").write_text(json.dumps(
            {"notes": {rel: {"sha": ri._sha256_bytes(old.encode())}}, "batches": {}}))
        self.assertIn("PLANNED TEXT", ri.archived_old_text(self.staging, rel))

    def test_defect_without_planned_sha_a_respeak_or_heal_archive_counts_as_newest(self) -> None:
        """_archive_candidates (run_integrity.py ~2146) orders by the run FOLDER NAME. A
        `respeak-*`/`heal-*`/`maintenance` folder name sorts after every run id
        (`2026...`), so an older respeak archive beats a newer run archive in the fallback
        (no planned sha, e.g. a note outside repair_state.json)."""
        rel = f"{PN}/Old Order.md"
        self._arch("respeak-20250101T000000Z", "respeak-20250101T000000Z", rel, "---\ntype: x\n---\n# Old Order\n\nOLDER\n")
        self._arch("20261003T000000Z-1", "u2", rel, "---\ntype: x\n---\n# Old Order\n\nNEWER\n")
        self.assertIn("OLDER", ri.archived_old_text(self.staging, rel))


# --------------------------------------------------------------------------- #
# X14: the attempt limit holds, but the driver's exit code hides failed videos
# --------------------------------------------------------------------------- #


class X14AttemptLimit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probe10A-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stg, self.zk, self.env, self.replies = _bl.LoopProbes._setup(self, self.tmp)

    def test_defect_all_videos_failed_and_loop_sh_exits_0(self) -> None:
        """Adapted from test_probe9_D_exitcodes.LoopSh.test_defect_crash_attempt_limit_never_enforced.
        The limit now holds (both videos `failed` after 3 attempts). But loop.sh prints
        'ALL EXTRACTED ITEMS PROCESSED' and exits 0: operator_work() (run_integrity.py
        ~2996) does not list failed queue items, so exit 0 ("nothing left, no operator work")
        hides that no video was synthesized."""
        r = _run("loop.sh", _crash_env(dict(self.env, MAX_ITERS="12", MAX_STALL="2"), self.tmp))
        q = json.loads((self.stg / "queue.json").read_text())["items"]
        self.assertEqual([i["stage"] for i in q], ["failed", "failed"], r.stdout[-500:])
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-800:])


# --------------------------------------------------------------------------- #
# W40 (N6) and W27 (N7): the v8 probes now stop at the W26 name check; adapted with
# force_name=True (the operator's override), otherwise unchanged. DEFECT convention.
# --------------------------------------------------------------------------- #

import tests.probes_v8.test_probe8_C_speakers as C8  # noqa: E402
from tests.unit.test_synth_single import _Single  # noqa: E402


class W40W27RespeakAdapted(_Single):
    _put = C8.Respeak._put

    def test_defect_N6_channel_with_the_word_video_breaks_the_new_title(self) -> None:
        old = self._put(channel="Brink Video Lab")
        self.assertTrue(old.startswith("Unresolved Speaker In Brink Video Lab Video "))
        out = sc.respeak(self.staging, self.zk, C8.EX2, "Tomas Brink", apply=False, force_name=True)
        self.assertTrue(out[0]["to"].startswith("Tomas Brink Lab Video Predicts"), out)

    def test_defect_N7_respeak_leaves_case_path_and_md_links_dead(self) -> None:
        old = self._put()
        linker = "Linker Note"
        (self.zk / PN / f"{linker}.md").write_text(
            f"---\ntype: permanent note\n---\n\n# {linker}\n\nx [[{old.lower()}]] y [[{PN}/{old}]] z [[{old}.md]]\n")
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{linker}.md", sources=["vid-OTHER"])
        before = [e for e in validate.check(self.zk, None, str(self.staging))[0] if "dead link" in e and linker in e]
        self.assertEqual(before, [])
        out = sc.respeak(self.staging, self.zk, C8.EX2, "Tomas Brink", apply=True, force_name=True)
        self.assertIsNone(out[0].get("problem"), out)
        after = [e for e in validate.check(self.zk, None, str(self.staging))[0] if "dead link" in e and linker in e]
        self.assertEqual(len(after), 3, after)


class X5FolderAtLimitWithReply(_Repair):
    def test_ok_folder_at_the_limit_with_an_answered_reply_is_taken(self) -> None:
        key = "ab" * 16
        pdir = self.staging / "pending-review" / "20261004T000000Z-1--u"
        (pdir / "out" / PN).mkdir(parents=True)
        (pdir / "out" / PN / f"{self.a}.md").write_text(self.ta)
        (pdir / "review-x").mkdir()
        (pdir / "review-x" / "agent_result.json").write_text(json.dumps(
            {"end": "waiting-for-reply", "detail": f"waiting-for-reply: request {key}"}))
        (pdir / "meta.json").write_text(json.dumps({"pending_cycles": ri.MAX_PENDING_CYCLES, "notes": [f"{PN}/{self.a}.md"],
                                                    "reasons": {f"{PN}/{self.a}.md": "waiting-for-reply"}}))
        self.assertEqual(sc.count_waits(self.staging, "synth"), (0, 1))  # no reply yet: stuck
        x = self.staging / "exchange" / "replies"
        x.mkdir(parents=True)
        (x / f"{key}.txt").write_text("{}")
        self.assertTrue(ri.folder_holds_reply(self.staging, pdir))
        self.assertEqual(sc.count_waits(self.staging, "synth"), (1, 0))
        self.assertEqual(len(ri.pending_units(self.staging, self.run_dir, os.getpid())), 1)
