#!/usr/bin/env python3
"""Upsert one concept into the index without the agent ever loading the whole file.

Pairs with index_query.py: the agent reads candidates via query, writes via this. Neither
ever pulls the growing concept-index.json into the agent's context (the SELECT lever).
The source id is also appended to provenance.json keyed by the note's FILE PATH (a
legacy hint; the harness keeps provenance.jsonl).

Video notes (NOTE_CONTRACT.md). Write the note file FIRST, with the new source in its
`sources:` frontmatter, then run:

    python index_add.py --title "<H1 of the note>" --file "01 Permanent Notes/<Title>.md" \
        --gist "<lead sentence>" --tags calisthenics,isometric-volume \
        --source vid-krqBQjUydGY --speaker "SasaVenos" \
        --skill "maltese on rings" --level "not stated" --equipment "rings, band" \
        --basis "speaker's own practice" --modality recommendation

Video mode applies when `--source` starts with `vid-` or the note file has `sources:`.
In video mode the tool keys on the FILE and reads the truth from the note frontmatter.
It refuses (exit code 3, nothing changed) when:

- the vault folder (`--vault` or $ZK_DIR) or the note file does not exist;
- the H1 of the file is not `--title`, or another file already has this title;
- the note has no `sources:` (an old note), unless `--repair` is given;
- `--source` is given without `--speaker`, or the source is not in the note's `sources`
  with that speaker;
- the note's sources have more than one speaker, or the speaker `not stated` with more
  than one source;
- the source is NEW for this file (a fold) and the scope arguments do not equal the
  note's scope (`not stated` never equals anything).

Tweet and article notes (other source ids, no `sources:` in the note) keep the old
behavior: the entry is keyed on the title and no speaker check runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from _lock import state_lock
from index_query import scope_differences
from index_rebuild import (
    NOT_STATED,
    SCOPE_FIELDS,
    _norm,
    fm_and_body,
    h1,
    note_scope,
    note_sources,
    speakers_of,
)

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
INDEX = STAGING / "concept-index.json"
PROV = STAGING / "provenance.json"
EXIT_FOLD_REFUSED = 3


class Refused(Exception):
    """The update breaks the fold rule; nothing is written."""


def _load(p: Path, default: dict) -> dict:
    return json.loads(p.read_text()) if p.exists() else default


def _save(p: Path, d: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    tmp.replace(p)


def _csv(s: str | None, sep: str = ",") -> list[str]:
    return [x.strip() for x in (s or "").split(sep) if x.strip()]


def read_note(vault: Path | None, rel: str) -> tuple[dict, str] | None:
    """Return (frontmatter, H1) of the note file, or None if it does not exist."""
    if vault is None:
        return None
    p = vault / rel
    if not p.is_file():
        return None
    fm, body = fm_and_body(p.read_text(errors="replace"))
    return fm, h1(body) or p.stem


def video_conflicts(
    *,
    fm: dict,
    note_title: str,
    title: str,
    source: str,
    speaker: str,
    want: dict,
    prior_sources: set[str],
    repair: bool,
) -> list[str]:
    """Return the reasons to refuse a video-mode update (empty list = allowed).

    `fm` is the note frontmatter on disk, `prior_sources` the source ids that the index
    and provenance.json already record for this FILE.
    """
    out: list[str] = []
    if note_title != title:
        out.append(f"--title {title!r} does not match the H1 of the file ({note_title!r})")
    srcs = note_sources(fm)
    if not srcs:
        if not repair:
            out.append(
                "old note without `sources:` in its frontmatter: never fold into or edit it; "
                "create a new contract note (NOTE_CONTRACT.md 7.4), or use --repair"
            )
        return out
    have = speakers_of(srcs)
    norm_have = {_norm(s) for s in have}
    if len(norm_have) > 1:
        out.append(f"the note mixes speakers {have}: one note holds one speaker")
    if NOT_STATED in norm_have and len(srcs) > 1:
        out.append("a note with speaker 'not stated' never folds")
    if source:
        if not speaker:
            out.append(f"give --speaker for source {source}")
        else:
            match = [s for s in srcs if s["id"] == source]
            if not match:
                out.append(
                    f"source {source} is not in the note's `sources:`; add it to the note first"
                )
            elif _norm(match[0]["speaker"]) != _norm(speaker):
                out.append(
                    f"--speaker {speaker!r} differs from the note's speaker {match[0]['speaker']!r}"
                )
        is_fold = bool(prior_sources) and source not in prior_sources
        if is_fold and not repair:
            out.extend(f"fold refused: {r}" for r in scope_differences(note_scope(fm), want))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--file", required=True, help="note path relative to the vault folder")
    ap.add_argument("--gist", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--aliases", default="", help="semicolon-separated")
    ap.add_argument("--moc", default="")
    ap.add_argument("--source", default="", help="source id to record (vid-<id> for videos)")
    ap.add_argument("--source-url", default="", help="ignored for video notes (read from the note)")
    ap.add_argument("--speaker", default="", help="speaker of the source passage")
    ap.add_argument("--channel", default="", help="ignored for video notes (read from the note)")
    for k in SCOPE_FIELDS:
        ap.add_argument(f"--{k}", default=NOT_STATED, help=f"scope.{k} of the new passage")
    ap.add_argument(
        "--vault", default=os.environ.get("ZK_DIR", ""), help="vault folder (default $ZK_DIR)"
    )
    ap.add_argument(
        "--repair",
        action="store_true",
        help="repair run (prompts/repair_note.md): allow an old note without sources",
    )
    ap.add_argument("--claim-inc", action="store_true", help="increment claim_count")
    a = ap.parse_args()

    vault = Path(a.vault) if a.vault else None
    note = read_note(vault, a.file)
    video = a.source.startswith("vid-") or bool(note and note_sources(note[0]))
    want = {k: getattr(a, k) for k in SCOPE_FIELDS}

    try:
        with state_lock():
            idx = _load(INDEX, {"version": 1, "concepts": [], "mocs": []})
            prov = _load(PROV, {})
            if video:
                if note is None:
                    raise Refused(
                        f"note file not found: {a.file!r} in vault {a.vault or '(no --vault/$ZK_DIR)'}; "
                        "write the note first"
                    )
                fm, note_title = note
                entry = next((c for c in idx["concepts"] if c.get("file") == a.file), None)
                other = next(
                    (
                        c
                        for c in idx["concepts"]
                        if c["title"] == a.title and c.get("file") != a.file
                    ),
                    None,
                )
                if other is not None and not a.repair:
                    raise Refused(f"title {a.title!r} already belongs to {other.get('file')!r}")
                prior = {s.get("id") for s in (entry or {}).get("sources", [])} | set(
                    prov.get(a.file, [])
                )
                problems = video_conflicts(
                    fm=fm,
                    note_title=note_title,
                    title=a.title,
                    source=a.source,
                    speaker=a.speaker,
                    want=want,
                    prior_sources=prior,
                    repair=a.repair,
                )
                if problems:
                    raise Refused("\n  - ".join(problems))
            else:
                entry = next((c for c in idx["concepts"] if c["title"] == a.title), None)

            if entry is None:
                entry = {
                    "title": a.title,
                    "file": a.file,
                    "aliases": [],
                    "gist": "",
                    "tags": [],
                    "mocs": [],
                    "claim_count": 0,
                }
                idx["concepts"].append(entry)
            entry["title"] = a.title
            entry["file"] = a.file
            if a.gist:
                entry["gist"] = a.gist
            entry["aliases"] = sorted(set(entry.get("aliases", []) + _csv(a.aliases, ";")))
            entry["tags"] = sorted(set(entry.get("tags", []) + _csv(a.tags)))
            if a.moc:
                entry["mocs"] = sorted(set(entry.get("mocs", []) + [a.moc]))
            if a.claim_inc:
                entry["claim_count"] = entry.get("claim_count", 0) + 1
            if video:
                # The note frontmatter is the truth; arguments never become stored sources.
                fm = note[0]
                srcs = note_sources(fm)
                for k in ("sources", "speakers", "scope"):
                    entry.pop(k, None)
                if srcs:
                    entry["sources"] = srcs
                    entry["speakers"] = speakers_of(srcs)
                if isinstance(fm.get("scope"), dict):
                    entry["scope"] = note_scope(fm)
            _save(INDEX, idx)

            if a.source:
                prov.setdefault(a.file, [])
                if a.source not in prov[a.file]:
                    prov[a.file].append(a.source)
                _save(PROV, prov)
    except Refused as e:
        print(
            f"refused: '{a.title}':\n  - {e}\n"
            "Nothing was changed. Create a new note for the new passage and link the two. If a "
            "value differs, add '## Disagreement' to the NEW note (NOTE_CONTRACT.md section 7).",
            file=sys.stderr,
        )
        sys.exit(EXIT_FOLD_REFUSED)

    print(f"upserted: {a.title}  (sources for file: {len(_load(PROV, {}).get(a.file, []))})")


if __name__ == "__main__":
    main()
