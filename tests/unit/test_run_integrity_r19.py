"""Round 19: V7, V9, V10, V27, V29 in the correct-behaviour convention (from the review-v7
probes), plus pilot-7 items. Fake LLMs only."""
from __future__ import annotations

import json
import os
import re

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5, _Single, reply

import synth_call as sc  # noqa: E402


def _rep(old: str, title: str, bullets: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bullets}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class V7ExtendKeepsContent(_Repair):
    def test_rewrite_that_drops_evidence_is_refused_and_the_invariant_sees_files(self) -> None:
        os.environ["ZR_REPAIR_MAX_BULLETS"] = "1"
        rel = self.old("Old Ext", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.run_batch(state, _rep("Old Ext", self.a, "b1", self.ta, f"Old Ext.md#b1 | kept in {self.a}\n"))
        first = self.vpath(self.a).read_text()
        rec = ri.last_published(self.staging, f"{PN}/{self.a}.md")
        self.assertEqual(rec["bullets"], ["Old Ext.md#b1"])
        self.assertTrue(rec["evidence_tags"])
        tb_as_a = self.tb.replace(f"# {self.b}\n", f"# {self.a}\n")
        _b2, _u2, _res2, ev2 = self.run_batch(state, _rep("Old Ext", self.a, "b1,b2", tb_as_a,
                                                          f"Old Ext.md#b2 | kept in {self.a}\n"))
        n = [x for x in ev2["notes"] if x["note"].endswith(f"{self.a}.md")][0]
        self.assertIn("extend-lost-evidence", n["failure_types"])
        self.assertEqual(self.vpath(self.a).read_text(), first)  # the published version stays
        st = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(st["bullets"]["Old Ext.md#b1"]["answers"][V5]["decision"], "kept")  # b1 is still in A
        self.assertNotEqual(st["bullets"]["Old Ext.md#b2"]["answers"][V5].get("decision"), "kept")  # b2 open
        # the invariant reads the published FILE: drop A's Evidence by hand -> reported
        self.vpath(self.a).write_text(first.split("## Evidence")[0] + "## Evidence\n\n## Connected Ideas\n\n- [[Home]] — x.\n")
        self.assertTrue([v for v in ri.bullet_invariant(self.staging, self.zk) if "lost Evidence" in v["why"]])


class V9V10Stubs(_Repair):
    def _state(self, old: str, t1: str) -> str:
        rel = f"{PN}/{old}.md"
        # round 21: a stub carries the old text, so the old note is in the archive
        arch = self.staging / "retired" / "r0" / "u0" / PN / f"{old}.md"
        arch.parent.mkdir(parents=True, exist_ok=True)
        from tests.unit.test_run_integrity import OLD_NOTE
        arch.write_text(OLD_NOTE.format(t=old).replace("- legacy bullet\n", "- Planche lean presses can be done on the floor without parallettes.\n- Four sets of planche lean presses after the planche workout are an option.\n"))
        st = {"notes": {rel: {"sha": "x", "status": "done", "titles": [t1, self.b], "pos": 0, "sources": [V5],
                              "bullets": {f"{old}.md#b1": {"text": "Planche lean presses can be done on the floor without parallettes.", "answers": {V5: {"decision": "kept", "title": t1}}},
                                          f"{old}.md#b2": {"text": "Four sets of planche lean presses after the planche workout are an option.", "answers": {V5: {"decision": "kept", "title": self.b}}}},
                              "unsupported_bullets": []}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        return rel

    def test_V9_rename_by_respeak_keeps_the_stub_link(self) -> None:
        old = "Old Spk"
        unres = "Unresolved Speaker (Ch) Says Floor Presses Need No Parallettes"
        t_un = self.ta.replace(f"# {self.a}\n", f"# {unres}\n").replace("verification:", "speaker_status: unresolved\nverification:", 1)
        t_un = re.sub(r'(?m)^(\s+speaker:\s*).*$', r'\1"unresolved speaker, Ch video"', t_un, count=1)
        self.vpath(unres).write_text(t_un)
        ri.provenance.record(self.staging, "note-published", note=f"{PN}/{unres}.md", sources=[V5])
        self.vpath(self.b).write_text(self.tb)
        self.vpath(old).write_text(ri.repair_stub(old, [unres, self.b]))
        rel = self._state(old, unres)
        out = sc.respeak(self.staging, self.zk, V5, "Sasha", apply=True, force_name=True)
        new_title = out[0]["to"][:-3]
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = self.vpath(old).read_text()
        self.assertIn(f"[[{new_title}]]", stub)
        self.assertNotIn("bullet: ", stub)
        self.assertEqual(json.loads((self.staging / "repair_state.json").read_text())["notes"][rel]["titles"][0], new_title)
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])

    def test_V10_a_stub_never_points_to_a_stub(self) -> None:
        old = "Old Chain"
        a2 = "Second Version Of Floor Presses"
        self.vpath(a2).write_text(self.ta.replace(f"# {self.a}\n", f"# {a2}\n"))
        self.vpath(self.a).write_text(ri.repair_stub(self.a, [a2]))
        self.vpath(self.b).write_text(self.tb)
        self.vpath(old).write_text(ri.repair_stub(old, [self.a, self.b]))
        self._state(old, self.a)
        self.assertTrue([v for v in ri.bullet_invariant(self.staging, self.zk) if "a stub" in v["why"]])
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        stub = self.vpath(old).read_text()
        self.assertIn(f"[[{a2}]]", stub)
        self.assertNotIn(f"[[{self.a}]]", stub)
        self.assertNotIn("bullet: ", stub)
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])


