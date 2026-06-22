#!/usr/bin/env python3
"""Make Phase C item-batched (WIP=1) instead of one agent over the whole vault.

A single fresh agent cannot hold ~1000 notes to review them all in one window — that
reintroduces the monolithic-context failure Phase B exists to avoid. So the per-note
review passes iterate one note at a time, tracked here.

    python review_queue.py build --vault "<zk dir>"
    python review_queue.py next  --pass atomicity          # -> next note path, or empty
    python review_queue.py done  --pass atomicity --file "<rel path>"
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RQ = HERE / "staging" / "review_queue.json"


def load() -> dict:
    return json.loads(RQ.read_text())


def save(d: dict) -> None:
    tmp = RQ.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    tmp.replace(RQ)


def build(vault: str) -> None:
    root = Path(vault)
    notes = sorted(str(p.relative_to(root)) for p in root.glob("01 Permanent Notes/*.md"))
    save({"version": 1, "vault": vault, "items": [{"file": f, "passes_done": []} for f in notes]})
    print(f"review_queue: {len(notes)} notes")


def nxt(p: str) -> None:
    for it in load()["items"]:
        if p not in it["passes_done"]:
            print(it["file"])
            return
    print("")


def done(p: str, f: str) -> None:
    d = load()
    for it in d["items"]:
        if it["file"] == f and p not in it["passes_done"]:
            it["passes_done"].append(p)
    save(d)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--vault", required=True)
    n = sub.add_parser("next")
    n.add_argument("--pass", dest="p", required=True)
    dn = sub.add_parser("done")
    dn.add_argument("--pass", dest="p", required=True)
    dn.add_argument("--file", required=True)
    a = ap.parse_args()
    {"build": lambda: build(a.vault), "next": lambda: nxt(a.p), "done": lambda: done(a.p, a.file)}[a.cmd]()


if __name__ == "__main__":
    main()
