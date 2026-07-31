#!/usr/bin/env python3
"""Phase A2: precompute themed tweet clusters (deterministic).

The synthesis loop processes short tweets in themed clusters (<=8) so each iteration has a
coherent unit. Letting a fresh agent form clusters by eyeballing ~496 tweets every
iteration is a context-rot trap (and non-deterministic across memoryless iterations), so
the grouping is computed ONCE here and written into each tweet's `cluster` field in
queue.json. Articles/videos are singleton units (each is its own work unit).

Dependency-free greedy cosine over token counts. Run after ingest.py:

    python cluster.py
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from math import sqrt
from pathlib import Path

HERE = Path(__file__).resolve().parent
_staging_env = os.environ.get("ZR_STAGING")
STAGING = Path(_staging_env) if _staging_env else HERE / "staging"
if not STAGING.is_absolute():
    STAGING = (Path.cwd() / STAGING).resolve()
QUEUE = STAGING / "queue.json"
STOP = set(
    "a an the of to and or in on for is are be it its as with that this by from at into i "
    "you we they he she but if not no so this that have has had will can just like get".split()
)
MAX_CLUSTER = 8
SIM_THRESHOLD = 0.18  # cosine; tuned for short text. Lower = larger, looser clusters.


def vec(text: str) -> Counter:
    return Counter(w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 2)


def cos(a: Counter, b: Counter) -> float:
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[w] * b[w] for w in common)
    na, nb = sqrt(sum(v * v for v in a.values())), sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def main() -> None:
    q = json.loads(QUEUE.read_text())

    for it in q["items"]:
        if it["kind"] != "tweet":
            it["cluster"] = f"single-{it['id']}"

    tweets = [it for it in q["items"] if it["kind"] == "tweet" and it.get("lit_note")]
    vecs = {}
    for it in tweets:
        p = HERE / it["lit_note"]
        vecs[it["id"]] = vec(p.read_text(errors="replace") if p.exists() else it["id"])

    assigned: set[str] = set()
    sizes: Counter = Counter()
    cid = 0
    for it in tweets:
        if it["id"] in assigned:
            continue
        cid += 1
        label = f"cl-{cid:04d}"
        it["cluster"] = label
        assigned.add(it["id"])
        sizes[label] = 1
        cand = sorted(
            ((cos(vecs[it["id"]], vecs[o["id"]]), o) for o in tweets if o["id"] not in assigned),
            key=lambda x: x[0],
            reverse=True,
        )
        for sim, o in cand:
            if sizes[label] >= MAX_CLUSTER:
                break
            if sim >= SIM_THRESHOLD:
                o["cluster"] = label
                assigned.add(o["id"])
                sizes[label] += 1

    # Don't strand un-extracted/failed tweets (they have no lit note): give them a visible
    # 'unclustered' marker rather than null, so nothing is silently lost (don't-lose-info).
    stranded = [it for it in q["items"] if it["kind"] == "tweet" and not it.get("cluster")]
    for it in stranded:
        it["cluster"] = "unclustered"

    tmp = QUEUE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(q, indent=2, ensure_ascii=False))
    tmp.replace(QUEUE)
    multi = sum(1 for v in sizes.values() if v > 1)
    print(f"clustered {len(tweets)} tweets into {len(sizes)} clusters ({multi} multi-item).")
    if stranded:
        print(f"WARNING: {len(stranded)} tweets have no lit note (failed/unextracted) -> "
              f"marked 'unclustered'. Re-run ingest or reconcile from PDFs before synthesis.")


if __name__ == "__main__":
    main()
