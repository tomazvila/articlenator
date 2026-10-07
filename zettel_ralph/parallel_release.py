#!/usr/bin/env python3
"""Release a claimed item back to extracted (for retry after failure).

    python parallel_release.py vid-a,vid-b
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
Q = STAGING / "queue.json"

from _lock import state_lock  # noqa: E402

ids = {x.strip() for x in sys.argv[1].split(",") if x.strip()}
with state_lock():
    q = json.loads(Q.read_text())
    released = []
    for it in q["items"]:
        if it["id"] in ids and it["stage"] == "claimed":
            it["stage"] = "extracted"
            it.pop("worker", None)
            released.append(it["id"])
    tmp = Q.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(q, indent=2, ensure_ascii=False))
    tmp.replace(Q)
    print(f"released: {sorted(released)} (asked: {sorted(ids)})")
