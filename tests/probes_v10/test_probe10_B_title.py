"""Probe 10 B (reviewer B, round 5): the `title-equals-old` refusal (synth_call.py, X2).
Fake LLMs only, no network.

Convention of THIS file: classes named `Defect*` assert the DEFECT (PASS = the defect is
present). Classes named `Holds*` assert the CORRECT behavior (PASS = no defect; they record
the attacks that found nothing)."""
from __future__ import annotations

import json

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import OLD_NOTE, PN, ri, validate

import synth_call as sc  # noqa: E402


class _T(_Repair):
    def reply_with_title(self, old: str, title: str) -> tuple[dict, str]:
        rel = self.old(old, 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        text = self.ta.replace(f"# {self.a}\n", f"# {title}\n")
        rep = (f"=====NOTE: {title} | repairs: {old}.md | bullets: b1, b2=====\n{text.rstrip()}\n"
               f"=====BULLETS=====\n{old}.md#b1 | kept in {title}\n{old}.md#b2 | kept in {title}\n")
        _b, _u, res, _ev = self.run_batch(state, rep)
        return res, rel


class DefectMdSuffixTitle(_T):
    """Z-B9: the new title `<Old Title>.md` (the old FILE NAME, which the prompts forbid) is
    not refused: name_key does not strip `.md`. The note is published as
    `<Old Title>.md.md`, and the stub's `superseded_by: [[<Old Title>.md]]`. Obsidian (and
    validate._resolves, which strips `.md`) resolves that link to the OLD note itself: the
    stub points to itself, and the new note is not reachable by its link."""

    def test_md_suffix_title_is_published_and_the_stub_links_itself(self) -> None:
        res, rel = self.reply_with_title("Old Md", "Old Md.md")
        self.assertNotIn("title-equals-old", {p["type"] for p in res["problems"]}, res["problems"])
        self.assertTrue((self.zk / PN / "Old Md.md.md").is_file())
        stub = (self.zk / rel).read_text()
        self.assertIn("superseded_by: \"[[Old Md.md]]\"", stub)
        idx = validate.VaultIndex(self.zk)
        # the project's own Obsidian resolution: [[Old Md.md]] -> name "Old Md" (the old note)
        name = "Old Md.md".rsplit(".md", 1)[0]
        self.assertTrue(validate._resolves("Old Md.md", idx))
        self.assertIn(name, idx.titles)


class HoldsTitleVariants(_T):
    """Attacks that found nothing: case, inner/trailing spaces and a title equal to the old
    note after name_key are refused (`title-equals-old`); the old note becomes no stub with
    that title; nothing is written in place."""

    def _refused(self, old: str, title: str) -> None:
        before = (self.zk / PN / f"{old}.md").read_text() if (self.zk / PN / f"{old}.md").exists() else None
        res, rel = self.reply_with_title(old, title)
        types = {p["type"] for p in res["problems"]}
        self.assertTrue({"title-equals-old", "not-written"} & types, res["problems"])
        self.assertNotIn("superseded", (self.zk / rel).read_text())
        self.assertIn("legacy bullet 1", (self.zk / rel).read_text())

    def test_case(self) -> None:
        self._refused("Old Case", "old case")

    def test_double_space(self) -> None:
        self._refused("Old Space", "Old  Space")

    def test_exact(self) -> None:
        self._refused("Old Same", "Old Same")


class HoldsOtherVaultNote(_T):
    """Attack that found nothing: a new title equal to ANOTHER vault note that is not in the
    plan (a user note, any case, any folder) is never overwritten: title-equals-old (exact
    path in 01 Permanent Notes) or write_note's clash check (name_key over the whole vault)."""

    def test_user_note_in_permanent_and_other_folder(self) -> None:
        user = OLD_NOTE.format(t="User Owned")
        self.vpath("User Owned").write_text(user)
        (self.zk / "02 Examples").mkdir(exist_ok=True)
        (self.zk / "02 Examples" / "Example Owned.md").write_text(OLD_NOTE.format(t="Example Owned"))
        for title in ("User Owned", "user owned", "Example Owned"):
            with self.subTest(title=title):
                rel = self.old(f"Old For {title.replace(' ', '')}", 2)
                state = sc.state_path(self.staging)
                sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
                b = sc.next_batch(self.staging, self.zk, state)
                self.assertIsNotNone(b)
                text = self.ta.replace(f"# {self.a}\n", f"# {title}\n")
                old = rel.split("/")[-1]
                self.fake(repair=[f"=====NOTE: {title} | repairs: {old} | bullets: b1=====\n{text.rstrip()}\n"
                                  f"=====BULLETS=====\n{old}#b1 | kept in {title}\n"])
                u = self.new_unit("repair", [])
                res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                                     b["prior_titles"], b["last"])
                self.assertTrue({"title-equals-old", "not-written"} & {p["type"] for p in res["problems"]})
                self.assertFalse(list((u / "out").rglob(f"{title}.md")))
        self.assertEqual(self.vpath("User Owned").read_text(), user)


class DefectPriorTitleEqualsOld(_T):
    """Z-B13: `existing = title in prior_titles` skips BOTH title-equals-old and write_note's
    clash check (allow_existing=True). A repair_state.json whose `titles` holds the old
    note's own title (written by a pre-round-22 in-place repair, X-B2 of v9) lets the next
    batch rewrite the old note in place again: the old text leaves the vault. Only the
    after-the-fact two_state error sees it."""

    def test_prior_in_place_title_bypasses_the_refusal(self) -> None:
        import json as _j
        rel = self.old("Old Prior", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        st = _j.loads(state.read_text())
        st["notes"][rel]["titles"] = ["Old Prior"]  # the v9 in-place answer
        state.write_text(_j.dumps(st))
        text = self.ta.replace(f"# {self.a}\n", "# Old Prior\n")
        rep = (f"=====NOTE: Old Prior | repairs: Old Prior.md | bullets: b1, b2=====\n{text.rstrip()}\n"
               f"=====BULLETS=====\nOld Prior.md#b1 | kept in Old Prior\nOld Prior.md#b2 | kept in Old Prior\n")
        _b, _u, res, _ev = self.run_batch(state, rep)
        self.assertNotIn("title-equals-old", {p["type"] for p in res["problems"]}, res["problems"])
        now = (self.zk / rel).read_text()
        self.assertNotIn("legacy bullet 1", now)
        self.assertNotIn("superseded", now)
        self.assertTrue(any("Old Prior" in e for e in validate.two_state_errors(self.staging, self.zk)))
