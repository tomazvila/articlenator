"""Probe 9 A (reviewer A, round 4): note shapes against stub_check.render + stub_check.check.

Convention of THIS file: every test asserts the DEFECT. PASS = the defect is present.
FAIL = the defect is gone (or the setup changed). No LLM, no network, no vault."""
from __future__ import annotations

import unittest

from tests.unit.test_run_integrity import ri  # noqa: F401  (puts zettel_ralph on sys.path)

import stub_check as sc  # noqa: E402
import verify_claims as vc  # noqa: E402

FM = "---\ntype: permanent note\ncreated: 2026-09-12\nstatus: expanded\nverification: unverified\ntags:\n  - planche\n---\n"


def stub_of(old: str) -> str:
    _h1, bullets, _o = sc.old_parts(old)
    return sc.render(old, "T", ["open: x"] * len(bullets), [], [], "retired/r/u/T.md")


class LostTextPassesTheCheck(unittest.TestCase):
    def test_A2_no_frontmatter_but_first_line_is_a_rule(self) -> None:
        """X-A2: a note without frontmatter whose first line is `---` (a rule). split() takes
        the text up to the next `---` line as frontmatter. That text is not in the stub, and
        check() is clean (render and check use the same split)."""
        old = "---\nThe lead paragraph that the stub loses.\n---\n# T\n\nBody.\n\n## Details\n\n- b1\n"
        stub = stub_of(old)
        self.assertEqual(sc.check(old, stub, None), [])
        self.assertNotIn("The lead paragraph that the stub loses.", stub)

    def test_A10_frontmatter_is_not_carried(self) -> None:
        """X-A10: the old frontmatter (real notes: tags, created, status) is in no part of
        the stub; check() is clean."""
        old = FM + "\n# T\n\nLead.\n\n## Details\n\n- b1\n"
        stub = stub_of(old)
        self.assertEqual(sc.check(old, stub, None), [])
        for gone in ("planche", "2026-09-12", "expanded"):
            self.assertNotIn(gone, stub)


class AlteredOrMovedText(unittest.TestCase):
    def test_A5_code_comment_becomes_the_stub_title(self) -> None:
        """X-A5: a note without an H1 and a `# comment` line in a code block. old_parts takes
        the comment as the H1: the stub's title line is `# comment`, the line leaves its code
        block, and check() never looks at the H1."""
        old = FM + "\nIntro.\n\n```\n# comment\nx = 1\n```\n\n## Details\n\n- b1\n"
        stub = stub_of(old)
        self.assertEqual(sc.check(old, stub, None), [])
        body = sc.split(stub)[1]
        self.assertTrue(body.lstrip("\n").startswith("# comment\n"), body[:80])
        self.assertNotIn("> # comment", stub)

    def test_A5b_H1_is_not_checked(self) -> None:
        """X-A5: a stub with another H1 passes check() (the old H1 line is not verified)."""
        old = FM + "\n# The Old Title\n\nLead.\n\n## Details\n\n- b1\n"
        stub = stub_of(old).replace("# The Old Title", "# Something Else")
        self.assertEqual(sc.check(old, stub, None), [])

    def test_A6_nested_bullets_are_joined_into_one_claim_with_one_status(self) -> None:
        """X-A6: sub-bullets under a Details bullet become part of the parent bullet text
        ("b1 - sub a - sub b"); one status line covers three old lines."""
        old = FM + "\n# T\n\n## Details\n\n- b1\n  - sub a\n  - sub b\n- b2\n"
        stub = stub_of(old)
        self.assertEqual(sc.check(old, stub, None), [])
        self.assertEqual([e[0] for e in sc.claim_entries(stub)], ["b1 - sub a - sub b", "b2"])
        self.assertIn("old_bullets: 2", stub)

    def test_A6b_star_marker_becomes_dash(self) -> None:
        old = FM + "\n# T\n\n## Details\n\n* starred claim\n"
        stub = stub_of(old)
        self.assertEqual(sc.check(old, stub, None), [])
        self.assertNotIn("* starred claim", stub)
        self.assertIn("- starred claim", stub)

    def test_A6c_plus_and_numbered_Details_items_get_no_status(self) -> None:
        """X-A6: `+ ` and `1. ` items under ## Details are claims of the old note, but the
        stub shows them only as quoted old text, with no status; old_bullets is 0."""
        old = FM + "\n# T\n\n## Details\n\n+ plus claim\n1. numbered claim\n"
        stub = stub_of(old)
        self.assertEqual(sc.check(old, stub, None), [])
        self.assertEqual(sc.claim_entries(stub), [])
        self.assertIn("old_bullets: 0", stub)

    def test_A6d_line_separator_in_a_bullet_cuts_the_bullet(self) -> None:
        """splitlines() splits on U+2028: the bullet keeps only its first part; the rest is
        quoted under ## Old text, without the status."""
        old = FM + "\n# T\n\n## Details\n\n- first part second part\n"
        stub = stub_of(old)
        self.assertEqual(sc.check(old, stub, None), [])
        self.assertEqual([e[0] for e in sc.claim_entries(stub)], ["first part"])


