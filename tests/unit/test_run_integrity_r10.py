"""Round 10: checker false positives of pilot 4, manifest check at start, fix-call
renames, transcript_text spans, oversize replies, item passages, budget stop of fix
calls, single-mode times and model, skeleton fix message, allowed lines. No network."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from tests.unit.test_run_integrity import PN, ZR, _Env, ri
from tests.unit.test_synth_single import V5, _Single

import prompt_build  # noqa: E402
import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402
import verify_claims as vc  # noqa: E402

FP = ZR.parent / "tests" / "fixtures" / "pilot4_false_positives"
QUANTITY_TYPES = {"changed-period", "dropped-period", "number-not-in-quote", "changed-number-form", "dropped-unit",
                  "changed-unit", "range-mismatch", "changed-qualifier"}


# --------------------------------------------------------------------------- #
# P8: checker false positives (pilot 4, grading/partA_grades.tsv A11, A12, A13, A16, A20, A24)
# --------------------------------------------------------------------------- #


class PilotFourFalsePositives(_Env):
    def verify(self, part: str) -> set[tuple[str, str]]:
        p = next(p for p in (FP / "notes").glob("*.md") if part in p.name)
        r = vc.verify_note(p, vc.LitIndex([FP / "lit"]))
        return {(f["type"], f.get("where", "")) for f in r["failures"]}

    def test_graded_fine_notes_have_no_quantity_failure(self) -> None:
        for part in ("Six Weekly Levels", "For Six Weeks", "Never Going To Failure", "70% Exhausted"):
            self.assertEqual({t for t, _ in self.verify(part)} & QUANTITY_TYPES, set(), part)

    def test_ninety_degree_name_and_twice_stays_a_form_change(self) -> None:
        self.assertNotIn(("number-not-in-quote", "title"), self.verify("90 Degree"))
        # Round 11 (K21 changed): "twice" for a quote "two times" is the same form.
        self.assertNotIn("changed-number-form", {t for t, _ in self.verify("Twice A Day")})

    def test_extractor_rules(self) -> None:
        def q(text: str) -> list[tuple]:
            return [(x.values, x.unit, x.period) for x in vc.extract_quantities(text)]
        self.assertEqual(q("He lays out six weekly levels."), [((6.0,), None, None)])
        self.assertIn(((6.0,), "week", None), q("Train four times per week for six weeks."))
        self.assertEqual(q("His rule number three: never go to failure."), [])
        self.assertEqual(q("Finish a workout 70% exhausted."), [((70.0,), "percent", None)])
        self.assertEqual(q("Learn the 90 degree handstand push-up."), [])
        # The rules do not remove real quantities and periods.
        self.assertEqual(q("Train two times a week."), [((2.0,), "times", "per week")])
        self.assertEqual(q("Bend the elbows 90 degrees."), [((90.0,), "degree", None)])
        self.assertEqual(q("Do it weekly, 3 sets."), [((3.0,), "set", None)])
        c = vc.extract_quantities("a workout of one hour each, then rest")[0]
        d = vc.extract_quantities("one hour each workout")[0]
        self.assertEqual(vc.match_quantity(c, [d])[0], "supported")


# --------------------------------------------------------------------------- #
# M7: the prompt manifest at start
# --------------------------------------------------------------------------- #


class ManifestAtStart(_Env):
    def test_stale_manifest_stops_the_run(self) -> None:
        os.environ["ZR_TEST_MANIFEST"] = "1"
        self.addCleanup(os.environ.pop, "ZR_TEST_MANIFEST", None)
        with mock.patch.object(prompt_build, "check_manifest", return_value=["prompts/x.md [single]: differs"]):
            with self.assertRaisesRegex(ri.HarnessError, "prompt manifest is not current"):
                ri.start(self.staging, "synth", self.zk)


# --------------------------------------------------------------------------- #
# m1, m2, P10, m6: fix calls
# --------------------------------------------------------------------------- #


class FixCallsRound10(_Single):
    def written(self) -> None:
        self.fake(synth=[sc_reply(self)])
        self.synth()

    def test_rename_onto_an_existing_title_is_refused(self) -> None:
        self.written()
        rel = f"{PN}/{self.a}.md"
        parsed = sc.parse_reply(f"=====NOTE: {self.b}=====\n" + self.tb)
        self.assertEqual(sc._apply_fix(self.staging, self.zk, self.unit, rel, parsed)["result"], "not written")
        self.assertTrue((self.unit / "out" / rel).is_file())
        self.vpath("Vault Title").write_text("x")
        parsed = sc.parse_reply("=====NOTE: Vault Title=====\n" + self.ta.replace(f"# {self.a}", "# Vault Title"))
        self.assertEqual(sc._apply_fix(self.staging, self.zk, self.unit, rel, parsed)["result"], "not written")

    def test_review_repair_record_follows_a_rename(self) -> None:
        self.written()
        rel = f"{PN}/{self.a}.md"
        (self.unit / "review-repair.json").write_text(json.dumps({"notes": {rel: "sha"}}))
        parsed = sc.parse_reply("=====NOTE: New Name=====\n" + self.ta.replace(f"# {self.a}", "# New Name"))
        self.assertEqual(sc._apply_fix(self.staging, self.zk, self.unit, rel, parsed)["result"], "renamed")
        notes = json.loads((self.unit / "review-repair.json").read_text())["notes"]
        self.assertEqual(notes[f"{PN}/New Name.md"], "sha")

    def test_missing_sections_get_the_skeleton(self) -> None:
        self.written()
        p = self.unit / "out" / PN / f"{self.a}.md"
        p.write_text(p.read_text().split("## Evidence")[0])
        self.fake(fix=["=====DROP=====\nno"])
        sc.fix_unit(self.staging, self.zk, self.unit, "mech")
        user = [c for c in self.log() if c["kind"] == "fix"][0]["user"]
        self.assertIn("NOTE SKELETON", user)
        self.assertIn("type: permanent note", user.split("NOTE SKELETON", 1)[1])
        self.assertIn("note-skeleton", open(ZR / "NOTE_CONTRACT.md").read())

    def test_oversize_reply_is_costed(self) -> None:
        big = "x" * (rc.MAX_REPLY_BYTES + 10)
        self.fake(synth=[big, big])
        res = self.synth()
        rec = json.loads((self.unit / "calls" / "synth-p1-0.json").read_text())
        self.assertGreater(rec["usage"]["cost"], 0)
        self.assertEqual(rec["usage"]["calls_usage_estimated"], 1)
        self.assertIn(res["outcome"], ("no-output",))

    def _make_fix_wait(self, *, legacy: bool = False) -> tuple[str, str]:
        self.written()
        rel = f"{PN}/{self.a}.md"
        note = self.unit / "out" / rel
        note.write_text(note.read_text().split("## Evidence")[0])
        with mock.patch.dict(os.environ, {"ZR_LLM_BACKEND": "files"}):
            out = sc.fix_unit(self.staging, self.zk, self.unit, "mech")
        self.assertEqual(out, [{"note": rel, "result": "waiting-for-reply"}])
        requests = list((self.staging / "exchange" / "requests").glob("*.json"))
        self.assertEqual(len(requests), 1)
        key = json.loads(requests[0].read_text())["key"]
        if legacy:
            fp = json.loads((self.unit / "fix-pending.json").read_text())
            fp.pop("wait_keys", None)
            (self.unit / "fix-pending.json").write_text(json.dumps(fp))
        return rel, key

    def test_fix_wait_key_reaches_pending_folder_and_clears_on_success(self) -> None:
        rel, key = self._make_fix_wait()
        self.assertEqual(ri._unit_wait_keys(self.unit), {key})
        self.finalize()
        pdir = next((self.staging / "pending-review").glob("*--*"))
        meta = json.loads((pdir / "meta.json").read_text())
        self.assertEqual(meta["wait_keys"], [key])
        self.assertEqual(ri.folder_waits_keys(pdir), {key})
        unit = Path(ri.pending_units(self.staging, self.run_dir, None)[0])
        self.assertEqual(ri._unit_wait_keys(unit), {key})
        request = json.loads(next((self.staging / "exchange" / "requests").glob("*.json")).read_text())
        Path(request["reply_path"]).write_text("=====DROP=====\nnot needed")
        Path(request["meta_path"]).write_text(json.dumps({"key": key, "worker": "fix-worker", "model": "test"}))
        with mock.patch.dict(os.environ, {"ZR_LLM_BACKEND": "files"}):
            out = sc.fix_unit(self.staging, self.zk, unit, "mech")
        self.assertEqual(out[0]["result"], "dropped")
        self.assertFalse((unit / "fix-pending.json").exists())
        self.assertEqual(ri._unit_wait_keys(unit), set())

    def test_at_limit_waiting_fix_reply_reenters_without_resetting_cycles(self) -> None:
        _, key = self._make_fix_wait()
        request = json.loads(next((self.staging / "exchange" / "requests").glob("*.json")).read_text())
        Path(request["reply_path"]).write_text("=====DROP=====\nnot needed")
        Path(request["meta_path"]).write_text(json.dumps({"key": key, "worker": "fix-worker", "model": "test"}))
        info = json.loads((self.unit / "unit.json").read_text())
        info["pending_cycles"] = ri.MAX_PENDING_CYCLES - 1
        (self.unit / "unit.json").write_text(json.dumps(info))
        self.finalize()
        pdir = next((self.staging / "pending-review").glob("*--*"))
        self.assertEqual(json.loads((pdir / "meta.json").read_text())["pending_cycles"], ri.MAX_PENDING_CYCLES)
        self.assertEqual(ri.folder_waits_keys(pdir), {key})
        self.assertTrue(ri.folder_holds_reply(self.staging, pdir))
        unit = Path(ri.pending_units(self.staging, self.run_dir, None)[0])
        self.assertEqual(json.loads((unit / "unit.json").read_text())["pending_cycles"], ri.MAX_PENDING_CYCLES)

    def test_orphan_fix_wait_key_does_not_count_as_live(self) -> None:
        rel = f"{PN}/{self.a}.md"
        key = "a" * 32
        (self.unit / "fix-pending.json").write_text(json.dumps({"notes": {}, "wait_keys": {rel: [key]}}))
        self.assertEqual(ri._unit_wait_keys(self.unit), set())

    def test_legacy_fix_pending_retries_and_records_exact_key(self) -> None:
        rel, key = self._make_fix_wait(legacy=True)
        self.finalize()
        pdir = next((self.staging / "pending-review").glob("*--*"))
        self.assertEqual(ri.folder_waits_keys(pdir), set())
        unit = Path(ri.pending_units(self.staging, self.run_dir, None)[0])
        self.assertEqual(ri._unit_wait_keys(unit), set())
        with mock.patch.dict(os.environ, {"ZR_LLM_BACKEND": "files"}):
            out = sc.fix_unit(self.staging, self.zk, unit, "mech")
        self.assertEqual(out, [{"note": rel, "result": "waiting-for-reply"}])
        self.assertEqual(ri._unit_wait_keys(unit), {key})
        self.assertEqual(json.loads((unit / "fix-pending.json").read_text())["wait_keys"], {rel: [key]})

    def test_budget_stop_of_a_fix_call_keeps_the_note_pending(self) -> None:
        self.written()
        p = self.unit / "out" / PN / f"{self.a}.md"
        p.write_text(p.read_text().split("## Evidence")[0])
        os.environ["ZR_MAX_COST_USD"] = "0.001"  # the synth call already cost 0.01
        self.fake(fix=["never sent"])
        out = sc.fix_unit(self.staging, self.zk, self.unit, "mech")
        self.assertEqual(out, [{"note": f"{PN}/{self.a}.md", "result": "budget-stop"}])
        self.assertEqual([c for c in self.log() if c["kind"] == "fix"], [])
        fp = json.loads((self.unit / "fix-pending.json").read_text())
        self.assertEqual(fp["notes"], {f"{PN}/{self.a}.md": "mech"})
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        n = {Path(x["note"]).stem: x for x in ev["notes"]}[self.a]
        self.assertEqual((n["action"], n.get("pending_reason")), ("pending-review", "budget-stop"))
        pdir = next((self.staging / "pending-review").glob("*--*"))
        self.assertTrue((pdir / "fix-pending.json").is_file())
        units = ri.pending_units(self.staging, self.run_dir, None)
        self.assertTrue((Path(units[0]) / "fix-pending.json").is_file())
        self.assertTrue((Path(units[0]) / "candidates.json").is_file())
        lib = (ZR / "run_lib.sh").read_text()
        self.assertIn('if [ -f "$unit_dir/fix-pending.json" ]; then', lib)

    def test_single_mode_times_and_model(self) -> None:
        self.written()
        rec = json.loads((self.unit / "calls" / "synth-p1-0.json").read_text())
        rec["elapsed_s"] = 12.5
        (self.unit / "calls" / "synth-p1-0.json").write_text(json.dumps(rec))
        self.assertEqual(ri._unit_times(self.unit)["synthesis"], 12.5)
        ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        s = ri.summary(self.run_dir, self.staging)
        self.assertEqual(s["model"], "anthropic/claude-sonnet-5.5")
        self.assertGreaterEqual(s["wall_time_s"]["synthesis"], 12.5)


def sc_reply(t: FixCallsRound10) -> str:
    from tests.unit.test_synth_single import reply
    return reply([("C1", "01:16", "a"), ("C2", "01:22", "b")], [(t.a, ["C1"], t.ta), (t.b, ["C2"], t.tb)])


# --------------------------------------------------------------------------- #
# m5, m7, B item 8: transcript_text spans and item passages
# --------------------------------------------------------------------------- #


class TranscriptTextRound10(_Env):
    PASSAGES = ("[EVIDENCE 1 (skeleton item evidence-1)] vid-a @ 00:10\n>>> i train rings twice per week <<<\n"
                "[EVIDENCE 2 (skeleton item evidence-2)] vid-a @ 01:00\n>>> rest three minutes <<<\n"
                "[EVIDENCE 3 (skeleton item evidence-3)] vid-a @ 02:00\n>>> yes <<<\n")

    def obj(self, tts: dict[str, str]) -> dict:
        return {"items": [{"item": k, "verdict": "supported", "also": [], "transcript_text": v, "problem": ""}
                          for k, v in tts.items()], "scope_complete": True}

    def test_four_words_from_the_items_own_passage(self) -> None:
        imap = {"evidence-1": [1], "evidence-2": [2], "evidence-3": [3]}
        ok = self.obj({"evidence-1": "train rings twice per", "evidence-2": "rest three minutes", "evidence-3": "yes"})
        exp = list(imap)
        self.assertIsNone(rc.validate(ok, exp, self.PASSAGES, imap))  # a marked text under 4 words: all of it
        short = self.obj({"evidence-1": "rings twice", "evidence-2": "rest three minutes", "evidence-3": "yes"})
        self.assertIn("4 consecutive words", rc.validate(short, exp, self.PASSAGES, imap))
        other = self.obj({"evidence-1": "rest three minutes", "evidence-2": "rest three minutes", "evidence-3": "yes"})
        self.assertIsNotNone(rc.validate(other, exp, self.PASSAGES, imap))  # words of another item's passage
        fill = self.obj({"evidence-1": "i train rings", "evidence-2": "rest three minutes", "evidence-3": "yes"})
        self.assertIsNotNone(rc.validate(fill, exp, self.PASSAGES, imap))
        three = self.obj({"evidence-1": "i train ... rings twice ... per week", "evidence-2": "rest three minutes",
                          "evidence-3": "yes"})
        self.assertIsNotNone(rc.validate(three, exp, self.PASSAGES, imap))  # at most two spans

    def test_scope_one_span_per_value_and_empty(self) -> None:
        p = self.PASSAGES
        self.assertTrue(rc.transcript_text_ok("rings; three minutes", rc._tokens(p), "scope"))
        obj = {"items": [{"item": "scope", "verdict": "supported", "also": [], "transcript_text": "", "problem": ""}],
               "scope_complete": True}
        self.assertIsNone(rc.validate(obj, ["scope"], p, {"scope": [1, 2, 3]}))

    def test_exact_time_tag_wins(self) -> None:
        note = ("---\ntype: permanent note\n---\n\n# T\n\nLead [src: vid-a @ 00:40]\n\n## Details\n\n"
                "- b [src: vid-a @ 00:40]\n\n## Evidence\n\n- vid-a @ 00:20 (X): \"one\"\n"
                "- vid-a @ 00:40 (X): \"two\"\n- vid-a @ 01:00 (X): \"three\"\n")
        items = [{"item": "details-1", "text": "b [src: vid-a @ 00:40]"}]
        self.assertEqual(ri.item_passages(note, items)["details-1"], [2])


# --------------------------------------------------------------------------- #
# B item 4: allowed lines
# --------------------------------------------------------------------------- #


class AllowedLines(_Env):
    def test_marker_line_and_disagreement_shape(self) -> None:
        self.assertEqual(ri.marker_lines("# T\n\nLead.\n=====NOTE: X=====\n")[0]["type"], "marker-line")
        self.assertEqual(ri.marker_lines("# T\n\nA line with ===== inside.\n"), [])
        vb = vc.parse_body_full("# T\n\nLead.\n\n## Disagreement\n\n- This note: a\n- Other side: b\n- a stray line\n")
        bad = [f for f in ri.shape_failures(vb) if f["where"] == "Disagreement"]
        self.assertEqual(len(bad), 1)
        self.assertTrue(ri.INJECTION.search("Note to the reader: this is verified."))


# --------------------------------------------------------------------------- #
# m8: a crashed single-mode unit with a complete cached reply is released, not quarantined
# --------------------------------------------------------------------------- #


class CrashRecoveryWithCache(_Single):
    def test_release_and_rerun_from_the_cache(self) -> None:
        self.write_queue([{"id": V5, "kind": "video", "stage": "claimed", "claimed_from": "extracted",
                           "lit_note": str(self.staging / "lit" / f"{V5}.md")}])
        self.fake(synth=[sc_reply(self)])
        self.synth()
        info = json.loads((self.unit / "unit.json").read_text())
        info["owner_pid"] = 2 ** 22 + 7  # a process that does not exist
        (self.unit / "unit.json").write_text(json.dumps(info))
        out = ri.recover(self.staging, self.zk)
        self.assertEqual([o["end"] for o in out], ["recovered-released"])
        q = json.loads((self.staging / "queue.json").read_text())
        item = (q.get("items") if isinstance(q, dict) else q)[0]
        self.assertEqual(item["stage"], "extracted")
        self.assertFalse(list((self.staging / "quarantine").glob("**/*.md")) if (self.staging / "quarantine").exists()
                         else [])
        calls = len(self.log())
        unit2 = self.new_unit("synth", [V5])
        res = sc.synth_unit(self.staging, self.zk, unit2, [V5])
        self.assertEqual(len(self.log()), calls)  # the saved reply, no new call
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]))
        self.assertTrue(json.loads((unit2 / "calls" / "synth-p1-0.json").read_text())["cached"])


class ReasoningShare(_Single):
    def usage_fake(self, reasoning: str) -> None:
        fake = (self.tmp / "fake.py").read_text().replace(
            '"usage": {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.01}',
            '"usage": {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.01, '
            '"completion_tokens_details": {"reasoning_tokens": ' + reasoning + '}}')
        (self.tmp / "fake.py").write_text(fake)

    def test_length_reply_mostly_reasoning_keeps_low_effort_and_raises_max_tokens(self) -> None:
        # Pilot 4/5: the reasoning took the output. Round 11: same setting, 1.5 x max_tokens.
        self.usage_fake('495 if finish == "length" else 0')
        self.fake(synth=[{"content": "=====CLAIMS=====\n" + "C1 | 1 | a claim of some length\n" * 12, "finish": "length"},
                         sc_reply(self)])
        res = self.synth()
        self.assertEqual(sorted(res["notes"]), sorted([self.a, self.b]))
        log = self.log()
        self.assertEqual([c["reasoning"] for c in log][:2], [{"effort": "low"}, {"effort": "low"}])
        self.assertEqual([c["max_tokens"] for c in log][:2], [24000, 36000])
        self.assertTrue((self.unit / "calls" / "synth-p1-0-more.json").is_file())

    def test_length_stop_without_reasoning_is_a_truncation(self) -> None:
        self.usage_fake("0")
        cut = sc_reply(self).split(f"=====NOTE: {self.b}")[0] + f"=====NOTE: {self.b} | claims: C2=====\n---\n"
        self.fake(synth=[{"content": cut, "finish": "length"}], cont=[sc_reply(self).split("=====NOTE:", 1)[0]
                                                                       + "=====NOTE:" + sc_reply(self).split("=====NOTE:")[2]])
        self.synth()
        self.assertEqual([c["kind"] for c in self.log()][:2], ["synth", "cont"])
        self.assertFalse((self.unit / "calls" / "synth-p1-0-more.json").exists())

    def test_off_is_refused_for_a_model_that_reasons(self) -> None:
        os.environ["ZR_SYNTH_REASONING"] = "off"
        with self.assertRaisesRegex(ri.HarnessError, "reasons by default"):
            sc.settings("synth")
        os.environ.pop("ZR_SYNTH_REASONING")
        self.assertIsNone(sc.no_reasoning())  # no default {"enabled": false}
        self.assertIsNone(sc.reasoning_for("acme/plain-model"))
        self.assertIn("probe-reasoning", (ZR / "synth_call.py").read_text())


class RepairLedgerInSummary(_Single):
    kind = "repair"

    def test_dropped_bullets_and_problems_reach_the_summary(self) -> None:
        from tests.unit.test_run_integrity import OLD_NOTE
        text = OLD_NOTE.format(t="Old Ledger").replace("created:", f"sources:\n  - id: {V5}\n    speaker: X\ncreated:")
        self.vpath("Old Ledger").write_text(text)
        rel = f"{PN}/Old Ledger.md"
        self.fake(repair=[f"=====NOTE: {self.a} | repairs: Old Ledger.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                          "=====KEEP-NEEDS-REPAIR: Unknown.md=====\nx\n"
                          "=====BULLETS=====\nOld Ledger.md#b1 | dropped: contradicts the transcript\n"])
        unit = self.new_unit("repair", [])
        sc.repair_unit(self.staging, self.zk, unit, V5, [rel], [])
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
        self.assertEqual([b["bullet"] for b in ev["bullets_dropped"]], ["Old Ledger.md#b1"])
        self.assertIn("keep-unknown-note", {p["type"] for p in ev["repair_problems"]})
        self.assertEqual(ev["repair_stubs"][0]["note"], rel)
        s = ri.summary(self.run_dir, self.staging)
        self.assertEqual(s["bullets_dropped"][0]["reason"], "contradicts the transcript")
        self.assertIn(unit.name, " ".join(s["repair_problems"]))
