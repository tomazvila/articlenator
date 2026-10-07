from __future__ import annotations

import json
import os

from tests.unit.test_run_integrity import PN, SASA, VID, _Env, agent, good_text
import provenance  # noqa: E402
import run_integrity as ri  # noqa: E402


class RepairWrittenTargetOwnership(_Env):
    target = SASA
    rel = f"{PN}/{SASA}.md"

    def _set_repair_plan(self) -> None:
        (self.staging / "repair_state.json").write_text(
            json.dumps({"notes": {f"{PN}/Old Source.md": {
                "status": "open", "titles": [self.target], "bullets": {},
            }}}),
            encoding="utf-8",
        )

    def _publish_in_run(self, kind: str, date: str) -> tuple[object, object, dict]:
        run_dir = ri.start(self.staging, kind, self.zk)
        unit_dir = ri.unit_start(run_dir, kind, [VID], None, os.getpid())
        os.environ.update({
            "ZR_STAGING": str(self.staging), "VAULT": str(self.tmp / "vault"), "ZK_DIR": str(self.zk),
            "ZR_RUN_DIR": str(unit_dir), "ZR_SHADOW_DIR": str(unit_dir / "out"),
            "ZR_UNIT": unit_dir.name, "ZR_UNIT_IDS": VID, "ZR_RUN_ID": run_dir.name,
            "ZR_UNIT_KIND": kind,
        })
        text = good_text().replace("created: 2026-10-03", f"created: {date}")
        agent.write_file(str(self.vpath(self.target)), text)
        self.approve(self.target, unit=unit_dir)
        event = ri.finalize(self.staging, run_dir, unit_dir, [VID], 0, self.zk)
        return run_dir, unit_dir, event

    def test_repair_can_extend_same_target_in_a_later_run(self) -> None:
        self._set_repair_plan()
        _, first_unit, first = self._publish_in_run("repair", "2026-10-03")
        self.assertEqual(self.note(first, self.target)["action"], "published")
        self.assertTrue(any(x.get("phase") == "done" and x.get("rel") == self.rel
                            for x in ri._read_jsonl(first_unit / "publish.jsonl")))

        _, second_unit, second = self._publish_in_run("repair", "2026-10-04")
        self.assertEqual(self.note(second, self.target)["action"], "published")
        self.assertTrue(any(x.get("phase") == "done" and x.get("rel") == self.rel
                            for x in ri._read_jsonl(second_unit / "publish.jsonl")))
        self.assertIn("created: 2026-10-04", self.vpath(self.target).read_text())

    def test_synthesis_replacement_revokes_old_repair_ownership(self) -> None:
        self._set_repair_plan()
        _, _, first = self._publish_in_run("repair", "2026-10-03")
        self.assertEqual(self.note(first, self.target)["action"], "published")

        _, synth_unit, synth = self._publish_in_run("synth", "2026-10-04")
        self.assertEqual(self.note(synth, self.target)["action"], "published")
        self.assertEqual(json.loads((synth_unit / "unit.json").read_text())["kind"], "synth")
        synth_text = self.vpath(self.target).read_text()
        self.assertIn("created: 2026-10-04", synth_text)

        _, _, attempted = self._publish_in_run("repair", "2026-10-05")
        result = self.note(attempted, self.target)
        self.assertEqual(result["action"], "quarantined", result)
        self.assertIn("title-equals-old", result["failure_types"])
        self.assertEqual(self.vpath(self.target).read_text(), synth_text)

    def test_legacy_event_is_accepted_only_with_unique_repair_unit_journal(self) -> None:
        self._set_repair_plan()
        _, repair_unit, first = self._publish_in_run("repair", "2026-10-03")
        self.assertEqual(self.note(first, self.target)["action"], "published")

        path = self.staging / provenance.JSONL_NAME
        records = provenance.read_records(self.staging)
        for record in records:
            if record.get("event") == "note-published" and record.get("unit") == repair_unit.name:
                record.pop("run_id", None)
                record.pop("sha256", None)
                record.pop("unchanged", None)
        path.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in records), encoding="utf-8")
        self.assertEqual(ri._repair_written_targets(self.staging, self.zk), {self.rel})

        _, _, continued = self._publish_in_run("repair", "2026-10-04")
        self.assertEqual(self.note(continued, self.target)["action"], "published")
        self.assertIn("created: 2026-10-04", self.vpath(self.target).read_text())
