"""Tests for zettel_ralph/verify_claims.py (deterministic note-vs-transcript check).

Fixtures: the four worked examples of NOTE_CONTRACT.md section 14 and their real
transcripts (tests/fixtures/zettel_verify), and the round-1 reviewer's attack table
(tests/fixtures/zettel_attack). Runs under pytest and under `python -m unittest`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ZR = ROOT / "zettel_ralph"
FIX = ROOT / "tests" / "fixtures" / "zettel_verify"
ATTACK = ROOT / "tests" / "fixtures" / "zettel_attack"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

import verify_claims as vc  # noqa: E402
from twitter_articlenator.sources import asr_tools as asr  # noqa: E402

SASA = "SasaVenos Recommends No More Than 30 To 40 Seconds Of Banded Skill Volume Per Session After His Own Maltese Trial"
RADO = "Radoslav Radev Advises One, Maximum Two Failure Workouts At Four Workouts Per Week"
PRED = "Radoslav Radev Predicts A First Pike Straddle Planche Hold Of Around Three To Five Seconds After 15 Seconds Of Frog Stand"
DAVID = "David Packer Recommends Ring Strength Skills Twice A Week And Front Lever Three To Four Times A Week From His Own Experience"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vc-"))
        self.lit = self.tmp / "lit"
        shutil.copytree(FIX / "lit", self.lit)
        self.notes = self.tmp / "notes"
        shutil.copytree(FIX / "notes", self.notes)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def edit(self, title: str, old: str, new: str, count: int = 1) -> Path:
        path = self.notes / f"{title}.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, "fixture text changed; update the test")
        path.write_text(text.replace(old, new, count), encoding="utf-8")
        return path

    def verify(self, path: Path, **kw) -> dict:
        return vc.verify_note(path, vc.LitIndex([self.lit]), vc.Options(**kw))

    def types(self, rep: dict) -> set[str]:
        return {f["type"] for f in rep["failures"]}


class ContractExamples(_Base):
    def test_all_worked_examples_pass(self) -> None:
        for title in (SASA, RADO, PRED, DAVID):
            rep = self.verify(self.notes / f"{title}.md")
            self.assertEqual(rep["failures"], [], title)

    def test_speaker_evidence_accepts_guest_with_warning(self) -> None:
        rep = self.verify(self.notes / f"{DAVID}.md")
        self.assertIn("speaker-from-evidence", {w["type"] for w in rep["warnings"]})
        path = self.edit(DAVID, "speaker_evidence: \"i'm sure", "speaker_evidence: \"i am sure")
        self.assertIn("wrong-speaker", self.types(self.verify(path)))

    def test_14_2_without_the_five_times_sentence(self) -> None:
        """M2: the count noun 'failure workouts' compares with the quote's frame quantity."""
        q = "And my advice for you is if you train four times per week only one maximum two to go to a failure."
        path = self.edit(RADO, '"And my advice for you is if you train four times per week only one maximum two '
                               'to go to a failure. Five times per week, two times going to failure."', f'"{q}"')
        path = self.edit(RADO, "- At five workouts per week: two go to failure. [src: vid-KCRETdZ0l78 @ 01:50]\n", "")
        path = self.edit(RADO, '    - value: "two workouts to failure, at five times"\n      period: "per week"\n', "")
        rep = self.verify(path)
        self.assertEqual(rep["failures"], [])


class AuditDefects(_Base):
    def test_per_week_to_each_fails(self) -> None:
        path = self.edit(SASA, "He did around 10 sets of holds per week.", "He did around 10 sets of holds each.")
        self.assertIn("changed-period", self.types(self.verify(path)))

    def test_period_dropped_fails(self) -> None:
        path = self.edit(SASA, "He did around 10 sets of holds per week.", "He did around 10 sets of holds.")
        self.assertIn("dropped-period", self.types(self.verify(path)))

    def test_four_five_swap_fails(self) -> None:
        path = self.edit(RADO, "- At four workouts per week: only one, maximum two, go to failure.",
                         "- At five workouts per week: only one, maximum two, go to failure.")
        self.assertIn("changed-reference", self.types(self.verify(path)))

    def test_only_one_without_maximum_two_fails(self) -> None:
        path = self.edit(RADO, "- At four workouts per week: only one, maximum two, go to failure.",
                         "- At four workouts per week: only one goes to failure.")
        self.assertIn("range-mismatch", self.types(self.verify(path)))

    def test_qualifier_flip_fails(self) -> None:
        path = self.edit(SASA, "and do not do more than 20 to 30 reps", "and do at least 20 to 30 reps")
        self.assertIn("changed-qualifier", self.types(self.verify(path)))

    def test_number_in_bracket_in_details_fails(self) -> None:
        path = self.edit(SASA, "He did around 10 sets of holds per week.",
                         "He did around 10 sets of holds per week [20 for advanced athletes].")
        self.assertTrue(self.types(self.verify(path)) & {"number-not-in-quote", "range-mismatch"})

    def test_claim_without_quote_support_fails(self) -> None:
        path = self.edit(SASA, "- He did around 10 sets of holds per week.",
                         "- Calcium depletes motor neurons faster under isometric load. [src: vid-krqBQjUydGY]\n"
                         "- He did around 10 sets of holds per week.")
        rep = self.verify(path)
        self.assertNotIn("low-quote-support", self.types(rep))  # never a failure on its own
        self.assertIn("low-quote-support", {w["type"] for w in rep["warnings"]})


