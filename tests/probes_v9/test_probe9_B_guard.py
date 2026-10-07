"""Probe 9 B (reviewer B, round 4): false refusals of guard_vault_write and the
needs_operator.json record.

Convention of THIS file: every test asserts the DEFECT. PASS = the defect is present.
FAIL = the defect is gone (or the setup changed). Fake LLMs only, no network."""
from __future__ import annotations

import json
from unittest import mock

from tests.probes_v6.test_probe_A_crash import _Repair
from tests.unit.test_run_integrity import PN, ri


class KillBeforeRegistryUpdate(_Repair):
    """X-B3: a kill after os.replace and before record_pipeline_write. recover() accepts the
    write (intent sha == vault sha) but does not update pipeline_writes.json. Every later
    pipeline write of that note is refused as a "user edit", for good."""

    def test_recovered_write_is_refused_as_user_edit_forever(self) -> None:
        rel = f"{PN}/{self.a}.md"
        u1 = self.new_unit("repair", [])
        p1 = ri._Publisher(self.staging, self.run_dir.name, u1, self.zk, {})
        self.assertTrue(p1.write(rel, self.ta, "first"))
        u2 = self.new_unit("repair", [])
        p2 = ri._Publisher(self.staging, self.run_dir.name, u2, self.zk, {})
        text2 = self.ta.replace("\n## Details\n", "\n## Details\n\n- one more bullet\n", 1)
        self.assertNotEqual(text2, self.ta)
        with mock.patch.object(ri, "record_pipeline_write", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                p2.write(rel, text2, "second")
        self.assertEqual(self.vpath(self.a).read_text(), text2)   # the write reached the vault
        self.kill_owner(u2)
        ri.recover(self.staging, self.zk)
        fin = json.loads((u2 / "finalized.json").read_text())
        self.assertIn(rel, fin["published"])                      # recover accepts it as published
        u3 = self.new_unit("repair", [])
        p3 = ri._Publisher(self.staging, self.run_dir.name, u3, self.zk, {})
        text3 = text2.replace("- one more bullet", "- one more bullet, rewritten")
        self.assertFalse(p3.write(rel, text3, "third"))           # refused ...
        self.assertIn("user edit", p3.refused[0]["why"])          # ... as a "user edit"


class RespeakLinkRewriteLooksLikeUserEdit(_Repair):
    """X-B4: respeak renames a note and rewrite_links() changes every linking note with a
    direct write_text: no publisher, no record_pipeline_write. A linking note that the
    pipeline published before (a target, or a stub) then fails the guard: every later
    pipeline write of it is refused as a "user edit"."""

    def test_respeak_makes_linking_pipeline_note_unwritable(self) -> None:
        import synth_call as sc
        from tests.unit.test_synth_single import V5
        ut = "Unresolved Speaker In Planche Video Says Leans Work"
        utext = self.tb.replace("verification:", "speaker_status: unresolved\nverification:", 1)
        utext = utext.replace(f"# {self.b}\n", f"# {ut}\n", 1)
        u1 = self.new_unit("repair", [])
        pub = ri._Publisher(self.staging, self.run_dir.name, u1, self.zk, {})
        urel, arel = f"{PN}/{ut}.md", f"{PN}/{self.a}.md"
        atext = self.ta.replace("- [[Home]] — home map.\n", f"- [[Home]] — home map.\n- [[{ut}]] — related.\n")
        self.assertTrue(pub.write(urel, utext, "publish"))
        self.assertTrue(pub.write(arel, atext, "publish"))
        for r in (urel, arel):
            ri.provenance.record(self.staging, "note-published", note=r, unit=u1.name)
        rows = sc.respeak(self.staging, self.zk, V5, "Radoslav Radev", apply=True, force_name=True)
        self.assertTrue(any(r.get("changed") for r in rows), rows)
        self.assertNotIn(f"[[{ut}]]", self.vpath(self.a).read_text())   # link rewritten in place
        u2 = self.new_unit("repair", [])
        pub2 = ri._Publisher(self.staging, self.run_dir.name, u2, self.zk, {})
        new = self.vpath(self.a).read_text().replace("- [[Home]] — home map.", "- [[Home]] — the home map.")
        self.assertFalse(pub2.write(arel, new, "backlink"))
        self.assertIn("user edit", pub2.refused[0]["why"])
