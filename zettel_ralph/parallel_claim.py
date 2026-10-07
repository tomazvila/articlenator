#!/usr/bin/env python3
"""Atomically claim one ready item from the queue (any worker, any shard).

Ready stages: `extracted` first, then `incomplete`. Prints the unit JSON, or `{}`.

    python parallel_claim.py [--worker W]
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
Q = STAGING / "queue.json"
READY_STAGES = ("extracted", "incomplete")  # never `held` (degraded transcript) or `claimed`

from _lock import state_lock  # noqa: E402


def _save(q: dict) -> None:
    tmp = Q.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(q, indent=2, ensure_ascii=False))
    tmp.replace(Q)


def main() -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--worker", default=os.environ.get("WORKER", ""))
    a = ap.parse_args()
    with state_lock():
        q = json.loads(Q.read_text())
        chosen = None
        for ready in READY_STAGES:
            chosen = next((it for it in q["items"] if it["stage"] == ready), None)
            if chosen is not None:
                break
        if chosen is None:
            print("{}")
            return 0
        chosen["stage"] = "claimed"
        if a.worker != "":
            chosen["worker"] = a.worker
        _save(q)
    print(json.dumps({
        "cluster": chosen.get("cluster", ""),
        "kind": chosen["kind"],
        "ids": [chosen["id"]],
        "lit_notes": [chosen["lit_note"]],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
