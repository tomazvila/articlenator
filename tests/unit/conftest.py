"""Shared pytest setup for the zettel_ralph tests (round 5, M10).

- Harness state (vault locks, the vault registry) goes to a temp folder, never to
  ~/.local/state or the code tree.
- ZR_TEST_RUN=1: run_integrity refuses a staging inside the code tree.
- A session guard hashes zettel_ralph/ before and after the tests: a test that writes
  into the code tree (a staging, a lock, a log) fails the run. The same guard covers
  the real state folder (~/.local/state/zettel_ralph).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pytest

ZR = Path(__file__).resolve().parents[2] / "zettel_ralph"
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def tree_hash(root: Path = ZR) -> dict[str, str]:
    """sha256 of every file under root (names relative to root); caches are skipped."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(".pyc"):
                continue
            p = Path(dirpath) / name
            try:
                out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                out[p.relative_to(root).as_posix()] = "unreadable"
    return out


# The default state folder of real runs; tests must not change it.
HOME_STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "zettel_ralph"
_STATE = tempfile.mkdtemp(prefix="zr-state-")
os.environ["ZR_STATE_DIR"] = _STATE  # always a temp folder, never the real state
os.environ["ZR_TEST_RUN"] = "1"
# Round 22: also the XDG default, so a subprocess whose environment drops ZR_* and PYTEST_*
# (probe wrappers, driver tests) still never reaches the owner's state folder
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="zr-xdg-")


@pytest.fixture(scope="session", autouse=True)
def code_tree_unchanged():
    before = tree_hash()
    state_before = tree_hash(HOME_STATE) if HOME_STATE.is_dir() else None
    yield
    after = tree_hash()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    # The guard runs once per session; pytest reports a failure here as an ERROR of the
    # LAST test that ran (an "intermittent" error of a random test). Its cause is a write
    # into the code tree during the session: a test, or an editor/agent changing files while
    # the suite runs.
    assert not changed, (f"files in the code tree zettel_ralph/ changed during the test session (a test wrote "
                         f"there, or the files were edited while the suite ran): {changed[:20]}")
    state_after = tree_hash(HOME_STATE) if HOME_STATE.is_dir() else None
    assert state_after == state_before, f"tests changed the real harness state folder {HOME_STATE}"
