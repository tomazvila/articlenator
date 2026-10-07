"""Round 25: the SHOULD-FIX items of the sixth review (frozen-v11, REVIEW.md), from the
reviewer's probes (tests/unit/test_probe11_*.py of review-v11), in the correct-behaviour
convention: every test asserts the CORRECT behaviour (PASS = fixed)."""
from __future__ import annotations

import json
import subprocess
import sys

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.probes_v8.test_probe8_B_attacks import _DriverEnv
from tests.probes_v9.test_probe9_D_exitcodes import _run
import tests.probes_v10.test_probe10_C_exitcodes as _cx
from tests.unit.test_run_integrity import OLD_NOTE, PN, SASA, ZR, _Env, agent, good_text, ri, validate
from tests.unit.test_run_integrity_r13 import work
from tests.unit.test_synth_single import V5, _Single, reply

import provenance  # noqa: E402
import synth_call as sc  # noqa: E402

DROP = {"decision": "dropped", "reason": "not in this transcript"}
R1 = "source transcript missing"


def _ops(staging) -> list[dict]:
    p = staging / "needs_operator.json"
    return json.loads(p.read_text()) if p.is_file() else []


# --------------------------------------------------------------------------- #
# Z-A1: no in-place repair write over an existing vault note
# --------------------------------------------------------------------------- #


class ZA1InPlace(_Env):
    def _write(self, published_before: bool) -> tuple[dict, str]:
        old = OLD_NOTE.format(t=SASA).replace("- legacy bullet\n", "- legacy bullet ONE\n- legacy bullet TWO\n")
        self.vpath(SASA).write_text(old)
        if published_before:  # an earlier synthesis run of this staging published the old note
            provenance.record(self.staging, "note-published", note=f"{PN}/{SASA}.md", run_id="20260101T000000Z-1")
        unit = self.new_unit("repair", [])
        agent.write_file(str(self.vpath(SASA)), good_text())
        self.approve(SASA, unit=unit)
        ev = ri.finalize(self.staging, self.run_dir, unit, [], 0, self.zk)
        return next(x for x in ev["notes"] if x["note"].endswith(f"{SASA}.md")), self.vpath(SASA).read_text()

    def test_pipeline_published_old_note_is_refused(self) -> None:
        n, now = self._write(published_before=True)
        self.assertEqual(n["action"], "quarantined", n)
        self.assertIn("title-equals-old", n["failure_types"])
        self.assertIn("legacy bullet ONE", now)

    def test_user_note_is_refused(self) -> None:
        n, now = self._write(published_before=False)
        self.assertEqual(n["action"], "quarantined", n)
        self.assertIn("legacy bullet ONE", now)

    def test_repair_written_target_may_be_extended(self) -> None:
        """R5: a target that an earlier unit of THIS repair wrote and published stays writable."""
        t = f"{PN}/{SASA}.md"
        self.vpath(SASA).write_text(good_text())
        unit = self.new_unit("repair", [])
        sha = ri._sha256(self.vpath(SASA))
        (unit / "publish.jsonl").write_text(json.dumps({"phase": "done", "rel": t, "sha256": sha}) + "\n")
        provenance.record(self.staging, "note-published", note=t, run_id=self.run_dir.name,
                          unit=unit.name, sha256=sha, unchanged=False)
        st = {"notes": {f"{PN}/Old Z.md": {"status": "open", "titles": [SASA], "bullets": {}}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        allowed = ri._repair_written_targets(self.staging, self.zk)
        self.assertIn(t, allowed)
        self.assertNotIn(f"{PN}/Other.md", allowed)


# --------------------------------------------------------------------------- #
# Z-A2: an all-dropped note is marked at the end of its repair
# --------------------------------------------------------------------------- #


class ZA2AllDroppedMarked(_DriverEnv):
    _old = _cx.RepairLoop._old

    def _passes(self, rel: str, rep: str) -> list[int]:
        codes = []
        for _ in range(4):
            r = _run("repair_loop.sh", self._env(), rel)
            codes.append(r.returncode)
            if r.returncode != 5:
                break
            work(self.xdir, lambda q: rep if q["kind"] == "repair" else None, worker="w1")
        codes.append(_run("repair_loop.sh", self._env(), rel).returncode)
        return codes

    def test_all_dropped_note_is_marked_and_exit_0(self) -> None:
        rel = self._old("Old Drop")
        codes = self._passes(rel, "=====BULLETS=====\nOld Drop.md#b1 | dropped: not in this transcript\n")
        now = (self.zk / rel).read_text()
        self.assertIn("unsupported_marked:", now)
        self.assertIn("no source video states any claim of this note", now)
        row = next(x for x in sc.repair_status(self.staging, self.zk) if x["note"] == rel)
        self.assertEqual(row["vault"], "marked-unsupported", row)
        self.assertFalse(row["unlisted"], row)
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])
        self.assertEqual(codes[-1], 0, codes)

    def test_unmarked_done_unsupported_note_is_operator_work(self) -> None:
        """Until it is marked, a done-unsupported note stays operator work (never exit 0)."""
        rel = self._old("Old Wait")
        st = {"notes": {rel: {"sha": ri._sha256(self.zk / rel), "status": "done-unsupported", "kind": "known",
                              "titles": [], "sources": [V5], "bullets": {}}}, "batches": {}}
        (self.staging / "repair_state.json").write_text(json.dumps(st))
        self.assertIn("done-unsupported: Old Wait", ri.operator_work(self.staging, self.zk))


