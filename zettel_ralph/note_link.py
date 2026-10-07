#!/usr/bin/env python3
"""Record a link between two notes for the harness to complete at publish time.

    python note_link.py --from "01 Permanent Notes/A.md" --to "01 Permanent Notes/B.md" \
        --kind disagreement|supersedes-candidate|related

Appends {from, to, kind, unit, run_id, ts} to $ZR_STAGING/disagreements.jsonl under the
state lock. The agent never edits the other note: when the `from` note is published,
run_integrity.py adds the backlink to the `to` note (NOTE_CONTRACT.md 7.3, 7.4).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import provenance  # noqa: E402

KINDS = ("disagreement", "supersedes-candidate", "related")


def _note_rel(raw: str) -> str:
    p = Path(raw)
    for env in ("ZR_SHADOW_DIR", "ZK_DIR"):
        root = os.environ.get(env)
        if root and p.is_absolute():
            try:
                return p.resolve().relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                continue
    if p.is_absolute() or ".." in p.parts or p.suffix != ".md":
        raise ValueError(f"note path must be relative to the vault folder and end in .md: {raw}")
    return p.as_posix()


def _read_note(rel: str) -> str | None:
    """The unit's shadow version of a note, else the vault version."""
    for env in ("ZR_SHADOW_DIR", "ZK_DIR"):
        root = os.environ.get(env)
        if root and (Path(root) / rel).is_file():
            return (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
    return None


def _has_sources(text: str) -> bool:
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    return end != -1 and re.search(r"(?m)^sources:", text[3:end]) is not None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record a note link for the harness", allow_abbrev=False)
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("--kind", required=True, choices=KINDS)
    a = ap.parse_args(argv)
    try:
        src, dst = _note_rel(a.src), _note_rel(a.dst)
    except ValueError as exc:
        print(f"note_link: {exc}", file=sys.stderr)
        return 2
    src_text, dst_text = _read_note(src), _read_note(dst)
    if a.kind == "supersedes-candidate" and dst_text is not None and _has_sources(dst_text):
        print("note_link: --kind supersedes-candidate is only for an OLD note (no 'sources:'); "
              f"{dst} has sources. Use --kind related or --kind disagreement.", file=sys.stderr)
        return 2
    if a.kind == "disagreement" and (src_text is None or "\n## Disagreement" not in src_text):
        print(f"note_link: --kind disagreement needs a '## Disagreement' section in {src} "
              "(NOTE_CONTRACT.md 7.3). Add it first, or use --kind related.", file=sys.stderr)
        return 2
    staging = Path(os.environ.get("ZR_STAGING") or HERE / "staging")
    rec = {"ts": provenance.now_iso(), "from": src, "to": dst, "kind": a.kind,
           "unit": os.environ.get("ZR_UNIT", ""), "run_id": os.environ.get("ZR_RUN_ID", "")}
    provenance.append_jsonl(staging / "disagreements.jsonl", rec, staging)
    print(f"note_link: recorded {a.kind}: {src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
