"""Cross-process advisory lock for serializing state mutations in parallel mode.

All writers to queue.json / concept-index.json / provenance.json acquire this lock around
their read-modify-write so concurrent workers can't corrupt shared state. Heavy work (the
nested agent reasoning) runs in parallel; only these short critical sections serialize.
"""
from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path

_LOCK = Path(__file__).resolve().parent / "staging" / ".statelock"


@contextlib.contextmanager
def state_lock():
    _LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(_LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
