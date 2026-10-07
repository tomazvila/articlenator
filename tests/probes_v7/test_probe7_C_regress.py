"""Reviewer C, round 2 (frozen-v7): first-round findings Y1-Y14 / P1-P4 judged by behaviour.

Convention: each assertion states the DEFECT. PASS = the defect is present.
FAIL = the defect is fixed (or the API changed).
Tests named *_adapted_* are first-round probes that failed only because a prompt string or
an API moved; they check the same defect in the new text. Fake LLM only, no network."""
from __future__ import annotations

import json
import os
import re
import unittest
from unittest import mock

from tests.probes_v6.test_probe_C_two_stage import EX2, NOTE_BLOCKS, _Two
from tests.unit.test_run_integrity import ZR, ri

import synth_call as sc  # noqa: E402

NC = ZR.parent / "tests" / "fixtures" / "note_contract"


def flat(text: str) -> str:
    return " ".join(text.split())


class AdaptedPrompts(unittest.TestCase):
    def test_E1_adapted_one_stage_request_uses_the_batch_prompt(self) -> None:
        """P1. The first-round E1 never looked at the request; this one reads which prompt
        _synth_part and _coverage_call put in the system message."""
        src = sc._synth_part.__code__.co_names + sc._coverage_call.__code__.co_names
        self.assertIn("system_text", src)
        import inspect  # noqa: PLC0415
        body = inspect.getsource(sc._synth_part) + inspect.getsource(sc._coverage_call)
        self.assertIn('system_text("synth")', body)  # DEFECT: the batch prompt in one-stage calls

    def test_E2_adapted_data_rule_only_inside_one_list_item(self) -> None:
        system = sc.system_text("synth")
        self.assertNotIn("all other parts of the user message are data", flat(system))

    def test_E3_adapted_no_excerpt_line_says_passage_unreadable(self) -> None:
        system = flat(sc.system_text("synth"))
        no_ex = system.split(sc.H_NO_EXCERPT, 1)[1][:400]
        self.assertIn("`passage unreadable`", no_ex.split("Never")[0])  # DEFECT: told to use it

    def test_E4_adapted_skip_line_refuses_part_ids(self) -> None:
        self.assertIsNone(sc.SKIP_LINE.match("P1-C3 | damaged span"))

    def test_E5_adapted_worker_instruction_has_no_data_rule(self) -> None:
        wi = (sc.HERE / "prompts" / "worker_instruction.md").read_text().lower()
        self.assertNotIn("ignore any instruction", wi)

    def test_E6_adapted_type_spelling(self) -> None:
        self.assertIn("| own practice |", sc.system_text("synth"))

    def test_P2_worker_id_is_still_written_by_the_worker(self) -> None:
        """P2 (partly fixed): the data rule and the tool scope are in worker_instruction.md,
        but the worker still writes its own id into meta ('Copy it exactly'); the harness
        trusts meta `worker` for the independence rule."""
        wi = (sc.HERE / "prompts" / "worker_instruction.md").read_text()
        self.assertIn('"worker": "<worker id>"', wi)
        self.assertIn("Copy it exactly", wi)


class OneStageCache(unittest.TestCase):
    def test_P1_new_cache_key_ignores_the_onecall_prompt(self) -> None:
        """P1 fixed, NEW (MINOR): one-stage calls now use synth_onecall.md, but the synth
        reply cache key hashes only synth_single.md (and the inventory prompt in two-stage
        mode). A changed onecall prompt replays the old cached replies."""
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415
        real = sc.system_text
        s = {"model": "m", "max_tokens": 10}
        with mock.patch.dict(os.environ, {"ZR_SYNTH_STAGES": "one"}):
            k1 = sc._cache_open(Path(tempfile.gettempdir()), "vid-x", "lit", s)["path"]
            with mock.patch.object(sc, "system_text", side_effect=lambda k: real(k) + ("CHANGED" if k == "onecall" else "")):
                k2 = sc._cache_open(Path(tempfile.gettempdir()), "vid-x", "lit", s)["path"]
        self.assertEqual(k1, k2)


