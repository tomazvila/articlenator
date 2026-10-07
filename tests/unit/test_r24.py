"""Round 24: the "marked unsupported" state (the owner's mark), correct-behaviour convention."""
from __future__ import annotations

import json

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import OLD_NOTE, PN, ri

import synth_call as sc  # noqa: E402
import validate  # noqa: E402


class MarkWrite(_Repair):
    def test_keys_only_body_identical_and_validate_accepts(self) -> None:
        rel = self.old("Old Nolit", 2)
        before = self.vpath("Old Nolit").read_text()
        self.assertTrue(ri.mark_unsupported_note(self.staging, self.zk, rel, "source transcript missing"))
        after = self.vpath("Old Nolit").read_text()
        self.assertTrue(ri.is_unsupported_mark(before, after))
        self.assertEqual(ri._fm_and_body(before)[1], ri._fm_and_body(after)[1])  # body byte-identical
        self.assertIn("verification: unsupported", after)
        self.assertIn('unsupported_reason: "source transcript missing"', after)
        self.assertIn("unsupported_marked: ", after)
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])
        self.assertEqual(ri.marked_unsupported_notes(self.zk, self.staging)[0]["note"], rel)
        self.assertFalse([w for w in ri.operator_work(self.staging, self.zk) if "Old Nolit" in w])

    def test_mark_with_any_other_change_is_an_error(self) -> None:
        rel = self.old("Old Nolit", 2)
        ri.mark_unsupported_note(self.staging, self.zk, rel, "source transcript missing")
        p = self.vpath("Old Nolit")
        p.write_text(p.read_text().replace("legacy bullet 1", "legacy bullet ONE"))
        errs = validate.two_state_errors(self.staging, self.zk)
        self.assertTrue([e for e in errs if "Old Nolit" in e and "three mark keys" in e], errs)

    def test_no_frontmatter_is_refused_and_listed(self) -> None:
        self.vpath("Bare").write_text("# Bare\n\n## Details\n\n- a claim\n")
        self.assertFalse(ri.mark_unsupported_note(self.staging, self.zk, f"{PN}/Bare.md", "source transcript missing"))
        self.assertEqual(self.vpath("Bare").read_text(), "# Bare\n\n## Details\n\n- a claim\n")
        ops = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertTrue([o for o in ops if o["note"] == f"{PN}/Bare.md"])


class PlanNoteMark(_Repair):
    def _plan(self, title: str, answers: dict, status: str = "done-unsupported") -> str:
        rel = self.old(title, 1, srcs=("v1", "v2"))  # round 25 (Z-B5): the recorded sources are the planned ones
        st = {"notes": {rel: {"sha": ri._sha256(self.vpath(title)), "status": status, "titles": [], "kind": "known",
                              "sources": ["v1", "v2"],
                              "bullets": {f"{title}.md#b1": {"text": "legacy bullet 1", "answers": answers}}}},
              "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        return rel

    def test_every_video_dropped_marks_at_finalize(self) -> None:
        drop = {"decision": "dropped", "reason": "not in this transcript"}
        rel = self._plan("Old Drop", {"v1": drop, "v2": drop})
        before = self.vpath("Old Drop").read_text()
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        after = self.vpath("Old Drop").read_text()
        self.assertTrue(ri.is_unsupported_mark(before, after))
        self.assertIn("no source video states any claim of this note", after)
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])
        row = next(r for r in sc.repair_status(self.staging, self.zk) if r["note"] == rel)
        self.assertEqual(row["vault"], "marked-unsupported")
        self.assertFalse([w for w in ri.operator_work(self.staging, self.zk) if "Old Drop" in w])

    def test_contradicts_or_open_is_not_marked(self) -> None:
        for title, answers in (("Old Con", {"v1": {"decision": "dropped", "reason": "contradicts the transcript"}}),
                               ("Old Half", {"v1": {"decision": "dropped", "reason": "not in this transcript"}})):
            self._plan(title, answers)
            before = self.vpath(title).read_text()
            ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
            self.assertEqual(self.vpath(title).read_text(), before, title)

    def test_user_edit_is_refused(self) -> None:
        drop = {"decision": "dropped", "reason": "not in this transcript"}
        self._plan("Old Edit", {"v1": drop, "v2": drop})
        p = self.vpath("Old Edit")
        p.write_text(p.read_text() + "\nUSER LINE\n")
        edited = p.read_text()
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        self.assertEqual(p.read_text(), edited)  # never marked over a user edit

    def test_marked_note_planned_again_becomes_a_normal_stub(self) -> None:
        rel = self.old("Old Later", 1)
        ri.mark_unsupported_note(self.staging, self.zk, rel, "source transcript missing")
        state = sc.state_path(self.staging)
        out = sc.repair_groups(self.staging, self.zk, [rel], force=True, state_file=state)
        e = json.loads(state.read_text())["notes"][rel]
        self.assertTrue(e.get("premark_sha"))
        self.assertIn(rel, out["resumed"] + [r for v in out["videos"].values() for r in v["notes"]])
        self.run_batch(state, f"=====NOTE: {self.a} | repairs: Old Later.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                              f"=====BULLETS=====\nOld Later.md#b1 | kept in {self.a}\n")
        sc.next_batch(self.staging, self.zk, state)
        stub = self.vpath("Old Later").read_text()
        self.assertIn("old_bullets: 1", stub)
        self.assertNotIn("unsupported_marked", stub)
        self.assertNotIn("verification: unsupported", stub)
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])
        _ = OLD_NOTE
