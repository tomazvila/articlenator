"""Probe 8 A2 (reviewer A2, round 3): kills, recovery and the round-20 repair_groups skip.

Convention of THIS file: the assertion states the DEFECT. PASS = the defect is present.
FAIL = the defect is not present (or the setup broke; read the failure). Fake LLMs only."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5

import review_call as rc  # noqa: E402
import synth_call as sc  # noqa: E402


def _rep(old: str, title: str, bids: str, text: str, ledger: str) -> str:
    return f"=====NOTE: {title} | repairs: {old}.md | bullets: {bids}=====\n{text.rstrip()}\n=====BULLETS=====\n{ledger}"


class _Kill(_Repair):
    def silent_losses(self, rel: str, state: Path) -> list[str]:
        """Bullets of `rel` that are in no published final target, not listed in the stub, not
        dropped by a ledger answer, while the summary lists nothing for the note."""
        st = json.loads(state.read_text())
        e = st["notes"][rel]
        p = self.zk / rel
        fm = ri.verify_claims.split_frontmatter(p.read_text())[0] if p.is_file() else {}
        old_unchanged = p.is_file() and ri._sha256(p) == e.get("sha")
        listed = {str(x).split("bullet: ", 1)[-1] for x in fm.get("superseded_pending") or []}
        visible = {b for o in ri.repair_open_bullets(self.staging) if o["note"] == rel for b in o["bullets"]}
        visible |= {b for v in ri.bullet_invariant(self.staging, self.zk) if v.get("note") == rel for b in v.get("bullets") or []}
        if any(x.get("note") == rel for x in ri._read_json(self.staging / "needs_operator.json", []) or []):
            return []
        out = []
        for bid, b in e["bullets"].items():
            if old_unchanged:
                continue  # the old text is still in the vault
            ok = False
            for a in b["answers"].values():
                if a.get("decision") == "dropped":
                    ok = True
                for x in a.get("titles") or [a.get("title")]:
                    if x and a.get("decision") in ("kept", "corrected") and ri._final_targets(self.zk, x):
                        ok = True
            if not ok and bid not in listed and bid not in visible:
                out.append(bid)
        return out


class CrashDuringReviewNeverRetried(_Kill):
    """K1: a kill during the review (before finalize). recover() quarantines the shadow notes
    and reopens the bullets as `target-rejected`; next_batch closes the note `done-unrepaired`.
    In the next pass, repair_groups (round 20 skip, synth_call.py:2459) skips the note because
    its sha is unchanged and its status is not `open`: a transient kill ends the repair of the
    note for good (only --force plans it again)."""

    def test_kill_during_review_then_next_pass(self) -> None:
        rel = self.old("Old Kr", 2)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = _rep("Old Kr", self.a, "b1,b2", self.ta, f"Old Kr.md#b1 | kept in {self.a}\nOld Kr.md#b2 | kept in {self.a}\n")
        _b, u, _res, _ = self.run_batch(state, rep, review=False, finalize=False)
        self.kill_owner(u)
        ri.recover(self.staging, self.zk)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        # pass 2
        plan = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertEqual([s["state"] for s in plan["skipped"] if s["note"] == rel], ["repair-done-unrepaired"])
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))  # never asked again
        self.assertFalse(self.vpath(self.a).exists())


class RecoveredRenameNotReopened(_Kill):
    """K2: a fix call renamed A -> A2; then a kill before finalize. The recovered finalize
    does not call rename_in_state, so reopen_rejected_bullets compares the state titles {A}
    with the quarantined {A2}: nothing reopens. b1 stays `kept in A` (a note that exists
    nowhere), the note closes `done` (titles [A]), and repair_groups skips it for good."""

    def test_rename_then_kill(self) -> None:
        rel = self.old("Old Rk", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        _b, u, _res, _ = self.run_batch(state, _rep("Old Rk", self.a, "b1", self.ta, f"Old Rk.md#b1 | kept in {self.a}\n"),
                                        review=False, finalize=False)
        a2 = "Floor Presses Need No Parallettes Renamed"
        sc._apply_fix(self.staging, self.zk, u, f"{PN}/{self.a}.md",
                      sc.parse_reply(f"=====NOTE: {a2}=====\n" + self.ta.replace(f"# {self.a}\n", f"# {a2}\n")))
        self.kill_owner(u)
        ri.recover(self.staging, self.zk)
        sc.next_batch(self.staging, self.zk, state)
        e = json.loads(state.read_text())["notes"][rel]
        self.assertEqual(e["bullets"]["Old Rk.md#b1"]["answers"][V5]["decision"], "kept")  # not reopened
        self.assertEqual(e["bullets"]["Old Rk.md#b1"]["answers"][V5].get("title"), self.a)
        self.assertEqual((e["status"], e.get("unsupported_bullets")), ("done", []))
        plan = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertEqual([s["state"] for s in plan["skipped"] if s["note"] == rel], ["repair-done"])
        self.assertEqual(ri.repair_open_bullets(self.staging), [])  # not listed as open
        if os.environ.get("A2_DEBUG"):
            print("\nDBG-K2", ri.bullet_invariant(self.staging, self.zk), ri._read_json(self.staging / "needs_operator.json", []),
                  self.vpath("Old Rk").read_text()[:80])


class RecoveredPublishLosesBulletRecord(_Kill):
    """K3: a kill after the vault write of A and before its `note-published` record. The
    recovered finalize writes a `note-published` record with no `bullets` and no
    `evidence_tags`; last_published() returns it, so extension_loss() and the invariant's V7
    file check are off for A from then on."""

    def test_guard_off_after_recovery(self) -> None:
        rel = self.old("Old Pr", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        _b, u, _res, _ = self.run_batch(state, _rep("Old Pr", self.a, "b1", self.ta, f"Old Pr.md#b1 | kept in {self.a}\n"),
                                        finalize=False)
        real_rec = ri.provenance.record

        def dying_record(staging, event, **kw):
            if event == "note-published" and str(kw.get("note", "")).endswith(f"{self.a}.md"):
                raise SystemExit("killed after the vault write")
            return real_rec(staging, event, **kw)

        with mock.patch.object(ri.provenance, "record", dying_record):
            with self.assertRaises(SystemExit):
                ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        self.assertTrue(self.vpath(self.a).is_file())
        self.kill_owner(u)
        ri.recover(self.staging, self.zk)
        rec = ri.last_published(self.staging, f"{PN}/{self.a}.md")
        self.assertIsNone(rec.get("bullets"))
        self.assertIsNone(rec.get("evidence_tags"))
        empty = self.ta.split("## Evidence")[0] + "## Evidence\n\n## Connected Ideas\n\n- [[Home]] — x.\n"
        self.assertIsNone(ri.extension_loss(self.staging, f"{PN}/{self.a}.md", empty))  # no guard


class KillSweep(_Kill):
    """K4: kill the finalize at each vault write (before and after the write), recover, run
    one more pass, and look for a bullet that is lost while the summary says nothing."""

    def _scenario(self, k: int, after: bool) -> tuple[str, Path, int]:
        title = f"Old Sw{k}{'a' if after else 'b'}"
        rel = self.old(title, 3)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: {title}.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====NOTE: {self.b} | repairs: {title}.md | bullets: b2=====\n{self.tb.rstrip()}\n"
               f"=====BULLETS=====\n{title}.md#b1 | kept in {self.a}\n{title}.md#b2 | kept in {self.b}\n"
               f"{title}.md#b3 | dropped: not in this transcript\n")
        _b, u, _res, _ = self.run_batch(state, rep, finalize=False)
        real = ri._Publisher.write
        n = {"n": 0}

        def dying(pub, rel_, text, why):
            n["n"] += 1
            if n["n"] == k and not after:
                raise SystemExit("killed before write")
            out = real(pub, rel_, text, why)
            if n["n"] == k and after:
                raise SystemExit("killed after write")
            return out

        with mock.patch.object(ri._Publisher, "write", dying):
            try:
                ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
            except SystemExit:
                pass
        if not (u / "finalized.json").exists():
            self.kill_owner(u)
            ri.recover(self.staging, self.zk)
        sc.next_batch(self.staging, self.zk, state)
        ri.finalize(self.staging, self.run_dir, self.new_unit("repair", []), [], 0, self.zk)
        return rel, state, n["n"]



def _mk(k: int, after: bool):
    def t(self) -> None:
        rel, state, total = self._scenario(k, after)
        if total < k:
            self.skipTest(f"the finalize made only {total} writes")
        lost = self.silent_losses(rel, state)
        if os.environ.get("A2_DEBUG"):
            fm = ri.verify_claims.split_frontmatter((self.zk / rel).read_text())[0]
            print("\nDBG", k, after, total, {kk: fm.get(kk) for kk in ("status", "superseded_by", "superseded_pending")},
                  [(x["note"], x["why"][:40]) for x in ri._read_json(self.staging / "needs_operator.json", []) or []],
                  ri.repair_open_bullets(self.staging), ri.bullet_invariant(self.staging, self.zk),
                  [p_.name for p_ in self.zk.joinpath(PN).glob("*.md")])
        self.assertTrue(lost, f"kill at write {k} ({'after' if after else 'before'}): no silent loss")
    return t


for _k in range(1, 6):
    for _after in (False, True):
        setattr(KillSweep, f"test_kill_{'after' if _after else 'before'}_write_{_k}", _mk(_k, _after))


class GarbledReplyNeverRetried(_Kill):
    """K1b: one garbled repair reply (no note, no keep, no ledger line). It is not cached (R11),
    but record_answers records every bullet `unaccounted` for this video, the note closes
    `done-unrepaired`, and the next pass skips it (round 20 skip): the request is never sent
    again, although R11 wants a new call."""

    def test_garbled_reply(self) -> None:
        rel = self.old("Old Gb", 1)
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.run_batch(state, "Sorry, I cannot help with that.")
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        plan = sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        self.assertEqual([s["state"] for s in plan["skipped"] if s["note"] == rel], ["repair-done-unrepaired"])
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state))
        self.assertEqual(len([c for c in self.log() if c["kind"] == "repair"]), 1)  # one call, never again


class PendingTargetRenamedLater(_Kill):
    """K5: target A waits (no review verdict) while B publishes: partial stub [B] + `note: A`.
    The pending unit's review-fix renames A -> A2 and A2 publishes. Does the old note end as a
    complete stub [A2; B] with b1 settled? (DEFECT form: PASS = the stub misses A2, or b1 is
    listed open, or the invariant reports something.)"""

    def test_pending_target_renamed_then_published(self) -> None:
        rel = self.old("Old Pt", 2)
        # round 22 adaptation (X16): bullets with content, so a pointer can be confirmed
        p_ = self.vpath("Old Pt")
        p_.write_text(p_.read_text().replace("- legacy bullet 1\n", "- Planche lean presses can be done on the floor without parallettes.\n").replace("- legacy bullet 2\n", "- Four sets of planche lean presses after the planche workout are an option.\n"))
        state = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state)
        rep = (f"=====NOTE: {self.a} | repairs: Old Pt.md | bullets: b1=====\n{self.ta.rstrip()}\n"
               f"=====NOTE: {self.b} | repairs: Old Pt.md | bullets: b2=====\n{self.tb.rstrip()}\n"
               f"=====BULLETS=====\nOld Pt.md#b1 | kept in {self.a}\nOld Pt.md#b2 | kept in {self.b}\n")
        _b, u, _res, _ = self.run_batch(state, rep, review=False, finalize=False)
        rc.review_unit(self.staging, self.zk, u)
        for d in u.glob("review-*"):
            if json.loads((d / "request.json").read_text()).get("note", "").endswith(f"{self.a}.md"):
                shutil.rmtree(d)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        acts = {Path(n["note"]).stem: n["action"] for n in ev["notes"]}
        self.assertEqual((acts[self.a], acts[self.b]), ("pending-review", "published"))
        sc.next_batch(self.staging, self.zk, state)
        pu = Path(ri.pending_units(self.staging, self.run_dir, os.getpid())[0])
        a2 = "Floor Presses Need No Parallettes Renamed"
        sc._apply_fix(self.staging, self.zk, pu, f"{PN}/{self.a}.md",
                      sc.parse_reply(f"=====NOTE: {a2}=====\n" + self.ta.replace(f"# {self.a}\n", f"# {a2}\n")))
        rc.review_unit(self.staging, self.zk, pu)
        ev2 = ri.finalize(self.staging, self.run_dir, pu, [], 0, self.zk)
        acts2 = {Path(n["note"]).stem: n["action"] for n in ev2["notes"]}
        self.assertEqual(acts2.get(a2), "published", ev2["notes"])
        stub = self.vpath("Old Pt").read_text()
        bad = (f"[[{a2}]]" not in stub or "bullet: Old Pt.md#b1" in stub
               or ri.bullet_invariant(self.staging, self.zk) or ri.replacement_beside_old(self.staging, self.zk))
        self.assertTrue(bad, stub)