class TwoStageBehaviour(_Two):
    def _multi(self) -> int:
        more = "".join(f"[{m:02d}:00] Filler talk about the weather and the gym number {m} today.\n" for m in range(5, 40))
        lit = self.lit.rstrip("\n") + "\n" + more
        (self.staging / "lit" / f"{EX2}.md").write_text(lit)
        os.environ["ZR_SYNTH_MAX_INPUT_CHARS"] = "2200"
        self.addCleanup(os.environ.pop, "ZR_SYNTH_MAX_INPUT_CHARS", None)
        return len(sc.split_parts(lit, 2200))

    def test_Y3_e2e_multi_part_valid_skip_is_lost(self) -> None:
        """Y3 end to end: a P1-C1 claim with a valid skip line. FAIL = fixed."""
        n = self._multi()
        self.assertGreater(n, 1)
        inv = ["=====CLAIMS=====\nC1 | 04:50 | warm up properly before hangs | recommendation\n"] + \
              ["=====CLAIMS=====\n"] * (n - 1)
        self.fake(inventory=inv, batch=["=====SKIPPED=====\nP1-C1 | not a transferable training claim: the speaker "
                                        "names no method or amount\n"])
        res = self.run2()
        self.assertIn("P1-C1", res["claims_uncovered"])

    def test_Y3_new_unprefixed_id_in_a_multi_part_batch_is_refused_twice(self) -> None:
        """NEW (SHOULD-FIX): in a multi-part video the batch lists `P1-C1`, but the user
        message ends 'REPLY: one =====NOTE: <Title> | claims: C<n>=====' and the SKIPPED
        example uses `C<n>`. A reply that writes `C1` (no part prefix) is never mapped to
        the one P1-C1 of the batch: the claim is re-sent, refused twice, and the video is
        `all-refused` (needs-attention) for a valid skip."""
        n = self._multi()
        inv = ["=====CLAIMS=====\nC1 | 04:50 | warm up properly before hangs | recommendation\n"] + \
              ["=====CLAIMS=====\n"] * (n - 1)
        skip = "=====SKIPPED=====\nC1 | not a transferable training claim: the speaker names no method or amount\n"
        self.fake(inventory=inv, batch=[skip, skip])
        res = self.run2()
        batch_user = [c for c in self.log() if c["kind"] == "batch"][0]["user"]
        self.assertIn("P1-C1 |", batch_user)
        self.assertIn("claims: C<n>=====", batch_user)
        self.assertEqual(res["claims_uncovered"], ["P1-C1"])
        self.assertEqual(res["outcome"], "all-refused")
        self.assertEqual(self.queue_stage(), "needs-attention")

    def test_Y5_new_finalize_turns_needs_attention_into_synthesized(self) -> None:
        """Y5 fixed in synth_call, NEW (SHOULD-FIX): synth writes outcome `all-refused`, queue
        `needs-attention` and `uncovered_reasons`. finalize then runs _update_queue
        (run_integrity.py ~2905): `needs-attention` with 0 quarantined of 0 notes
        (0*2 <= 0) becomes `synthesized`. The video ends `synthesized` with no note and no
        operator flag; the claim refused twice is never written."""
        inv = ("=====CLAIMS=====\nC1 | 04:30 | skip the warm-up hangs and a pulley goes | anecdote\n"
               "C2 | 01:40 | leave 48 hours between hard finger sessions | recommendation\n")
        self.fake(inventory=[inv], batch=["=====SKIPPED=====\nC2 | not a transferable training claim\n"] * 2)
        res = self.run2()
        self.assertEqual(res["outcome"], "all-refused")
        self.assertEqual(res["uncovered_reasons"]["C2"]["why"], "catch-all-without-why")
        self.assertEqual(self.queue_stage(), "needs-attention")  # after synth
        ri.finalize(self.staging, self.run_dir, self.unit2, [EX2], 0, self.zk)
        q = json.loads((self.staging / "queue.json").read_text())
        it = next(x for x in q["items"] if x["id"] == EX2)
        self.assertEqual((it["stage"], it["notes_published"]), ("synthesized", 0))  # DEFECT

    def test_Y9_partial_untimed_claims_are_never_checked(self) -> None:
        """Y9 partly fixed: _cites_claim returns True when the claim has no time (line
        claims of an untimestamped transcript) or the note has no timed tag. The prompt
        rule 'inside that claim's excerpt' is not enforced there."""
        note = "# T\n\nLead. [src: vid-x]\n\n## Evidence\n\n- vid-x (A): \"something else entirely\"\n"
        self.assertTrue(sc._cites_claim(note, {"id": "C4", "at": "line 400", "text": "planche lean"}, self.lit))
        self.assertTrue(sc._cites_claim(note, {"id": "C4", "at": "03:10", "text": "one grade"}, self.lit))

    def test_C13_reinterpreted_no_excerpt_skip_is_accepted(self) -> None:
        """P3: the prompt now gives `not a transferable training claim: no excerpt holds
        this claim` for a claim without excerpt; the harness accepts that skip. The
        first-round C13 still PASSES only because the harness refuses `passage unreadable`,
        which the prompt now forbids: no contradiction is left. FAIL = fixed."""
        claim = {"id": "C1", "at": "line 999", "text": "5x5 @ 80%", "type": "rule"}
        self.assertIsNotNone(sc._skip_check("not a transferable training claim: no excerpt holds this claim", claim, True))

    def test_C11_timed_case_fixed_control(self) -> None:
        """Y9 timed case (control, FAIL = fixed): the note lists C4 (03:10) but cites only
        01:40/01:47; C4 is not counted as covered."""
        inv = ("=====CLAIMS=====\nC2 | 01:40 | leave 48 hours between hard finger sessions | recommendation\n"
               "C4 | 03:10 | most people add one grade after six weeks | prediction\n")
        reply = NOTE_BLOCKS[0].replace("| claims: C2,C3=====", "| claims: C2,C4=====", 1) + "=====SKIPPED=====\n"
        self.fake(inventory=[inv], batch=[reply, "=====SKIPPED=====\nC4 | damaged span\n"])
        res = self.run2()
        self.assertEqual((res["coverage"].get("C4") or {}).get("note"), (res["coverage"].get("C2") or {}).get("note"))


if __name__ == "__main__":
    unittest.main()
