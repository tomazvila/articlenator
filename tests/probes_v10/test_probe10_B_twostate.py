"""Probe 10 B (reviewer B, round 5): the two-state invariant (validate.two_state_errors,
operator_work = exit 7, repair-status). Fake LLMs only, no network.

Convention of THIS file: every test asserts the DEFECT. PASS = the defect is present.
FAIL = the defect is gone (or the setup changed)."""
from __future__ import annotations

import json
import os
import shutil

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import OLD_NOTE, PN, SASA, _Env, agent, good_text, ri, validate

import synth_call as sc  # noqa: E402


def _val_errors(t) -> list[str]:
    errors, _w, _n = validate.check(t.zk, None, str(t.staging))
    return errors


def _good_repair(t: _Repair, title: str = "Old Two") -> tuple[str, str]:
    """A correct single-mode repair: old note -> stub with old_bullets, target published."""
    rel = t.old(title, 2)
    state = sc.state_path(t.staging)
    sc.repair_groups(t.staging, t.zk, [rel], state_file=state)
    rep = (f"=====NOTE: {t.a} | repairs: {title}.md | bullets: b1, b2=====\n{t.ta.rstrip()}\n"
           f"=====BULLETS=====\n{title}.md#b1 | kept in {t.a}\n{title}.md#b2 | kept in {t.a}\n")
    t.run_batch(state, rep)
    return rel, str(state)


class AgentModeInPlaceRepairIsInvisible(_Env):
    """Z-B1: an agent-mode repair unit (repair_loop.sh with --allow-other-sources or
    ZR_SYNTH_MODE != single) still rewrites the old note IN PLACE. No code refuses it (X2
    exists only in single mode, as a prompt rule here). The old body text leaves the vault.
    two_state_errors, validate --staging and operator_work (exit 7) say nothing: the plan
    (repair_state.json) has no entry in agent mode, and the registry branch skips every
    note with a `note-published` record."""

    def test_in_place_agent_repair_drops_old_text_silently(self) -> None:
        old = OLD_NOTE.format(t=SASA).replace("- legacy bullet\n", "- legacy bullet ONE\n- legacy bullet TWO\n")
        self.vpath(SASA).write_text(old)
        unit = self.new_unit("repair", [])
        agent.write_file(str(self.vpath(SASA)), good_text())  # write_file at the OLD path
        self.approve(SASA, unit=unit)
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
        n = next(x for x in ev["notes"] if x["note"].endswith(f"{SASA}.md"))
        self.assertEqual(n["action"], "published", n)
        now = self.vpath(SASA).read_text()
        self.assertNotIn("legacy bullet ONE", now)       # old text is not in the vault
        self.assertNotIn("Legacy claim.", now)
        self.assertNotIn("status: superseded", now)      # and it is not a stub
        rel = f"{PN}/{SASA}.md"
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])
        self.assertEqual([e for e in _val_errors(self) if SASA in e and "contract" not in e], [])
        self.assertEqual([w for w in ri.operator_work(self.staging, self.zk) if SASA in w], [])
        self.assertIn(rel, json.loads((self.staging / "pipeline_writes.json").read_text()))


class StubThatLostTextPassesExit7AndRepairStatus(_Repair):
    """Z-B2: a plan note that is a stub with `old_bullets` but no longer carries its old
    text (a bullet line removed) is a two-state violation. validate --staging reports it
    (stub_errors), but operator_work (exit 7) runs only two_state_errors (no stub check)
    and repair-status prints `stub`. The driver exits 0 on it."""

    def test_stub_without_its_bullet_is_silent_for_exit7_and_status(self) -> None:
        rel, _ = _good_repair(self)
        p = self.zk / rel
        txt = p.read_text()
        self.assertIn("old_bullets:", txt)
        self.assertIn("- legacy bullet 2\n", txt)
        p.write_text(txt.replace("- legacy bullet 2\n", "", 1))  # the old bullet leaves the vault
        self.assertTrue(any("Old Two" in e and "stub check" in e for e in _val_errors(self)))  # validate sees it
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])
        self.assertEqual([w for w in ri.operator_work(self.staging, self.zk) if "Old Two" in w], [])
        row = next(r for r in sc.repair_status(self.staging, self.zk) if r["note"] == rel)
        self.assertEqual(row["vault"], "stub")


