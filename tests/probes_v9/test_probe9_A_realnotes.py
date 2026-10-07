"""Probe 9 A (reviewer A, round 4): render + check over 30 real pilot-8 notes
(tests/unit/fixtures_probe9/, copied from vaultB_before_repair: permanent notes with 1 to
21 bullets, `**bold**`, `[[links]]` and `|` in bullets, maps with tables, review notes,
Home, _trash_ and _archived notes).

Convention of THIS file (a measurement, not a defect probe): PASS = NO defect. Each note
renders a stub that the check accepts (no false refusal), the claim entries equal the old
Details bullets in order, and the quoted old text equals the other old body lines in order
(a stricter test than check(), which uses a set). The same sweep over all 476 notes of
vaultB_before_repair: 0 refusals, 0 differences."""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.unit.test_run_integrity import ri  # noqa: F401  (puts zettel_ralph on sys.path)

import stub_check as sc  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_probe9"


class RealNotes(unittest.TestCase):
    def test_every_fixture_renders_a_clean_lossless_stub(self) -> None:
        files = sorted(FIX.glob("*.md"))
        self.assertGreaterEqual(len(files), 30)
        for f in files:
            with self.subTest(note=f.name):
                old = f.read_text(encoding="utf-8")
                _h1, bullets, other = sc.old_parts(old)
                stub = sc.render(old, f.stem, ["open: not yet processed"] * len(bullets), [], [], "retired/r/u/x.md")
                self.assertEqual(sc.check(old, stub, None), [])
                self.assertEqual([e[0] for e in sc.claim_entries(stub)], bullets)
                body = sc.split(stub)[1]
                quoted = body.split(sc.OLD_TEXT_HEADING, 1)[1] if sc.OLD_TEXT_HEADING in body else ""
                got = [ln[2:] if ln.startswith("> ") else ln[1:] for ln in quoted.splitlines() if ln.startswith(">")]
                self.assertEqual([x for x in got if x.strip()], other)


if __name__ == "__main__":
    unittest.main()
