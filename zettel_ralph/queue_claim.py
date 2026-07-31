#!/usr/bin/env python3
"""Atomically claim / release synthesis units for a worker shard (parallel mode).

Shards are contiguous blocks of the queue, which keeps topically-adjacent bookmarks in the
same worker and so reduces cross-worker duplicate concepts. All mutations run under the
shared state lock, so two workers never grab the same unit.

    python queue_claim.py --worker 0 --of 4             # claim next unit (prints unit JSON or {})
    python queue_claim.py --worker 0 --of 4 --reclaim   # reset this worker's stale claim(s)
    python queue_claim.py --release ar-1,tw-2           # reset ids claimed->extracted (stall recovery)
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from _lock import state_lock

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
Q = STAGING / "queue.json"


def _save(q: dict) -> None:
    tmp = Q.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(q, indent=2, ensure_ascii=False))
    tmp.replace(Q)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", type=int)
    ap.add_argument("--of", type=int)
    ap.add_argument("--reclaim", action="store_true", help="reset this worker's stale 'claimed' first")
    ap.add_argument("--release", help="ids to retry: claimed->extracted, bump attempts, fail at cap")
    ap.add_argument("--release-soft", help="ids to retry WITHOUT counting an attempt (rate-limited)")
    ap.add_argument("--max-synth-attempts", type=int, default=12)
    a = ap.parse_args()

    with state_lock():
        q = json.loads(Q.read_text())
        items = q["items"]
        total = max(1, len(items))

        if a.release or a.release_soft:
            soft = bool(a.release_soft)
            rel = {x.strip() for x in (a.release_soft or a.release).split(",") if x.strip()}
            for it in items:
                if it["id"] in rel and it["stage"] == "claimed":
                    it.pop("worker", None)
                    if soft:
                        it["stage"] = "extracted"  # transient (rate limit) - don't penalize
                    else:
                        it["synth_attempts"] = it.get("synth_attempts", 0) + 1
                        if it["synth_attempts"] >= a.max_synth_attempts:
                            it["stage"] = "failed"
                            it["error"] = f"synthesis failed after {it['synth_attempts']} attempts"
                        else:
                            it["stage"] = "extracted"
            _save(q)
            print("{}")
            return

        if a.reclaim:
            for it in items:
                if it.get("worker") == a.worker and it["stage"] == "claimed":
                    it["stage"] = "extracted"
                    it.pop("worker", None)
            _save(q)
            print("{}")
            return

        chosen = None
        for idx, it in enumerate(items):
            if idx * a.of // total == a.worker and it["stage"] == "extracted":
                chosen = it
                break

        if chosen is None:
            _save(q)  # persist any reclaim
            print("{}")
            return

        cl = chosen.get("cluster") or ""
        if chosen["kind"] == "tweet" and cl and not cl.startswith(("single-", "unclustered")):
            unit = [it for it in items if it.get("cluster") == cl and it["stage"] == "extracted"]
        else:
            unit = [chosen]
        for it in unit:
            it["stage"] = "claimed"
            it["worker"] = a.worker
        _save(q)

    print(json.dumps({
        "cluster": cl,
        "kind": chosen["kind"],
        "ids": [it["id"] for it in unit],
        "lit_notes": [it["lit_note"] for it in unit],
    }))


if __name__ == "__main__":
    main()