class StubWithAnotherNotesArchive(_Repair):
    """Z-B3: two_state_errors accepts any `old_bullets` stub, and stub_errors checks the
    stub against whatever file `repair_archive` names. Nothing compares the archive with
    the plan's sha. A plan note whose stub carries ANOTHER note's text and archive (here: the
    stub of note B copied over note A) passes validate --staging, exit 7 and repair-status;
    note A's old text is not in the vault."""

    def test_stub_of_b_over_a_passes_every_check(self) -> None:
        rel_a = self.old("Old Alpha", 2)
        self.vpath("Old Alpha").write_text(self.vpath("Old Alpha").read_text().replace("legacy bullet", "alpha bullet"))
        rel_b, _ = _good_repair(self, "Old Beta")
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel_a], state_file=state)  # A is in the plan
        st = json.loads(state.read_text())
        self.assertIn(rel_a, st["notes"])
        stub_b = (self.zk / rel_b).read_text()
        (self.zk / rel_a).write_text(stub_b)  # byte copy of B's stub
        self.assertNotIn("alpha bullet", (self.zk / rel_a).read_text())
        errs = [e for e in _val_errors(self) if "Old Alpha" in e]
        self.assertEqual(errs, [])  # validate --staging: no error at all for Old Alpha
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])
        self.assertEqual([w for w in ri.operator_work(self.staging, self.zk) if "Old Alpha" in w], [])


class PlanNoteRemovedIsSilent(_Repair):
    """Z-B4: a plan note that is no longer in the vault (removed or renamed: maintenance
    retire, respeak of a `needs-repair` note that the pipeline published, a sync delete)
    holds neither its old text nor a stub. two_state_errors skips a missing file
    (`not p.is_file()`), so validate --staging and exit 7 are silent; repair-status prints
    `missing` but exits 0."""

    def test_missing_plan_note_is_no_error(self) -> None:
        rel = self.old("Old Gone", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        (self.zk / rel).unlink()
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])
        self.assertEqual([e for e in _val_errors(self) if "Old Gone" in e], [])
        self.assertEqual([w for w in ri.operator_work(self.staging, self.zk) if "Old Gone" in w], [])
        row = next(r for r in sc.repair_status(self.staging, self.zk) if r["note"] == rel)
        self.assertEqual(row["vault"], "missing")


class BacklinkIntoOldNoteIsPermanentExit7(_Repair):
    """Z-B5 (false positive): `_apply_links` adds the documented backlink line
    (note_link.py, `supersedes-candidate` is ONLY for an old note) to an old user note
    through the publisher. The note is now in pipeline_writes.json, is not a stub, not
    published, and differs from its archive in more than status lines: two_state_errors
    reports it, so every later driver run ends with exit 7 for a change that the pipeline
    made on purpose. Nothing clears it (it is not in needs_operator.json)."""

    def test_backlink_to_user_note_is_an_error_forever(self) -> None:
        self.vpath("User Note").write_text(OLD_NOTE.format(t="User Note"))
        rel = f"{PN}/User Note.md"
        unit = self.new_unit("synth", [])
        pub = ri._Publisher(self.staging, self.run_dir.name, unit, self.zk, {})
        new = ri._add_connected_line(self.vpath("User Note").read_text(), "- Possibly superseded by [[X]]")
        self.assertTrue(pub.write(rel, new, why="backlink from X"))  # what _apply_links does
        errs = validate.two_state_errors(self.staging, self.zk)
        self.assertTrue(any("User Note" in e and "neither a stub nor a note it published" in e for e in errs), errs)
        self.assertTrue(any("User Note" in w for w in ri.operator_work(self.staging, self.zk)))


