from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
ZR = ROOT / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

from tests.unit.test_run_integrity import _Env  # noqa: E402
import run_integrity as ri  # noqa: E402
import synth_call as sc  # noqa: E402
import review_call as rc  # noqa: E402


TITLE = "Pending Review Fixture"
REL = f"01 Permanent Notes/{TITLE}.md"
MESSAGES = [{"role": "system", "content": "Review the supplied note."},
            {"role": "user", "content": "Check the exact claim and source."}]
SETTINGS = {"model": "review-model", "max_tokens": 1000, "reasoning": None}
EXTRA = {"writer_keys": ["writer-key"], "notes": [REL], "refuse_workers": []}


class ConsumedWaitRecovery(_Env):
    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_LLM_BACKEND"] = "files"
        self.xdir = self.staging / "exchange"
        for part in ("requests", "replies", "consumed", "reissues"):
            (self.xdir / part).mkdir(parents=True, exist_ok=True)
        self.base_key = sc.request_key(MESSAGES, SETTINGS)
        self._seed_base_request()

    def _seed_base_request(self) -> None:
        system, user = sc._files_prompt(MESSAGES)
        (self.xdir / "requests" / f"{self.base_key}.json").write_text(json.dumps({
            "key": self.base_key, "kind": "review", "name": "review-old", "unit": "old-unit",
            "model_hint": SETTINGS["model"], "max_tokens": SETTINGS["max_tokens"],
            "system": system, "user": user, "must_not_share_worker_with": EXTRA["writer_keys"],
            "notes": EXTRA["notes"],
        }))

    def _consume_base(self) -> None:
        ri._write_json(self.xdir / "consumed" / f"{self.base_key}.json",
                       {"key": self.base_key, "unit": "different-unit", "name": "review-old", "at": "now"})

    def _pending_folder(self, key: str | None = None, cycles: int = 6) -> Path:
        key = key or self.base_key
        pdir = self.staging / "pending-review" / f"test-run--pending-unit-{cycles}"
        out = pdir / "out" / REL
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("review note fixture\n")
        ri._write_json(pdir / "meta.json", {
            "kind": "repair", "ids": [], "notes": [REL], "stored": "now", "pending_cycles": cycles,
            "wait_keys": [key], "reasons": {REL: "waiting-for-reply"}, "options": {},
        })
        return pdir

    def _select_pending(self) -> Path:
        selected = ri.pending_units(self.staging, self.run_dir, os.getpid())
        self.assertEqual(len(selected), 1, selected)
        return Path(selected[0])

    def _request_or_wait(self, unit: Path, extra: dict | None = None) -> str:
        with self.assertRaises(sc.PendingReply) as ctx:
            sc.files_call(unit, "review-pending-fixture", MESSAGES, SETTINGS, extra or EXTRA)
        return ctx.exception.key

    def _post_reply(self, key: str, worker: str = "independent-reviewer") -> None:
        (self.xdir / "replies" / f"{key}.txt").write_text('{"verdict":"supported"}\n')
        ri._write_json(self.xdir / "replies" / f"{key}.meta.json",
                       {"key": key, "worker": worker, "model": SETTINGS["model"]})

    def test_over_limit_consumed_no_body_reissues_then_propagates_same_key(self) -> None:
        self._consume_base()
        self._pending_folder()
        unit = self._select_pending()
        derived = self._request_or_wait(unit)
        self.assertNotEqual(derived, self.base_key)
        self.assertTrue((self.xdir / "requests" / f"{derived}.json").is_file())
        (unit / "agent_result.json").write_text(json.dumps(
            {"rc": 0, "end": "waiting-for-reply", "detail": f"waiting-for-reply: request {derived}"}))
        self.assertEqual(ri._unit_wait_keys(unit), {derived})

        # Simulate a crash before the pending folder records the derived key.
        self._pending_folder(self.base_key)
        replay_unit = self._select_pending()
        self.assertEqual(self._request_or_wait(replay_unit), derived)
        self.assertEqual(len(list((self.xdir / "reissues").glob("*.json"))), 1)

        # Once the derived key is recorded, an unanswered reply stays capped.
        (replay_unit / "agent_result.json").write_text(json.dumps(
            {"rc": 0, "end": "waiting-for-reply", "detail": f"request {derived}"}))
        derived_folder = self._pending_folder(derived)
        self.assertFalse(ri.folder_holds_reply(self.staging, derived_folder))
        self.assertEqual(ri.pending_units(self.staging, self.run_dir, os.getpid()), [])

    def test_crash_alias_with_reply_resumes_the_same_derived_key(self) -> None:
        self._consume_base()
        self._pending_folder()
        first_unit = self._select_pending()
        derived = self._request_or_wait(first_unit)
        self._pending_folder(self.base_key)
        self._post_reply(derived)
        self.assertTrue(ri.folder_holds_reply(self.staging, self.staging / "pending-review" / "test-run--pending-unit-6"))
        resumed = self._select_pending()
        text, finish, meta = sc.files_call(resumed, "review-pending-fixture", MESSAGES, SETTINGS, EXTRA)
        self.assertIn("supported", text)
        self.assertEqual(finish, "stop")
        self.assertEqual(meta["key"], derived)

    def test_existing_consumed_reply_body_can_be_revalidated_and_reused(self) -> None:
        self._consume_base()
        self._post_reply(self.base_key)
        original_consumption = (self.xdir / "consumed" / f"{self.base_key}.json").read_bytes()
        pdir = self._pending_folder()
        self.assertTrue(ri.folder_holds_reply(self.staging, pdir))
        unit = self._select_pending()
        text, _, meta = sc.files_call(unit, "review-pending-fixture", MESSAGES, SETTINGS, EXTRA)
        self.assertIn("supported", text)
        self.assertEqual(meta["key"], self.base_key)
        self.assertEqual((self.xdir / "consumed" / f"{self.base_key}.json").read_bytes(), original_consumption)
        events = list((self.xdir / "consumed-reuses" / self.base_key).glob("*.json"))
        self.assertEqual(len(events), 1)
        event = json.loads(events[0].read_text())
        self.assertEqual((event["key"], event["unit"], event["name"], event["worker"]),
                         (self.base_key, unit.name, "review-pending-fixture", "independent-reviewer"))
        self.assertEqual(len(event["reply_sha256"]), 64)
        self.assertFalse((self.xdir / "reissues" / f"{self.base_key}.json").exists())

    def test_ordinary_pending_reply_still_resumes(self) -> None:
        self._post_reply(self.base_key)
        pdir = self._pending_folder()
        self.assertTrue(ri.folder_holds_reply(self.staging, pdir))
        unit = self._select_pending()
        _, _, meta = sc.files_call(unit, "review-pending-fixture", MESSAGES, SETTINGS, EXTRA)
        self.assertEqual(meta["key"], self.base_key)

    def test_wrong_writer_reply_is_refused_after_reissue(self) -> None:
        self._consume_base()
        self._pending_folder()
        unit = self._select_pending()
        derived = self._request_or_wait(unit, {"writer_keys": [], "notes": [REL], "refuse_workers": []})
        derived_request = json.loads((self.xdir / "requests" / f"{derived}.json").read_text())
        self.assertIn("writer-key", derived_request["must_not_share_worker_with"])
        writer_req = self.xdir / "requests" / "writer-key.json"
        writer_req.write_text(json.dumps({"key": "writer-key", "kind": "repair"}))
        ri._write_json(self.xdir / "replies" / "writer-key.meta.json",
                       {"key": "writer-key", "worker": "writer-worker", "model": "m"})
        self._post_reply(derived, worker="writer-worker")
        with self.assertRaises(sc.PendingReply) as ctx:
            sc.files_call(unit, "review-pending-fixture", MESSAGES, SETTINGS, EXTRA)
        self.assertEqual(ctx.exception.key, derived)
        self.assertTrue(list((self.xdir / "replies").glob(f"{derived}.refused-*.json")))
        self.assertFalse((self.xdir / "consumed" / f"{derived}.json").exists())

    def test_dead_base_missing_request_and_corrupt_alias_are_not_reopened(self) -> None:
        self._consume_base()
        for n in range(1, ri.MAX_REFUSALS + 1):
            (self.xdir / "replies" / f"{self.base_key}.refused-{n}.json").write_text("{}")
        (self.xdir / "requests" / f"{self.base_key}.obsolete").write_text("dead\n")
        self._pending_folder()
        self.assertEqual(ri.pending_units(self.staging, self.run_dir, os.getpid()), [])

        (self.xdir / "requests" / f"{self.base_key}.obsolete").unlink()
        for n in range(1, ri.MAX_REFUSALS + 1):
            (self.xdir / "replies" / f"{self.base_key}.refused-{n}.json").unlink()
        (self.xdir / "requests" / f"{self.base_key}.json").unlink()
        with self.assertRaises(ri.HarnessError):
            sc.files_call(self.unit, "review-pending-fixture", MESSAGES, SETTINGS, EXTRA)

        self._seed_base_request()
        sidecar = self.xdir / "reissues" / f"{self.base_key}.json"
        sidecar.write_text("not-json")
        with self.assertRaises(ri.HarnessError):
            sc.files_call(self.unit, "review-pending-fixture", MESSAGES, SETTINGS, EXTRA)
        self.assertFalse(list(self.xdir.joinpath("requests").glob("*.reissue.json")))

    def test_dead_derived_request_is_never_reopened(self) -> None:
        self._consume_base()
        self._pending_folder()
        unit = self._select_pending()
        derived = self._request_or_wait(unit)
        for n in range(1, ri.MAX_REFUSALS + 1):
            (self.xdir / "replies" / f"{derived}.refused-{n}.json").write_text("{}")
        (self.xdir / "requests" / f"{derived}.obsolete").write_text("dead\n")
        with self.assertRaisesRegex(ri.HarnessError, "dead/obsolete"):
            sc.files_call(unit, "review-pending-fixture", MESSAGES, SETTINGS, EXTRA)
        self._pending_folder(self.base_key)
        self.assertEqual(ri.pending_units(self.staging, self.run_dir, os.getpid()), [])

    def test_changed_payload_in_pinned_reissue_is_rejected(self) -> None:
        self._consume_base()
        self._pending_folder()
        unit = self._select_pending()
        derived = self._request_or_wait(unit)
        sidecar = self.xdir / "reissues" / f"{self.base_key}.json"
        record = json.loads(sidecar.read_text())
        record["payload_sha256"] = "0" * 64
        sidecar.write_text(json.dumps(record))
        with self.assertRaises(ri.HarnessError):
            sc.files_call(unit, "review-pending-fixture", MESSAGES, SETTINGS, EXTRA)
        self.assertTrue((self.xdir / "requests" / f"{derived}.json").is_file())

    def test_reissue_notes_fall_back_to_pending_unit_output(self) -> None:
        self._consume_base()
        self._pending_folder()
        unit = self._select_pending()
        derived = self._request_or_wait(unit, {"writer_keys": EXTRA["writer_keys"], "refuse_workers": []})
        req = json.loads((self.xdir / "requests" / f"{derived}.json").read_text())
        self.assertEqual(req["notes"], [REL])
        self._pending_folder(self.base_key)
        replay = self._select_pending()
        self.assertEqual(self._request_or_wait(replay, {"writer_keys": EXTRA["writer_keys"], "refuse_workers": []}), derived)

    def test_consumed_derived_key_without_body_stops_without_silent_wait(self) -> None:
        self._consume_base()
        self._pending_folder()
        unit = self._select_pending()
        derived = self._request_or_wait(unit)
        self._consume_base_key(derived)
        with self.assertRaisesRegex(ri.HarnessError, "bounded stop"):
            sc.files_call(unit, "review-pending-fixture", MESSAGES, SETTINGS, EXTRA)
        recursive_settings = dict(SETTINGS, review_attempt={"consumed_reissue_of": self.base_key, "attempt": 1})
        with self.assertRaisesRegex(ri.HarnessError, "recursive consumed-review reissue"):
            sc.files_call(unit, "review-pending-fixture", MESSAGES, recursive_settings, EXTRA)

    def test_capped_pending_folder_does_not_recover_consumed_derived_request(self) -> None:
        self._consume_base()
        self._pending_folder()
        unit = self._select_pending()
        derived = self._request_or_wait(unit)
        req = json.loads((self.xdir / "requests" / f"{derived}.json").read_text())
        self.assertEqual(req["reissue_of"], self.base_key)
        self._consume_base_key(derived)
        folder = self._pending_folder(derived, cycles=ri.MAX_PENDING_CYCLES)
        self.assertFalse((self.xdir / "reissues" / f"{derived}.json").exists())
        self.assertFalse(ri._folder_has_recoverable_consumed_wait(self.staging, folder))
        self.assertEqual(ri.pending_units(self.staging, self.run_dir, os.getpid()), [])

    def _consume_base_key(self, key: str) -> None:
        ri._write_json(self.xdir / "consumed" / f"{key}.json",
                       {"key": key, "unit": "different-unit", "name": "review-old", "at": "now"})

    def _review_settings(self) -> dict:
        return {"model": SETTINGS["model"], "max_tokens": SETTINGS["max_tokens"], "reasoning": None,
                "retries": 0, "base_url": "https://example.invalid"}

    def _seed_review_note(self) -> Path:
        rel = f"01 Permanent Notes/{TITLE}.md"
        note = self.unit / "out" / rel
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("---\ntype: permanent note\nsources:\n  - id: vid-5zALUKd7h3g\n    speaker: Speaker\n"
                        "scope:\n  skill: not stated\n  level: not stated\n  equipment: not stated\n"
                        "  basis: general rule\n  modality: option\n  quantities: []\nverification: unverified\n---\n\n"
                        "# Pending Review Fixture\n\nThe speaker gives a general training claim.\n\n"
                        "## Details\n\n- The speaker describes the training claim.\n\n"
                        "## Evidence\n\n- vid-5zALUKd7h3g @ 00:01 (Speaker): \"training claim\"\n")
        ri._write_json(self.unit / "marked.json", {"notes": [TITLE]})
        return note

    def _call_review(self, note: Path, unit: Path | None = None) -> dict:
        unit = unit or self.unit
        with patch.object(rc, "settings", return_value=self._review_settings()), \
             patch.object(rc, "review_reasoning", return_value=None), \
             patch.object(ri, "passages", return_value="[EVIDENCE 1 @ 00:01]\n>>> training claim <<<\n"):
            return rc.review_note(self.staging, self.zk, unit, REL, note, [])

    def _resume_pending_unit(self) -> tuple[Path, Path]:
        units = ri.pending_units(self.staging, self.run_dir, os.getpid())
        self.assertEqual(len(units), 1, units)
        unit = Path(units[0])
        return unit, unit / "out" / REL

    def _seed_consumed_base_through_review_caller(self, note: Path) -> tuple[str, str]:
        first = self._call_review(note)
        self.assertEqual(first["end"], "waiting-for-reply")
        first_result = json.loads((self.unit / "review-0" / "agent_result.json").read_text())
        base = __import__("re").search(r"request ([0-9a-f]{32})", first_result["detail"]).group(1)
        self.assertTrue((self.xdir / "requests" / f"{base}.json").is_file())
        self._consume_base_key(base)
        second = self._call_review(note)
        self.assertEqual(second["end"], "waiting-for-reply")
        second_dir = sorted(self.unit.glob("review-*"), key=lambda p: int(p.name.split("-")[-1]))[-1]
        second_result = json.loads((second_dir / "agent_result.json").read_text())
        derived = __import__("re").search(r"request ([0-9a-f]{32})", second_result["detail"]).group(1)
        self.assertNotEqual(base, derived)
        self.assertTrue((self.xdir / "requests" / f"{derived}.json").is_file())
        return base, derived

    def _finalize_waiting_review(self, note: Path, key: str) -> Path:
        ri._write_json(self.unit / "agent_result.json", {
            "rc": 1, "end": "waiting-for-reply", "detail": f"waiting-for-reply: request {key}"})
        data = note.read_bytes()
        fm, _, _ = __import__("verify_claims").split_frontmatter(data.decode())
        result = {"rel": REL, "data": data, "fm": fm, "stub": False, "existed_before": False,
                  "status": "ok", "report": {"details": []}, "failures": [], "ok": True, "warnings": []}
        with patch.object(ri._Checker, "check_all", return_value=[result]), \
             patch.object(ri._Checker, "needs_review", return_value=True):
            ri.finalize(self.staging, self.run_dir, self.unit, ["vid-5zALUKd7h3g"], 1, self.zk)
        folders = list((self.staging / "pending-review").glob("*--*"))
        self.assertEqual(len(folders), 1)
        pdir = folders[0]
        meta = json.loads((pdir / "meta.json").read_text())
        self.assertIn(key, meta["wait_keys"])
        self.assertIn(key, ri.folder_waits_keys(pdir))
        return pdir

    def _finalize_unusable_review(self, note: Path) -> Path:
        ri._write_json(self.unit / "agent_result.json", {"rc": 0, "end": "finished", "detail": ""})
        data = note.read_bytes()
        fm, _, _ = __import__("verify_claims").split_frontmatter(data.decode())
        result = {"rel": REL, "data": data, "fm": fm, "stub": False, "existed_before": False,
                  "status": "ok", "report": {"details": []}, "failures": [], "ok": True, "warnings": []}
        with patch.object(ri._Checker, "check_all", return_value=[result]), \
             patch.object(ri._Checker, "needs_review", return_value=True):
            ri.finalize(self.staging, self.run_dir, self.unit, ["vid-5zALUKd7h3g"], 0, self.zk)
        folders = list((self.staging / "pending-review").glob("*--*"))
        self.assertEqual(len(folders), 1)
        return folders[0]

    def test_real_review_malformed_derived_reply_refuses_and_propagates_key(self) -> None:
        note = self._seed_review_note()
        base, derived = self._seed_consumed_base_through_review_caller(note)
        original_base_marker = (self.xdir / "consumed" / f"{base}.json").read_bytes()
        self._post_reply(derived)
        (self.xdir / "replies" / f"{derived}.txt").write_text("this is malformed JSON")
        result = self._call_review(note)
        self.assertEqual(result["end"], "waiting-for-reply", result)
        latest = sorted(self.unit.glob("review-*"), key=lambda p: int(p.name.split("-")[-1]))[-1]
        agent_result = json.loads((latest / "agent_result.json").read_text())
        self.assertIn(f"request {derived}", agent_result["detail"])
        self.assertEqual((self.xdir / "consumed" / f"{base}.json").read_bytes(), original_base_marker)
        self.assertTrue(list((self.xdir / "replies").glob(f"{derived}.refused-1.json")))
        self.assertFalse((self.xdir / "consumed" / f"{derived}.json").exists())
        self.assertFalse((self.xdir / "requests" / f"{base}.obsolete").exists())
        pdir = self._finalize_waiting_review(note, derived)
        self.assertIn(base, ri.folder_waits_keys(pdir))
        self.assertIn(derived, ri.folder_waits_keys(pdir))

        # The refused malformed body reopens the same derived request under the existing
        # refusal cap, so another worker can correct it without a new alias or dead wait.
        retry_unit, retry_note = self._resume_pending_unit()
        retry = self._call_review(retry_note, retry_unit)
        retry_dir = sorted(retry_unit.glob("review-*"), key=lambda p: int(p.name.split("-")[-1]))[-1]
        retry_result = json.loads((retry_dir / "agent_result.json").read_text())
        self.assertEqual(retry["end"], "waiting-for-reply")
        self.assertIn(f"request {derived}", retry_result["detail"])
        self.assertEqual(len(list((self.xdir / "reissues").glob("*.json"))), 1)
        self.assertEqual(len(list((self.xdir / "replies").glob(f"{derived}.refused-*.json"))), 1)
        self.assertFalse((self.xdir / "consumed" / f"{derived}.json").exists())

    def test_real_review_unusable_derived_reply_obsoletes_derived_and_retries_fresh(self) -> None:
        note = self._seed_review_note()
        base, derived = self._seed_consumed_base_through_review_caller(note)
        original_base_marker = (self.xdir / "consumed" / f"{base}.json").read_bytes()
        skeleton = json.loads((self.unit / "review-0" / "skeleton.json").read_text())
        unusable = {"scope_complete": True, "items": [
            {"item": item["item"], "verdict": "transcript-missing"} for item in skeleton["items"]]}
        self.assertIsNotNone(ri.parse_verdict_usable(unusable), (skeleton, unusable))
        self.assertTrue(ri.verdict_obj_unusable(unusable, json.loads((self.unit / "review-0" / "request.json").read_text())["expected_items"]))
        self._post_reply(derived)
        (self.xdir / "replies" / f"{derived}.txt").write_text(json.dumps(unusable))
        result = self._call_review(note)
        self.assertEqual(result["end"], "finished", result)
        self.assertTrue(json.loads((self.unit / "review-2" / "review.json").read_text()))
        self.assertEqual((self.xdir / "consumed" / f"{base}.json").read_bytes(), original_base_marker)
        self.assertTrue(list((self.xdir / "replies").glob(f"{derived}.refused-1.json")))
        self.assertTrue((self.xdir / "requests" / f"{derived}.obsolete").is_file())
        pending = self._finalize_unusable_review(note)
        held = set(json.loads((pending / "meta.json").read_text())["wait_keys"])
        self.assertIn(base, held)
        self.assertIn(derived, held)

        # The unusable-review counter changes the review request settings; the next call
        # opens a fresh key while the dead derived key remains closed.
        retry_unit, retry_note = self._resume_pending_unit()
        retry = self._call_review(retry_note, retry_unit)
        retry_dir = sorted(retry_unit.glob("review-*"), key=lambda p: int(p.name.split("-")[-1]))[-1]
        retry_result = json.loads((retry_dir / "agent_result.json").read_text())
        import re
        fresh = re.search(r"request ([0-9a-f]{32})", retry_result["detail"]).group(1)
        self.assertEqual(retry["end"], "waiting-for-reply")
        self.assertNotEqual(fresh, derived)
        self.assertNotEqual(fresh, base)
        self.assertTrue((self.xdir / "requests" / f"{fresh}.json").is_file())

    def test_prior_refusal_does_not_reopen_accepted_then_pruned_derived_reply(self) -> None:
        note = self._seed_review_note()
        base, derived = self._seed_consumed_base_through_review_caller(note)
        self._post_reply(derived)
        (self.xdir / "replies" / f"{derived}.txt").write_text("malformed")
        malformed = self._call_review(note)
        self.assertEqual(malformed["end"], "waiting-for-reply")
        self.assertFalse((self.xdir / "consumed" / f"{derived}.json").exists())
        self.assertEqual(len(list((self.xdir / "replies").glob(f"{derived}.refused-*.json"))), 1)

        skeleton = json.loads((self.unit / "review-0" / "skeleton.json").read_text())
        corrected = {"scope_complete": True, "items": [
            {"item": item["item"], "verdict": "no-tag"} for item in skeleton["items"]]}
        self._post_reply(derived)
        (self.xdir / "replies" / f"{derived}.txt").write_text(json.dumps(corrected))
        accepted = self._call_review(note)
        self.assertEqual(accepted["end"], "finished")
        derived_marker = (self.xdir / "consumed" / f"{derived}.json").read_bytes()
        (self.xdir / "replies" / f"{derived}.txt").unlink()
        self.assertEqual(len(list((self.xdir / "replies").glob(f"{derived}.refused-*.json"))), 1)

        with self.assertRaisesRegex(ri.HarnessError, "bounded stop"):
            self._call_review(note)
        self.assertEqual((self.xdir / "consumed" / f"{derived}.json").read_bytes(), derived_marker)
        self.assertTrue((self.xdir / "consumed" / f"{base}.json").is_file())


if __name__ == "__main__":
    unittest.main()