# --------------------------------------------------------------------------- #
# Z-B5: mark only when every recorded source was asked
# --------------------------------------------------------------------------- #


class ZB5PartialSources(_Repair):
    def test_unasked_recorded_source_is_listed_not_marked(self) -> None:
        rel = self.old("Old Part", 1, srcs=(V5, "vid-NOTRANSCR01x"))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        st = json.loads(state.read_text())
        e = st["notes"][rel]
        self.assertEqual(e["sources"], [V5])
        self.assertEqual(e["recorded_sources"], [V5, "vid-NOTRANSCR01x"])
        for b in e["bullets"].values():
            b["answers"] = {V5: DROP}
        e["status"] = "done-unsupported"
        state.write_text(json.dumps(st))
        before = self.vpath("Old Part").read_bytes()
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        self.assertEqual(ri.mark_finished(self.staging, self.zk), [{"note": rel, "result": "listed"}])
        self.assertEqual(self.vpath("Old Part").read_bytes(), before)
        self.assertTrue([o for o in _ops(self.staging)
                         if o["note"] == rel and o["why"] == "source vid-NOTRANSCR01x has no transcript"], _ops(self.staging))
        self.assertTrue([w for w in ri.operator_work(self.staging, self.zk) if "has no transcript" in w])

    def test_every_recorded_source_asked_is_marked(self) -> None:
        rel = self.old("Old Full", 1, srcs=(V5,))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        st = json.loads(state.read_text())
        e = st["notes"][rel]
        for b in e["bullets"].values():
            b["answers"] = {V5: DROP}
        e["status"] = "done-unsupported"
        state.write_text(json.dumps(st))
        self.assertEqual(ri.mark_finished(self.staging, self.zk), [{"note": rel, "result": "marked"}])
        self.assertIn("no source video states any claim of this note", self.vpath("Old Full").read_text())
        self.assertEqual(validate.two_state_errors(self.staging, self.zk), [])


# --------------------------------------------------------------------------- #
# Z-B1..Z-B4, Z-B7: the mark shape
# --------------------------------------------------------------------------- #


