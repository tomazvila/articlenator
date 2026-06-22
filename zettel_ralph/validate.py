#!/usr/bin/env python3
"""The verification gate (worker != checker).

Deterministically validates the emitted zettelkasten against the conventions proven
by the `Andrew Torba AI Programming Zettelkasten`. The Ralph loop must see this exit 0
before marking any item `synthesized`. Run standalone any time to audit the vault.

    python zettel_ralph/validate.py --vault "$HOME/Documents/Themis 2.0/Twitter Bookmarks Zettelkasten"

Exit code 0 = no errors (warnings allowed). Exit code 1 = at least one error.
"""
from __future__ import annotations

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ALLOWED_TYPES = {"permanent note", "map note", "map", "index", "example index", "review"}
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
BARE_URL = re.compile(r"(?<!\()https?://", re.I)
PROVENANCE = re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com|t\.co)/", re.I)
REQUIRED_PERMANENT_SECTIONS = ("## Why This Matters", "## Details", "## Connected Ideas")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Tolerant YAML-ish parser: handles inline `k: v`, inline lists `k: [a, b]`,
    and block sequences (`k:` then `  - a` lines). Values are str or list[str]."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip("\n")
    body = text[end + 4 :]
    fm: dict[str, object] = {}
    cur_key: str | None = None
    for line in raw.splitlines():
        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val == "":
                fm[key] = []  # a block sequence may follow
                cur_key = key
            elif val.startswith("[") and val.endswith("]"):
                fm[key] = [x.strip().strip("'\"") for x in val[1:-1].split(",") if x.strip()]
                cur_key = None
            else:
                fm[key] = val.strip("'\"")
                cur_key = None
        else:
            lm = re.match(r"^\s*-\s*(.+)$", line)
            if lm and cur_key is not None and isinstance(fm.get(cur_key), list):
                fm[cur_key].append(lm.group(1).strip().strip("'\""))  # type: ignore[union-attr]
    return fm, body


def _as_str(v: object) -> str:
    return v if isinstance(v, str) else (" ".join(v) if isinstance(v, list) else "")


def _section_bullets(body: str, header: str) -> int:
    """Count top-level bullets in a `## Section` (atomicity heuristic)."""
    lines = body.splitlines()
    try:
        i = next(k for k, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        return 0
    n = 0
    for ln in lines[i + 1 :]:
        if ln.startswith("## "):
            break
        if re.match(r"^\s*-\s+\S", ln):
            n += 1
    return n


def _ngrams(text: str, n: int = 8) -> set[str]:
    w = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(w[i : i + n]) for i in range(len(w) - n + 1)}


def declarative_title_ok(title: str) -> bool:
    """Soft heuristic: a claim, not a fragment. >=3 words, not a question."""
    words = title.split()
    return len(words) >= 3 and not title.strip().endswith("?")


BOILERPLATE_TAGS = {"zettelkasten", "permanent-note", "map-note", "index", "example-index", "review", "tension"}
# Broad domain tags get a domain index on Home.md, not a squeeze MOC; MOCs form around the
# FINE topic tags below the domain level (keeps maps coherent in a multi-domain corpus).
BROAD_DOMAINS = {
    "ai", "agents", "markets", "finance", "semiconductors", "hardware", "software",
    "geopolitics", "politics", "language-learning", "health", "personal", "crypto", "science",
}


def _tag_list(fm: dict) -> list[str]:
    tv = fm.get("tags", [])
    raw = tv if isinstance(tv, list) else [x for x in re.split(r"[,\[\]]", str(tv))]
    return [t for t in (x.strip().strip("\"'") for x in raw) if t and t not in BOILERPLATE_TAGS]


def _title_tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


