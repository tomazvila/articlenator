"""Regression for preserving complete source-check review item text."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ZR = ROOT / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

import run_integrity as ri  # noqa: E402


class ReviewPacketFullText(unittest.TestCase):
    def test_long_scope_claim_and_evidence_round_trip_without_truncation(self) -> None:
        skill = "planche progression " * 35 + "final scope marker"
        quantities = [
            {"value": f"{n} workouts", "period": "per week" if n == 16 else "not stated"}
            for n in range(10, 26)
        ]
        quantities.append({"value": "16 workouts at the final step", "period": "per week"})
        scope = {
            "skill": skill,
            "level": "intermediate",
            "equipment": "parallel bars and floor",
            "basis": "speaker's own practice",
            "modality": "recommendation",
            "quantities": quantities,
        }
        scope_yaml = json.dumps(scope, ensure_ascii=False)
        claim_tail = "only when all 16 workouts are completed in that week. [src: vid-long @ 04:56]"
        claim = "- He recommends " + ("steady planche progression " * 30) + claim_tail
        evidence_tail = "and never skip the final four weekly workouts."
        evidence = (
            "- vid-long @ 04:56 (Coach): \""
            + ("marked source words " * 30)
            + evidence_tail
            + "\""
        )
        note = (
            "---\n"
            "type: permanent note\n"
            "scope: " + scope_yaml + "\n"
            "---\n\n"
            "# A Long Source-Checked Claim\n\n"
            "A lead sentence.\n\n"
            "## Details\n\n"
            + claim
            + "\n\n## Evidence\n\n"
            + evidence
            + "\n\n"
        )
        before_hash = ri.reviewed_hash(note)

        skeleton = ri.review_skeleton("01 Permanent Notes/Long.md", note)
        decoded = json.loads(json.dumps(skeleton, ensure_ascii=False))
        by_name = {item["item"]: item for item in decoded["items"]}
        scope_text = by_name["scope"]["text"]
        claim_item = by_name["details-1"]
        evidence_item = by_name["evidence-1"]

        self.assertGreater(len(scope_text), 400)
        self.assertEqual(json.loads(scope_text), scope)
        self.assertEqual(claim_item["text"], claim[2:])
        self.assertIn(claim_tail, claim_item["text"])
        self.assertGreater(len(claim_item["text"]), 400)
        self.assertEqual(claim_item["tag"], "vid-long @ 04:56")
        self.assertEqual(evidence_item["text"], evidence[2:])
        self.assertIn(evidence_tail, evidence_item["text"])
        self.assertGreater(len(evidence_item["text"]), 400)
        self.assertEqual(evidence_item["tag"], "vid-long @ 04:56")
        self.assertEqual(list(by_name), ["title", "scope", "lead", "details-1", "evidence-1"])
        self.assertTrue(all(set(item) == {"item", "text", "tag", "verdict", "also", "transcript_text", "problem"}
                            for item in decoded["items"]))
        self.assertEqual(ri.reviewed_hash(note), before_hash)
        self.assertEqual(note.count("only when all 16 workouts"), 1)


if __name__ == "__main__":
    unittest.main()
