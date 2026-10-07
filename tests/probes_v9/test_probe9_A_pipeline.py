"""Probe 9 A (reviewer A, round 4): the stub in the pipeline (old-text source, later rewrites).

Convention of THIS file: every test asserts the DEFECT. PASS = the defect is present.
FAIL = the defect is gone (or the setup changed). Fake LLMs only, no network."""
from __future__ import annotations

import json
from pathlib import Path

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri, validate
from tests.unit.test_synth_single import V5

import stub_check  # noqa: E402
import synth_call as sc  # noqa: E402


def _rep(old: str, title: str, bullets: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bullets}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class _Base(_Repair):
    def _repair(self, title: str = "Old Two", n: int = 2) -> str:
        rel = self.old(title, n)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        ledger = f"{title}.md#b1 | kept in {self.a}\n" + "".join(
            f"{title}.md#b{i} | dropped: not in this transcript\n" for i in range(2, n + 1))
        self.run_batch(state, _rep(title, self.a, "b1", self.ta, ledger))
        return rel

    def _errors_for(self, title: str) -> list[str]:
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        return [e for e in errors if title in e]

    def _pub(self) -> ri._Publisher:
        return ri._Publisher(self.staging, self.run_dir.name, self.new_unit("repair", []), self.zk, {})


class LegacyStubIsNeverUpgraded(_Base):
    """X-A1: a stub written before round 21 (the bare `repair_stub` text: no old bullets, no
    old text, no `old_bullets`) is exactly the text that recompute_repair_stubs wants, so it
    is never written again and never re-rendered with the old text. validate.stub_errors
    skips a stub without `old_bullets`, and replacement_beside_old skips every stub: the old
    text is not in the vault and validate is clean. Real data: 17 of the 18 stubs of the
    pilot-8 vault (vaultB_before_rearm + stagingB_before_rearm) stay so after a v9
    recompute; the archives of all 18 exist."""

    def test_legacy_stub_stays_without_old_text_and_validate_is_clean(self) -> None:
        rel = self._repair()
        p = self.vpath("Old Two")
        e = json.loads((self.staging / "repair_state.json").read_text())["notes"][rel]
        v = ri._repair_view(self.staging, self.zk, e)
        legacy = ri.repair_stub("Old Two", v["published"],
                                [f"note: {x}" for x in v["pending"]] + [f"bullet: {b}" for b in v["open"]])
        p.write_text(legacy)  # the vault as a round-20 run left it
        (self.staging / ri.PIPELINE_WRITES).unlink(missing_ok=True)  # no registry before round 21
        self.assertIsNotNone(ri.archived_old_text(self.staging, rel))  # the old text is in the archive
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = p.read_text()
        self.assertEqual(stub, legacy)
        self.assertNotIn("legacy bullet 1", stub)
        self.assertNotIn("Legacy claim.", stub)
        self.assertEqual(self._errors_for("Old Two"), [])


class ArchiveCopyAtEveryFinalize(_Base):
    """X-A3: recompute_repair_stubs compares the bare `repair_stub` text with the vault text;
    a round-21 stub never equals it, so every finalize calls _Publisher.write, which
    archives the stub BEFORE it renders and finds no change. One archive copy and one
    `note-archived` record per stub per finalize."""

    def test_unchanged_stub_is_archived_again_at_each_finalize(self) -> None:
        self._repair()
        before = self.vpath("Old Two").read_text()
        n0 = len(list((self.staging / "retired").rglob("Old Two*.md")))
        for _ in range(3):
            ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        self.assertEqual(self.vpath("Old Two").read_text(), before)
        self.assertEqual(len(list((self.staging / "retired").rglob("Old Two*.md"))) - n0, 3)


