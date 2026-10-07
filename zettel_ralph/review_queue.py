#!/usr/bin/env python3
"""Make Phase C item-batched (WIP=1) instead of one agent over the whole vault.

A single fresh agent cannot hold ~1000 notes to review them all in one window — that
reintroduces the monolithic-context failure Phase B exists to avoid. So the per-note
review passes iterate one note at a time, tracked here.

    python review_queue.py build --vault "<zk dir>"
    python review_queue.py next  --pass atomicity          # -> next note path, or empty
    python review_queue.py done  --pass atomicity --file "<rel path>"
    python review_queue.py fail  --pass atomicity --file "<rel path>" --reason "..."

`fail` records a pass whose result did not pass verification (run_integrity.py
quarantined it). The note is not offered again for that pass, so a note that can
never pass does not loop forever. `release` is for a run that ended by a limit,
a timeout or an error; the note is offered again.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from _lock import state_lock

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
RQ = STAGING / "review_queue.json"


def load() -> dict:
    return json.loads(RQ.read_text())


def save(d: dict) -> None:
    tmp = RQ.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    tmp.replace(RQ)


def build(vault: str) -> None:
    root = Path(vault)
    notes = sorted(str(p.relative_to(root)) for p in root.glob("01 Permanent Notes/*.md"))
    existing = {}
    if RQ.exists():
        try:
            existing = {it["file"]: it for it in load().get("items", [])}
        except (OSError, ValueError, KeyError, TypeError):
            existing = {}
    items = []
    for f in notes:
        old = existing.get(f, {})
        items.append(
            {
                "file": f,
                "passes_done": old.get("passes_done", []),
                "claims": old.get("claims", {}),
                "passes_failed": old.get("passes_failed", {}),
            }
        )
    save({"version": 1, "vault": vault, "items": items})
    print(f"review_queue: {len(notes)} notes")


def _open(it: dict, p: str) -> bool:
    return p not in it["passes_done"] and p not in it.get("passes_failed", {})


def nxt(p: str) -> None:
    for it in load()["items"]:
        if _open(it, p):
            print(it["file"])
            return
    print("")


def claim(p: str, worker: str) -> None:
    with state_lock():
        d = load()
        picked = ""
        for it in d["items"]:
            claims = it.setdefault("claims", {})
            if _open(it, p) and p not in claims:
                claims[p] = worker
                picked = it["file"]
                break
        save(d)
    print(picked)


def done(p: str, f: str) -> None:
    with state_lock():
        d = load()
        for it in d["items"]:
            if it["file"] == f:
                if p not in it["passes_done"]:
                    it["passes_done"].append(p)
                it.setdefault("claims", {}).pop(p, None)
        save(d)


def fail(p: str, f: str, reason: str) -> None:
    with state_lock():
        d = load()
        for it in d["items"]:
            if it["file"] == f:
                it.setdefault("passes_failed", {})[p] = reason
                it.setdefault("claims", {}).pop(p, None)
        save(d)


def release(p: str, f: str) -> None:
    with state_lock():
        d = load()
        for it in d["items"]:
            if it["file"] == f:
                it.setdefault("claims", {}).pop(p, None)
        save(d)


def reclaim(worker: str) -> None:
    with state_lock():
        d = load()
        for it in d["items"]:
            claims = it.setdefault("claims", {})
            for p, owner in list(claims.items()):
                if owner == worker:
                    claims.pop(p, None)
        save(d)


def main() -> None:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", allow_abbrev=False)
    b.add_argument("--vault", required=True)
    n = sub.add_parser("next", allow_abbrev=False)
    n.add_argument("--pass", dest="p", required=True)
    c = sub.add_parser("claim", allow_abbrev=False)
    c.add_argument("--pass", dest="p", required=True)
    c.add_argument("--worker", required=True)
    dn = sub.add_parser("done", allow_abbrev=False)
    dn.add_argument("--pass", dest="p", required=True)
    dn.add_argument("--file", required=True)
    r = sub.add_parser("release", allow_abbrev=False)
    r.add_argument("--pass", dest="p", required=True)
    r.add_argument("--file", required=True)
    fl = sub.add_parser("fail", allow_abbrev=False)
    fl.add_argument("--pass", dest="p", required=True)
    fl.add_argument("--file", required=True)
    fl.add_argument("--reason", default="verification failed")
    rc = sub.add_parser("reclaim", allow_abbrev=False)
    rc.add_argument("--worker", required=True)
    a = ap.parse_args()
    {
        "build": lambda: build(a.vault),
        "next": lambda: nxt(a.p),
        "claim": lambda: claim(a.p, a.worker),
        "done": lambda: done(a.p, a.file),
        "release": lambda: release(a.p, a.file),
        "fail": lambda: fail(a.p, a.file, a.reason),
        "reclaim": lambda: reclaim(a.worker),
    }[a.cmd]()


if __name__ == "__main__":
    main()
