from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ZR = ROOT / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))
import run_integrity as ri  # noqa: E402


class RenameFinalizeScopeTests(unittest.TestCase):
    def _marked(self, staging: Path, run: str, unit: str, renamed: dict[str, str]) -> Path:
        path = staging / "runs" / run / "units" / unit / "marked.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        ri._write_json(path, {"renamed": renamed})
        return path

    def test_finalize_scope_caches_discovery_and_observes_marked_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            vault = Path(tmp) / "vault"
            first = self._marked(staging, "r1", "u1", {"Old": "Middle"})
            self._marked(staging, "r2", "u2", {"Middle": "Final"})
            real_glob = Path.glob
            scans = []

            def counted_glob(path: Path, pattern: str):
                if path == staging and pattern == "runs/*/units/*/marked.json":
                    scans.append(pattern)
                return real_glob(path, pattern)

            with ri._rename_index_scope(staging), mock.patch.object(Path, "glob", counted_glob):
                for _ in range(4):
                    ri.load_renames(staging)
                self.assertEqual(ri._rename_chain(vault, "Old"), "Final")
                self.assertEqual(scans, ["runs/*/units/*/marked.json"])

                ri._write_json(first, {"renamed": {"Old": "Updated"}})
                ri.load_renames(staging)
                self.assertEqual(ri._rename_chain(vault, "Old"), "Updated")
                self.assertEqual(len(scans), 2)

                self._marked(staging, "r3", "u3", {"Updated": "Newest"})
                ri.load_renames(staging)
                self.assertEqual(ri._rename_chain(vault, "Old"), "Newest")
                self.assertEqual(len(scans), 3)

    def test_nested_staging_scopes_do_not_share_rename_maps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            one, two = Path(tmp) / "one", Path(tmp) / "two"
            self._marked(one, "r", "u", {"Old": "One"})
            self._marked(two, "r", "u", {"Old": "Two"})
            vault = Path(tmp) / "vault"
            with ri._rename_index_scope(one):
                ri.load_renames(one)
                self.assertEqual(ri._rename_chain(vault, "Old"), "One")
                # An explicit load of another staging keeps the legacy most-recently-loaded map.
                ri.load_renames(two)
                self.assertEqual(ri._rename_chain(vault, "Old"), "Two")
                ri.load_renames(one)  # restore the finalize-local map without a new scan
                self.assertEqual(ri._rename_chain(vault, "Old"), "One")
                with ri._rename_index_scope(two):
                    ri.load_renames(two)
                    self.assertEqual(ri._rename_chain(vault, "Old"), "Two")
                    with ri._rename_index_scope(Path(tmp) / "empty"):
                        self.assertEqual(ri._rename_map(Path(tmp) / "empty"), {})
                        self.assertEqual(ri._rename_chain(vault, "Old"), "Old")
                # Nested context cleanup restores the outer scope's active map.
                self.assertEqual(ri._rename_chain(vault, "Old"), "One")
                self.assertEqual(ri._rename_map(one), {"Old": "One"})
                self.assertEqual(ri._rename_chain(vault, "Old"), "One")


    def test_invalidation_finds_staging_root_named_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "runs"
            marked = self._marked(staging, "r", "u", {"Old": "First"})
            vault = Path(tmp) / "vault"
            with ri._rename_index_scope(staging):
                ri.load_renames(staging)
                self.assertEqual(ri._rename_chain(vault, "Old"), "First")
                ri._write_json(marked, {"renamed": {"Old": "Second"}})
                ri.load_renames(staging)
                self.assertEqual(ri._rename_chain(vault, "Old"), "Second")

    def test_uncached_audit_read_observes_same_signature_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            marked = self._marked(staging, "r", "u", {"Old": "One"})
            self.assertEqual(ri._read_json_snapshot(marked, {}), {"renamed": {"Old": "One"}})
            before = marked.stat()
            marked.write_text(json.dumps({"renamed": {"Old": "Two"}}, indent=2) + "\n", encoding="utf-8")
            os.utime(marked, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = marked.stat()
            self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                             (before.st_ino, before.st_size, before.st_mtime_ns))
            self.assertEqual(ri._load_renames_uncached(staging), {"Old": "Two"})

    def test_calls_without_finalize_scope_keep_fresh_scan_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            vault = Path(tmp) / "vault"
            marked = self._marked(staging, "r", "u", {"Old": "First"})
            ri.load_renames(staging)
            self.assertEqual(ri._rename_chain(vault, "Old"), "First")
            marked.write_text(json.dumps({"renamed": {"Old": "Second"}}), encoding="utf-8")
            ri.load_renames(staging)
            self.assertEqual(ri._rename_chain(vault, "Old"), "Second")


if __name__ == "__main__":
    unittest.main()
