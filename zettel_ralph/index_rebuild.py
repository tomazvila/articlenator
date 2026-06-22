#!/usr/bin/env python3
"""Rebuild concept-index.json from the vault on disk (crash-recovery for dedup).

A crashed iteration (e.g. an API hiccup mid-response) can leave notes on disk that were
never recorded in the index. Running this before each synthesis iteration makes the dedup
oracle reflect what is actually on disk, so re-processing a half-finished item folds into the
existing notes instead of creating duplicates. Preserves gist/claim_count/mocs from the
prior index where titles still match. Provenance (provenance.json) is left untouched.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "staging" / "concept-index.json"
VAULT = Path.home() / "Documents" / "Themis 2.0" / "Twitter Bookmarks Zettelkasten"


def fm_and_body(t: str):
    if not t.startswith("---"):
        return {}, t
    e = t.find("\n---", 3)
    if e < 0:
        return {}, t
    raw, body = t[3:e], t[e + 4 :]
    fm: dict = {}
    cur = None
    for ln in raw.splitlines():
        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", ln)
        if m:
            k, v = m.group(1), m.group(2).strip()
            if v == "":
                fm[k] = []
                cur = k
            elif v.startswith("[") and v.endswith("]"):
                fm[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                cur = None
            else:
                fm[k] = v.strip("'\"")
                cur = None
        else:
            lm = re.match(r"^\s*-\s*(.+)$", ln)
            if lm and cur and isinstance(fm.get(cur), list):
                fm[cur].append(lm.group(1).strip().strip("'\""))
    return fm, body


def h1(body: str) -> str:
    return next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), "")


def gist(body: str) -> str:
    seen = False
    for ln in body.splitlines():
        if ln.startswith("# "):
            seen = True
            continue
        if seen and ln.strip() and not ln.startswith("#"):
            return ln.strip()[:160]
    return ""


def main() -> None:
    old = json.loads(INDEX.read_text()) if INDEX.exists() else {"concepts": []}
    prev = {c["title"]: c for c in old.get("concepts", [])}
    concepts, mocs = [], []

    for f in sorted((VAULT / "01 Permanent Notes").glob("*.md")):
        fm, body = fm_and_body(f.read_text(errors="replace"))
        title = h1(body) or f.stem
        tags = fm.get("tags", [])
        tags = tags if isinstance(tags, list) else [tags]
        al = fm.get("aliases", [])
        al = al if isinstance(al, list) else [al]
        p = prev.get(title, {})
        concepts.append({
            "title": title,
            "file": str(f.relative_to(VAULT)),
            "aliases": [a for a in al if a],
            "gist": p.get("gist") or gist(body),
            "tags": [t for t in tags if t],
            "mocs": p.get("mocs", []),
            "claim_count": p.get("claim_count", 1),
        })
    for f in sorted((VAULT / "00 Maps").glob("*.md")):
        _, body = fm_and_body(f.read_text(errors="replace"))
        mocs.append({"title": h1(body) or f.stem, "file": str(f.relative_to(VAULT)), "note_count": 0})

    out = {"version": 1, "concepts": concepts, "mocs": mocs}
    tmp = INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    tmp.replace(INDEX)
    print(f"rebuilt index: {len(concepts)} concepts, {len(mocs)} mocs")


if __name__ == "__main__":
    main()
