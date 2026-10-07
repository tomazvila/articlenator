from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
ZR = ROOT / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))
import run_integrity as ri  # noqa: E402


class RepairStateCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        ri._JSON_SNAPSHOT_CACHE.clear()

    def test_snapshot_reuses_parse_and_reloads_external_or_harness_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "repair_state.json"
            state.write_text('{"notes":{"old":{"status":"open"}}}', encoding="utf-8")
            with patch.object(ri.json, "loads", wraps=json.loads) as loads:
                first = ri._read_json_snapshot(state, {})
                second = ri._read_json_snapshot(state, {})
                self.assertIs(first, second)
                self.assertEqual(loads.call_count, 1)

                replacement = state.with_suffix(".new")
                replacement.write_text('{"notes":{"new":{"status":"done"}}}', encoding="utf-8")
                replacement.replace(state)
                fresh = ri._read_json_snapshot(state, {})
                self.assertEqual(fresh["notes"]["new"]["status"], "done")
                self.assertEqual(loads.call_count, 2)

                ri._write_json(state, {"notes": {"written": {"status": "updated"}}})
                written = ri._read_json_snapshot(state, {})
                self.assertEqual(written["notes"]["written"]["status"], "updated")
                self.assertEqual(loads.call_count, 3)


if __name__ == "__main__":
    unittest.main()
