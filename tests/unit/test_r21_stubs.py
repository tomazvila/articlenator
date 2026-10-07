"""Round 21: the stub carries every old bullet verbatim (design change). The stub check
(`stub_check`) is the safety check: it runs at every stub write (a stub that fails is not
written; the old note stays; needs_operator.json) and in `validate.py --staging`.
Correct-behaviour convention. Fake LLMs only."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri

import stub_check  # noqa: E402
import synth_call as sc  # noqa: E402
import validate  # noqa: E402

OLD = ("---\ntype: permanent note\ncreated: 2026-09-12\nverification: unverified\n---\n\n# Old Lean\n\n"
       "Lead paragraph of the old note with [[Some Link]].\n\n## Details\n\n"
       "- Planche lean presses can be done on the floor without parallettes.\n"
       "- A second claim that\n  continues on a second line.\n\n## Connected Ideas\n\n- [[Other]]\n")


def _rep(old: str, title: str, bullets: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bullets}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class StubCheckUnit(_Repair):
    def test_render_then_check_is_clean_and_carries_every_line(self) -> None:
        self.vpath(self.a).write_text(self.ta)
        stub = stub_check.render(OLD, "Old Lean", [f"→ [[{self.a}]]", "open: target waiting (X)"], [self.a],
                                 ["bullet: Old Lean.md#b2"], "retired/r/u/x.md")
        self.assertEqual(stub_check.check(OLD, stub, self.zk), [])
        self.assertIn("- Planche lean presses can be done on the floor without parallettes.\n", stub)
        self.assertIn("- A second claim that continues on a second line.\n", stub)
        self.assertIn("> Lead paragraph of the old note with [[Some Link]].", stub)
        self.assertIn("old_bullets: 2", stub)

    def test_a_lost_line_a_wrong_count_or_a_bad_target_fails(self) -> None:
        self.vpath(self.a).write_text(self.ta)
        stub = stub_check.render(OLD, "Old Lean", [f"→ [[{self.a}]]", "open: x"], [self.a], [], "a")
        self.assertTrue(stub_check.check(OLD, stub.replace("> Lead paragraph", "> Changed"), self.zk))
        self.assertTrue(stub_check.check(OLD, stub.replace("old_bullets: 2", "old_bullets: 3"), self.zk))
        self.assertTrue(stub_check.check(OLD, stub.replace("- A second claim", "- A changed claim"), self.zk))
        self.assertTrue(stub_check.check(OLD, stub.replace(f"→ [[{self.a}]]", "→ [[Missing Note]]"), self.zk))
        self.vpath(self.a).write_text(ri.repair_stub(self.a, ["Elsewhere"]))  # the target became a stub
        self.assertTrue(stub_check.check(OLD, stub, self.zk))


class StubInPipeline(_Repair):
    def _repair(self, title: str = "Old Two", n: int = 2) -> tuple[str, Path]:
        rel = self.old(title, n)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        ledger = f"{title}.md#b1 | kept in {self.a}\n" + "".join(
            f"{title}.md#b{i} | dropped: not in this transcript\n" for i in range(2, n + 1))
        self.run_batch(state, _rep(title, self.a, "b1", self.ta, ledger))
        sc.next_batch(self.staging, self.zk, state)
        return rel, state

    def test_stub_carries_each_old_bullet_with_its_status_and_validate_checks_it(self) -> None:
        rel, _ = self._repair()
        stub = self.vpath("Old Two").read_text()
        self.assertIn(stub_check.CLAIMS_HEADING, stub)
        entries = stub_check.claim_entries(stub)
        self.assertEqual([e[0] for e in entries], ["legacy bullet 1", "legacy bullet 2"])
        self.assertTrue(entries[0][1].startswith(f"→ [[{self.a}]]"), entries)
        self.assertEqual(entries[1][1], "unsupported: no source video states this")
        self.assertIn("verification: superseded", stub)
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        self.assertEqual([e for e in errors if "stub" in e], [])
        # the archive named in the stub holds the old text, and the check passes against it
        arch = self.staging / stub.split('repair_archive: "', 1)[1].split('"', 1)[0]
        self.assertEqual(stub_check.check(arch.read_text(), stub, self.zk), [])

    def test_a_stub_that_fails_the_check_is_not_written(self) -> None:
        rel = self.old("Old Ref", 1)
        before = self.vpath("Old Ref").read_text()
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        with mock.patch.object(stub_check, "check", return_value=["simulated: old line not carried"]):
            self.run_batch(state, _rep("Old Ref", self.a, "b1", self.ta, f"Old Ref.md#b1 | kept in {self.a}\n"))
        self.assertEqual(self.vpath("Old Ref").read_text(), before)  # the old note stays, unchanged
        ops = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertIn("stub refused by the stub check", [o["why"] for o in ops if o["note"] == rel])
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        self.assertTrue([e for e in errors if "beside its published replacement" in e])  # visible

    def test_V34_user_edit_of_a_stub_is_not_overwritten(self) -> None:
        rel, _ = self._repair()
        p = self.vpath("Old Two")
        edited = p.read_text() + "\nUSER NOTE ADDED BY HAND.\n"
        p.write_text(edited)
        self.vpath("Later Target").write_text(self.tb.replace(f"# {self.b}\n", "# Later Target\n"))
        st = json.loads((self.staging / "repair_state.json").read_text())
        st["notes"][rel]["bullets"]["Old Two.md#b2"]["answers"] = {"v2": {"decision": "kept", "title": "Later Target"}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)  # recompute wants a write
        self.assertEqual(p.read_text(), edited)
        ops = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertTrue([o for o in ops if o["note"] == rel and "edited since" in o["why"]])

    def test_V34_user_revert_of_a_stub_is_not_stubbed_again(self) -> None:
        rel, _ = self._repair()
        p = self.vpath("Old Two")
        arch = self.staging / p.read_text().split('repair_archive: "', 1)[1].split('"', 1)[0]
        p.write_text(arch.read_text())  # the user puts the old text back
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        self.assertEqual(p.read_text(), arch.read_text())

    def test_W13_user_edit_of_a_published_target_is_not_overwritten(self) -> None:
        self._repair()
        pa = self.vpath(self.a)
        edited = pa.read_text().replace("## Connected Ideas", "USER LINE.\n\n## Connected Ideas", 1)
        pa.write_text(edited)
        pub = ri._Publisher(self.staging, "r", self.new_unit("repair", []), self.zk, {})
        self.assertFalse(pub.write(f"{PN}/{self.a}.md", self.ta, "an extension"))
        self.assertEqual(pa.read_text(), edited)
        ops = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertTrue([o for o in ops if o["note"] == f"{PN}/{self.a}.md"])