class StructureRules(_Base):
    def test_unknown_section_and_two_paragraph_lead(self) -> None:
        path = self.edit(SASA, "## Details", "## Practical Application\n\nDo 5 sets.\n\n## Details")
        self.assertIn("unknown-section", self.types(self.verify(path)))
        path.write_text((FIX / "notes" / f"{SASA}.md").read_text())
        path = self.edit(SASA, "## Details", "Beginners hold 90 seconds per set.\n\n## Details")
        self.assertIn("multi-paragraph-lead", self.types(self.verify(path)))

    def test_title_must_match_file_and_frontmatter_title(self) -> None:
        path = self.edit(SASA, "status: expanded", 'status: expanded\ntitle: "Other Title"')
        self.assertIn("title-mismatch", self.types(self.verify(path)))
        moved = self.notes / "Different Name.md"
        shutil.copy(self.notes / f"{RADO}.md", moved)
        self.assertIn("title-mismatch", self.types(self.verify(moved)))

    def test_disagreement_quote_is_checked(self) -> None:
        path = self.edit(RADO, '"it\'s okay to go all out if you work out once or twice a week',
                         '"always train to failure 6 times per week')
        self.assertIn("quote-mismatch", self.types(self.verify(path)))

    def test_scope_quantity_must_match_the_quotes(self) -> None:
        path = self.edit(SASA, '    - value: "around 10 sets of holds"\n      period: "per week"',
                         '    - value: "around 10 sets of holds"\n      period: "per session"')
        self.assertIn("scope-mismatch", self.types(self.verify(path)))

    def test_not_stated_second_speaker_counts_as_another(self) -> None:
        path = self.edit(RADO, "scope:", '  - id: vid-BjZmElNOI0g\n    url: u\n    speaker: "not stated"\n'
                                         '    channel: "SasaVenos"\nscope:')
        text = path.read_text()
        text = text.replace("## Evidence\n", "## Evidence\n\n- vid-BjZmElNOI0g (not stated): \"you want to reasonably avoid failure\"\n", 1)
        text = text.replace("## Evidence", "- Avoid failure when you train often. [src: vid-BjZmElNOI0g]\n\n## Evidence", 1)
        path.write_text(text)
        self.assertIn("mixed-speakers", self.types(self.verify(path)))


class QuoteRules(_Base):
    def test_curly_apostrophe_is_normalized(self) -> None:
        path = self.edit(SASA, "don't do more than", "don’t do more than")
        self.assertEqual(self.verify(path)["failures"], [])

    def test_case_change_fails_with_hint(self) -> None:
        path = self.edit(SASA, '"i got from 8.5 seconds', '"I got from 8.5 seconds')
        d = [f["detail"] for f in self.verify(path)["failures"] if f["type"] == "quote-mismatch"]
        self.assertTrue(d and "case, punctuation" in d[0])

    def test_dots_may_not_bridge_two_passages(self) -> None:
        path = self.edit(SASA, '"of course this is my opinion and you\'re allowed to disagree with me"',
                         '"i train rings twice per week ... of course this is my opinion"')
        self.assertIn("quote-mismatch", self.types(self.verify(path)))

    def test_bracket_rules(self) -> None:
        for bad in ("of body [bodyweight, at least]", "of body [20]", "of body [ring dips]"):
            path = self.edit(SASA, "of body [bodyweight] with maltese", bad + " with maltese")
            self.assertIn("invented-correction", self.types(self.verify(path)), bad)
            path.write_text((FIX / "notes" / f"{SASA}.md").read_text())

    def test_standard_toe_asr_correction_passes_without_widening_bracket_rules(self) -> None:
        vid = "vid-LKtqzTLYoL0"
        (self.lit / f"{vid}.md").write_text("""---
id: "vid-LKtqzTLYoL0"
kind: "video"
title: "Front Lever Leg Cues"
channel: "Radoslav Radev"
author: "Radoslav Radev"
speakers: ["Radoslav Radev"]
asr_quality: "ok"
timestamps: true
---

# Front Lever Leg Cues

[03:22] when you do front lever
[03:45] by the way so pointed or counterpointed tools it's not that important
""", encoding="utf-8")
        title = "Radoslav Radev Says Pointed Or Counterpointed Toes Are Not Important"
        path = self.tmp / f"{title}.md"
        path.write_text(f"""---
type: permanent note
created: 2026-10-06
status: expanded
tags: [calisthenics, front-lever]
sources:
  - id: {vid}
    url: https://www.youtube.com/watch?v=LKtqzTLYoL0
    speaker: "Radoslav Radev"
    channel: "Radoslav Radev"
scope:
  skill: "front lever"
  level: "not stated"
  equipment: "not stated"
  basis: "general rule"
  modality: "observation"
  quantities: []
verification: unverified
---

# {title}

In the front lever, pointed or counterpointed toe position is not that important. [src: {vid} @ 03:45]

## Details

- Pointed or counterpointed toes are not that important in the front lever. [src: {vid} @ 03:45]

## Evidence

- {vid} @ 03:22 (Radoslav Radev): "when you do front lever"
- {vid} @ 03:45 (Radoslav Radev): "by the way so pointed or counterpointed tools [toes] it's not that important"
""", encoding="utf-8")
        self.assertIn("toes", vc.VOCABULARY)
        self.assertTrue({"toe", "toes"}.issubset({w.lower() for w in asr.DEFAULT_CALISTHENICS_VOCABULARY}))
        self.assertEqual(self.verify(path)["failures"], [])

        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace("tools [toes]", "tools [notatoe]"), encoding="utf-8")
        self.assertIn("invented-correction", self.types(self.verify(path)))
        path.write_text(original.replace("tools [toes]", "tools [20]"), encoding="utf-8")
        self.assertIn("invented-correction", self.types(self.verify(path)))

    def test_timestamp_rules(self) -> None:
        path = self.edit(RADO, "- vid-KCRETdZ0l78 @ 01:50 (Radoslav Radev)", "- vid-KCRETdZ0l78 @ 03:30 (Radoslav Radev)")
        self.assertIn("timestamp-far", self.types(self.verify(path)))
        path = self.edit(RADO, "go to failure. [src: vid-KCRETdZ0l78 @ 01:50]\n- At five",
                         "go to failure. [src: vid-KCRETdZ0l78]\n- At five")
        self.assertIn("missing-timestamp", self.types(self.verify(path)))


