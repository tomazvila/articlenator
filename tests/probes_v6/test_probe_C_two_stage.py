"""Reviewer C probes (v6): two-stage synthesis. Each test PASSES when the defect it
names is present (the assertion states the wrong behavior). Fake LLM only."""
from __future__ import annotations

import json
import os
import shutil

from tests.unit.test_run_integrity import ZR
from tests.unit.test_synth_single import _Single

import synth_call as sc  # noqa: E402
import verify_claims as vc  # noqa: E402

NC = ZR.parent / "tests" / "fixtures" / "note_contract"
EX2 = "vid-EXAMPLE0002"
BATCH = (NC / "batch_reply_fictional.txt").read_text()
NOTE_BLOCKS = ["=====NOTE: " + b for b in BATCH.split("=====NOTE: ")[1:]]
NOTE_48H = NOTE_BLOCKS[0].split("\n", 1)[1]  # the note text without its marker (C2,C3)
NOTE_PRED = NOTE_BLOCKS[1].split("\n", 1)[1]  # the predictions note (C4)


class _Two(_Single):
    def setUp(self) -> None:
        super().setUp()
        os.environ["ZR_SYNTH_STAGES"] = "two"
        shutil.copy(NC / "lit_fictional" / f"{EX2}.md", self.staging / "lit")
        self.write_queue([{"id": EX2, "kind": "video", "stage": "claimed", "claimed_from": "extracted",
                           "lit_note": str(self.staging / "lit" / f"{EX2}.md")}])
        self.unit2 = self.new_unit("synth", [EX2])
        self.lit = (self.staging / "lit" / f"{EX2}.md").read_text()

    def run2(self) -> dict:
        return sc.synth_unit(self.staging, self.zk, self.unit2, [EX2])

    def queue_stage(self) -> str:
        q = json.loads((self.staging / "queue.json").read_text())
        return next(it for it in q["items"] if it["id"] == EX2)["stage"]


class ProbeInventory(_Two):
    def test_C1_bracketed_time_crashes_the_unit(self) -> None:
        """DEFECT: `[01:47]` (the marker form the inventory prompt points to) reaches
        vc.ts_to_seconds in _same_claim -> ValueError escapes synth_unit."""
        inv = ("=====CLAIMS=====\nC1 | [01:40] | leave 48 hours between hard finger sessions | recommendation\n"
               "C2 | [01:47] | easy mileage on big holds on the days between | recommendation\n")
        self.fake(inventory=[inv], batch=[])
        with self.assertRaises(ValueError):
            self.run2()
        # the same for a range or an approximate time
        with self.assertRaises(ValueError):
            sc._same_claim({"at": "01:40-01:50", "text": "a b c"}, {"at": "01:41", "text": "a b c"})
        with self.assertRaises(ValueError):
            sc._same_claim({"at": "~01:40", "text": "a b c"}, {"at": "01:41", "text": "a b c"})

    def test_C2_distinct_steps_in_one_part_are_merged_as_duplicates(self) -> None:
        """DEFECT: dedup runs inside ONE part too: two benchmark steps 6 s apart that differ
        only in a 2-digit number and the step name are one claim; the second is lost."""
        inv = ("=====CLAIMS=====\n"
               "C1 | 03:40 | hold 10 seconds two-armed on the 20 millimeter edge before one-arm hangs | benchmark\n"
               "C2 | 03:46 | hold 20 seconds two-armed on the 20 millimeter edge before one-arm lock-offs | benchmark\n")
        self.fake(inventory=[inv], batch=["=====SKIPPED=====\nC1 | damaged span\n"])
        res = self.run2()
        self.assertEqual([c["id"] for c in res["claims"]], ["C1"])  # C2 is gone
        self.assertNotIn("C2", res["coverage"])
        self.assertNotIn("C2", res["claims_uncovered"])  # not even listed as uncovered
        self.assertEqual(res["yield"]["inventoried"], 1)
        self.assertNotIn("C2 |", self.log()[1]["user"])  # never sent to a batch

    def test_C3_unknown_type_spelling_is_skipped_without_a_call(self) -> None:
        """DEFECT: `own practice` (the spelling of the synth prompt table) or a missing type
        becomes `other` and is skipped with no batch call."""
        inv = ("=====CLAIMS=====\nC1 | 03:10 | most people add one grade after six weeks | Prediction.\n"
               "C2 | 01:40 | I tell my athletes to leave 48 hours | own practice\n"
               "C3 | 04:05 | a fourth session in the off-season is an option\n")
        self.fake(inventory=[inv], batch=[])
        res = self.run2()
        self.assertEqual([c["kind"] for c in self.log()], ["inventory"])  # no batch at all
        self.assertEqual({c["id"]: c["type"] for c in res["claims"]}, {"C1": "other", "C2": "other", "C3": "other"})
        self.assertEqual(res["outcome"], "all-skipped")
        self.assertEqual(self.queue_stage(), "skipped")

    def test_C4_line_references_of_part_2_point_into_part_1(self) -> None:
        """DEFECT: a `line N` of part 2 is read as a line of the whole body: the excerpt is
        from part 1 and does not hold the claim."""
        body = "".join(f"filler sentence number {i} about nothing at all here.\n" for i in range(400))
        body += "Keep the elbows locked in every planche lean set.\n"
        lit = "---\nid: vid-x\ntimestamps: false\n---\n\n# T\n\n" + body
        parts = sc.split_parts(lit, 12000)
        self.assertGreater(len(parts), 1)
        last = parts[-1]
        local = [ln for ln in last.split("\n")]
        n_local = next(i for i, ln in enumerate(local, 1) if "planche lean" in ln)
        ex = sc.excerpts(lit, [{"id": "P2-C1", "at": f"line {n_local}", "text": "elbows locked planche lean"}])
        self.assertNotIn("planche lean", ex[0]["text"])

    def test_C5_no_dedup_for_line_claims_across_the_overlap(self) -> None:
        """Part overlap without timestamps: _same_claim needs ':' so the same claim of both
        parts is kept twice (two batches, two notes)."""
        a = {"at": "line 40", "text": "keep the elbows locked in every planche lean set"}
        b = {"at": "line 3", "text": "keep the elbows locked in every planche lean set"}
        self.assertFalse(sc._same_claim(a, b))


