"""Reviewer D, round 7: probes of the new dead-link ERROR in validate.py.

Convention: each assertion states the DEFECT. A PASS means the defect is present.
A FAIL means the defect is absent (the attack failed).
"""
from __future__ import annotations

from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5, _Single

import validate  # noqa: E402


class DeadLinkError(_Single):
    def tearDown(self) -> None:
        validate.PIPELINE_NOTES.clear()  # do not leak the module global into other tests
        super().tearDown()

    def errors_for(self, line: str, extra_files: dict[str, str] | None = None) -> list[str]:
        for rel, body in (extra_files or {}).items():
            p = self.zk / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        self.vpath(self.a).write_text(self.ta.replace("- [[Home]] — home map.", "- [[Home]] — home map.\n" + line))
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        return [e for e in errors if "dead link" in e]

    def test_image_embed_is_a_dead_link_error(self) -> None:
        """DEFECT (false positive): an embedded image that exists is an ERROR."""
        self.assertTrue(self.errors_for("- ![[diagram.png]] — figure.", {"Attachments/diagram.png": "x"}))

    def test_pdf_link_is_a_dead_link_error(self) -> None:
        """DEFECT (false positive): a link to an existing non-note file is an ERROR."""
        self.assertTrue(self.errors_for("- [[paper.pdf]] — the paper.", {"Attachments/paper.pdf": "x"}))

    def test_path_link_is_a_dead_link_error(self) -> None:
        """DEFECT (false positive): Obsidian resolves [[00 Maps/Other Map]]; validate does not."""
        self.assertTrue(self.errors_for("- [[00 Maps/Other Map]] — map.",
                                        {"00 Maps/Other Map.md": "---\ntype: map note\n---\n\n# Other Map\n"}))

    def test_case_variant_is_a_dead_link_error(self) -> None:
        """DEFECT (false positive): Obsidian resolves links without regard to case."""
        self.assertTrue(self.errors_for("- [[other map]] — map.",
                                        {"00 Maps/Other Map.md": "---\ntype: map note\n---\n\n# Other Map\n"}))

    def test_md_suffix_is_a_dead_link_error(self) -> None:
        """DEFECT (false positive): [[Other Map.md]] resolves in Obsidian."""
        self.assertTrue(self.errors_for("- [[Other Map.md]] — map.",
                                        {"00 Maps/Other Map.md": "---\ntype: map note\n---\n\n# Other Map\n"}))

    def test_table_escaped_pipe_is_a_dead_link_error(self) -> None:
        """DEFECT (false positive): in a Markdown table Obsidian writes [[A\\|alias]]."""
        self.assertTrue(self.errors_for("\n| a | b |\n|---|---|\n| [[Home\\|home]] | x |\n"))

    def test_link_in_code_is_a_dead_link_error(self) -> None:
        """DEFECT (false positive): text in inline code is not a link in Obsidian."""
        self.assertTrue(self.errors_for("- the syntax is `[[Some Title]]` — note."))

    def test_stale_global_makes_finalize_check_differ(self) -> None:
        """DEFECT (MINOR): PIPELINE_NOTES is a module global that only check() fills. The
        same check_one call gives an ERROR or a warning by the history of the process."""
        self.vpath(self.a).write_text(self.ta.replace("- [[Home]] — home map.", "- [[Nowhere Note]] — x."))
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        idx = validate.VaultIndex(self.zk)
        row = next(r for r in idx.parsed if r[0].stem == self.a)
        validate.PIPELINE_NOTES.clear()
        e1, _ = validate.check_one(*row, self.zk, idx, [], contract_check=False)
        validate.check(self.zk, None, str(self.staging))
        e2, _ = validate.check_one(*row, self.zk, idx, [], contract_check=False)
        validate.PIPELINE_NOTES.clear()
        self.assertNotEqual(bool([e for e in e1 if "dead link" in e]), bool([e for e in e2 if "dead link" in e]))

    def test_respeak_renamed_note_loses_the_error(self) -> None:
        """DEFECT (false negative): PIPELINE_NOTES holds only 'note-published' paths. After
        respeak renames a pipeline note (event 'note-respeak'), a dead link in it is only
        a warning."""
        new = "Host Says Something"
        self.vpath(new).write_text(self.ta.replace(f"# {self.a}", f"# {new}")
                                   .replace("- [[Home]] — home map.", "- [[Nowhere Note]] — x."))
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        ri.provenance.record(self.staging, "note-respeak", note=f"{PN}/{new}.md", old=f"{self.a}.md")
        errors, warnings, _n = validate.check(self.zk, None, str(self.staging))
        self.assertFalse([e for e in errors if "Nowhere Note" in e])
        self.assertTrue([w for w in warnings if "Nowhere Note" in w])