class V29V27Links(_Single):
    def test_V29_dead_link_in_the_lead_is_refused_before_publish(self) -> None:
        lead = [ln for ln in self.ta.split("\n") if "[src:" in ln][0]
        bad = self.ta.replace(lead, lead.replace(" [src:", " (see [[Nowhere Note]]) [src:", 1), 1)
        self.fake(synth=[reply([("C1", "01:16", "a")], [(self.a, ["C1"], bad)])])
        self.synth()
        chk = ri.check_unit(self.staging, self.unit, self.zk)
        self.assertIn("dead-link", {f["type"] for f in chk[f"{PN}/{self.a}.md"]["failures"]})

    def test_V27_recovery_unlinks_a_sibling_that_never_published(self) -> None:
        import review_call as rc
        ta = self.ta.replace("- [[Home]] — home map.", f"- [[{self.b}]] — sibling.")
        tb = self.tb.replace("- [[Home]] — home map.", f"- [[{self.a}]] — sibling.")
        self.fake(synth=[reply([("C1", "01:16", "a"), ("C2", "01:22", "b")], [(self.a, ["C1"], ta), (self.b, ["C2"], tb)])])
        self.synth()
        rc.review_unit(self.staging, self.zk, self.unit)
        orig = ri._Publisher.write
        calls = {"n": 0}

        def boom(pub, rel, text, why):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt("killed")
            return orig(pub, rel, text, why)
        ri._Publisher.write = boom
        try:
            ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        except KeyboardInterrupt:
            pass
        finally:
            ri._Publisher.write = orig
        info = json.loads((self.unit / "unit.json").read_text())
        info["owner_pid"] = 999999999
        (self.unit / "unit.json").write_text(json.dumps(info))
        ri.recover(self.staging, self.zk)
        for p in (self.vpath(self.a), self.vpath(self.b)):
            if p.is_file():
                other = self.b if p.stem == self.a else self.a
                if not self.vpath(other).is_file():
                    self.assertNotIn(f"[[{other}]]", p.read_text())  # plain text, no dead link
                    self.assertIn("[[Home]]", p.read_text())


