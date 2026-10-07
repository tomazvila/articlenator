from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ZR = ROOT / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))
import run_integrity as ri  # noqa: E402


PN = ri.PERMANENT_DIR
REL = f"{PN}/A [title].md"


class ArchiveCandidateIndexTests(unittest.TestCase):
    def _archive(self, staging: Path, run: str, unit: str, rel: str, text: str) -> Path:
        path = staging / "retired" / run / unit / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_index_order_and_exact_path_match_legacy_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            rels = [REL, f"{PN}/A [title].v2.md", f"{PN}/sub/Deep.md"]
            self._archive(staging, "run-1", "unit-a", rels[0], "old-a")
            self._archive(staging, "run-1", "unit-a", f"{PN}/A [title].v2.md", "version-2")
            self._archive(staging, "run-1", "unit-b", rels[0], "old-b")
            self._archive(staging, "run-2", "unit-c", rels[0], "old-c")
            self._archive(staging, "run-2", "unit-c", f"{PN}/A [title].v3.md", "version-3")
            self._archive(staging, "run-1", "unit-a", rels[2], "deep")

            with ri._archive_candidate_scope(staging):
                for rel in rels:
                    self.assertEqual(ri._archive_candidates(staging, rel),
                                     ri._archive_candidates_uncached(staging, rel))
                candidates = ri._archive_candidates(staging, REL)

            self.assertEqual(
                [p.read_text(encoding="utf-8") for p in candidates],
                ["version-3", "old-c", "old-b", "version-2", "old-a"],
            )
            # Callers outside finalize retain the original traversal and ordering.
            self.assertEqual(ri._archive_candidates(staging, REL),
                             ri._archive_candidates_uncached(staging, REL))

    def test_noncanonical_and_nonpermanent_paths_use_legacy_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            for rel in ("Outside Notes/A.md", f"{PN}//A.md", f"{PN}/./A.md"):
                self._archive(staging, "run-1", "unit-a", rel, "legacy")
                expected = ri._archive_candidates_uncached(staging, rel)
                with ri._archive_candidate_scope(staging):
                    with mock.patch.object(ri, "_build_archive_candidate_index",
                                           side_effect=AssertionError("index must not be built")):
                        self.assertEqual(ri._archive_candidates(staging, rel), expected)

    def test_index_walk_propagates_directory_read_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_iterdir = Path.iterdir

            def unreadable(path):
                if path == root:
                    raise OSError("fixture unreadable")
                return real_iterdir(path)

            with mock.patch.object(Path, "iterdir", unreadable):
                with self.assertRaisesRegex(OSError, "fixture unreadable"):
                    list(ri._walk_archive_files(root))

    def test_equal_version_keys_keep_legacy_directory_iteration_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            unit_dir = staging / "retired" / "run-1" / "unit-a" / PN
            unit_dir.mkdir(parents=True)
            # The direct archive ties with .v1; .v02 and .v2 tie numerically.
            # Create the aliases in a known order, then treat legacy iteration as oracle.
            stem = Path(REL).stem
            for name, content in ((f"{stem}.v02.md", "v02"), (f"{stem}.md", "direct"),
                                  (f"{stem}.v1.md", "v1"), (f"{stem}.v2.md", "v2")):
                (unit_dir / name).write_text(content, encoding="utf-8")
            legacy = ri._archive_candidates_uncached(staging, REL)
            with ri._archive_candidate_scope(staging):
                indexed = ri._archive_candidates(staging, REL)
            self.assertEqual(indexed, legacy)
            tie_order = [p.name for p in unit_dir.iterdir()
                         if p.name in {f"{stem}.v02.md", f"{stem}.v2.md"}]
            self.assertEqual([p.name for p in indexed[:2]], tie_order)
            # The direct archive was appended before .v1 in the legacy traversal.
            self.assertEqual([p.name for p in indexed[2:]],
                             [f"{stem}.md", f"{stem}.v1.md"])

    def test_publisher_archive_and_explicit_invalidation_refresh_active_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging, vault = root / "staging", root / "vault"
            unit = staging / "runs" / "zz-new" / "units" / "unit-new"
            target = vault / REL
            target.parent.mkdir(parents=True)
            target.write_text("first version", encoding="utf-8")
            old_archive = self._archive(staging, "run-old", "unit-old", REL, "planned original")
            planned_sha = hashlib.sha256(b"planned original").hexdigest()
            ri._write_json(staging / "repair_state.json", {"notes": {REL: {"sha": planned_sha}}})
            pub = ri._Publisher(staging, "zz-new", unit, vault, {})

            with ri._archive_candidate_scope(staging):
                self.assertEqual(ri._archive_candidates(staging, REL), [old_archive])
                created = pub.archive(REL, "test replacement")
                self.assertIsNotNone(created)
                self.assertEqual(ri._archive_candidates(staging, REL)[0], Path(created))
                self.assertEqual(ri._archived_old(staging, REL), old_archive)

                changed_metadata = staging / "retired" / "zz-newer" / "unit-newer" / REL
                changed_metadata.parent.mkdir(parents=True)
                changed_metadata.write_text("newer archive", encoding="utf-8")
                ri._invalidate_archive_candidate_index(staging)
                self.assertEqual(ri._archive_candidates(staging, REL)[0], changed_metadata)
                self.assertEqual(ri._archived_old(staging, REL), old_archive)

                changed_metadata.write_text("changed archive contents", encoding="utf-8")
                ri._invalidate_archive_candidate_index(staging)
                self.assertEqual(ri._archive_candidates(staging, REL)[0], changed_metadata)
                self.assertEqual((staging / "retired" / "zz-newer" / "unit-newer" / REL).read_text(),
                                 "changed archive contents")


if __name__ == "__main__":
    unittest.main()
