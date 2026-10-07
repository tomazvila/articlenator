"""Reviewer C, round 3 (frozen-v8): unlink + [[Home]] fallback, validate dead-link rules
after V30, the narrow merge, and the Y9 untimed-claim rule.

Convention: each assertion states the DEFECT. PASS = the defect is present. FAIL = the
defect is absent. Exception: `test_ok_*` states the CORRECT behaviour (PASS = works).
Fake LLM only, no network."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5, _Single, reply

import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402
import validate  # noqa: E402


def _res(rel: str, text: str, existed: bool = False) -> dict:
    return {"rel": rel, "data": text.encode(), "entry": {}, "existed_before": existed, "stub": False}


class UnlinkEndToEnd(_Single):
    def _run(self, ta: str) -> dict:
        """A and B in one unit; B gets no review, so B waits (pending) and A publishes."""
        self.fake(synth=[reply([("C1", "01:16", "a"), ("C2", "01:22", "b")], [(self.a, ["C1"], ta), (self.b, ["C2"], self.tb)])])
        self.synth()
        rc.review_unit(self.staging, self.zk, self.unit)
        for d in self.unit.glob("review-*"):
            if (json.loads((d / "review.json").read_text()) or {}).get("note") == f"{PN}/{self.b}.md":
                shutil.rmtree(d)
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        acts = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertEqual(acts, {self.a: "published", self.b: "pending-review"}, ev["notes"])
        return ev

    def test_L1_case_variant_link_to_a_waiting_sibling_publishes_dead(self) -> None:
        """DEFECT (SHOULD-FIX): the V29 body check resolves `[[lower case title]]` of a sibling
        case-insensitively (validate._resolves on the shadow titles), so the note passes.
        unlink_waiting_siblings compares the exact target with `gone`, so the link stays.
        A publishes a dead link; the final validate reports an ERROR."""
        ta = self.ta.replace("on the floor. [src:", f"on the floor (see [[{self.b.lower()}]]). [src:", 1)
        self._run(ta)
        self.assertIn(f"[[{self.b.lower()}]]", self.vpath(self.a).read_text())
        errors = validate.check(self.zk, None, str(self.staging))[0]
        self.assertTrue([e for e in errors if "dead link" in e and self.a in e], errors)

    def test_L2_unlinked_line_breaks_the_connected_ideas_shape(self) -> None:
        """DEFECT (SHOULD-FIX): the unlinked sibling stays as a plain-text bullet in
        '## Connected Ideas'. SHAPE requires 'a bullet with a [[link]]' there, so any later
        check of the published note (rereview, repair in place) fails section-shape.
        Links are never restored (round 18), so the line stays for good."""
        ta = self.ta.replace("- [[Home]] — home map.", f"- [[Home]] — home map.\n- [[{self.b}]] — sibling.")
        self._run(ta)
        text = self.vpath(self.a).read_text()
        self.assertIn(f"\n- {self.b} — sibling.", text)
        ck = ri._Checker(self.staging, self.unit, self.zk, "synth", {"video"})
        r = ck.check_text(f"{PN}/{self.a}.md", text, True, True)
        self.assertIn("section-shape", {f["type"] for f in r["failures"]}, r["failures"])

    def test_ok_L3_only_link_to_a_waiting_sibling_gets_home(self) -> None:
        """CORRECT (V25): A's only link goes to B (waiting); A publishes with [[Home]] and
        the final validate has no ORPHAN and no dead link for A."""
        ta = self.ta.replace("- [[Home]] — home map.", f"- [[{self.b}]] — sibling.")
        self._run(ta)
        self.assertIn("[[Home]]", self.vpath(self.a).read_text())
        errors = validate.check(self.zk, None, str(self.staging))[0]
        self.assertEqual([e for e in errors if self.a in e], [])


class UnlinkDirect(_Single):
    def test_L4_link_in_a_table_row_of_a_map_stays_dead(self) -> None:
        """DEFECT (MINOR): unlink skips every line that starts with '|'. A clustering unit's
        map (not a contract note, so no SHAPE rule) that lists a quarantined new note in a
        table keeps [[New Note]], a dead link in a pipeline note."""
        results = [_res("00 Maps/New Map.md", "# New Map\n\n| note | why |\n|---|---|\n| [[New Note]] | x |\n- [[Home]]\n"),
                   _res(f"{PN}/New Note.md", "# New Note\n")]
        ri.unlink_waiting_siblings(self.staging, results, {"00 Maps/New Map.md": "publish",
                                                           f"{PN}/New Note.md": "quarantine"}, self.zk)
        self.assertIn("[[New Note]]", results[0]["data"].decode())

    def test_ok_L5_home_exists_or_is_created(self) -> None:
        """CORRECT: _ensure_a_link creates Home.md when the zettelkasten has none, so the
        [[Home]] fallback never adds a dead link."""
        (self.zk / "Home.md").unlink(missing_ok=True)
        x = self.ta.replace("- [[Home]] — home map.", "- [[Y]] — the sibling.")
        results = [_res(f"{PN}/X.md", x), _res(f"{PN}/Y.md", "# Y\n")]
        ri.unlink_waiting_siblings(self.staging, results, {f"{PN}/X.md": "publish", f"{PN}/Y.md": "quarantine"}, self.zk)
        self.assertIn("[[Home]]", results[0]["data"].decode())
        self.assertTrue((self.zk / "Home.md").is_file())


class ValidateAfterV30(_Single):
    def _dead(self, line: str) -> list[str]:
        self.vpath(self.a).write_text(self.ta.replace("- [[Home]] — home map.", "- [[Home]] — home map.\n" + line))
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{self.a}.md", sources=[V5])
        errors, warnings, _n = validate.check(self.zk, None, str(self.staging))
        return [x for x in errors + warnings if "dead link" in x or "unresolved wikilink" in x]

    def test_ok_V30_existing_targets_resolve(self) -> None:
        """CORRECT (V30): case, path, .md, a table alias and code give no dead link."""
        (self.zk / "00 Maps").mkdir(exist_ok=True)
        (self.zk / "00 Maps" / "Other Map.md").write_text("---\ntype: map note\n---\n\n# Other Map\n")
        self.assertEqual(self._dead("- [[other map]] a\n- [[00 Maps/Other Map]] b\n- [[Other Map.md]] c\n"
                                    "\n| a |\n|---|\n| [[Home\\|home]] |\n\n- `[[Nowhere]]` d"), [])

    def test_V30a_embed_of_a_missing_note_is_never_reported(self) -> None:
        """DEFECT (MINOR, false negative): _note_links drops every embed, so `![[Missing
        Note]]` (a transclusion of a note that does not exist) is neither ERROR nor warning."""
        self.assertEqual(self._dead("- ![[Missing Note]] — embedded."), [])

    def test_V30b_link_to_a_missing_file_is_never_reported(self) -> None:
        """DEFECT (MINOR, false negative): _resolves returns True for any 'x.pdf' form,
        whether or not the file exists (its docstring says 'when such a file exists')."""
        self.assertEqual(self._dead("- [[missing-paper.pdf]] — the paper."), [])

    def test_V30c_title_that_ends_in_a_decimal_is_taken_for_a_file(self) -> None:
        """DEFECT (MINOR, false negative): a missing note whose title ends in '1.25' matches
        the file-extension rule and is never reported."""
        self.assertEqual(self._dead("- [[Hang Ratio Of 1.25]] — a ratio."), [])

    def test_V30d_double_backtick_code_counts_as_a_link(self) -> None:
        """DEFECT (MINOR, false positive): '``[[Nowhere]]``' is inline code in Obsidian, but
        the single-backtick regex leaves the link, so a pipeline note gets an ERROR."""
        self.assertTrue(self._dead("- the syntax ``[[Nowhere]]`` d"))


class NarrowMerge(_Single):
    def _pair(self, evid_keep: int | None = None) -> tuple[list[dict], dict]:
        copy_t = "Says Four Sets Of Planche Lean Presses After The Planche Workout Are An Option"
        copy = re.sub(r"(?m)^# .+$", f"# {copy_t}", self.tb, count=1)
        lead = next(ln for ln in copy.split("\n") if "[src:" in ln and not ln.startswith(("-", "#")))
        copy = copy.replace(lead, "Clearly, " + lead, 1)
        tb = self.tb
        if evid_keep is not None:  # the alphabetically first note keeps only N Evidence items
            head, ev = tb.split("## Evidence", 1)
            ev_body, tail = ev.split("## Connected Ideas", 1)
            items = [ln for ln in ev_body.split("\n") if ln.startswith("- vid-")]
            tb = head + "## Evidence\n\n" + "\n".join(items[:evid_keep]) + "\n\n## Connected Ideas" + tail
        results = [_res(f"{PN}/{self.b}.md", tb), _res(f"{PN}/{copy_t}.md", copy)]
        return results, {r["rel"]: "publish-reviewed" for r in results}

    def test_ok_merge_in_a_synth_unit(self) -> None:
        """CORRECT: two new notes of one unit with the same title claim, numbers, speaker
        and lead are merged."""
        results, dec = self._pair()
        out = ri.merge_duplicates(self.staging, self.zk, self.unit, results, dec, "synth")
        self.assertEqual(len(out), 1)

    def test_ok_no_merge_in_a_repair_unit(self) -> None:
        """CORRECT: the same pair in a repair unit is not merged (ZR_MERGE_IN_REPAIR unset)."""
        results, dec = self._pair()
        self.assertEqual(ri.merge_duplicates(self.staging, self.zk, self.unit, results, dec, "repair"), [])
        self.assertEqual(set(dec.values()), {"publish-reviewed"})

    def test_ok_no_merge_with_a_pending_partner(self) -> None:
        """CORRECT (M5): a publish-reviewed note is not merged into a pending one."""
        results, dec = self._pair()
        dec[results[0]["rel"]] = "pending"
        self.assertEqual(ri.merge_duplicates(self.staging, self.zk, self.unit, results, dec, "synth"), [])

    def test_M9_the_kept_note_is_the_alphabetically_first(self) -> None:
        """DEFECT (MINOR): the kept note is the first by file name, not the one with more
        Evidence. The kept note here has 1 Evidence item, the dropped one has all of them;
        the dropped text is kept only in staging (merged/)."""
        results, dec = self._pair(evid_keep=1)
        n_ev = [r["data"].decode().split("## Evidence")[1].count("\n- vid-") for r in results]
        self.assertLess(n_ev[0], n_ev[1])
        out = ri.merge_duplicates(self.staging, self.zk, self.unit, results, dec, "synth")
        self.assertEqual(out[0]["kept"], results[0]["rel"])


class UntimedClaims(_Single):
    def test_Y9a_timed_claim_and_a_note_without_time_tags_counts_as_cited(self) -> None:
        """DEFECT (MINOR, Y9 rest): `if not times: return True`: a timed claim is covered by
        any note that has no timed source tag."""
        note = "# T\n\nLead. [src: vid-x]\n\n## Evidence\n\n- vid-x (A): \"something else entirely\"\n"
        self.assertTrue(sc._cites_claim(note, {"id": "C4", "at": "03:10", "text": "one grade"}, ""))

    def test_Y9b_two_common_words_cover_an_untimed_claim(self) -> None:
        """DEFECT (MINOR): the untimed rule counts any two words of 4+ letters, stop words
        included ('your', 'with'): a quote about another subject covers the claim."""
        note = "# T\n\nLead.\n\n## Evidence\n\n- vid-x (A): \"stay with your weight over the hands\"\n"
        claim = {"id": "C4", "at": "line 400", "text": "lock your elbows with straight arms in planche leans"}
        self.assertTrue(sc._cites_claim(note, claim, ""))
