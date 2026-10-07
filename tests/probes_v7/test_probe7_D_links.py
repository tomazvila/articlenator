"""Reviewer D, round 7: probes of unlink_waiting_siblings / relink_published.

Convention: each assertion states the DEFECT. A PASS means the defect is present.
A FAIL means the defect is absent (the attack failed).
"""
from __future__ import annotations

import json

from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import _Single

import synth_call as sc  # noqa: E402


class _Pub:
    def __init__(self, zk):
        self.zk, self.writes = zk, []

    def write(self, rel, text, why=""):
        p = self.zk / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.writes.append(rel)


def _res(rel, text, existed=False):
    return {"rel": rel, "data": text.encode(), "entry": {}, "existed_before": existed}


class UnlinkRelink(_Single):
    def contract(self, title: str) -> str:
        return self.ta.replace(f"# {self.a}", f"# {title}")

    def test_relink_of_shared_line_restores_a_dead_link(self) -> None:
        """DEFECT: one line links two waiting siblings Y and Z. Y publishes, Z does not.
        relink puts back the whole original line, so X gets a dead [[Z]]."""
        x = "# X\n\n## Connected Ideas\n\n- [[Y]] and [[Z]] - both.\n"
        results = [_res(f"{PN}/X.md", x), _res(f"{PN}/Y.md", "# Y\n"), _res(f"{PN}/Z.md", "# Z\n")]
        dec = {f"{PN}/X.md": "publish", f"{PN}/Y.md": "pending", f"{PN}/Z.md": "quarantine"}
        ri.unlink_waiting_siblings(self.staging, results, dec)
        self.vpath("X").write_text(results[0]["data"].decode())
        self.vpath("Y").write_text(self.contract("Y"))  # Y publishes later; Z never does
        ri.relink_published(self.staging, self.zk, _Pub(self.zk))
        text = self.vpath("X").read_text()
        self.assertIn("[[Z]]", text)
        self.assertFalse(self.vpath("Z").exists())

    def test_embed_becomes_bang_text(self) -> None:
        """DEFECT: an embed ![[Y]] of a waiting sibling becomes the text '!Y'."""
        results = [_res(f"{PN}/X.md", "# X\n\n![[Y]]\n- [[Home]]\n"), _res(f"{PN}/Y.md", "# Y\n")]
        ri.unlink_waiting_siblings(self.staging, results, {f"{PN}/X.md": "publish", f"{PN}/Y.md": "pending"})
        self.assertIn("\n!Y\n", results[0]["data"].decode())

    def test_only_link_removed_leaves_an_orphan(self) -> None:
        """DEFECT: the only link of X goes to a waiting sibling. After the unlink X has no
        wikilink at all, and finalize adds no [[Home]] (only drop_links get _ensure_a_link)."""
        x = self.ta.replace("- [[Home]] — home map.", "- [[Y]] — the sibling.")
        results = [_res(f"{PN}/X.md", x), _res(f"{PN}/Y.md", "# Y\n")]
        ri.unlink_waiting_siblings(self.staging, results, {f"{PN}/X.md": "publish", f"{PN}/Y.md": "quarantine"}, self.zk)
        out = results[0]["data"].decode()
        self.assertEqual(ri.verify_claims.WIKILINK.findall(out), [])
        self.assertFalse(results[0].get("drop_links"))  # so finalize never calls _ensure_a_link

    def test_link_to_existing_old_note_removed_for_good(self) -> None:
        """DEFECT: Y is an EXISTING vault note (old format). This unit's edit of Y is
        quarantined, so the old Y stays in the vault and [[Y]] is a valid link. The unlink
        still removes it, and relink never restores it (old Y is not a contract note)."""
        self.vpath("Y").write_text("---\ntype: permanent note\n---\n\n# Y\n\nOld text. [[Home]]\n")
        results = [_res(f"{PN}/X.md", "# X\n\n- [[Y]] - see.\n"), _res(f"{PN}/Y.md", "# Y\nnew\n", existed=True)]
        ri.unlink_waiting_siblings(self.staging, results, {f"{PN}/X.md": "publish", f"{PN}/Y.md": "quarantine"})
        self.vpath("X").write_text(results[0]["data"].decode())
        self.assertEqual(ri.relink_published(self.staging, self.zk, _Pub(self.zk)), [])
        self.assertTrue(self.vpath("Y").is_file())
        self.assertNotIn("[[Y]]", self.vpath("X").read_text())
        self.assertEqual(len(json.loads((self.staging / "unlinked.json").read_text())), 1)  # waits forever

    def test_target_renamed_by_respeak_is_never_relinked(self) -> None:
        """DEFECT: the waiting sibling publishes, then respeak renames it. The record
        keeps the old title, rewrite_links does not see the plain text, so X never gets
        the link back and the record never ends."""
        old = "Unresolved Speaker (Ch) Says Y"
        results = [_res(f"{PN}/X.md", f"# X\n\n- [[{old}]] - see.\n"), _res(f"{PN}/{old}.md", "# x\n")]
        ri.unlink_waiting_siblings(self.staging, results, {f"{PN}/X.md": "publish", f"{PN}/{old}.md": "pending"})
        self.vpath("X").write_text(results[0]["data"].decode())
        self.vpath("Host Says Y").write_text(self.contract("Host Says Y"))  # published, then renamed
        sc.rewrite_links(self.zk, old, "Host Says Y")
        self.assertEqual(ri.relink_published(self.staging, self.zk, _Pub(self.zk)), [])
        self.assertNotIn("[[Host Says Y]]", self.vpath("X").read_text())
        self.assertEqual(len(json.loads((self.staging / "unlinked.json").read_text())), 1)

    def test_note_renamed_after_unlink_is_never_relinked(self) -> None:
        """DEFECT: X itself is renamed (respeak) after the unlink. The record keeps the old
        path, relink skips it ('not is_file') without a stale mark, and the new X never
        gets the link back."""
        old = "Unresolved Speaker (Ch) Says X"
        results = [_res(f"{PN}/{old}.md", f"# {old}\n\n- [[Y]] - see.\n"), _res(f"{PN}/Y.md", "# Y\n")]
        ri.unlink_waiting_siblings(self.staging, results, {f"{PN}/{old}.md": "publish", f"{PN}/Y.md": "pending"})
        self.vpath("Host Says X").write_text(results[0]["data"].decode())  # the renamed note
        self.vpath("Y").write_text(self.contract("Y"))
        self.assertEqual(ri.relink_published(self.staging, self.zk, _Pub(self.zk)), [])
        self.assertNotIn("[[Y]]", self.vpath("Host Says X").read_text())

    def test_quarantined_new_map_keeps_dead_link_in_home(self) -> None:
        """DEFECT: unlink looks only at siblings under 01 Permanent Notes. A clustering
        unit's Home.md that links a new map which is quarantined keeps [[New Map]]."""
        results = [_res("Home.md", "# Home\n\n- [[New Map]]\n"), _res("00 Maps/New Map.md", "# New Map\n")]
        ri.unlink_waiting_siblings(self.staging, results, {"Home.md": "publish", "00 Maps/New Map.md": "quarantine"})
        self.assertIn("[[New Map]]", results[0]["data"].decode())

    def test_relink_writes_outside_the_zettelkasten(self) -> None:
        """DEFECT (defense in depth): relink takes rec['note'] from unlinked.json without
        safe_note_rel; a record with '../' writes a file outside the zettelkasten."""
        outside = self.zk.parent / "outside.md"
        outside.write_text("- Y - see.\n")
        self.vpath("Y").write_text(self.contract("Y"))
        (self.staging / "unlinked.json").write_text(json.dumps([
            {"note": "../outside.md", "target": "Y", "line": "- [[Y]] - see.", "plain": "- Y - see."}]))
        ri.relink_published(self.staging, self.zk, _Pub(self.zk))
        self.assertIn("[[Y]]", outside.read_text())