class ParserDivergenceGivesWrongStatus(_Base):
    """X-A4: a fenced block with a `## ` line inside ## Details. The repair state (fence-aware
    parse_body_full) has 2 bullets, stub_check.old_parts has 1. render_pipeline_stub then
    shows "open: no repair record for this bullet" for b1 although the ledger says kept in
    a published note, and b2 has no status at all; the stub check passes."""

    def test_answered_bullet_shows_no_repair_record(self) -> None:
        rel = self.old("Old Fence", 2)
        p = self.vpath("Old Fence")
        p.write_text(p.read_text().replace("- legacy bullet 2\n", "```\n## not a heading\n```\n- legacy bullet 2\n"))
        self.vpath(self.a).write_text(self.ta)
        st = {"notes": {rel: {"sha": ri._sha256(p), "status": "done", "titles": [self.a], "pos": 0, "sources": [V5],
                              "bullets": {"Old Fence.md#b1": {"text": "legacy bullet 1",
                                                              "answers": {V5: {"decision": "kept", "title": self.a}}},
                                          "Old Fence.md#b2": {"text": "legacy bullet 2",
                                                              "answers": {V5: {"decision": "kept", "title": self.a}}}},
                              "unsupported_bullets": []}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        old = p.read_text()
        stub = ri.render_pipeline_stub(self.staging, self.zk, rel, ri.repair_stub("Old Fence", [self.a]), old, "a")
        self.assertEqual(stub_check.check(old, stub, self.zk), [])
        self.assertEqual(stub_check.claim_entries(stub), [("legacy bullet 1", "open: no repair record for this bullet")])
        self.assertIn("> - legacy bullet 2", stub)


class BacklinkToAStubIsDiscardedButRecorded(_Base):
    """X-A9: _apply_links adds a backlink line to a `to` note that is a stub. The publisher
    re-renders the stub from the old text (the line is dropped), returns True, and
    _apply_links records `applied: True` and a `note-linked` provenance record. The stub is
    archived once more."""

    def test_backlink_reported_applied_but_absent(self) -> None:
        self._repair()
        pub = self._pub()
        src = f"{PN}/{self.b}.md"
        pub.published.append(src)
        (self.staging / "disagreements.jsonl").write_text(json.dumps(
            {"unit": pub.unit_dir.name, "from": src, "to": f"{PN}/Old Two.md", "kind": "related"}) + "\n")
        res = ri._apply_links(pub, None, self.staging, pub.unit_dir.name)
        self.assertEqual(res[0].get("applied"), True, res)
        self.assertNotIn(f"[[{self.b}]]", self.vpath("Old Two").read_text())


class LinkRewriteChangesTheVerbatimOldText(_Base):
    """X-A11: rewrite_links (respeak) rewrites `[[old title]]` in every vault note, also in
    the quoted OLD text of a stub. The old text in the vault is no longer verbatim; the stub
    check fails in validate, and the pipeline can not put the verbatim text back: the guard
    refuses every later write of the stub as a "user edit" (no pipeline_writes update)."""

    def test_old_text_altered_and_stub_frozen(self) -> None:
        self.vpath("Linked Old").write_text("---\ntype: map note\n---\n\n# Linked Old\n")
        rel = self.old("Old Ln", 2)
        p = self.vpath("Old Ln")
        p.write_text(p.read_text().replace("- [[Other]]", "- [[Linked Old]] — the old link"))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        ledger = f"Old Ln.md#b1 | kept in {self.a}\nOld Ln.md#b2 | dropped: not in this transcript\n"
        self.run_batch(state, _rep("Old Ln", self.a, "b1", self.ta, ledger))
        self.assertIn("> - [[Linked Old]] — the old link", p.read_text())
        sc.rewrite_links(self.zk, "Linked Old", "Linked New")  # what respeak calls
        self.vpath("Linked New").write_text("---\ntype: map note\n---\n\n# Linked New\n")
        self.assertNotIn("[[Linked Old]]", p.read_text())
        self.assertTrue([e for e in self._errors_for("Old Ln") if "stub check" in e])
        pub = self._pub()
        self.assertFalse(pub.write(rel, ri.repair_stub("Old Ln", [self.a]), "stub recomputed"))
        self.assertIn("user edit", pub.refused[0]["why"])


class ArchiveSearchUsesGlobPatterns(_Base):
    """X-A12: archived_old_text / archived_old_path put the note path into glob() without
    glob.escape. Old notes are user-written (no _safe_title): `?` matches any character, so
    the archive of ANOTHER note ("Why Planche Hurts!.md" for "Why Planche Hurts?.md") can be
    the newest candidate and its text becomes the "old text" of the stub (render + check +
    validate all agree on it). `[...]` in a name matches nothing: once the note is a stub,
    every later stub write is refused ("no old text")."""

    def _arch(self, run: str, name: str, text: str, mtime: float) -> Path:
        p = self.staging / "retired" / run / "u" / PN / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        import os
        os.utime(p, (mtime, mtime))
        return p

    def test_question_mark_picks_another_notes_archive(self) -> None:
        own = self._arch("r1", "Why Planche Hurts?.md", "---\ntype: x\n---\n# Why Planche Hurts?\n\nOWN TEXT\n", 1000)
        other = self._arch("r2", "Why Planche Hurts!.md", "---\ntype: x\n---\n# Why Planche Hurts!\n\nOTHER TEXT\n", 2000)
        self.assertTrue(own.is_file())
        rel = f"{PN}/Why Planche Hurts?.md"
        self.vpath("Why Planche Hurts?").write_text(ri.repair_stub("Why Planche Hurts?", [self.a]))
        text, arch = ri.stub_old_text(self.staging, self.zk, rel)
        self.assertIn("OTHER TEXT", text)
        self.assertEqual(Path(arch), other)

    def test_brackets_find_no_archive(self) -> None:
        self._arch("r1", "Planche [Part 1].md", "---\ntype: x\n---\n# Planche [Part 1]\n\nOWN\n", 1000)
        self.assertIsNone(ri.archived_old_text(self.staging, f"{PN}/Planche [Part 1].md"))
