#!/usr/bin/env python3
"""Upsert one concept into the index without the agent ever loading the whole file.

Pairs with index_query.py: the agent reads candidates via query, writes via this. Neither
ever pulls the growing concept-index.json into the agent's context (the SELECT lever).
Provenance is appended to provenance.json keyed by the note's FILE PATH (so any note can
be traced to its sources even though the vault itself stays source-free).

    python index_add.py --title "An Atomic Note Separates One Concern" \
        --file "01 Permanent Notes/An Atomic Note Separates One Concern.md" \
        --gist "one idea per note enables reuse" --tags note-design,pkm \
        --aliases "Atomicity;One idea per note" --moc "MOC Note Design" \
        --source tw-123 --claim-inc
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _lock import state_lock

HERE = Path(__file__).resolve().parent
INDEX = HERE / "staging" / "concept-index.json"
PROV = HERE / "staging" / "provenance.json"


def _load(p: Path, default: dict) -> dict:
    return json.loads(p.read_text()) if p.exists() else default


def _save(p: Path, d: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    tmp.replace(p)


def _csv(s: str | None, sep: str = ",") -> list[str]:
    return [x.strip() for x in (s or "").split(sep) if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--file", required=True, help="note path relative to the vault folder")
    ap.add_argument("--gist", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--aliases", default="", help="semicolon-separated")
    ap.add_argument("--moc", default="")
    ap.add_argument("--source", default="", help="source id to record as provenance")
    ap.add_argument("--claim-inc", action="store_true", help="increment claim_count")
    a = ap.parse_args()

    with state_lock():
        idx = _load(INDEX, {"version": 1, "concepts": [], "mocs": []})
        entry = next((c for c in idx["concepts"] if c["title"] == a.title), None)
        if entry is None:
            entry = {"title": a.title, "file": a.file, "aliases": [], "gist": "", "tags": [], "mocs": [], "claim_count": 0}
            idx["concepts"].append(entry)

        entry["file"] = a.file
        if a.gist:
            entry["gist"] = a.gist
        entry["aliases"] = sorted(set(entry.get("aliases", []) + _csv(a.aliases, ";")))
        entry["tags"] = sorted(set(entry.get("tags", []) + _csv(a.tags)))
        if a.moc:
            entry["mocs"] = sorted(set(entry.get("mocs", []) + [a.moc]))
        if a.claim_inc:
            entry["claim_count"] = entry.get("claim_count", 0) + 1
        _save(INDEX, idx)

        if a.source:
            prov = _load(PROV, {})
            prov.setdefault(a.file, [])
            if a.source not in prov[a.file]:
                prov[a.file].append(a.source)
            _save(PROV, prov)

    print(f"upserted: {a.title}  (sources for file: {len(_load(PROV, {}).get(a.file, []))})")


if __name__ == "__main__":
    main()
