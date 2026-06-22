#!/usr/bin/env python3
"""Mark queue items' stage so the agent never loads the big queue.json to write.

    python queue_mark.py --ids tw-1,tw-2 --stage synthesized --notes "Title A;Title B"
    python queue_mark.py --ids ar-9 --stage skipped --reason "no reusable idea: <claim>"
"""
import argparse
import json
from pathlib import Path

from _lock import state_lock

HERE = Path(__file__).resolve().parent
Q = HERE / "staging" / "queue.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True, help="comma-separated item ids")
    ap.add_argument("--stage", required=True, choices=["synthesized", "skipped", "failed"])
    ap.add_argument("--notes", default="", help="semicolon-separated note titles emitted")
    ap.add_argument("--reason", default="", help="reason (required for skipped; name the discarded idea)")
    a = ap.parse_args()

    ids = {x.strip() for x in a.ids.split(",") if x.strip()}
    notes = [x.strip() for x in a.notes.split(";") if x.strip()]
    with state_lock():
        q = json.loads(Q.read_text())
        n = 0
        for it in q["items"]:
            if it["id"] in ids:
                it["stage"] = a.stage
                it.pop("worker", None)
                if notes:
                    it["notes_emitted"] = notes
                if a.reason:
                    it["error"] = a.reason
                n += 1
        tmp = Q.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(q, indent=2, ensure_ascii=False))
        tmp.replace(Q)
    print(f"marked {n} item(s) {a.stage}")


if __name__ == "__main__":
    main()
