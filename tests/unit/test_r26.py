"""Round 26 (coordinator decisions on the round-25 open points), correct-behaviour
convention: every test asserts the CORRECT behaviour (PASS = fixed)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.unit.test_run_integrity import PN, SASA, ZR, _Env, good_text, ri

import synth_call as sc  # noqa: E402

KEY = "d" * 32


def _dead(staging: Path, key: str = KEY) -> None:
    x = staging / "exchange"
    (x / "requests").mkdir(parents=True, exist_ok=True)
    (x / "replies").mkdir(parents=True, exist_ok=True)
    (x / "requests" / f"{key}.json").write_text(json.dumps({"key": key, "kind": "review", "notes": [f"{PN}/{SASA}.md"]}))
    for i in range(3):
        (x / "replies" / f"{key}.txt").write_text("bad")
        sc.refuse_reply(x, key, "the same worker wrote the note", worker=f"w{i}")


class DeadPendingFolder(_Env):
    """(b) a pending folder that waits only for a request refused for good."""

    def _folder(self) -> Path:
        pdir = self.staging / "pending-review" / "20261005T000000Z-1--u1"
        (pdir / "out" / PN).mkdir(parents=True)
        (pdir / "out" / PN / f"{SASA}.md").write_text(good_text())
        (pdir / "meta.json").write_text(json.dumps({
            "ids": [], "kind": "repair", "notes": [f"{PN}/{SASA}.md"], "pending_cycles": 1,
            "reasons": {f"{PN}/{SASA}.md": "waiting-for-reply"}, "wait_keys": [KEY]}))
        (pdir / "repair_map.json").write_text(json.dumps({f"{PN}/Old B.md": {"targets": [SASA], "final": True}}))
        _dead(self.staging)
        return pdir

    def test_is_operator_work_and_not_waiting(self) -> None:
        self._folder()
        work = ri.operator_work(self.staging, self.zk)
        self.assertIn(f"pending target {SASA} waits on a dead review request {KEY}", work)
        self.assertNotIn(SASA, ri.waiting_titles(self.staging))
        self.assertEqual(sc.count_waits(self.staging, "repair"), (0, 0))

    def test_operator_done_on_the_old_note_quarantines_at_the_next_pass(self) -> None:
        pdir = self._folder()
        res = ri.operator_done(self.staging, "Old B", "dead review")
        self.assertEqual(res["pending_quarantined_next_pass"], [pdir.name])
        self.assertTrue((pdir / ri.OPERATOR_DEAD).is_file())
        self.assertEqual([o for o in json.loads((self.staging / "needs_operator.json").read_text())
                          if o.get("request") == KEY], [])  # the entry of the refused request is cleared
        self.assertTrue([w for w in ri.operator_work(self.staging, self.zk) if "operator-done" in w])  # never exit 0 yet
        units = ri.pending_units(self.staging, self.run_dir, os.getpid())
        self.assertEqual(len(units), 1)
        ev = ri.finalize(self.staging, self.run_dir, Path(units[0]), [], 0, self.zk)
        n = next(x for x in ev["notes"] if x["note"].endswith(f"{SASA}.md"))
        self.assertEqual(n["action"], "quarantined", n)
        self.assertIn("review-invalid", n["failure_types"])
        self.assertFalse(self.vpath(SASA).exists())
        self.assertEqual(list((self.staging / "pending-review").glob("*--*")), [])
        self.assertFalse([w for w in ri.operator_work(self.staging, self.zk) if "dead review" in w or "pending" in w])

    def test_other_old_note_of_the_same_folder_is_closed_normally(self) -> None:
        pdir = self._folder()
        rm = json.loads((pdir / "repair_map.json").read_text())
        rm[f"{PN}/Old C.md"] = {"targets": ["Some Published Target"], "final": True}
        (pdir / "repair_map.json").write_text(json.dumps(rm))
        (self.staging / "repair_state.json").write_text(json.dumps(
            {"notes": {f"{PN}/Old C.md": {"status": "needs-repair", "bullets": {}}}, "batches": {}}))
        res = ri.operator_done(self.staging, "Old C", "checked by hand")
        self.assertTrue(res["plan_closed"], res)
        self.assertEqual(res["pending_quarantined_next_pass"], [])
        self.assertFalse((pdir / ri.OPERATOR_DEAD).exists())

    def test_operator_done_on_the_key_matches_the_folder(self) -> None:
        pdir = self._folder()
        r = subprocess.run([sys.executable, str(ZR / "run_integrity.py"), "operator-done", "--staging", str(self.staging),
                            "--note", KEY, "--reason", "dead review"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((pdir / ri.OPERATOR_DEAD).is_file())


class DeadQueueItem(_Env):
    """(c) a synthesis queue item that waits only for a request refused for good."""

    def test_filter_operator_work_and_operator_done(self) -> None:
        _dead(self.staging)
        (self.staging / "queue.json").write_text(json.dumps({"version": 1, "items": [
            {"id": "vid-QQQQQQQQQQQ", "kind": "video", "stage": "waiting-for-reply", "wait_keys": [KEY]}]}))
        self.assertEqual(sc.count_waits(self.staging, "synth"), (0, 0))
        self.assertIn(f"waiting: vid-QQQQQQQQQQQ waits on a dead request {KEY}", ri.operator_work(self.staging, self.zk))
        res = ri.operator_done(self.staging, KEY, "answered by hand")
        self.assertEqual(res["queue_failed"], ["vid-QQQQQQQQQQQ"])
        it = json.loads((self.staging / "queue.json").read_text())["items"][0]
        self.assertEqual(it["stage"], "failed")

    def test_live_key_is_still_a_wait(self) -> None:
        (self.staging / "queue.json").write_text(json.dumps({"version": 1, "items": [
            {"id": "vid-QQQQQQQQQQQ", "kind": "video", "stage": "waiting-for-reply", "wait_keys": ["e" * 32]}]}))
        self.assertEqual(sc.count_waits(self.staging, "synth"), (1, 0))

    def test_waiting_queue_item_records_its_key(self) -> None:
        (self.staging / "queue.json").write_text(json.dumps({"version": 1, "items": [
            {"id": "vid-QQQQQQQQQQQ", "kind": "video", "stage": "claimed"}]}))
        ri._update_queue(self.staging, ["vid-QQQQQQQQQQQ"], "waiting-for-reply",
                         f"waiting-for-reply: request {KEY} (x)", False, "R1", [], [], [], [], "synth")
        it = json.loads((self.staging / "queue.json").read_text())["items"][0]
        self.assertEqual(it.get("wait_keys"), [KEY])


class SessionTempFolder(_Env):
    """(d) the test session's temp folders live below one session folder (removed at the end)."""

    def test_tempdir_is_the_session_folder(self) -> None:
        self.assertTrue(Path(tempfile.gettempdir()).name.startswith("zr-tests-"), tempfile.gettempdir())
        self.assertTrue(str(self.tmp).startswith(tempfile.gettempdir()))


class ClosedBatch(_Env):
    """A batch whose old notes are all operator-closed is no wait and is never handed out again."""

    def test_closed_batch(self) -> None:
        rel = f"{PN}/Old D.md"
        st = {"notes": {rel: {"status": "operator-closed", "sources": ["v1"], "pos": 0, "bullets": {}}},
              "batches": {"k1": {"vid": "v1", "status": "waiting", "bullets": {rel: []}, "wait_keys": ["f" * 32]}}}
        sp = self.staging / "repair_state.json"
        sp.write_text(json.dumps(st))
        self.assertEqual(sc.count_waits(self.staging, "repair"), (0, 0))
        self.assertIsNone(sc.next_batch(self.staging, self.zk, sp))
        self.assertEqual(json.loads(sp.read_text())["batches"]["k1"]["status"], "operator-closed")
