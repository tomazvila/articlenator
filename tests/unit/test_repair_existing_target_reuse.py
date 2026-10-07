"""Narrow cross-old-note reuse through the real repair/apply/finalize path."""
from __future__ import annotations

import copy
import json
import os
import re

from tests.unit.test_run_integrity import PN, provenance, ri
import synth_call as sc
from tests.unit.test_synth_single import RepairSingle, V5


class ExistingTargetReuse(RepairSingle):
    def _ready_state(self, title_status="source-checked", recorded_video=None, unrelated=False,
                     multi=False, second_status="source-checked", second_video=None):
        owner = self.old("Old Owner")
        current = self.old("Old Cross")
        bid = "Old Cross.md#b1"
        oldpath = self.vpath("Old Cross")
        body = oldpath.read_text().replace(
            "- legacy bullet 1\n",
            ("- A completely unrelated claim about market returns and portfolio performance.\n" if unrelated else
             "- You can do planche lean presses on the floor without parallettes.\n"),
        )
        oldpath.write_text(body)
        targets = [self.a]
        target = self.vpath(self.a)
        target.write_text(self.ta.replace("verification: unverified", "verification: source-checked"))
        if title_status:
            provenance.record(
                self.staging, "note-published", note=f"{PN}/{self.a}.md",
                sources=[recorded_video or V5], verification=title_status,
                sha256=ri._sha256(target), created=True,
                bullets=["Old Owner.md#b1"], evidence_tags=[[V5, 76]],
            )
        if multi:
            targets.append(self.b)
            second = self.vpath(self.b)
            second_text = re.sub(r"(?m)^# .+$", f"# {self.b}", self.ta, count=1)
            second.write_text(second_text.replace("verification: unverified", "verification: source-checked"))
            if second_status:
                provenance.record(
                    self.staging, "note-published", note=f"{PN}/{self.b}.md",
                    sources=[second_video or V5], verification=second_status,
                    sha256=ri._sha256(second), created=True,
                    bullets=["Old Owner.md#b1"], evidence_tags=[[V5, 76]],
                )
        state = self.plan([owner, current])
        st = json.loads(state.read_text())
        st["notes"][owner]["status"] = "done"
        st["notes"][owner]["bullets"]["Old Owner.md#b1"]["answers"][V5] = {
            "decision": "kept", "title": targets[0], "titles": targets,
        }
        state.write_text(json.dumps(st))
        os.environ["ZR_REPAIR_MAX_NOTES"] = "1"
        batch = sc.next_batch(self.staging, self.zk, state)
        self.assertEqual(batch["notes"], [current])
        self.assertEqual(batch["video_notes"], {t: ["Old Owner.md#b1"] for t in targets})
        return owner, current, bid, state, batch, {t: self.vpath(t) for t in targets}

    def _run_ledger(self, batch, state, titles, ordinary=False):
        title_list = titles if isinstance(titles, list) else [titles]
        reply = ""
        if ordinary:
            text = re.sub(r"(?m)^# .+$", "# Repair Ordinary", self.ta, count=1)
            reply += f"=====NOTE: Repair Ordinary | repairs: Old Cross.md | bullets: b1=====\n{text.rstrip()}\n"
            title_list = ["Repair Ordinary", *title_list]
        reply += "=====BULLETS=====\nOld Cross.md#b1 | kept in " + "; ".join(title_list) + "\n"
        self.fake(repair=[reply])
        unit = self.new_unit("repair", [])
        result = sc.repair_unit(
            self.staging, self.zk, unit, batch["vid"], batch["notes"], batch["unknown"],
            batch.get("bullets"), batch.get("prior_titles"), batch.get("last"),
            batch.get("prior_bullets"), batch.get("video_notes"),
        )
        sc.record_answers(state, batch, result)
        return unit, result

    def _ledger_target_note(self, title, claims, evidence):
        frontmatter = self.ta[:self.ta.index("# ")].replace(
            "verification: unverified", "verification: source-checked")
        connected = self.ta[self.ta.index("## Connected Ideas\n"):]
        body = [f"# {title}", "", *claims, "", "## Evidence", "", *evidence, ""]
        return frontmatter + "\n".join(body) + "\n" + connected

    def _verify_two_target_ledger(self, second_claim, second_quote):
        _owner, current, bid, _state, batch, targets = self._ready_state(multi=True)
        high_quote = "If you want, you can do it just on wrist, on the floor."
        lower_quote = "But don't do it with fingers forward; rotate them with the thumb forward or backward."
        note_a = self._ledger_target_note(self.a, [
            f"Planche lean presses can be done on the floor without parallettes. [src: {V5} @ 01:16]",
            f"Planche presses on wrists. [src: {V5} @ 01:22]",
        ], [
            f'- {V5} @ 01:16 (Radoslav Radev): "{high_quote}"',
            f'- {V5} @ 01:22 (Radoslav Radev): "{lower_quote}"',
        ])
        note_b = self._ledger_target_note(self.b, [second_claim], [
            f'- {V5} @ 01:22 (Radoslav Radev): "{second_quote}"',
        ])
        for title, note in ((self.a, note_a), (self.b, note_b)):
            target = targets[title]
            target.write_text(note)
            provenance.record(
                self.staging, "note-published", note=f"{PN}/{title}.md",
                sources=[V5], verification="source-checked", sha256=ri._sha256(target),
                created=True, bullets=["Old Owner.md#b1"], evidence_tags=[[V5, 76]],
            )
        bullet_text = "You can do planche lean presses on the floor without parallettes."
        bullets = {bid: {"decision": "kept", "titles": [self.a, self.b]}}
        parsed = {
            "notes": [
                {"title": self.a, "text": note_a, "complete": True, "truncated": False},
                {"title": self.b, "text": note_b, "complete": True, "truncated": False},
            ],
            "bullets": bullets,
            "problems": [],
        }
        cand_by = {bid: [
            {"at": "01:16", "text": bullet_text},
            {"at": "01:22", "text": bullet_text},
        ]}
        sc._verify_ledger(parsed, cand_by, self.zk, {bid: bullet_text}, V5)
        linked = sc._linked_existing_targets(
            self.staging, self.zk, V5, {bid: (current, bullet_text)},
            batch["video_notes"], parsed["bullets"],
        )
        return bid, parsed, linked

    def test_heterogeneous_multi_target_evidence_selects_best_per_target_and_keeps_claim_lines(self):
        bid, parsed, linked = self._verify_two_target_ledger(
            f"Planche presses on wrists. [src: {V5} @ 01:22]",
            "But don't do it with fingers forward; rotate them with the thumb forward or backward.",
        )
        high_quote = "If you want, you can do it just on wrist, on the floor."
        lower_quote = "But don't do it with fingers forward; rotate them with the thumb forward or backward."
        self.assertEqual(parsed["bullets"][bid]["cited"], sorted([
            [V5, 76, sc._qhash('\"' + high_quote + '\"')],
            [V5, 82, sc._qhash('\"' + lower_quote + '\"')],
        ]))
        claim_lines = {title: line for title, line in parsed["bullets"][bid]["claim_lines"]}
        self.assertIn("Planche lean presses can be done", claim_lines[self.a])
        self.assertIn("Planche presses on wrists", claim_lines[self.b])
        self.assertEqual({item["title"] for item in linked}, {self.a, self.b})

    def test_invalid_second_target_evidence_rejects_entire_link_bundle(self):
        bid, parsed, linked = self._verify_two_target_ledger(
            f"An unrelated statement about portfolio returns. [src: {V5} @ 01:22]",
            "A completely unrelated statement about market returns and investing.",
        )
        self.assertEqual(len(parsed["bullets"][bid]["cited"]), 1)
        self.assertEqual(linked, [])

    def test_mixed_timed_and_untimed_target_evidence_sorts_safely(self):
        _owner, _current, bid, _state, _batch, _targets = self._ready_state(multi=True)
        bullet_text = "You can do planche lean presses on the floor without parallettes."
        timed_quote = "If you want, you can do it just on wrist, on the floor."
        untimed_quote = bullet_text
        note_a = self._ledger_target_note(self.a, [
            f"{bullet_text} [src: {V5} @ 01:16]",
        ], [f'- {V5} @ 01:16 (Radoslav Radev): "{timed_quote}"'])
        note_b = self._ledger_target_note(self.b, [f"{bullet_text} [src: {V5}]"], [
            f'- {V5} (Radoslav Radev): "{untimed_quote}"',
        ])
        parsed = {
            "notes": [
                {"title": self.a, "text": note_a, "complete": True, "truncated": False},
                {"title": self.b, "text": note_b, "complete": True, "truncated": False},
            ],
            "bullets": {bid: {"decision": "kept", "titles": [self.a, self.b]}},
            "problems": [],
        }
        sc._verify_ledger(
            parsed, {bid: [{"at": "01:16", "text": bullet_text}]},
            self.zk, {bid: bullet_text}, V5,
        )
        cited = parsed["bullets"][bid]["cited"]
        self.assertEqual(len(cited), 2)
        self.assertEqual({(item[0], item[1]) for item in cited}, {(V5, 76), (V5, None)})

    def test_link_only_reuse_publishes_old_stub_without_rewriting_target(self):
        owner, current, bid, state, batch, targets = self._ready_state()
        target_bytes = targets[self.a].read_bytes()
        owner_before = copy.deepcopy(json.loads(state.read_text())["notes"][owner])
        unit, result = self._run_ledger(batch, state, self.a)
        self.assertEqual(result["written"], [])
        self.assertEqual([(x["old"], x["bullet"], x["title"]) for x in result["linked_existing"]],
                         [(current, bid, self.a)])
        self.assertTrue(result["linked_existing"][0]["claim_lines"])
        self.assertEqual(targets[self.a].read_bytes(), target_bytes)
        answer = json.loads(state.read_text())["notes"][current]["bullets"][bid]["answers"][V5]
        self.assertEqual(answer["linked_existing"], [self.a])
        self.assertEqual(json.loads(state.read_text())["notes"][owner], owner_before)
        self.assertEqual(json.loads((unit / "repair_map.json").read_text())[current]["targets"], [self.a])
        ev = self.finalize(unit=unit)
        old_fm = ri.verify_claims.split_frontmatter(self.vpath("Old Cross").read_text())[0]
        self.assertTrue(ri.verify_claims.is_stub(old_fm))
        self.assertIn(f"[[{self.a}]]", old_fm["superseded_by"])
        self.assertEqual(targets[self.a].read_bytes(), target_bytes)
        self.assertEqual(self.note(ev, "Old Cross")["action"], "published")

    def _assert_rejected(self, mode):
        owner, current, bid, state, batch, targets = self._ready_state(
            title_status="" if mode == "unreviewed" else "source-checked",
            recorded_video="vid-other-source" if mode == "wrong-video" else None,
            unrelated=mode == "unrelated",
        )
        if mode == "missing":
            targets[self.a].unlink()
        if mode == "unlisted":
            batch["video_notes"] = {}
        unit, result = self._run_ledger(batch, state, self.a)
        answer = json.loads(state.read_text())["notes"][current]["bullets"][bid]["answers"][V5]
        self.assertEqual(result.get("linked_existing"), [])
        self.assertNotIn(answer.get("decision"), ("kept", "corrected"))
        self.assertFalse(answer.get("linked_existing"))
        if (unit / "repair_map.json").exists():
            self.assertFalse(json.loads((unit / "repair_map.json").read_text()).get(current))
        if mode != "missing":
            self.assertTrue(targets[self.a].exists())

    def test_missing_target_is_not_linked(self):
        self._assert_rejected("missing")

    def test_unreviewed_target_is_not_linked(self):
        self._assert_rejected("unreviewed")

    def test_wrong_video_target_is_not_linked(self):
        self._assert_rejected("wrong-video")

    def test_unlisted_target_is_not_linked(self):
        self._assert_rejected("unlisted")

    def test_unrelated_bullet_evidence_is_not_linked(self):
        self._assert_rejected("unrelated")

    def _assert_mixed_set_rejected(self, mode):
        _owner, current, bid, state, batch, targets = self._ready_state(
            multi=True,
            second_status="" if mode in ("pending", "unreviewed") else "source-checked",
            second_video="vid-other-source" if mode == "wrong-video" else None,
        )
        if mode == "missing":
            targets[self.b].unlink()
        if mode == "unlisted":
            batch["video_notes"].pop(self.b)
        unit, result = self._run_ledger(batch, state, [self.a, self.b])
        answer = json.loads(state.read_text())["notes"][current]["bullets"][bid]["answers"][V5]
        self.assertEqual(result.get("linked_existing"), [])
        self.assertEqual(answer["decision"], "unaccounted")
        self.assertFalse(answer.get("linked_existing"))
        if (unit / "repair_map.json").exists():
            self.assertFalse(json.loads((unit / "repair_map.json").read_text()).get(current))

    def test_mixed_set_with_pending_target_fails_atomically(self):
        self._assert_mixed_set_rejected("pending")

    def test_mixed_set_with_missing_target_fails_atomically(self):
        self._assert_mixed_set_rejected("missing")

    def test_mixed_set_with_wrong_video_target_fails_atomically(self):
        self._assert_mixed_set_rejected("wrong-video")

    def test_mixed_set_with_unlisted_target_fails_atomically(self):
        self._assert_mixed_set_rejected("unlisted")

    def test_stale_one_of_two_targets_invalidates_whole_bullet_bundle(self):
        _owner, current, bid, state, batch, targets = self._ready_state(multi=True)
        old_bytes = self.vpath("Old Cross").read_bytes()
        a_bytes = targets[self.a].read_bytes()
        unit, result = self._run_ledger(batch, state, [self.a, self.b])
        self.assertEqual({x["title"] for x in result["linked_existing"]}, {self.a, self.b})
        targets[self.b].write_text(targets[self.b].read_text() + "\nExternal drift.\n")
        b_after_edit = targets[self.b].read_bytes()
        self.finalize(unit=unit)
        answer = json.loads(state.read_text())["notes"][current]["bullets"][bid]["answers"][V5]
        self.assertEqual(answer["decision"], "unverified")
        self.assertFalse(answer.get("linked_existing"))
        rmap = json.loads((unit / "repair_map.json").read_text())[current]
        self.assertEqual(rmap["targets"], [])
        self.assertFalse(rmap.get("linked_existing"))
        self.assertEqual(self.vpath("Old Cross").read_bytes(), old_bytes)
        self.assertEqual(targets[self.a].read_bytes(), a_bytes)
        self.assertEqual(targets[self.b].read_bytes(), b_after_edit)

    def test_prior_video_link_titles_survive_current_video_stale_validation(self):
        _owner, current, bid, state, batch, targets = self._ready_state(multi=True)
        unit, _result = self._run_ledger(batch, state, self.a)
        st = json.loads(state.read_text())
        entry = st["notes"][current]
        entry["status"] = "done"
        entry["linked_titles"] = [self.a, "Prior Pending"]
        entry["bullets"][bid]["answers"]["vid-prior"] = {
            "decision": "kept", "titles": ["Prior Pending"], "linked_existing": ["Prior Pending"],
        }
        state.write_text(json.dumps(st))
        targets[self.a].write_text(targets[self.a].read_text() + "\nExternal drift.\n")
        self.finalize(unit=unit)
        entry = json.loads(state.read_text())["notes"][current]
        self.assertEqual(entry["linked_titles"], ["Prior Pending"])
        self.assertEqual(entry["bullets"][bid]["answers"]["vid-prior"]["linked_existing"], ["Prior Pending"])
        self.assertEqual(entry["status"], "done")
        self.assertIn(bid, ri._repair_view(self.staging, self.zk, entry)["open"])

    def test_mixed_ordinary_and_pending_external_target_is_unaccounted(self):
        _owner, current, bid, state, batch, targets = self._ready_state(title_status="")
        unit, result = self._run_ledger(batch, state, self.a, ordinary=True)
        self.assertEqual(result.get("linked_existing"), [])
        self.assertIn("Repair Ordinary", result["written"])
        answer = json.loads(state.read_text())["notes"][current]["bullets"][bid]["answers"][V5]
        self.assertEqual(answer["decision"], "unaccounted")
        self.assertFalse(answer.get("linked_existing"))
        self.finalize(unit=unit)
        entry = json.loads((unit / "repair_map.json").read_text())[current]
        self.assertEqual(entry["targets"], ["Repair Ordinary"])
        self.assertFalse(entry.get("linked_existing"))
        self.assertTrue(targets[self.a].exists())

    def test_mixed_ordinary_and_existing_stale_target_keeps_classes_consistent(self):
        _owner, current, bid, state, batch, targets = self._ready_state()
        unit, result = self._run_ledger(batch, state, self.a, ordinary=True)
        self.assertIn("Repair Ordinary", result["written"])
        self.assertEqual([x["title"] for x in result["linked_existing"]], [self.a])
        targets[self.a].write_text(targets[self.a].read_text() + "\nExternal drift.\n")
        self.finalize(unit=unit)
        entry = json.loads(state.read_text())["notes"][current]
        answer = entry["bullets"][bid]["answers"][V5]
        self.assertEqual(answer["decision"], "unverified")
        self.assertEqual(answer["titles"], ["Repair Ordinary"])
        self.assertEqual(entry["titles"], ["Repair Ordinary"])
        self.assertEqual(entry["linked_titles"], [])
        self.assertIn(bid, ri._repair_view(self.staging, self.zk, entry)["open"])
        rmap = json.loads((unit / "repair_map.json").read_text())[current]
        self.assertEqual(rmap["targets"], ["Repair Ordinary"])
        self.assertEqual(rmap.get("ordinary_targets"), ["Repair Ordinary"])
        self.assertFalse(rmap.get("linked_existing"))

    def test_target_hash_drift_before_commit_cancels_link_and_stub(self):
        _owner, current, bid, state, batch, targets = self._ready_state()
        old_bytes = self.vpath("Old Cross").read_bytes()
        unit, result = self._run_ledger(batch, state, self.a)
        self.assertEqual(len(result["linked_existing"]), 1)
        targets[self.a].write_text(targets[self.a].read_text() + "\nExternal drift.\n")
        target_after_edit = targets[self.a].read_bytes()
        linked = json.loads((unit / "repair_map.json").read_text())[current]["linked_existing"][0]
        self.assertNotEqual(ri._sha256(targets[self.a]), linked["sha256"])
        ev = self.finalize(unit=unit)
        answer = json.loads(state.read_text())["notes"][current]["bullets"][bid]["answers"][V5]
        self.assertEqual(answer["decision"], "unverified")
        self.assertFalse(answer.get("linked_existing"))
        self.assertEqual(self.vpath("Old Cross").read_bytes(), old_bytes)
        self.assertEqual(targets[self.a].read_bytes(), target_after_edit)
        fm = ri.verify_claims.split_frontmatter(self.vpath("Old Cross").read_text())[0]
        self.assertFalse(ri.verify_claims.is_stub(fm))
        problems = json.loads((unit / "claims.json").read_text()).get("problems", [])
        self.assertIn("linked-existing-stale", {p["type"] for p in problems})
        self.assertNotIn("Old Cross", {n["note"].split("/")[-1].removesuffix(".md") for n in ev["notes"]})