class UnassistedB3(_Repair):
    """Round 20: pilot 7 "Unassisted" b3: the ledger said "corrected in T", but T cites no
    time near the bullet's passage. The bullet stays open (in the stub), never settled."""

    def test_unverified_ledger_claim_stays_open(self) -> None:
        rel = self.old("Old Una", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        far = self.ta  # A cites only its own times
        rep = _rep("Old Una", self.a, "b1,b2", far,
                   f"Old Una.md#b1 | kept in {self.a}\nOld Una.md#b2 | corrected in {self.a}\n")
        lit = (self.staging / "lit" / f"{V5}.md").read_text()
        cand = {"Old Una.md#b2": [{"at": "59:00", "text": "x", "score": 1, "pair": False, "overlap": 0.1}],
                "Old Una.md#b1": []}
        p = sc.parse_reply(rep)
        sc._verify_ledger(p, cand, self.zk)
        self.assertEqual(p["bullets"]["Old Una.md#b2"]["decision"], "unverified")
        # round 21 (W2): no candidate passage: no check is possible -> unverified, never skipped
        self.assertEqual(p["bullets"]["Old Una.md#b1"]["decision"], "unverified")
        e = {"bullets": {"x#b1": {"answers": {"v": {"decision": "unverified"}}}}, "titles": [], "kind": "known"}
        sc._close(self.staging, "r", e)
        self.assertEqual((e["unsupported_bullets"], e["unverified_bullets"]), ([], ["x#b1"]))
        _ = lit

    def test_time_near_but_other_content_is_unverified(self) -> None:
        """Pilot 7: the target cites 05:05 (balance, lock the elbows), 10 s from b3's passage at
        05:15 (one rep at a time). The time alone passed; the quote shares no content with b3."""
        note = (f"# {self.a}\n\nBody. [src: {V5} @ 05:05]\n\n## Evidence\n\n"
                f'- {V5} @ 05:05 (Speaker): "the second is the balance, the balance is not that easy, '
                'especially to lock the elbows and to keep the handstand"\n')
        p = {"notes": [{"title": self.a, "text": note, "complete": True, "truncated": False}], "problems": [],
             "bullets": {"O.md#b2": {"decision": "kept", "title": self.a},
                         "O.md#b3": {"decision": "corrected", "title": self.a}}}
        cand = {b: [{"at": "05:15", "text": "x"}] for b in ("O.md#b2", "O.md#b3")}
        texts = {"O.md#b2": "Balance is the second challenge; locking the elbows at the top needs control.",
                 "O.md#b3": "The trainee should focus on one rep at a time; one clean rep signals readiness."}
        sc._verify_ledger(p, cand, self.zk, texts)
        self.assertEqual(p["bullets"]["O.md#b2"]["decision"], "kept")
        self.assertEqual(p["bullets"]["O.md#b3"]["decision"], "unverified")


class UnassistedSequence(_Repair):
    """Round 20: the exact pilot-7 sequence of "Unassisted" (batch f78ddb1d56cd5c4e). Pass 1:
    the repair reply is applied and the answers are recorded, but the replacement waits in
    pending review, so the old note is not a stub yet. Pass 2 starts with repair-groups. In v7
    (synth_call.py:2392-2405) the note was planned again: the answers were cleared and the batch
    was handed out again until it failed (2486-2500) with every bullet "unsupported". The pending
    unit then published the target and wrote the stub from the unit ledger, with no pending line
    for b3, and the invariant (run_integrity.py:2106-2119) treated "unsupported" as accounted."""

    def test_a_second_pass_keeps_the_answers_and_the_stub_keeps_open_bullets(self) -> None:
        rel = self.old("Old Una", 3)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        ledger = "".join(f"Old Una.md#b{i} | corrected in {self.a}\n" for i in (1, 2, 3))
        _b, u, _res, _ = self.run_batch(state, _rep("Old Una", self.a, "b1,b2,b3", self.ta, ledger),
                                        review=False, finalize=False)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))  # the driver loop: no more videos -> closed
        before = json.loads(state.read_text())["notes"][rel]
        self.assertNotEqual(before["status"], "open")
        answers = {b: d["answers"] for b, d in before["bullets"].items()}
        self.assertTrue(all(answers.values()))
        # pass 2 starts: the old note is unchanged in the vault (no stub yet)
        out = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertEqual(out["skipped"], [{"note": rel, "state": f"repair-{before['status']}"}])
        after = json.loads(state.read_text())["notes"][rel]
        self.assertEqual({b: d["answers"] for b, d in after["bullets"].items()}, answers)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))  # never handed out again
        # the pending unit finishes later: review and finalize
        import review_call as rc
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        e = json.loads(state.read_text())["notes"][rel]
        stub = self.vpath("Old Una").read_text()
        held = set(ri.last_published(self.staging, f"{PN}/{self.a}.md").get("bullets") or [])
        listed = set(re.findall(r"bullet: (Old Una\.md#b\d)", stub))
        recorded = set(e.get("unsupported_bullets", []) + e.get("unverified_bullets", []) + e.get("damaged_bullets", []))
        for b in ("Old Una.md#b1", "Old Una.md#b2", "Old Una.md#b3"):
            self.assertTrue(b in held or b in listed, (b, held, listed))  # in a published note or in the stub
            if b not in held:
                self.assertIn(b, recorded)
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])