class SetBasedCheckIsBlindToLaterEdits(unittest.TestCase):
    """X-A7: check() tests each old line with a set: a later edit that deletes one copy of a
    duplicated line, reorders the old text, or deletes an old line that equals a line the
    stub generates itself, passes check() (and so validate.stub_errors)."""

    OLD = (FM + "\n# T\n\nLead.\n\n## Details\n\n- claim one\n\n## Grounded Example\n\nSame line.\n\nFirst.\n\n"
           "Second.\n\nSame line.\n\n## Connected Ideas\n\n- claim one\n- [[Home]]\n")

    def test_deleting_a_duplicate_reordering_or_dropping_a_line_passes(self) -> None:
        stub = stub_of(self.OLD)
        self.assertEqual(sc.check(self.OLD, stub, None), [])
        lines = stub.split("\n")
        i = len(lines) - 1 - lines[::-1].index("> Same line.")
        dup_gone = "\n".join(lines[:i] + lines[i + 1:])
        self.assertEqual(sc.check(self.OLD, dup_gone, None), [])
        swapped = stub.replace("> First.", "> @@").replace("> Second.", "> First.").replace("> @@", "> Second.")
        self.assertEqual(sc.check(self.OLD, swapped, None), [])
        # "- claim one" under ## Connected Ideas equals the generated claim line
        no_ci = stub.replace("> - claim one\n", "")
        self.assertNotIn("> - claim one", no_ci)
        self.assertEqual(sc.check(self.OLD, no_ci, None), [])


class FalseRefusals(unittest.TestCase):
    """X-A6 (refusal side): render() drops every indented line under ## Details, old_parts()
    keeps an indented line before the first bullet as an "other" line. The stub of a
    correct note is refused (old note stays, needs_operator). 0 of 476 real notes."""

    def test_indented_text_before_the_first_bullet_is_refused(self) -> None:
        old = FM + "\n# T\n\n## Details\n\n  An indented intro.\n- b1\n"
        self.assertTrue(sc.check(old, stub_of(old), None))

    def test_one_space_bullets_are_refused(self) -> None:
        old = FM + "\n# T\n\n## Details\n\n - b1\n - b2\n"
        self.assertTrue(sc.check(old, stub_of(old), None))


class ParserDivergence(unittest.TestCase):
    """X-A4: the repair state numbers bullets with verify_claims.parse_body_full (fence aware,
    only the FIRST `## Details`, `##  Details` accepted); stub_check.old_parts does not. The
    bullet counts differ, so render_pipeline_stub gives no ledger status (see the pipeline
    probe) and a Details bullet ends under ## Old text with no status."""

    def _both(self, old: str) -> tuple[list[str], list[str]]:
        sec = vc.parse_body_full(vc.split_frontmatter(old)[1]).sections.get("Details")
        return (sec.bullets() if sec else []), sc.old_parts(old)[1]

    def test_counts_differ(self) -> None:
        fence = FM + "\n# T\n\n## Details\n\n- b1\n```\n## not a heading\n```\n- b2\n"
        two = FM + "\n# T\n\n## Details\n\n- b1\n\n## Other\n\nx\n\n## Details\n\n- b2\n"
        spaced = FM + "\n# T\n\n##  Details\n\n- b1\n"
        for old in (fence, two, spaced):
            repair, stub = self._both(old)
            self.assertNotEqual(repair, stub, old)
        stub = stub_of(fence)
        self.assertEqual(sc.check(fence, stub, None), [])
        self.assertNotIn("- b2\n  - ", stub)  # b2: no status line


class NonAsciiArchivePath(unittest.TestCase):
    def test_A8_repair_archive_is_json_escaped(self) -> None:
        """X-A8: render writes repair_archive with json.dumps (ensure_ascii): a non-ASCII path
        becomes `\\u00e9`; validate.stub_errors reads the raw value and does not find the
        archive (a false validate error)."""
        stub = sc.render(FM + "\n# Café\n\n## Details\n\n- b1\n", "Café", ["open: x"], [], [],
                         "retired/r/u/01 Permanent Notes/Café.md")
        self.assertIn('repair_archive: "retired/r/u/01 Permanent Notes/Caf\\u00e9.md"', stub)


if __name__ == "__main__":
    unittest.main()
