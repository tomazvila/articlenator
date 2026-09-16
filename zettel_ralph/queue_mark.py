#!/usr/bin/env python3
"""Atomically mark synthesis units as done in queue.json.

Called by the deepseek agent after it finishes synthesizing a unit:
    python queue_mark.py --ids <id> --stage synthesized --notes "Title A;Title B"

All mutations run under the shared state lock so concurrent workers
(calling queue_claim.py) never race on queue.json.
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
    ap = argparse.ArgumentParser(description="Mark a queue unit as synthesized / failed / skipped")
    ap.add_argument("--ids", required=True, help="comma-separated unit id(s) to update")
    ap.add_argument("--stage", default="synthesized", help="target stage (synthesized, failed, skipped)")
    ap.add_argument("--notes", default="", help="semicolon-separated note titles emitted")
    ap.add_argument("--reason", default="", help="failure/skip reason")
    a = ap.parse_args()

    target_ids = {x.strip() for x in a.ids.split(",") if x.strip()}
    if not target_ids:
        print("queue_mark: no valid ids provided", file=__import__("sys").stderr)
        return

    with state_lock():
        q = json.loads(Q.read_text())
        updated = 0
        for it in q["items"]:
            if it["id"] in target_ids:
                it["stage"] = a.stage
                it.pop("worker", None)
                if a.notes:
                    existing = it.get("notes_emitted", [])
                    for note in a.notes.split(";"):
                        n = note.strip()
                        if n and n not in existing:
                            existing.append(n)
                    it["notes_emitted"] = existing
                if a.reason:
                    it["error"] = a.reason
                updated += 1
        _save(q)

    print(f"queue_mark: marked {updated} item(s) as {a.stage}")


if __name__ == "__main__":
    main()