class Y9UntimedClaim(_Single):
    """Round 20 (Y9 rest): a claim with no time is covered only when an Evidence quote of the
    note shares two content words (4+ letters) with the claim's paraphrase."""

    NOTE = ("# T\n\nBody. [src: vid-5zALUKd7h3g @ 01:00]\n\n## Evidence\n\n"
            '- vid-5zALUKd7h3g @ 01:00 (Speaker): "Keep the elbows locked and lean forward slowly."\n')

    def test_shared_words_cover(self) -> None:
        self.assertTrue(sc._cites_claim(self.NOTE, {"id": "C1", "text": "Lean forward with locked elbows"}, ""))

    def test_other_words_do_not_cover(self) -> None:
        self.assertFalse(sc._cites_claim(self.NOTE, {"id": "C2", "text": "Rest three minutes between sets"}, ""))

    def test_body_words_do_not_count(self) -> None:
        note = self.NOTE.replace("Body.", "Rest three minutes between sets.")
        self.assertFalse(sc._cites_claim(note, {"id": "C2", "text": "Rest three minutes between sets"}, ""))

    def test_timed_claim_uses_the_window(self) -> None:
        self.assertTrue(sc._cites_claim(self.NOTE, {"id": "C3", "text": "x", "at": "00:30"}, ""))
        self.assertFalse(sc._cites_claim(self.NOTE, {"id": "C3", "text": "x", "at": "09:00"}, ""))


class EvidenceRemovedByFix(_Repair):
    """Round 20 (replay of pilot 7, "Unassisted" b3): the repair reply cited 05:15 ("only one
    rep") for b3; the review judged that item not-in-source and the review fix removed it. The
    ledger still said "corrected in T", so b3 counted as settled with no text for it anywhere.
    Now the Evidence items that carried a bullet are recorded (`cited`); when the published
    text has none of them, the bullet opens again (unverified, in the stub)."""

    def _run(self, keep: bool) -> tuple[dict, str]:
        import review_call as rc
        rel = self.old("Old Una", 2)
        # round 22 (X16): bullets with real content (an unmarked pointer needs it in the target)
        p_ = self.vpath("Old Una")
        p_.write_text(p_.read_text().replace("- legacy bullet 1\n", "- Planche lean presses can be done on the floor without parallettes.\n").replace("- legacy bullet 2\n", "- Planche lean presses can be done on the floor without parallettes.\n"))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        ledger = f"Old Una.md#b1 | kept in {self.a}\nOld Una.md#b2 | kept in {self.a}\n"
        _b, u, _res, _ = self.run_batch(state, _rep("Old Una", self.a, "b1,b2", self.ta, ledger),
                                        review=False, finalize=False)
        tags = sorted(ri._evidence_tags(self.ta))
        st = json.loads(state.read_text())
        a = st["notes"][rel]["bullets"]["Old Una.md#b1"]["answers"][V5]
        a["cited"] = [list(tags[0])] if keep else [[V5, 5999]]  # 5999 s: an item the fix removed
        state.write_text(json.dumps(st))
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        return json.loads(state.read_text())["notes"][rel], self.vpath("Old Una").read_text()

    def test_removed_evidence_opens_the_bullet(self) -> None:
        e, stub = self._run(keep=False)
        self.assertEqual(e["bullets"]["Old Una.md#b1"]["answers"][V5]["decision"], "unverified")
        self.assertIn("bullet: Old Una.md#b1", stub)
        self.assertNotIn("bullet: Old Una.md#b2", stub)  # no `cited`: unchanged behavior
        self.assertEqual(ri.bullet_invariant(self.staging, self.zk), [])

    def test_kept_evidence_keeps_the_bullet_settled(self) -> None:
        e, stub = self._run(keep=True)
        self.assertEqual(e["bullets"]["Old Una.md#b1"]["answers"][V5]["decision"], "kept")
        self.assertNotIn("bullet: Old Una.md#b1", stub)
