#!/usr/bin/env python3
"""Claim the next synthesis work unit so the agent never scans the (1500+ item) queue.

A unit is one `extracted` article/video, or all `extracted` tweets sharing a cluster id.
Prints `{}` when nothing is left to synthesize. A `held` item (degraded transcript) is never
offered. An `incomplete` item (a run that ended by limit, timeout or error) is ready again
after all `extracted` items.

The claim is atomic: under the staging state lock, the unit's items get stage `claimed`
with `worker: serial` and their old stage in `claimed_from`. finalize moves them on.

    python queue_next.py             # claim and print the next unit
    python queue_next.py --release   # return every serial claim to its old stage
    python queue_next.py --release-all   # every claim (the top-level driver at exit)
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
Q = STAGING / "queue.json"
SERIAL = "serial"

sys.path.insert(0, str(HERE))
from _lock import state_lock  # noqa: E402


def _save(q: dict) -> None:
    tmp = Q.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(q, indent=2, ensure_ascii=False))
    tmp.replace(Q)


def release(items: list[dict], every: bool = False) -> list[str]:
    """Return claims to their old stage: serial claims, or (every) all claims. A stopping
    driver holds the staging's driver lock, so no other driver owns a claim then."""
    out = []
    for it in items:
        if it.get("stage") == "claimed" and (every or it.get("worker") == SERIAL):
            it["stage"] = it.pop("claimed_from", None) or "extracted"
            it.pop("worker", None)
            out.append(it["id"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--release", action="store_true", help="return serial claims to their old stage")
    ap.add_argument("--release-all", action="store_true",
                    help="return EVERY claim (serial and worker) to its old stage; the driver runs it at exit")
    a = ap.parse_args()
    with state_lock(STAGING):
        q = json.loads(Q.read_text())
        items = q["items"]
        if a.release or a.release_all:
            done = release(items, every=a.release_all)
            if done:
                _save(q)
            print(json.dumps({"released": done}))
            return 0
        nxt = next((it for it in items if it["stage"] == "extracted"), None) or next(
            (it for it in items if it["stage"] == "incomplete"), None
        )
        if not nxt:
            print("{}")
            return 0
        cl = nxt.get("cluster") or ""
        if nxt["kind"] == "tweet" and cl and not cl.startswith(("single-", "unclustered")):
            unit = [it for it in items if it.get("cluster") == cl and it["stage"] == "extracted"] or [nxt]
        else:
            unit = [nxt]
        for it in unit:
            it["claimed_from"] = it["stage"]
            it["stage"] = "claimed"
            it["worker"] = SERIAL
        _save(q)
    print(json.dumps({
        "cluster": cl,
        "kind": nxt["kind"],
        "ids": [it["id"] for it in unit],
        "lit_notes": [it["lit_note"] for it in unit],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
