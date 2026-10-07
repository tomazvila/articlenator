#!/usr/bin/env python3
"""Rebuild concept-index.json from the vault on disk (crash-recovery for dedup).

A crashed iteration (e.g. an API hiccup mid-response) can leave notes on disk that were
never recorded in the index. Running this before each synthesis iteration makes the dedup
oracle reflect what is actually on disk. Preserves gist/claim_count/mocs from the
prior index where titles still match. Provenance (provenance.json) is left untouched.

Each concept entry also gets the note's `sources` (id, url, speaker, channel), its
`speakers`, and its `scope` from the note frontmatter (see NOTE_CONTRACT.md). These
three fields come ONLY from the frontmatter on disk, never from the old index: an old
note without `sources:` gets no `sources` entry, so the fold check treats it as an old
note. The fold decision in index_query.py and index_add.py uses these fields.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
INDEX = STAGING / "concept-index.json"
VAULT = Path(
    os.environ.get("ZK_DIR")
    or (Path.home() / "Documents" / "Themis 2.0" / "Twitter Bookmarks Zettelkasten")
)

_KEY = re.compile(r"^([A-Za-z0-9_-]+):(?:\s+(.*)|\s*)$")
SCOPE_FIELDS = ("skill", "level", "equipment", "basis", "modality")
NOT_STATED = "not stated"


def _indent(ln: str) -> int:
    return len(ln) - len(ln.lstrip(" "))


def _scalar(v: str):
    """Parse one YAML scalar of the small subset that notes use."""
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    cut = v.find(" #")
    if cut >= 0:
        v = v[:cut].rstrip()
    return v


def _block(lines: list[str], i: int, ind: int):
    """Parse a mapping or a list that starts at lines[i] with indent `ind`."""
    if lines[i].lstrip().startswith("- ") or lines[i].strip() == "-":
        out: list = []
        while i < len(lines) and _indent(lines[i]) == ind and lines[i].lstrip().startswith("-"):
            text = lines[i].lstrip()[1:].strip()
            if _KEY.match(text):
                # A list item that is a mapping: its first key is on the "- " line.
                lines[i] = " " * (ind + 2) + text
                val, i = _block(lines, i, ind + 2)
                out.append(val)
            else:
                out.append(_scalar(text))
                i += 1
        return out, i
    out_map: dict = {}
    while i < len(lines) and _indent(lines[i]) == ind and not lines[i].lstrip().startswith("- "):
        m = _KEY.match(lines[i].strip())
        if not m:
            i += 1
            continue
        k, v = m.group(1), (m.group(2) or "").strip()
        i += 1
        if v and not v.startswith("#"):
            out_map[k] = _scalar(v)
            continue
        if i < len(lines) and (
            _indent(lines[i]) > ind
            or (_indent(lines[i]) == ind and lines[i].lstrip().startswith("- "))
        ):
            out_map[k], i = _block(lines, i, _indent(lines[i]))
        else:
            out_map[k] = []
    return out_map, i


def parse_frontmatter(raw: str) -> dict:
    """Parse the YAML subset used by note frontmatter (no external dependency).

    Supports scalars, quoted scalars, flow lists `[a, b]`, block lists, nested
    mappings, and lists of mappings (the `sources:` and `scope.quantities:` shapes).
    """
    lines = [
        ln.rstrip() for ln in raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not lines:
        return {}
    fm, _ = _block(lines, 0, _indent(lines[0]))
    return fm if isinstance(fm, dict) else {}


def fm_and_body(t: str):
    if not t.startswith("---"):
        return {}, t
    e = t.find("\n---", 3)
    if e < 0:
        return {}, t
    raw, body = t[3:e], t[e + 4 :]
    return parse_frontmatter(raw), body


def _norm(v) -> str:
    s = re.sub(r"\s+", " ", str(v if v not in (None, [], "") else NOT_STATED)).strip().lower()
    return s or NOT_STATED


def note_sources(fm: dict) -> list[dict]:
    """Return the note's `sources:` entries as dicts with id/url/speaker/channel."""
    out = []
    for s in fm.get("sources") or []:
        if isinstance(s, dict) and s.get("id"):
            out.append({k: str(s.get(k, "") or "") for k in ("id", "url", "speaker", "channel")})
    return out


def note_scope(fm: dict) -> dict:
    """Return the note's `scope:` with every field present (`not stated` if absent)."""
    sc = fm.get("scope") if isinstance(fm.get("scope"), dict) else {}
    out = {k: str(sc.get(k) or NOT_STATED) for k in SCOPE_FIELDS}
    q = sc.get("quantities") or []
    out["quantities"] = [x for x in q if isinstance(x, dict)] if isinstance(q, list) else []
    return out


def speakers_of(sources: list[dict]) -> list[str]:
    return sorted({(s.get("speaker") or NOT_STATED) for s in sources})


def is_unknown(v) -> bool:
    """True for an empty value or `not stated`. Such a value never matches in a fold check."""
    return _norm(v) == NOT_STATED


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
        if f.name.startswith("_"):
            continue
        fm, body = fm_and_body(f.read_text(errors="replace"))
        title = h1(body) or f.stem
        tags = fm.get("tags", [])
        tags = tags if isinstance(tags, list) else [tags]
        al = fm.get("aliases", [])
        al = al if isinstance(al, list) else [al]
        p = prev.get(title, {})
        entry = {
            "title": title,
            "file": str(f.relative_to(VAULT)),
            "aliases": [a for a in al if a],
            "gist": p.get("gist") or gist(body),
            "tags": [t for t in tags if t],
            "mocs": p.get("mocs", []),
            "claim_count": p.get("claim_count", 1),
        }
        srcs = note_sources(fm)
        if srcs:
            entry["sources"] = srcs
            entry["speakers"] = speakers_of(srcs)
        if isinstance(fm.get("scope"), dict):
            entry["scope"] = note_scope(fm)
        concepts.append(entry)
    for f in sorted((VAULT / "00 Maps").glob("*.md")):
        _, body = fm_and_body(f.read_text(errors="replace"))
        mocs.append(
            {"title": h1(body) or f.stem, "file": str(f.relative_to(VAULT)), "note_count": 0}
        )

    out = {"version": 1, "concepts": concepts, "mocs": mocs}
    tmp = INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    tmp.replace(INDEX)
    print(f"rebuilt index: {len(concepts)} concepts, {len(mocs)} mocs")


if __name__ == "__main__":
    main()