class TranscriptMetadata(_Base):
    def test_degraded_fails(self) -> None:
        lit = self.lit / "vid-krqBQjUydGY.md"
        text = lit.read_text()
        lit.write_text(re.sub(r'asr_quality: "?ok"?', 'asr_quality: "degraded"', text))
        self.assertIn("degraded-source", self.types(self.verify(self.notes / f"{SASA}.md")))

    def test_old_lit_without_speakers_uses_channel_with_warning(self) -> None:
        """M12: an old lit file has `author`/`channel` only."""
        lit = self.lit / "vid-krqBQjUydGY.md"
        lit.write_text(re.sub(r"(?m)^speakers:.*\n", "", lit.read_text()))
        rep = self.verify(self.notes / f"{SASA}.md")
        self.assertEqual(rep["failures"], [])
        self.assertIn("transcript-speakers-derived", {w["type"] for w in rep["warnings"]})

    def test_registry_resolves_other_lit_folders(self) -> None:
        other = self.tmp / "staging_b" / "lit"
        other.mkdir(parents=True)
        shutil.move(str(self.lit / "vid-krqBQjUydGY.md"), other / "vid-krqBQjUydGY.md")
        (self.tmp / "staging_a" / "lit").mkdir(parents=True)
        (self.tmp / "lit_dirs.txt").write_text("staging_b/lit\n")
        dirs = vc.lit_dirs(self.tmp / "staging_a")
        rep = vc.verify_note(self.notes / f"{SASA}.md", vc.LitIndex(dirs))
        self.assertEqual(rep["failures"], [])


class PackageARound2(_Base):
    """asr_quality partial with damaged spans, transcripts.json, the vocabulary loader."""

    def test_quote_in_damaged_time_span_fails(self) -> None:
        lit = self.lit / "vid-KCRETdZ0l78.md"
        text = re.sub(r'asr_quality: "?ok"?', 'asr_quality: "partial"', lit.read_text(), count=1)
        text = text.replace("---\n", '---\nasr_span_unit: "timestamp"\nasr_damaged_spans: [["01:45", "01:55"]]\n', 1)
        lit.write_text(text)
        self.assertIn("quote-in-damaged-span", self.types(self.verify(self.notes / f"{RADO}.md")))

    def test_span_without_unit_is_a_warning(self) -> None:
        lit = self.lit / "vid-krqBQjUydGY.md"
        lit.write_text(lit.read_text().replace("---\n", '---\nasr_damaged_spans: ["lines 30-40"]\n', 1))
        rep = self.verify(self.notes / f"{SASA}.md")
        self.assertEqual(rep["failures"], [])
        self.assertIn("span-unit-unknown", {w["type"] for w in rep["warnings"]})

    def test_transcripts_json_names_the_canonical_file(self) -> None:
        stg = self.tmp / "stg"
        (stg / "lit" / "_superseded").mkdir(parents=True)
        shutil.copy(self.lit / "vid-krqBQjUydGY.md", stg / "lit" / "_superseded" / "vid-krqBQjUydGY.small.md")
        (stg / "lit" / "vid-krqBQjUydGY.md").write_text("---\nid: vid-krqBQjUydGY\n---\n\nother words\n")
        (stg / "transcripts.json").write_text(json.dumps({"items": {"vid-krqBQjUydGY": {
            "lit_note": "lit/_superseded/vid-krqBQjUydGY.small.md"}}}))
        idx = vc.LitIndex([stg / "lit"])
        self.assertNotIn("vid-krqBQjUydGY", idx.paths)  # never resolve against _superseded
        (stg / "transcripts.json").write_text(json.dumps({"items": {"vid-krqBQjUydGY": {
            "lit_note": "lit/vid-krqBQjUydGY.md"}}}))
        self.assertEqual(vc.LitIndex([stg / "lit"]).paths["vid-krqBQjUydGY"], stg / "lit" / "vid-krqBQjUydGY.md")

    def test_vocabulary_from_package_a_is_loaded(self) -> None:
        self.assertIn("gymnastic rings", vc.VOCABULARY)


HONEST = ROOT / "tests" / "fixtures" / "zettel_honest"