class ProbeBatches(_Two):
    def test_C6_multi_part_skip_lines_are_never_parsed(self) -> None:
        """DEFECT: SKIP_LINE needs `C<n>`; with more than one part the ids are `P1-C<n>`
        (the ids shown in the batch), so every skip is unparsed and lost."""
        p = sc.parse_reply("=====SKIPPED=====\nP1-C3 | damaged span\nP2-C1 | not a transferable training claim: "
                           "a greeting\n")
        self.assertEqual(p["skips"], {})
        self.assertEqual([x["type"] for x in p["problems"]], ["skip-line-unparsed", "skip-line-unparsed"])

    def test_C6b_multi_part_run_loses_every_skip(self) -> None:
        more = "".join(f"[{m:02d}:00] Filler talk about the weather and the gym number {m} today.\n"
                       for m in range(5, 40))
        lit = self.lit.rstrip("\n") + "\n" + more
        (self.staging / "lit" / f"{EX2}.md").write_text(lit)
        os.environ["ZR_SYNTH_MAX_INPUT_CHARS"] = "2200"  # force two parts
        parts = sc.split_parts(lit, 2200)
        self.assertGreater(len(parts), 1)
        inv = "=====CLAIMS=====\nC1 | 04:50 | warm up properly before hangs to protect the pulleys | recommendation\n"
        self.fake(inventory=[inv] * len(parts),
                  batch=["=====SKIPPED=====\nP1-C1 | damaged span\n", "=====SKIPPED=====\nP1-C1 | damaged span\n"])
        res = self.run2()
        self.assertIn("P1-C1", res["claims_uncovered"])
        self.assertIn("skip-line-unparsed", {p["type"] for p in res["problems"]})
        self.assertEqual(self.log()[-1]["kind"], "batch")
        os.environ.pop("ZR_SYNTH_MAX_INPUT_CHARS", None)

    def test_C7_already_covered_by_unknown_title_is_accepted(self) -> None:
        """DEFECT: two-stage never calls _drop_invalid_skips: `already covered by [[X]]`
        with X neither written nor a candidate covers the claim."""
        inv = "=====CLAIMS=====\nC1 | 04:05 | a fourth session in the off-season is an option | option\n"
        self.fake(inventory=[inv], batch=["=====SKIPPED=====\nC1 | already covered by [[Some Note That Does Not Exist]]\n"])
        res = self.run2()
        self.assertEqual(res["coverage"]["C1"], {"skip": "already covered by [[Some Note That Does Not Exist]]"})
        self.assertEqual(res["claims_uncovered"], [])
        self.assertEqual(len(self.log()), 2)  # no re-send
        self.assertEqual(res["outcome"], "all-skipped")

    def test_C8_twice_refused_claim_makes_the_video_no_output(self) -> None:
        """DEFECT: one anecdote (typed skip) plus one claim refused twice, no note: outcome
        `no-output` (an attempt, `failed` after 3) instead of a visible needs-attention."""
        inv = ("=====CLAIMS=====\nC1 | 04:30 | skip the warm-up hangs and a pulley goes | anecdote\n"
               "C2 | 01:40 | leave 48 hours between hard finger sessions | recommendation\n")
        self.fake(inventory=[inv], batch=["=====SKIPPED=====\nC2 | not a transferable training claim\n"] * 2)
        res = self.run2()
        self.assertEqual(res["outcome"], "no-output")
        self.assertEqual(res["claims_uncovered"], ["C2"])
        res_file = json.loads((self.unit2 / "agent_result.json").read_text())
        self.assertEqual((res_file["end"], res_file["detail"]), ("no-output", "no usable reply"))  # misleading
        # a rerun reads the same replies from the synth cache: the same end, no new call
        n = len(self.log())
        self.unit2 = self.new_unit("synth", [EX2])
        self.assertEqual(self.run2()["outcome"], "no-output")
        self.assertEqual(len(self.log()), n)

    def test_C9_unlabeled_note_with_no_shared_word_maps_to_the_first_claim(self) -> None:
        """DEFECT: without source-tag times a note that shares NO word with any claim is
        matched to the first claim of the batch (d = 1000 is still a 'best')."""
        claims = [{"id": "C2", "at": "line 3", "text": "leave hours between hard finger sessions"},
                  {"id": "C6", "at": "line 9", "text": "fourth session offseason option"}]
        parsed = {"notes": [{"title": "Zzz", "claims": [], "text": "# Zzz\n\nqqqq wwww eeee rrrr\n"}], "problems": []}
        sc._match_unlabeled(parsed, claims, "")
        self.assertEqual(parsed["notes"][0]["claims"], ["C2"])

    def test_C10_unlabeled_note_maps_to_nearest_time_not_its_claim(self) -> None:
        """A note for C3 (01:47) that cites only the 01:40 marker of its context is mapped
        to C2; C3 is re-sent and gets a second note."""
        claims = [{"id": "C2", "at": "01:40", "text": "leave 48 hours"},
                  {"id": "C3", "at": "01:47", "text": "easy mileage on big holds"}]
        text = "# Easy Mileage\n\nEasy mileage on big holds between hard days. [src: vid-EXAMPLE0002 @ 01:40]\n"
        parsed = {"notes": [{"title": "Easy Mileage", "claims": [], "text": text}], "problems": []}
        sc._match_unlabeled(parsed, claims, "")
        self.assertEqual(parsed["notes"][0]["claims"], ["C2"])

    def test_C11_note_lists_a_claim_it_does_not_cover(self) -> None:
        """DEFECT (not caught): a note marker that lists C4 for the 48-hours note (which cites
        only 01:40 and 01:47) covers C4; nothing compares the note times to the claim/excerpt."""
        inv = ("=====CLAIMS=====\nC2 | 01:40 | leave 48 hours between hard finger sessions | recommendation\n"
               "C4 | 03:10 | most people add one grade after six weeks | prediction\n")
        reply = NOTE_BLOCKS[0].replace("| claims: C2,C3=====", "| claims: C2,C4=====", 1) + "=====SKIPPED=====\n"
        self.fake(inventory=[inv], batch=[reply])
        res = self.run2()
        self.assertEqual(res["coverage"]["C4"]["note"], res["coverage"]["C2"]["note"])
        self.assertEqual(res["claims_uncovered"], [])
        self.assertFalse(any("C4" in json.dumps(p) for p in res["problems"]))

    def test_C12_damaged_span_with_char_detail_is_judged_clean(self) -> None:
        """DEFECT: with `asr_damaged_spans_detail` (character ranges) damaged_times is
        emptied, so _readable says the damaged passage is clean and the `damaged span` skip
        is refused (sent again)."""
        lit = self.lit.replace('asr_quality: "ok"', 'asr_quality: "partial"\nasr_span_unit: "timestamp"\n'
                               'asr_damaged_spans: [["04:30", "04:50"]]\n'
                               'asr_damaged_spans_detail: [{start_line: 10, start_char: 0, end_line: 11, end_char: 20}]')
        (self.staging / "lit" / f"{EX2}.md").write_text(lit)
        tr = vc.LitIndex(vc.lit_dirs(self.staging)).get(EX2)
        self.assertEqual(tr.damaged_times, [])
        self.assertTrue(sc._readable(lit, tr, "04:30"))
        self.assertEqual(sc._skip_check("damaged span", {"type": "recommendation"}, sc._readable(lit, tr, "04:30")),
                         "unreadable-on-clean-passage")

    def test_C13_no_excerpt_prompt_rule_is_refused_by_the_harness(self) -> None:
        """Prompt: `(no excerpt found ...)` -> skip as `passage unreadable`. Harness: the file
        is clean, so the skip is refused and the claim is re-sent with a flag that forbids
        `passage unreadable`."""
        claim = {"id": "C1", "at": "line 999", "text": "5x5 @ 80%", "type": "rule"}
        user = sc._batch_user(self.lit, [claim], 1, 1, [], [], {})
        self.assertIn(sc.H_NO_EXCERPT, user)
        tr = vc.LitIndex(vc.lit_dirs(self.staging)).get(EX2)
        self.assertEqual(sc._skip_check("passage unreadable", claim, sc._readable(self.lit, tr, claim["at"])),
                         "unreadable-on-clean-passage")

    def test_C14_wrong_time_inside_the_video_gives_an_excerpt_without_the_claim(self) -> None:
        """A claim time that exists but is wrong: no check that the excerpt holds the claim
        words, so the batch gets an unrelated passage (no word fallback)."""
        ex = sc.excerpts(self.lit, [{"id": "C8", "at": "00:10", "text": "warm up properly"}])
        self.assertNotIn("warm up properly", ex[0]["text"].lower())

    def test_C15_line_number_is_off_by_heading_lines(self) -> None:
        """`line N` counts lit body lines (contract 1: line 1 = first line after ---), but
        _lit_lines drops heading lines, so index N-1 is one line later per heading."""
        lit = "---\nid: x\n---\n# H1\n## Part\nline three text alpha\nline four text bravo\n"
        lines = sc._lit_lines(lit)
        # body line 3 is 'line three ...'; index 2 of _lit_lines is two lines later
        self.assertEqual(lines[0][0], "line three text alpha")
        self.assertNotEqual(lines[2][0], "line three text alpha")

    def test_C16_same_title_same_claim_new_wording_is_a_copy_not_a_duplicate(self) -> None:
        """Pilot CR 8: the re-sent claim gets the same note with one changed word in the lead
        -> `duplicate-title` and a title-fix call (a second note for one claim), not a
        mapping to the first note."""
        inv = "=====CLAIMS=====\nC4 | 03:10 | most people add one grade after six weeks | prediction\n"
        second = NOTE_BLOCKS[1].replace("Tomas Brink predicts that after about six weeks",
                                        "Tomas Brink predicts that after roughly six weeks", 1)
        total = {"claims": [{"id": "C4", "at": "03:10", "text": "x", "type": "prediction"}],
                 "notes": [sc.parse_reply(NOTE_BLOCKS[1])["notes"][0], sc.parse_reply(second)["notes"][0]],
                 "skips": {}, "keeps": {}, "drop": None, "stray": "", "problems": [], "skipped_block": True}
        self.fake(inventory=[inv], fix=["=====DROP=====\nno"])
        try:
            res = sc._write_synth(self.staging, self.zk, self.unit2, [EX2], total, [])
        except sc.PendingReply:
            res = None
        log = self.log()
        self.assertEqual([c["kind"] for c in log], ["fix"])  # a title-fix call for a copy
        self.assertIn("give this note a distinct title", log[0]["user"])
        if res is not None:
            self.assertIn("duplicate-title", {p["type"] for p in res["problems"]})
            self.assertNotIn("duplicate-note", {p["type"] for p in res["problems"]})

    def test_C17_yield_counts_claim_ids_that_do_not_exist(self) -> None:
        """claims_with_note counts coverage keys that are not inventory claims (a note that
        lists C9 when the batch has only C4)."""
        total = {"claims": [{"id": "C4", "at": "03:10", "text": "x", "type": "prediction"}],
                 "notes": [dict(sc.parse_reply(NOTE_BLOCKS[1])["notes"][0], claims=["C4", "C9", "C10"])],
                 "skips": {}, "keeps": {}, "drop": None, "stray": "", "problems": [], "skipped_block": True,
                 "yield_stats": {"inventoried": 1}}
        res = sc._write_synth(self.staging, self.zk, self.unit2, [EX2], total, [])
        self.assertEqual(res["yield"]["claims_with_note"], 3)  # 1 claim inventoried, 3 "with note"
        self.assertEqual(res["yield"]["inventoried"], 1)


class ProbeInventoryRetry(_Two):
    def test_C18_prose_inventory_is_no_output_and_the_cache_repeats_it(self) -> None:
        """No retry of an unusable inventory reply (one-stage has one); the reply is cached
        as complete, so each later attempt gets the same reply without a call."""
        self.fake(inventory=["Sorry, here is a summary of the video instead."], batch=[])
        res = self.run2()
        self.assertEqual(res["outcome"], "no-output")
        self.assertEqual([c["kind"] for c in self.log()], ["inventory"])
        self.unit2 = self.new_unit("synth", [EX2])
        self.assertEqual(self.run2()["outcome"], "no-output")
        self.assertEqual(len(self.log()), 1)  # attempt 2: no call, same end
