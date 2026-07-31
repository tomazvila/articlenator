#!/usr/bin/env python3
"""Just-in-time dedup retrieval — the SELECT lever (fixes the context-rot bomb).

The synthesis agent must NEVER load the whole concept-index into its context: it grows
toward ~1000+ entries and would dominate every iteration's input, degrading the very
dedup judgment it feeds (context rot). Instead the agent calls THIS tool per candidate
claim; the tool reads the big index here (outside the agent's window) and prints only the
top-K lexically-closest concepts. Per-iteration context stays bounded no matter how large
the index gets.

    python index_query.py "An atomic note separates one concern" --k 15

Lexical ranking is dependency-free. For higher recall on paraphrases (semantic dups /
title drift), swap in a local embedding model via the nix flake and rank by cosine — the
interface (claim in, top-K out) does not change.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
INDEX = STAGING / "concept-index.json"
STOP = set(
    "a an the of to and or in on for is are be it its as with that this by from at into "
    "not no do does can will should would could than then so such these those their our".split()
)


def toks(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 2}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("claim", help="the candidate claim to dedup")
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument("--index", default=str(INDEX))
    args = ap.parse_args()

    path = Path(args.index)
    idx = json.loads(path.read_text()) if path.exists() else {"concepts": []}
    q = toks(args.claim)
    qtags = q

    scored = []
    for c in idx.get("concepts", []):
        ct = toks(c.get("title", "")) | toks(" ".join(c.get("aliases", []))) | toks(c.get("gist", ""))
        inter = len(q & ct)
        if not ct or inter == 0:
            continue
        jaccard = inter / len(q | ct)
        tag_bonus = 0.1 * len(qtags & {t.lower() for t in c.get("tags", [])})
        scored.append((round(jaccard + tag_bonus, 3), c))

    scored.sort(key=lambda x: x[0], reverse=True)
    out = [
        {"score": s, "title": c["title"], "file": c.get("file"), "gist": c.get("gist", ""), "tags": c.get("tags", [])}
        for s, c in scored[: args.k]
    ]
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
