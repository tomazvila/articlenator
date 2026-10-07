#!/usr/bin/env python3
"""Just-in-time dedup retrieval — the SELECT lever (fixes the context-rot bomb).

The synthesis agent must NEVER load the whole concept-index into its context: it grows
toward ~1000+ entries and would dominate every iteration's input, degrading the very
dedup judgment it feeds (context rot). Instead the agent calls THIS tool per candidate
claim; the tool reads the big index here (outside the agent's window) and prints only the
top-K lexically-closest concepts. Per-iteration context stays bounded no matter how large
the index gets.

Video notes (default `--kind video`):

    python index_query.py "maltese hold volume per session" --k 15 \
        --speaker "SasaVenos" --skill "maltese on rings" --equipment "rings, band" \
        --level "not stated" --basis "speaker's own practice" --modality recommendation

The word match gives a CANDIDATE LIST only. A word match is never a reason to fold.
For each candidate the tool prints the note's `sources`, `speakers`, `scope`,
`old_format` and a `fold_check` (NOTE_CONTRACT.md section 7):

- `fold_allowed: true` only when the candidate is a contract note, all its sources have
  the same known speaker, and skill, level, equipment, basis and modality are equal. The
  value `not stated` never equals anything. The agent must still compare
  `scope.quantities`: if the new passage changes a quantity, do not fold.
- `old_format: true` means the candidate has no `sources:`. Never fold into it and never
  edit it: create a new contract note and link the old one (section 7.4).

Tweet and article notes (`--kind tweet` or `--kind article`) keep the old rule: the tool
prints the candidates, and `fold_check` says `old rule` (fold only the SAME claim).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from index_rebuild import (
    NOT_STATED,
    SCOPE_FIELDS,
    _norm,
    fm_and_body,
    is_unknown,
    note_scope,
    note_sources,
    speakers_of,
)

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
INDEX = STAGING / "concept-index.json"
STOP = set(
    "a an the of to and or in on for is are be it its as with that this by from at into "
    "not no do does can will should would could than then so such these those their our".split()
)
# Fields where `not stated` never matches. `basis` always has a stated value.
NEVER_MATCH_UNKNOWN = ("skill", "level", "equipment", "modality")
OLD_RULE = {
    "mode": "old rule",
    "fold_allowed": None,
    "reasons": [],
    "next_step": "tweet/article: fold only when it is the SAME claim, not only shared words; "
    "a contradiction becomes a tension note",
}


def toks(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 2}


def candidate_meta(c: dict, vault: Path | None) -> tuple[list[dict], dict | None]:
    """Return (sources, scope) of a candidate.

    The note frontmatter on disk is the truth. If the note is not readable, use the
    values that index_rebuild.py or index_add.py copied from the frontmatter.
    """
    srcs, scope = c.get("sources") or [], c.get("scope")
    if vault and c.get("file"):
        p = vault / c["file"]
        if p.is_file():
            fm, _ = fm_and_body(p.read_text(errors="replace"))
            srcs = note_sources(fm)
            scope = note_scope(fm) if isinstance(fm.get("scope"), dict) else None
    return srcs, scope


def scope_differences(have: dict, want: dict) -> list[str]:
    """Return one reason per scope field that does not match (NOTE_CONTRACT.md 7.1)."""
    out = []
    for k in SCOPE_FIELDS:
        a, b = have.get(k), want.get(k)
        if k in NEVER_MATCH_UNKNOWN and (is_unknown(a) or is_unknown(b)):
            out.append(
                f"{k} is 'not stated' (never equal): note has {a or NOT_STATED!r}, "
                f"new passage has {b or NOT_STATED!r}"
            )
        elif _norm(a) != _norm(b):
            out.append(
                f"{k} differs: note has {a or NOT_STATED!r}, new passage has {b or NOT_STATED!r}"
            )
    return out


def fold_check(sources: list[dict], scope: dict | None, speaker: str | None, want: dict) -> dict:
    """Decide if a new passage is allowed to fold into a candidate note (video notes).

    Rules 1-3 of NOTE_CONTRACT.md section 7.1. Rule 4 (no quantity changes) stays with
    the agent, so a `true` result always carries that reminder. In doubt: refuse.
    """
    reasons: list[str] = []
    if not sources:
        return {
            "fold_allowed": False,
            "old_format": True,
            "reasons": ["old note without `sources:`"],
            "next_step": "never edit or fold into an old note: create a new contract note, link "
            "'[[Old Note]] — older note without sources', and run note_link.py --kind "
            "supersedes-candidate",
        }
    if not speaker:
        reasons.append("no --speaker given: the fold rule needs the speaker and the scope")
    cand = speakers_of(sources)
    if NOT_STATED in {_norm(s) for s in cand} or (speaker and is_unknown(speaker)):
        reasons.append("speaker is 'not stated': the same speaker cannot be proven")
    elif speaker and {_norm(s) for s in cand} != {_norm(speaker)}:
        reasons.append(f"speaker differs: note has {cand}, new passage has {speaker!r}")
    if scope is None:
        reasons.append("candidate note has no `scope`")
    else:
        reasons.extend(scope_differences(scope, want))
    allowed = not reasons
    return {
        "fold_allowed": allowed,
        "old_format": False,
        "reasons": reasons,
        "next_step": (
            "fold only if the new passage changes no quantity in scope.quantities; "
            "otherwise create a new note and link it"
            if allowed
            else "do not fold: create a new note and link the two; if a value differs, add "
            "## Disagreement to the NEW note and run note_link.py --kind disagreement"
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("claim", help="the candidate claim to dedup")
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument(
        "--kind",
        choices=("video", "tweet", "article"),
        default="video",
        help="kind of the work unit (tweet/article keep the old fold rule)",
    )
    ap.add_argument("--speaker", default="", help="speaker of the new passage")
    for k in SCOPE_FIELDS:
        ap.add_argument(f"--{k}", default=NOT_STATED, help=f"scope.{k} of the new passage")
    args = ap.parse_args()

    # The index is $ZR_STAGING/concept-index.json and the vault is $ZK_DIR. There are no
    # flags for other paths: the tool reads no file that the caller names.
    idx = json.loads(INDEX.read_text()) if INDEX.exists() else {"concepts": []}
    vault = Path(os.environ["ZK_DIR"]) if os.environ.get("ZK_DIR") else None
    want = {k: getattr(args, k) for k in SCOPE_FIELDS}
    q = toks(args.claim)
    qtags = q

    scored = []
    for c in idx.get("concepts", []):
        ct = (
            toks(c.get("title", ""))
            | toks(" ".join(c.get("aliases", [])))
            | toks(c.get("gist", ""))
        )
        inter = len(q & ct)
        if not ct or inter == 0:
            continue
        jaccard = inter / len(q | ct)
        tag_bonus = 0.1 * len(qtags & {t.lower() for t in c.get("tags", [])})
        scored.append((round(jaccard + tag_bonus, 3), c))

    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for s, c in scored[: args.k]:
        row = {
            "score": s,
            "title": c["title"],
            "file": c.get("file"),
            "gist": c.get("gist", ""),
            "tags": c.get("tags", []),
        }
        if args.kind == "video":
            srcs, scope = candidate_meta(c, vault)
            row.update(
                {
                    "speakers": speakers_of(srcs) if srcs else [],
                    "sources": srcs,
                    "scope": scope,
                    "old_format": not srcs,
                    "fold_check": fold_check(srcs, scope, args.speaker or None, want),
                }
            )
        else:
            row["fold_check"] = dict(OLD_RULE)
        out.append(row)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