def _note_with_quote(vid: str, speaker: str, quote: str, channel: str = "sthenics_") -> str:
    return (f"---\ntype: permanent note\nsources:\n  - id: {vid}\n    url: u\n    speaker: \"{speaker}\"\n"
            f"    channel: \"{channel}\"\nscope:\n  skill: \"not stated\"\n  level: \"not stated\"\n"
            "  equipment: \"not stated\"\n  basis: \"general rule\"\n  modality: \"observation\"\n"
            f"  quantities: []\nverification: unverified\n---\n\n# Q\n\nThe speaker says it. [src: {vid}]\n\n"
            f"## Details\n\n- The speaker says it. [src: {vid}]\n\n## Evidence\n\n- {vid} ({speaker}): \"{quote}\"\n")


class DamagedSpansRealFile(unittest.TestCase):
    """M-b with the real vid-OYFU5AyLl9w (span = lit body lines 190-201 = transcript 187-198)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(HONEST / "lit" / "vid-OYFU5AyLl9w.md", self.tmp)
        text = (self.tmp / "vid-OYFU5AyLl9w.md").read_text()
        self.body = text.split("\n---", 1)[1].split("\n")[1:]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def check(self, quote: str) -> set[str]:
        p = self.tmp / "Q.md"
        p.write_text(_note_with_quote("vid-OYFU5AyLl9w", "sthenics_", quote))
        return {f["type"] for f in vc.verify_note(p, vc.LitIndex([self.tmp])).get("failures", [])}

    def test_line_inside_the_loop_fails(self) -> None:
        self.assertIn("quote-in-damaged-span", self.check(self.body[194].strip()))  # body line 195

    def test_clean_lines_around_the_loop_pass(self) -> None:
        for n in (186, 189, 202):
            self.assertNotIn("quote-in-damaged-span", self.check(self.body[n - 1].strip()), n)

    def test_clean_part_of_a_boundary_line_passes(self) -> None:
        p = self.tmp / "vid-OYFU5AyLl9w.md"
        line = self.body[189]  # body line 190, the first line of the span
        cut = line.index(" ", len(line) // 2)
        p.write_text(re.sub(r'"start_line": 190, "start_char": 0', f'"start_line": 190, "start_char": {cut + 1}',
                            p.read_text()))
        self.assertNotIn("quote-in-damaged-span", self.check(line[:cut].strip()))
        self.assertIn("quote-in-damaged-span", self.check(line[cut + 1:].strip()))


class HonestNotes(unittest.TestCase):
    """M-a: the 12 hand-written honest notes of the round-2 review pass the check."""

    def test_all_twelve_pass(self) -> None:
        lit = vc.LitIndex([HONEST / "lit"])
        notes = sorted((HONEST / "notes").glob("*.md"))
        self.assertEqual(len(notes), 12)
        for p in notes:
            rep = vc.verify_note(p, lit)
            fails = rep["failures"]
            if p.stem.startswith("A Speaker In "):
                # Round 15 (pilot 6, item 3): a vague speaker in a one-speaker video is now a
                # failure; this older fixture keeps its text and shows exactly that one failure.
                self.assertEqual([f["type"] for f in fails], ["vague-speaker"], p.stem)
                continue
            self.assertEqual(fails, [], p.stem)


class Round4Rules(_Base):
    def test_quantities_missing(self) -> None:
        path = self.edit(RADO, self._quantities_block(RADO), "  quantities: []\n")
        self.assertIn("quantities-missing", self.types(self.verify(path)))

    def _quantities_block(self, title: str) -> str:
        text = (self.notes / f"{title}.md").read_text()
        a = text.index("  quantities:\n")
        b = text.index("verification:", a)
        return text[a:b]

    def test_added_qualifier_is_a_warning(self) -> None:
        path = self.edit(SASA, "He did around 10 sets of holds per week.", "He did only around 10 sets of holds per week.")
        rep = self.verify(path)
        self.assertNotIn("changed-qualifier", self.types(rep))
        path = self.edit(SASA, "trained rings twice per week", "trained rings at least twice per week")
        rep = self.verify(path)
        self.assertIn("added-qualifier", {w["type"] for w in rep["warnings"]})
        self.assertEqual(rep["failures"], [])


class SpeakerNames(_Base):
    def test_owner_name_matches_channel_title(self) -> None:
        lit = self.lit / "vid-KCRETdZ0l78.md"
        lit.write_text(re.sub(r"(?m)^speakers:.*\n", "", lit.read_text()))  # old file: author only
        rep = self.verify(self.notes / f"{RADO}.md")
        self.assertNotIn("wrong-speaker", self.types(rep))


PILOT = ROOT / "tests" / "fixtures" / "zettel_pilot"
# Published pilot notes that miss scope.quantities (round 4 rule quantities-missing).
# Real pilot-1 output whose scope value is in no Evidence quote and not in the quote's
# sentence +-1 sentence (round 7 window): "planche" only in the video title; "rings" not said.
PILOT_SCOPE_NOT_IN_QUOTE = {
    "Radoslav Radev Requires Pressing The Parallettes Together And Pushing Forward And Down At The Same Time For Planche",
    "SasaVenos Observes That Isometric Skills Like Maltese And Iron Cross Are Isometric Portions Of Full Range Of Motion Skills",
}
PILOT_QUANTITIES_MISSING = {
    "Radoslav Radev Requires Breathing During Planche Holds After Reaching Five Seconds On Each Progression",
    "Radoslav Radev Recommends Having A Strategy And Structure For Planche Progression Timing",
    "SasaVenos Observes That Volume And Intensity Have An Inverse Relationship In Calisthenics Training",
}


class PilotNotes(unittest.TestCase):
    """The 24 notes that the real-model pilot published (flash 11, pro 13)."""

    def test_published_notes(self) -> None:
        lit = vc.LitIndex([PILOT / "lit"])
        notes = sorted((PILOT / "flash").glob("*.md")) + sorted((PILOT / "pro").glob("*.md"))
        self.assertEqual(len(notes), 24)
        for p in notes:
            types = {f["type"] for f in vc.verify_note(p, lit)["failures"]}
            want = ({"quantities-missing"} if p.stem in PILOT_QUANTITIES_MISSING else set()) \
                | ({"scope-not-in-quote"} if p.stem in PILOT_SCOPE_NOT_IN_QUOTE else set())
            self.assertEqual(types, want, p.stem)

    def test_spoken_number_forms_of_the_two_pro_notes(self) -> None:
        """'50, 60 push-ups' (the form the old checker forced) and 'two injuries' pass."""
        lit = vc.LitIndex([PILOT / "lit"])
        for stem in ("Radoslav Radev Recommends Minimum 15 Pull-Ups, Minimum 20 Dips And 30 Push-Ups In A Row",
                     "Radoslav Radev Predicts That Injuries From Skipping Progressions Waste One Year"):
            p = next((PILOT / "pro").glob(stem + "*.md"))
            self.assertEqual(vc.verify_note(p, lit)["failures"], [], stem)


class Quantities(unittest.TestCase):
    def test_each_object_does_not_mask_a_recurring_period(self) -> None:
        quote = "every month you go down one rep or two reps for each exercise"
        quantities = vc.extract_quantities(quote)
        reps = next(q for q in quantities if q.values == (1.0, 2.0))
        self.assertEqual((reps.unit, reps.period), ("rep", "per month"))

    def test_each_shorthand_and_period_nouns_remain_supported(self) -> None:
        shorthand = vc.extract_quantities("one hour each")[0]
        self.assertEqual(shorthand.period, "each")
        for text, period in (("one hour each session", "per session"),
                             ("five reps each set", "per set"),
                             ("one second each rep", "per rep")):
            with self.subTest(text=text):
                self.assertEqual(vc.extract_quantities(text)[0].period, period)

    def test_monthly_context_does_not_bind_across_sentence_or_clause(self) -> None:
        for text in ("Every month we review training. One hour each exercise.",
                     "Every month we review training, and separately one hour each exercise."):
            with self.subTest(text=text):
                amount = next(q for q in vc.extract_quantities(text) if q.values == (1.0,))
                self.assertIsNone(amount.period)

    def test_spoken_alternatives_are_equal(self) -> None:
        def vals(t: str) -> list:
            return [(q.values, q.unit) for q in vc.extract_quantities(t)]
        for a, b in (("one two times per week", "one or two times per week"), ("50 60 push-ups", "50 or 60 push-ups"),
                     ("50 60 push-ups", "50, 60 push-ups"), ("50 60 push-ups", "50 to 60 push-ups"),
                     ("50 60 push-ups", "50-60 push-ups"),
                     ("6, 7, 8, 9 seconds, 10 seconds", "6, 7, 8, 9 or 10 seconds"),
                     ("6, 7, 8, 9 seconds, 10 seconds", "6, 7, 8, 9, or 10 seconds"),
                     ("seven seconds or five seconds", "five to seven seconds")):
            self.assertEqual(vals(a), vals(b), (a, b))
        # Different units are never merged; a descending list with a range is not one list.
        self.assertEqual(len(vc.extract_quantities("20 to 30 reps or 30 to 40 seconds")), 2)
        self.assertEqual(len(vc.extract_quantities("only four weeks, or two or three weeks")), 2)

    def test_inherited_unit_is_not_dropped(self) -> None:
        quote = vc.extract_quantities("six months that's one year and you have two injuries that holding you back")
        claim = vc.extract_quantities("two injuries holding you back")
        self.assertEqual(vc.match_quantity(claim[-1], quote, strict_form=True)[0], "supported")


    def test_qualifier_pairs_ignore_small_words(self) -> None:
        a = vc.extract_quantities("a minimum of 10 seconds")[0]
        b = vc.extract_quantities("minimum 10 seconds")[0]
        self.assertEqual(a.quals, b.quals)
        self.assertEqual(vc.extract_quantities("only for four weeks")[0].quals, frozenset({"only"}))

    def test_added_limit_word_is_left_to_review(self) -> None:
        claim = vc.extract_quantities("more than 10 reps or 15")
        quote = vc.extract_quantities("more than 10 reps 15")
        self.assertEqual(vc.match_quantity(claim[-1], quote)[0], "supported")

    def test_spoken_range(self) -> None:
        self.assertEqual(vc.extract_quantities("one two times per week")[0].values, (1.0, 2.0))

    def test_episode_number_is_a_name(self) -> None:
        self.assertEqual(vc.extract_quantities("Debunking Calisthenics Myths Ep.7 Podcast"), [])


    def q(self, text: str) -> list[dict]:
        return [x.as_dict() for x in vc.extract_quantities(text)]

    def test_period_binds_within_the_clause(self) -> None:
        got = self.q("With 15 seconds on pike straddle and four times per week to train")
        self.assertEqual([g["period"] for g in got], [None, "per week"])

    def test_coordinated_quantities_share_period_and_limit(self) -> None:
        got = self.q("don't do more than 20 to 30 reps or 30 to 40 seconds of volume per session")
        self.assertEqual([(g["period"], g["qualifiers"]) for g in got],
                         [("per session", ["upper"]), ("per session", ["upper"])])

    def test_frame_quantity(self) -> None:
        got = self.q("if you train four times per week only one maximum two to go to a failure")
        self.assertEqual(got[1]["values"], ["1", "2"])
        self.assertEqual((got[1]["unit"], got[1]["period"], got[1]["ref"]), ("times", "per week", ["4"]))

    def test_one_of_is_kept_and_pronoun_is_not(self) -> None:
        self.assertEqual(self.q("one of the best cues is to lock the elbows"), [])
        self.assertEqual(self.q("only one of the four weekly workouts")[0]["values"], ["1"])

    def test_day_of_the_week_is_recurring_not_one_off(self) -> None:
        recurring = vc.extract_quantities("at least one day of the week")[0]
        one_off = vc.extract_quantities("at least one day this week")[0]
        self.assertEqual(recurring.period, "per week")
        self.assertEqual(one_off.period, "this week")
        self.assertEqual(vc.match_quantity(one_off, [recurring])[0], "changed-period")
        self.assertEqual(vc.extract_quantities("the first day of the week"), [])

    def test_every_n_weeks(self) -> None:
        self.assertEqual(self.q("deload with 3 sets every 2 weeks")[0]["period"], "every 2 weeks")

    def test_one_off_time_windows_remain_distinct_from_recurrence(self) -> None:
        quote = vc.extract_quantities("five seconds more this week and 10 reps in these two weeks")
        claim = vc.extract_quantities("five seconds more this week and 10 reps these two weeks")
        self.assertEqual([q.period for q in quote], ["this week", "these two weeks"])
        self.assertEqual([vc.match_quantity(q, quote)[0] for q in claim], ["supported", "supported"])
        recurring = vc.extract_quantities("10 reps every two weeks")[0]
        fabricated = vc.extract_quantities("10 reps these three weeks")[0]
        self.assertEqual(vc.match_quantity(recurring, quote)[0], "changed-period")
        self.assertEqual(vc.match_quantity(fabricated, quote)[0], "changed-period")
        scoped = vc.extract_quantities("you have only five so say to yourself these two weeks i will reach 10")
        self.assertEqual(scoped[0].period, None)
        self.assertEqual(scoped[-1].period, "these two weeks")

    def test_rate_does_not_leak_across_totaling_clause(self) -> None:
        source = vc.extract_quantities("four times per week that's four weeks one month so four weeks "
                                       "four days per four days per week that means 16 workouts")
        total = source[-1]
        self.assertEqual((total.values, total.unit, total.period), ((16.0,), "times", "over four weeks"))
        self.assertEqual(vc.match_quantity(vc.extract_quantities("16 workouts over four weeks")[0], [total])[0],
                         "supported")
        self.assertEqual(vc.match_quantity(vc.extract_quantities("16 workouts per week")[0], [total])[0],
                         "changed-period")
        self.assertEqual(vc.match_quantity(vc.extract_quantities("16 workouts over three weeks")[0], [total])[0],
                         "changed-period")
        untied = vc.extract_quantities("four weeks of training and 16 workouts")[-1]
        self.assertEqual(untied.period, None)
        self.assertEqual(vc.match_quantity(vc.extract_quantities("16 workouts over four weeks")[0], [untied])[0],
                         "changed-period")

    def test_spoken_repeated_unit_run_keeps_all_values(self) -> None:
        quote = vc.extract_quantities("you rest one day two days even if you rest three days")
        claim = vc.extract_quantities("rest one day, two days, or even three days")
        mismatch = vc.extract_quantities("rest one day, two days, or even four days")
        omitted = vc.extract_quantities("rest one day or three days")
        self.assertEqual(len(quote), 1)
        self.assertEqual(quote[0].values, (1.0, 2.0, 3.0))
        self.assertEqual(vc.match_quantity(claim[0], quote)[0], "supported")
        self.assertEqual(vc.match_quantity(mismatch[0], quote)[0], "range-mismatch")
        self.assertEqual(vc.match_quantity(omitted[0], quote)[0], "range-mismatch")
        separate_roles = vc.extract_quantities("rest one day, work two days")
        self.assertEqual([q.values for q in separate_roles], [(1.0,), (2.0,)])


class HeldEachMonthNote(_Base):
    def test_actual_held_note_passes_with_monthly_period_from_quote(self) -> None:
        source = ROOT / "tests" / "fixtures" / "pilot5_false_positives" / "lit"
        note = ROOT / "tests" / "fixtures" / "each_month" / "notes" / (
            "Sasha Says Rep-Range Choice Depends On Training Phase And Logged Response.md"
        )
        report = vc.verify_note(note, vc.LitIndex([source]), vc.Options())
        self.assertEqual(report["failures"], [], report)


class SourceWindowValidation(_Base):
    def setUp(self) -> None:
        super().setUp()
        transcript = self.lit / "vid-KCRETdZ0l78.md"
        raw = transcript.read_text()
        header, _ = raw.split("\n---\n", 1)
        transcript.write_text(header + "\n---\n\n# Synthetic Window Example\n\n"
                              "[00:00] This week I will add five seconds more.\n"
                              "[00:05] In these two weeks I will reach 10 reps.\n")

    def write_note(self, second_claim: str, second_period: str) -> Path:
        title = "Radoslav Radev Predicts Short-Term Training Goals"
        path = self.notes / f"{title}.md"
        path.write_text(f'''---
type: permanent note
created: 2026-10-05
status: expanded
tags: [calisthenics, training, zettelkasten, permanent-note]
sources:
  - id: vid-KCRETdZ0l78
    url: https://www.youtube.com/watch?v=KCRETdZ0l78
    speaker: "Radoslav Radev"
    channel: "Radoslav Radev ⎮ Calisthenics Mastery"
scope:
  skill: "not stated"
  level: "not stated"
  equipment: "not stated"
  basis: "speaker's own practice"
  modality: "prediction"
  quantities:
    - value: "five seconds more"
      period: "this week"
    - value: "10 reps"
      period: "{second_period}"
verification: unverified
---

# {title}

Radoslav Radev predicts short-term training goals. [src: vid-KCRETdZ0l78 @ 00:00]

## Details

- He says he will add five seconds more this week. [src: vid-KCRETdZ0l78 @ 00:00]
- {second_claim} [src: vid-KCRETdZ0l78 @ 00:05]

## Evidence

- vid-KCRETdZ0l78 @ 00:00 (Radoslav Radev): "This week I will add five seconds more."
- vid-KCRETdZ0l78 @ 00:05 (Radoslav Radev): "In these two weeks I will reach 10 reps."

## Connected Ideas

- [[Home]] — start page of the notes.
''')
        return path

    def test_literal_one_off_windows_pass_full_source_validation(self) -> None:
        path = self.write_note("He says he will reach 10 reps in these two weeks.", "these two weeks")
        self.assertEqual(self.verify(path)["failures"], [])

    def test_recurring_or_fabricated_window_fails_source_validation(self) -> None:
        for claim, period in (("He says he will reach 10 reps every two weeks.", "every two weeks"),
                              ("He says he will reach 10 reps in these three weeks.", "these three weeks")):
            path = self.write_note(claim, period)
            self.assertIn("changed-period", self.types(self.verify(path)), (claim, period))



class ActualF178WindowValidation(_Base):
    def write_actual_note(self, second_claim: str, second_period: str) -> Path:
        title = "Radoslav Radev Describes A Weekly Training Rate And A Four-Week Total"
        path = self.notes / f"{title}.md"
        path.write_text(f'''---
type: permanent note
created: 2026-10-05
status: expanded
tags: [calisthenics, planche, zettelkasten, permanent-note]
sources:
  - id: vid-yM5j7jHhKLA
    url: https://www.youtube.com/watch?v=yM5j7jHhKLA
    speaker: "Radoslav Radev"
    channel: "Radoslav Radev ⎮ Calisthenics Mastery"
scope:
  skill: "not stated"
  level: "not stated"
  equipment: "not stated"
  basis: "speaker's own practice"
  modality: "observation"
  quantities:
    - value: "four times"
      period: "per week"
    - value: "16 workouts"
      period: "{second_period}"
verification: unverified
---

# {title}

Radoslav Radev describes a weekly training rate and a four-week total. [src: vid-yM5j7jHhKLA @ 01:32]

## Details

- He says they train four times per week. [src: vid-yM5j7jHhKLA @ 01:32]
- {second_claim} [src: vid-yM5j7jHhKLA @ 01:32]

## Evidence

- vid-yM5j7jHhKLA @ 01:32 (Radoslav Radev): "four times per week that's four weeks one month so four weeks four days per four days per week that means 16 workouts"

## Connected Ideas

- [[Home]] — start page of the notes.
''')
        return path

    def test_actual_f178_separates_frequency_from_total_window(self) -> None:
        path = self.write_actual_note("He says the calculation totals 16 workouts over four weeks.",
                                      "over four weeks")
        lit = ROOT / "tests" / "fixtures" / "time_window" / "lit"
        self.assertEqual(vc.verify_note(path, vc.LitIndex([lit]))["failures"], [])

    def test_actual_f178_rejects_recurring_or_wrong_total_period(self) -> None:
        lit = ROOT / "tests" / "fixtures" / "time_window" / "lit"
        for claim, period in (("He says the calculation totals 16 workouts per week.", "per week"),
                              ("He says the calculation totals 16 workouts over three weeks.",
                               "over three weeks")):
            path = self.write_actual_note(claim, period)
            self.assertIn("changed-period", self.types(vc.verify_note(path, vc.LitIndex([lit]))), claim)



class ActualWeeklyDayValidation(_Base):
    def write_note(self, claim_period: str, scope_period: str) -> Path:
        title = "SasaVenos Reports Trying To Leave A Day Without Calisthenics"
        path = self.notes / f"{title}.md"
        claim = f"at least one day {claim_period} where he does nothing, usually Sunday"
        path.write_text(f'''---
type: permanent note
created: 2026-10-05
status: expanded
tags: [calisthenics, rest-days, zettelkasten, permanent-note]
sources:
  - id: vid-ozKrJzJNL24
    url: https://www.youtube.com/watch?v=ozKrJzJNL24
    speaker: "SasaVenos"
    channel: "SasaVenos"
scope:
  skill: "not stated"
  level: "not stated"
  equipment: "not stated"
  basis: "speaker's own practice"
  modality: "observation"
  quantities:
    - value: "at least one day"
      period: "{scope_period}"
verification: unverified
---

# {title}

SasaVenos reports trying to take a day away from calisthenics. [src: vid-ozKrJzJNL24 @ 10:37]

## Details

- He tries to leave {claim} [src: vid-ozKrJzJNL24 @ 10:37]

## Evidence

- vid-ozKrJzJNL24 @ 10:37 (SasaVenos): "but still I try to leave at least one day of the week at least where I do not do anything usually that's on Sunday I do it not so much for the physical reasons but more psychological one"

## Connected Ideas

- [[Home]] — start page of the notes.
''')
        return path

    def test_actual_week_phrase_is_weekly_cadence(self) -> None:
        path = self.write_note("of the week", "per week")
        lit = ROOT / "tests" / "fixtures" / "time_window" / "lit"
        self.assertEqual(vc.verify_note(path, vc.LitIndex([lit]))["failures"], [])

    def test_actual_weekly_cadence_rejects_one_off_this_week(self) -> None:
        path = self.write_note("this week", "this week")
        lit = ROOT / "tests" / "fixtures" / "time_window" / "lit"
        self.assertIn("changed-period", self.types(vc.verify_note(path, vc.LitIndex([lit]))))



class LegacyAndCli(_Base):
    LEGACY = ("---\ntype: permanent note\nverification: corroborated\n---\n\n# Old Note\n\n"
              "Train rings twice per week.\n\n## Details\n\n- Around 10 sets of holds per week.\n"
              "- Rest 7 minutes.\n\n## Grounded Example\n\nHe does 3 sets.\n")

    def test_legacy_note_is_reported(self) -> None:
        p = self.notes / "Old Note.md"
        p.write_text(self.LEGACY)
        rep = vc.verify_note(p, vc.LitIndex([self.lit]), legacy_sources=["vid-krqBQjUydGY"])
        self.assertEqual(rep["status"], "not-contract")
        self.assertEqual((rep["approx_numbers"]["numbers"], rep["approx_numbers"]["numbers_found"]), (3, 2))

    def test_write_and_exit_code(self) -> None:
        bad = self.edit(SASA, "He did around 10 sets of holds per week.", "He did around 10 sets of holds each.")
        out = self.tmp / "report.json"
        env = {k: v for k, v in os.environ.items() if k not in ("ZR_AGENT", "ZR_STAGING", "ZR_LIT_DIRS")}
        proc = subprocess.run([sys.executable, str(ZR / "verify_claims.py"), str(self.notes), "--lit", str(self.lit),
                               "--write", "--out", str(out)], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("verification: failed", bad.read_text())
        self.assertIn("verification: quote-checked", (self.notes / f"{RADO}.md").read_text())
        self.assertEqual(json.loads(out.read_text())["summary"]["by_status"], {"failed": 1, "quote-checked": 3})


# --------------------------------------------------------------------------- #
# The round-1 reviewer's attack table (attack/run.py and attack/extra.py).
# Expected results are the reviewer's, except the rows listed in DEVIATIONS.
# --------------------------------------------------------------------------- #

DEVIATIONS = {
    # The fixture's title has numbers that no Evidence quote of the note holds.
    "R9 ": "fail",
    # Round 3: low-quote-support is a warning for the LLM source-check review, not a
    # failure. K10 (a reversed claim without a number) passes the MECHANICAL check with
    # that warning; the review step before publication judges it (test_run_integrity
    # ReviewBeforePublish). N1/N2 now pass, as the reviewer expected.
    "K10 ": "pass",
    # Contract section 11: case must match.
    "K18 ": "fail",
    # "around 10 sets" with "30 to 40 seconds" lacking "no more than".
    "K28 ": "fail",
    # Contract: H:MM:SS from one hour on; "61:25" is the wrong form.
    "H2 ": "fail",
    # Round 11 (coordinator decision after pilot 5): "twice" == "two times".
    "K21 ": "pass",
}


class AttackTable(unittest.TestCase):
    def run_table(self, script: str) -> list[tuple[str, str, str]]:
        tmp = Path(tempfile.mkdtemp(prefix="attack-"))
        try:
            shutil.copytree(ATTACK, tmp, dirs_exist_ok=True)
            (tmp / "repo").mkdir()
            (tmp / "repo" / "zettel_ralph").symlink_to(ZR)
            env = {k: v for k, v in os.environ.items() if k not in ("ZR_STAGING", "ZR_LIT_DIRS", "ZR_AGENT")}
            out = subprocess.run([sys.executable, str(tmp / "attack" / script)], capture_output=True, text=True,
                                 cwd=tmp, env=env)
            self.assertEqual(out.returncode, 0, out.stderr[-2000:])
            rows = []
            for line in out.stdout.splitlines():
                m = re.match(r"^(pass|fail)\s+\(expect ([^)]*?)\s*\) (.+?) \[", line)
                if m:
                    rows.append((m.group(1), m.group(2), m.group(3)))
            return rows
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def check(self, rows: list[tuple[str, str, str]]) -> None:
        self.assertGreater(len(rows), 10)
        for got, expect, name in rows:
            want = next((v for k, v in DEVIATIONS.items() if name.lstrip(" )").startswith(k)), None)
            if want is None:
                if expect.startswith("?"):
                    continue
                want = "pass" if expect.startswith("pass") else "fail"
            self.assertEqual(got, want, f"{name}: got {got}, expected {want} (reviewer: {expect})")

    def test_run_table(self) -> None:
        self.check(self.run_table("run.py"))

    def test_extra_table(self) -> None:
        self.check(self.run_table("extra.py"))


if __name__ == "__main__":
    unittest.main()