class DoneUnchangedOpenBulletsExit0(_Repair):
    """Z-B6: a plan note in state `done`, unchanged, with open bullets and no operator
    entry (`UNLISTED` in repair-status; seen on the pilot-9 clone: "Achieve An Imperfect
    Handstand Hold Before Correcting Form", done, unchanged, open 5/5) is not operator work
    for exit 7. README: "Exit 0 means ... no operator work is known"."""

    def test_unlisted_note_is_not_operator_work(self) -> None:
        rel = self.old("Old Unl", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        st = json.loads(state.read_text())
        st["notes"][rel]["status"] = "done"
        state.write_text(json.dumps(st))
        row = next(r for r in sc.repair_status(self.staging, self.zk) if r["note"] == rel)
        self.assertTrue(row["unlisted"], row)
        self.assertGreater(row["open_bullets"], 0)
        self.assertEqual([w for w in ri.operator_work(self.staging, self.zk) if "Old Unl" in w], [])


class ArchiveOrderIsByRunName(_Repair):
    """Z-B7: `_archive_candidates` sorts by run folder NAME. Maintenance runs
    (`relink-<iso>`, `respeak-<ts>`) sort after every driver run (`2026...`), so an OLDER
    relink archive is "newest", and `arch[-1]` (two_state's "first archived version") is a
    LATER driver-run archive. Result: a content change by the first write is hidden when a
    later write changes only status lines."""

    def test_relink_archive_hides_first_content_change(self) -> None:
        rel = f"{PN}/User Two.md"
        orig = OLD_NOTE.format(t="User Two")
        self.vpath("User Two").write_text(orig)
        # 1st pipeline write (maintenance, older): a content change (a link line)
        mu = self.staging / "maintenance" / "relink-2026-01-01T000000Z"
        mu.mkdir(parents=True)
        pub1 = ri._Publisher(self.staging, mu.name, mu, self.zk, {})
        changed = orig.replace("- [[Other]]\n", "- [[Other]]\n- [[Added By Relink]]\n")
        self.assertTrue(pub1.write(rel, changed, "relink"))
        self.assertTrue(validate.two_state_errors(self.staging, self.zk))  # flagged now
        # 2nd pipeline write (a driver run, later): status lines only
        unit = self.new_unit("repair", [])
        pub2 = ri._Publisher(self.staging, self.run_dir.name, unit, self.zk, {})
        self.assertTrue(pub2.write(rel, changed.replace("verification: unverified", "verification: unsupported"), "x"))
        cands = ri._archive_candidates(self.staging, rel)
        self.assertTrue(cands[0].parts[-4].startswith("relink-"))   # the OLDER archive is "newest"
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])  # the content change is hidden


class VersionSuffixArchiveCollision(_Repair):
    """Z-B8: `_archive_candidates` takes `<stem>.v<n>.md` files in the unit folder as
    versions of `<stem>`. A different note whose title ends in `.v2` (a valid name) is
    taken as a version of the note without the suffix, and as the NEWEST one."""

    def test_other_note_is_taken_as_archive(self) -> None:
        for t in ("Foo", "Foo.v2"):
            self.vpath(t).write_text(OLD_NOTE.format(t=t))
        unit = self.new_unit("repair", [])
        pub = ri._Publisher(self.staging, self.run_dir.name, unit, self.zk, {})
        pub.archive(f"{PN}/Foo.md", "x")
        pub.archive(f"{PN}/Foo.v2.md", "x")
        cands = ri._archive_candidates(self.staging, f"{PN}/Foo.md")
        self.assertEqual(cands[0].name, "Foo.v2.md")
        self.assertIn("# Foo.v2", ri.archived_old_text(self.staging, f"{PN}/Foo.md"))