def squeeze_report(parsed: list, root: Path) -> int:
    """Deterministic MOC squeeze-point detection from the files on disk (ground truth,
    not agent self-report). Prints {tag: {count, has_moc}} so the agent ACTS on a computed
    trigger instead of eyeballing a huge index."""
    note_tags: dict[str, int] = {}
    moc_tags: set[str] = set()
    for f, fm, body, h1 in parsed:
        ntype = _as_str(fm.get("type", "")).strip().strip('"')
        tags = _tag_list(fm)
        if ntype == "permanent note":
            for t in tags:
                if t in BROAD_DOMAINS:  # domain tags get a Home index, not a squeeze MOC
                    continue
                note_tags[t] = note_tags.get(t, 0) + 1
        elif ntype in ("map note", "map"):
            moc_tags.update(tags)
    out = {
        t: {"count": n, "has_moc": t in moc_tags, "at_squeeze": n >= 5 and t not in moc_tags}
        for t, n in sorted(note_tags.items(), key=lambda kv: kv[1], reverse=True)
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True, help="path to the zettelkasten folder")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--squeeze", action="store_true", help="print topic counts + MOC squeeze points and exit")
    ap.add_argument("--staging", help="harness staging dir; enables own-words (verbatim overlap) check")
    args = ap.parse_args()

    root = Path(args.vault)
    if not root.exists():
        print(f"ERROR: vault not found: {root}")
        return 1

    md_files = sorted(root.rglob("*.md"))
    titles: set[str] = set()
    aliases: set[str] = set()
    errors: list[str] = []
    warnings: list[str] = []

    parsed: list[tuple[Path, dict, str, str]] = []
    for f in md_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        fm, body = parse_frontmatter(text)
        h1 = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), "")
        parsed.append((f, fm, body, h1))
        titles.add(f.stem)
        if h1:
            titles.add(h1)
        av = fm.get("aliases", [])
        alias_list = av if isinstance(av, list) else [x.strip() for x in re.split(r"[,\[\]]", str(av))]
        for a in alias_list:
            a = a.strip().strip("[]\"'")
            if a:
                aliases.add(a)

    if args.squeeze:
        return squeeze_report(parsed, root)

    resolvable = titles | aliases

    # Near-duplicate permanent-note titles (cheap backstop for semantic dups / title drift).
    perm_titles = [
        (f.relative_to(root), f.stem)
        for f, fm, body, h1 in parsed
        if _as_str(fm.get("type", "")).strip().strip('"') == "permanent note"
    ]
    for i in range(len(perm_titles)):
        ri, ti = perm_titles[i]
        a = _title_tokens(ti)
        for j in range(i + 1, len(perm_titles)):
            rj, tj = perm_titles[j]
            b = _title_tokens(tj)
            if a and b and len(a & b) / len(a | b) >= 0.7:
                warnings.append(f"{ri}: near-duplicate title of '{tj}' (possible semantic dup)")

    for f, fm, body, h1 in parsed:
        rel = f.relative_to(root)
        ntype = _as_str(fm.get("type", "")).strip().strip('"')

        # frontmatter
        if not fm:
            errors.append(f"{rel}: missing YAML frontmatter")
            continue
        if ntype not in ALLOWED_TYPES:
            errors.append(f"{rel}: type '{ntype}' not in {sorted(ALLOWED_TYPES)}")
        if "created" not in fm:
            warnings.append(f"{rel}: no 'created' date")

        # H1 / title
        if not h1:
            errors.append(f"{rel}: no H1 title")
        elif h1 != f.stem:
            warnings.append(f"{rel}: H1 '{h1}' != filename '{f.stem}'")

        # no raw provenance backlinks (a locked vault standard)
        if PROVENANCE.search(body):
            errors.append(f"{rel}: contains raw provenance backlink (x.com/twitter.com/t.co)")
        if BARE_URL.search(body):
            warnings.append(f"{rel}: contains a bare URL in body")

        links = WIKILINK.findall(body)

        if ntype == "permanent note":
            tags = _tag_list(fm)
            if h1 and not declarative_title_ok(h1):
                warnings.append(f"{rel}: title may not be a declarative claim")
            for sec in REQUIRED_PERMANENT_SECTIONS:
                if sec not in body:
                    errors.append(f"{rel}: missing required section '{sec}'")
            if "## Grounded Example" not in body:
                errors.append(f"{rel}: missing '## Grounded Example' (a hard vault standard)")
            if not links:
                errors.append(f"{rel}: ORPHAN - permanent note has no wikilinks")
            if not any(t not in BROAD_DOMAINS for t in tags):
                warnings.append(f"{rel}: no fine topic tag (only domain tags) - MOCs need fine tags")
            # atomicity guards (the user's #1 requirement: one claim per note)
            is_tension = "tension" in (_as_str(fm.get("tags", "")))
            if h1 and not is_tension and re.search(r"\b(?:and|vs\.?|versus)\b|,", h1, re.I):
                warnings.append(f"{rel}: title joins multiple ideas (atomicity smell - split?)")
            details = _section_bullets(body, "## Details")
            if details > 6:
                warnings.append(f"{rel}: ## Details has {details} bullets (fat note - split?)")
            wc = len(re.findall(r"\w+", body))
            if wc > 220:
                warnings.append(f"{rel}: body is {wc} words (atomic notes stay short - split?)")
            h1_count, in_fence = 0, False
            for ln in body.splitlines():
                if ln.lstrip().startswith("```"):
                    in_fence = not in_fence
                elif not in_fence and re.match(r"^# \S", ln):
                    h1_count += 1
            if h1_count > 1:
                warnings.append(f"{rel}: more than one H1 (atomicity smell)")

        # broken-link check
        for target in links:
            t = target.strip()
            if t not in resolvable:
                warnings.append(f"{rel}: unresolved wikilink [[{t}]]")

    # own-words check (anti collector's-fallacy): flag long verbatim overlap with a source
    if args.staging:
        stg = Path(args.staging)
        try:
            prov = json.loads((stg / "provenance.json").read_text())
        except (FileNotFoundError, ValueError):
            prov = {}
        for f, fm, body, h1 in parsed:
            rel = str(f.relative_to(root))
            src_ids = prov.get(rel, [])
            if not src_ids:
                continue
            body_ng = _ngrams(body, 8)
            for sid in src_ids:
                lit = stg / "lit" / f"{sid}.md"
                if lit.exists() and body_ng & _ngrams(lit.read_text(errors="replace"), 8):
                    warnings.append(f"{rel}: long verbatim overlap with source {sid} (rewrite in own words)")
                    break

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"\n{len(md_files)} notes | {len(errors)} errors | {len(warnings)} warnings")

    fail = bool(errors) or (args.strict and bool(warnings))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
