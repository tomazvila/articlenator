"""The `repairs:` header field and an old-note file name that holds a comma."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "zettel_ralph"))
import synth_call as sc  # noqa: E402


def _old_of(names):
    by_name = {Path(r).name: r for r in names}
    by_stem = {Path(r).stem: r for r in names}

    def old_of(name):
        name = name.strip()
        return by_name.get(name) or by_name.get(name + ".md") or by_stem.get(name)
    return old_of


class RejoinRepairs(unittest.TestCase):
    A = "01 Permanent Notes/Protraction Increases Range Of Motion, Making Pull-Ups Harder.md"
    B = "01 Permanent Notes/Plain Old Note.md"
    C = "01 Permanent Notes/Sequence Of Unlock, Consistency, Hold Time, Form, And Variety.md"

    def header(self, text):
        return sc._header("New Title | repairs: " + text + " | bullets: b1,b2")[1]["repairs"]

    def test_name_with_one_comma_is_one_old_note(self):
        parts = self.header(Path(self.A).name)
        self.assertEqual(len(parts), 2)  # the header parser splits it
        got = sc._rejoin_repairs(parts, _old_of([self.A, self.B]))
        self.assertEqual(got, [Path(self.A).name])

    def test_name_with_four_commas(self):
        got = sc._rejoin_repairs(self.header(Path(self.C).name), _old_of([self.C]))
        self.assertEqual(got, [Path(self.C).name])

    def test_two_old_notes_one_with_a_comma(self):
        got = sc._rejoin_repairs(self.header(Path(self.B).name + ", " + Path(self.A).name), _old_of([self.A, self.B]))
        self.assertEqual(got, [Path(self.B).name, Path(self.A).name])

    def test_stem_without_extension(self):
        got = sc._rejoin_repairs(self.header(Path(self.A).stem), _old_of([self.A]))
        self.assertEqual(_old_of([self.A])(got[0]), self.A)

    def test_plain_names_are_unchanged(self):
        parts = self.header("Plain Old Note.md, Other Note.md")
        self.assertEqual(sc._rejoin_repairs(parts, _old_of([self.B])), parts)

    def test_unknown_pieces_are_not_joined(self):
        parts = self.header("Unknown One, Unknown Two.md")
        self.assertEqual(sc._rejoin_repairs(parts, _old_of([self.B])), parts)

    def test_empty(self):
        self.assertEqual(sc._rejoin_repairs([], _old_of([self.B])), [])


if __name__ == "__main__":
    unittest.main()
