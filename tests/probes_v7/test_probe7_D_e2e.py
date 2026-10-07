"""Reviewer D, round 7: end-to-end probes (fake LLMs only) of finalize, unlink, relink,
crash recovery and the dead-link ERROR.

Convention: each assertion states the DEFECT. A PASS means the defect is present.
A FAIL means the defect is absent (the attack failed).
"""
from __future__ import annotations

import json

from tests.unit.test_run_integrity import PN, ri
from tests.unit.test_synth_single import V5, _Single, reply

import review_call as rc  # noqa: E402
import validate  # noqa: E402


class _Crash(Exception):
    pass


class EndToEnd(_Single):
    def run_unit(self, notes):
        self.fake(synth=[reply([("C1", "01:16", "a"), ("C2", "01:22", "b")], notes)])
        self.synth()
        rc.review_unit(self.staging, self.zk, self.unit)
        return ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)

    def acts(self, ev):
        return {n["note"].split("/")[-1][:-3]: n.get("action") for n in ev["notes"]}

    def test_dead_link_in_lead_publishes_then_final_validate_errors(self) -> None:
        """DEFECT (false negative at publish): finalize checks dead links only in
        Connected Ideas / Disagreement, and check_one there runs with an empty
        PIPELINE_NOTES. A dead link in the lead publishes; the final validate then
        reports an ERROR (review_loop.sh exits non-zero)."""
        validate.PIPELINE_NOTES.clear()  # as in a fresh finalize process (the global is stale otherwise)
        ta = self.ta.replace("on the floor. [src:", "on the floor (see [[Nowhere Note]]). [src:", 1)
        ev = self.run_unit([(self.a, ["C1"], ta)])
        self.assertEqual(self.acts(ev).get(self.a), "published", ev["notes"])
        self.assertIn("[[Nowhere Note]]", self.vpath(self.a).read_text())
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        self.assertTrue([e for e in errors if "dead link [[Nowhere Note]]" in e])

    def test_crash_mid_publish_leaves_a_dead_link(self) -> None:
        """DEFECT: A links its sibling B; both publish. The finalize dies after the first
        vault write (A). recover() keeps A (intent + sha) and quarantines B, so A in the
        vault links [[B]] that does not exist, and no unlink record exists to repair it."""
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
                raise _Crash("killed")
            return orig(pub, rel, text, why)

        ri._Publisher.write = boom
        try:
            ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        except _Crash:
            pass
        finally:
            ri._Publisher.write = orig
        info = json.loads((self.unit / "unit.json").read_text())
        info["owner_pid"] = 999999999  # the owner is dead
        (self.unit / "unit.json").write_text(json.dumps(info))
        ri.recover(self.staging, self.zk)
        first = [p for p in (self.vpath(self.a), self.vpath(self.b)) if p.is_file()]
        self.assertEqual(len(first), 1)
        other = self.b if first[0] == self.vpath(self.a) else self.a
        self.assertIn(f"[[{other}]]", first[0].read_text())
        self.assertFalse(self.vpath(other).exists())

    def test_unlink_of_the_only_link_publishes_an_orphan(self) -> None:
        """DEFECT: A's only link goes to sibling B; B waits (no review). finalize unlinks
        [[B]] in A and publishes A with no wikilink and no [[Home]] fallback; the final
        validate reports 'ORPHAN' as an ERROR."""
        import shutil
        ta = self.ta.replace("- [[Home]] — home map.", f"- [[{self.b}]] — sibling.")
        self.fake(synth=[reply([("C1", "01:16", "a"), ("C2", "01:22", "b")], [(self.a, ["C1"], ta), (self.b, ["C2"], self.tb)])])
        self.synth()
        rc.review_unit(self.staging, self.zk, self.unit)
        for d in self.unit.glob("review-*"):
            if (json.loads((d / "review.json").read_text()) or {}).get("note") == f"{PN}/{self.b}.md":
                shutil.rmtree(d)
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        self.assertEqual(self.acts(ev), {self.a: "published", self.b: "pending-review"}, ev["notes"])
        body = self.vpath(self.a).read_text().split("\n---\n", 1)[1]
        self.assertEqual(validate.WIKILINK.findall(body), [])
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        self.assertTrue([e for e in errors if self.a in e and "ORPHAN" in e])

    def test_merge_rewrite_makes_a_self_link(self) -> None:
        """DEFECT: the kept note K links the dropped duplicate D. merge_duplicates rewrites
        [[D]] to [[K]] in every result, also in K itself, after the self-link check ran.
        K publishes with a link to itself (and that link was its only link)."""
        import re
        copy = self.ta.replace(f"# {self.a}", "# A Copy With Another Title")
        lead = [ln for ln in copy.split("\n") if "[src:" in ln][0]
        copy = copy.replace(lead, "Clearly, " + lead[0].lower() + lead[1:], 1)
        copy = re.sub(r"## Connected Ideas\n\n.*", f"## Connected Ideas\n\n- [[{self.a}]] — the same claim.\n", copy, flags=re.S)
        self.fake(synth=[reply([("C1", "01:16", "a"), ("C3", "01:16", "a again")],
                               [(self.a, ["C1"], self.ta), ("A Copy With Another Title", ["C3"], copy)])])
        self.synth()
        rc.review_unit(self.staging, self.zk, self.unit)
        ev = ri.finalize(self.staging, self.run_dir, self.unit, [V5], 0, self.zk)
        acts = self.acts(ev)
        self.assertEqual(acts.get(self.a), "merged", ev["notes"])
        kept = "A Copy With Another Title"
        p = self.vpath(kept)
        if not p.is_file():
            p = next((self.staging / "pending-review").rglob(f"{kept}.md"))
        self.assertIn(f"[[{kept}]]", p.read_text())
