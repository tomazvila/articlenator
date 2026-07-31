#!/usr/bin/env python3
"""Print the next synthesis work unit so the agent never scans the (1500+ item) queue.

A unit is one `extracted` article/video, or all `extracted` tweets sharing a cluster id.
Prints `{}` when nothing is left to synthesize.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
Q = STAGING / "queue.json"

items = json.loads(Q.read_text())["items"]
nxt = next((it for it in items if it["stage"] == "extracted"), None)
if not nxt:
    print("{}")
    sys.exit(0)

cl = nxt.get("cluster") or ""
if nxt["kind"] == "tweet" and cl and not cl.startswith(("single-", "unclustered")):
    unit = [it for it in items if it.get("cluster") == cl and it["stage"] == "extracted"]
else:
    unit = [nxt]

print(json.dumps({
    "cluster": cl,
    "kind": nxt["kind"],
    "ids": [it["id"] for it in unit],
    "lit_notes": [it["lit_note"] for it in unit],
}))