class ZBMarkShape(_Repair):
    def _refused(self, title: str, raw: bytes, shape: str) -> None:
        self.vpath(title).write_bytes(raw)
        self.assertFalse(ri.mark_unsupported_note(self.staging, self.zk, f"{PN}/{title}.md", R1))
        self.assertEqual(self.vpath(title).read_bytes(), raw)  # unchanged, byte for byte
        self.assertTrue([o for o in _ops(self.staging) if title in o["note"] and o["why"] == f"mark refused: {shape}"],
                        _ops(self.staging))

    def test_multiline_verification_is_refused(self) -> None:
        self._refused("Old ML", b"---\ntype: permanent note\naliases:\n  - A\nverification:\n  - unverified\n"
                                b"tags: [x]\n---\n\n# Old ML\n\n- legacy bullet 1\n", "multi-line verification value")

    def test_duplicate_verification_is_refused(self) -> None:
        self._refused("Old Dup", b"---\ntype: permanent note\nverification: a\nverification: b\n---\n\n# Old Dup\n",
                      "duplicate verification key")

    def test_dots_closer_is_refused(self) -> None:
        self._refused("Old Dots", b"---\ntype: permanent note\n...\n# Old Dots\n\n- legacy bullet 1\n---\nmore text\n",
                      "frontmatter closed by ...")

    def test_crlf_is_refused(self) -> None:
        self._refused("Old CR", b"---\r\ntype: permanent note\r\ncreated: 2026-09-12\r\n---\r\n\r\n# Old CR\r\n",
                      "CRLF line endings")

    def test_bom_is_refused(self) -> None:
        self._refused("Old Bom", "﻿---\ntype: permanent note\n---\n\n# Old Bom\n".encode(), "byte order mark")

    def test_blank_line_before_closer_is_marked(self) -> None:
        raw = "---\ntype: permanent note\n\n---\n\n# Old Blank\n\n- legacy bullet 1\n"
        self.vpath("Old Blank").write_text(raw)
        self.assertTrue(ri.mark_unsupported_note(self.staging, self.zk, f"{PN}/Old Blank.md", R1))
        self.assertTrue(ri.is_unsupported_mark(raw, self.vpath("Old Blank").read_text()))

    def test_multiline_other_key_keeps_its_value(self) -> None:
        rel = self.old("Old Al", 1)
        old = self.vpath("Old Al").read_text()
        self.assertTrue(ri.mark_unsupported_note(self.staging, self.zk, rel, R1))
        new = self.vpath("Old Al").read_text()
        fo, fn = ri._mark_fm(old), ri._mark_fm(new)
        self.assertEqual({k: v for k, v in fn.items() if k not in ri.MARK_KEYS},
                         {k: v for k, v in fo.items() if k not in ri.MARK_KEYS})
        self.assertEqual(set(fn), set(fo) | set(ri.MARK_KEYS))

    def test_free_reason_is_not_a_clean_mark(self) -> None:
        self.old("Old Rs", 1)
        old = self.vpath("Old Rs").read_text()
        good = ri.unsupported_mark_text(old, R1)
        self.assertTrue(ri.is_unsupported_mark(old, good))
        self.assertFalse(ri.is_unsupported_mark(old, good.replace(f'"{R1}"', '"I think it is wrong"')))
        self.assertIsNone(ri.unsupported_mark_text(old, "I think it is wrong"))

    def test_parsed_key_sets_are_compared(self) -> None:
        self.old("Old Ks", 1)
        old = self.vpath("Old Ks").read_text()
        good = ri.unsupported_mark_text(old, R1)
        self.assertFalse(ri.is_unsupported_mark(old, good.replace("unsupported_marked:", "unsupported_marked_x:")))

    def test_cli_reason_is_a_closed_list(self) -> None:
        r = subprocess.run([sys.executable, str(ZR / "run_integrity.py"), "note-unsupported", "--staging", str(self.staging),
                            "--vault", str(self.zk), "--note", "x", "--reason", "free text"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


# --------------------------------------------------------------------------- #
# Z-C1, Z-C2: a request refused 3 times
# --------------------------------------------------------------------------- #


class ZC1RefusedForGood(_Repair):
    def _refuse3(self, notes: list[str] | None) -> tuple:
        xdir = self.staging / "exchange"
        (xdir / "requests").mkdir(parents=True, exist_ok=True)
        (xdir / "replies").mkdir(parents=True, exist_ok=True)
        key = "a" * 32
        body = {"key": key, "unit": "u1", "name": "review-x", "kind": "review"}
        if notes is not None:
            body["notes"] = notes
        (xdir / "requests" / f"{key}.json").write_text(json.dumps(body))
        for i in range(3):
            (xdir / "replies" / f"{key}.txt").write_text("bad")
            sc.refuse_reply(xdir, key, f"bad reply {i}", worker=f"w{i}")
        return xdir, key

    def test_obsolete_stays_when_asked_again(self) -> None:
        xdir, key = self._refuse3([f"{PN}/T.md"])
        self.assertTrue(sc.request_dead(xdir, key))
        self.assertEqual([r["key"] for r in sc.exchange_list(xdir, only_open=True)], [])
        # a pending folder that waits only for this key is no wait (no endless exit 5)
        pdir = self.staging / "pending-review" / "20261005T000000Z-1--u1"
        (pdir / "review-x").mkdir(parents=True)
        (pdir / "meta.json").write_text(json.dumps({"notes": [f"{PN}/T.md"], "reasons": {f"{PN}/T.md": "waiting-for-reply"},
                                                    "pending_cycles": 1, "kind": "repair"}))
        (pdir / "review-x" / "agent_result.json").write_text(json.dumps(
            {"end": "waiting-for-reply", "detail": f"waiting-for-reply: request {key} (x)"}))
        self.assertEqual(sc.count_waits(self.staging, "repair"), (0, 0))
        # a stored folder keeps only meta.json `wait_keys` (the review ran in a finalized unit)
        p2 = self.staging / "pending-review" / "20261005T000001Z-1--u2"
        p2.mkdir(parents=True)
        (p2 / "meta.json").write_text(json.dumps({"notes": [f"{PN}/T.md"], "reasons": {f"{PN}/T.md": "waiting-for-reply"},
                                                  "pending_cycles": 1, "kind": "repair", "wait_keys": [key]}))
        self.assertEqual(sc.count_waits(self.staging, "repair"), (0, 0))
        (p2 / "meta.json").write_text(json.dumps({"notes": [f"{PN}/T.md"], "reasons": {f"{PN}/T.md": "waiting-for-reply"},
                                                  "pending_cycles": 1, "kind": "repair", "wait_keys": ["b" * 32]}))
        self.assertEqual(sc.count_waits(self.staging, "repair"), (1, 0))  # a live key is still a wait

    def test_entry_names_the_note_and_operator_done_clears_it(self) -> None:
        self._refuse3([f"{PN}/T.md"])
        ops = _ops(self.staging)
        self.assertEqual([o["note"] for o in ops], [f"{PN}/T.md"])
        self.assertEqual(len(ops), 1)
        r = subprocess.run([sys.executable, str(ZR / "run_integrity.py"), "operator-done", "--staging", str(self.staging),
                            "--note", "T", "--reason", "answered by hand"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_ops(self.staging), [])

    def test_key_entry_clears_by_key(self) -> None:
        _x, key = self._refuse3(None)
        self.assertEqual([o["note"] for o in _ops(self.staging)], [f"exchange request {key}"])
        res = ri.operator_done(self.staging, key, "answered by hand")
        self.assertEqual(res["entries_removed"], 1)
        self.assertEqual(_ops(self.staging), [])

    def test_operator_done_without_match_is_nonzero_and_records_nothing(self) -> None:
        r = subprocess.run([sys.executable, str(ZR / "run_integrity.py"), "operator-done", "--staging", str(self.staging),
                            "--note", "No Such Note", "--reason", "x"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertFalse((self.staging / "operator_decisions.jsonl").exists())


class ZC1FilesCall(_Repair):
    def test_files_call_keeps_a_dead_key_obsolete(self) -> None:
        import os
        os.environ["ZR_LLM_BACKEND"] = "files"
        try:
            unit = self.new_unit("repair", [])
            msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
            s = {"model": "m", "max_tokens": 10}
            with self.assertRaises(sc.PendingReply) as cm:
                sc.files_call(unit, "review-a-0", msgs, s, {"notes": [f"{PN}/T.md"]})
            key = cm.exception.key
            xdir = sc.exchange_dir(self.staging)
            self.assertEqual(json.loads((xdir / "requests" / f"{key}.json").read_text())["notes"], [f"{PN}/T.md"])
            for i in range(3):
                (xdir / "replies" / f"{key}.txt").write_text("bad")
                sc.refuse_reply(xdir, key, "bad", worker=f"w{i}")
            with self.assertRaises(sc.PendingReply):
                sc.files_call(unit, "review-a-0", msgs, s, {"notes": [f"{PN}/T.md"]})
            self.assertTrue((xdir / "requests" / f"{key}.obsolete").is_file())
        finally:
            os.environ.pop("ZR_LLM_BACKEND", None)


# --------------------------------------------------------------------------- #
# Z-A3: a link line is accepted only in `## Connected Ideas`
# --------------------------------------------------------------------------- #


class ZA3LinkRule(_Repair):
    def test_link_line_in_details_is_an_error(self) -> None:
        orig = OLD_NOTE.format(t="User Three")
        new = orig.replace("## Details\n\n", "## Details\n\n- A new claim the pipeline added, see [[Other]]\n", 1)
        self.assertFalse(validate._only_links_added(orig, new))

    def test_link_line_in_connected_ideas_is_accepted(self) -> None:
        orig = OLD_NOTE.format(t="User Four")
        self.assertTrue(validate._only_links_added(orig, ri._add_connected_line(orig, "- [[Backlink Note]]")))

    def test_added_section_at_the_end_is_accepted(self) -> None:
        orig = "---\ntype: permanent note\n---\n\n# X\n\nText."
        self.assertTrue(validate._only_links_added(orig, ri._add_connected_line(orig, "- [[B]]")))

    def test_added_heading_before_old_text_is_an_error(self) -> None:
        orig = OLD_NOTE.format(t="User Five")
        new = orig.replace("## Details\n", "## Connected Ideas\n\n- [[X]] a claim\n\n## Details\n", 1)
        self.assertFalse(validate._only_links_added(orig, new))


# --------------------------------------------------------------------------- #
# Z28: a forced preflight is in the run summary
# --------------------------------------------------------------------------- #


class Z28ForcedInSummary(_Repair):
    def test_summary_lists_the_forced_findings(self) -> None:
        provenance.record(self.staging, "preflight-forced", findings=["legacy stub: X.md"])
        provenance.record(self.staging, "run-started", run_id="R2", kind="repair")
        self.assertEqual(ri._preflight_forced_for(self.staging, "R2"), ["legacy stub: X.md"])
        provenance.record(self.staging, "run-started", run_id="R3", kind="repair")
        self.assertEqual(ri._preflight_forced_for(self.staging, "R3"), [])


# --------------------------------------------------------------------------- #
# Z-C3: a consumed invalid review reply counts a pending cycle (files backend)
# --------------------------------------------------------------------------- #


class ZC3InvalidReplyCycle(_Single):
    def setUp(self) -> None:
        super().setUp()
        import os
        os.environ["ZR_LLM_BACKEND"] = "files"
        os.environ["ZR_REVIEW_RETRIES"] = "0"
        self.xdir = self.staging / "exchange"

    def _cycles(self) -> list[int]:
        return sorted(int(json.loads(p.read_text()).get("pending_cycles") or 0)
                      for p in (self.staging / "pending-review").glob("*--*/meta.json"))

    def test_invalid_reply_counts_and_no_reply_does_not(self) -> None:
        import os
        from pathlib import Path

        import review_call as rc
        self.fake()
        self.synth()
        work(self.xdir, lambda r: reply([("C1", "01:16", "a")], [(self.a, ["C1"], self.ta)]), worker="w1")
        u = self.new_unit("synth", [V5])
        sc.synth_unit(self.staging, self.zk, u, [V5])
        rc.review_unit(self.staging, self.zk, u)
        ri.finalize(self.staging, self.run_dir, u, [V5], 0, self.zk)
        self.assertEqual(self._cycles(), [0])  # waiting for a reply is no cycle

        def one_pass() -> None:
            for pu in ri.pending_units(self.staging, self.run_dir, os.getpid()):
                rc.review_unit(self.staging, self.zk, Path(pu))
                ri.finalize(self.staging, self.run_dir, Path(pu), [], 0, self.zk)

        one_pass()  # still no reply: no cycle
        self.assertEqual(self._cycles(), [0])
        work(self.xdir, lambda r: "not json" if r["kind"] == "review" else None, worker="w2")
        one_pass()  # an invalid reply was consumed and refused: one cycle
        self.assertEqual(self._cycles(), [1])
