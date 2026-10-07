#!/usr/bin/env python3
"""The form check step (worker != checker).

Deterministically validates the emitted zettelkasten against the vault conventions
and the form rules of NOTE_CONTRACT.md. It checks form only (frontmatter, sections,
links, title). `verify_claims.py` compares each note with its transcript.

    python zettel_ralph/validate.py --vault "<zk dir>"                 # whole vault
    python zettel_ralph/validate.py --vault "<zk dir>" --files A.md B.md   # only these notes

A note with a `sources:` list must follow NOTE_CONTRACT.md. For such a note this script
also runs verify_claims.verify_note: sources, scope fields and values, the verification
list, source tags, one speaker in Details, and (when the transcripts are found under
--staging/lit or $ZR_STAGING/lit) the Evidence quote match and the number check. Every
contract failure is an ERROR. Notes without `sources:` (the old format) get warnings only.

With --files, links still resolve against the whole vault, but only the listed notes
are checked. The run harness uses --files so that one worker's bad note never stops
another worker's note.

Exit code 0 = no errors (warnings allowed). Exit code 1 = at least one error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ALLOWED_TYPES = {"permanent", "permanent note", "map note", "map", "index", "example index", "review"}
PERMANENT_TYPES = {"permanent", "permanent note"}
# NOTE_CONTRACT.md section 2: the only allowed values of `verification:`.
VERIFICATION_VALUES = {"unverified", "quote-checked", "source-checked", "failed", "unsupported", "needs-repair",
                       "superseded"}  # round 21: a pipeline stub
def _note_links(body: str) -> list[str]:
    """Round 18 (V30): wiki links to notes, as Obsidian reads them: no embeds (`![[...]]`),
    nothing inside code blocks or inline code; a table's `\\|` alias is unescaped."""
    out, fence = [], False
    for ln in body.split("\n"):
        if ln.strip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        ln = re.sub(r"`[^`]*`", "", ln).replace("\\|", "|")
        for m in re.finditer(r"(!?)\[\[([^\]|#]+)", ln):
            if not m.group(1):
                out.append(m.group(2).strip())
    return out


def _resolves(target: str, index: "VaultIndex") -> bool:
    """Obsidian resolution: the last path part, `.md` stripped, case-insensitive; a link to a
    non-note file (`x.pdf`, `img.png`) resolves when such a file exists."""
    name = target.replace("\\", "").strip().split("/")[-1]
    if re.search(r"\.(?!md$)[A-Za-z0-9]{2,4}$", name):
        return True  # a file link (attachment), not a note: not judged here
    name = re.sub(r"\.md$", "", name, flags=re.I)
    if not hasattr(index, "_lower"):
        index._lower = {x.lower() for x in index.titles}
    return name.lower() in index._lower


WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
BARE_URL = re.compile(r"(?<!\()https?://", re.I)
PROVENANCE = re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com|t\.co)/", re.I)
REQUIRED_PERMANENT_SECTIONS = ("## Details", "## Connected Ideas")
# A contract note (one with a `sources:` list) also needs Evidence.
REQUIRED_CONTRACT_SECTIONS = ("## Evidence",)
# Sections that hold quotes or other people's words; excluded from the length count.
QUOTE_SECTIONS = ("## Evidence", "## Disagreement")


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
        if ntype in PERMANENT_TYPES:
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


def _strip_sections(body: str, headers: tuple[str, ...]) -> str:
    out, skip = [], False
    for ln in body.splitlines():
        if ln.startswith("## "):
            skip = ln.strip() in headers
        if not skip:
            out.append(ln)
    return "\n".join(out)


def _is_contract(text: str) -> bool:
    """A note that follows NOTE_CONTRACT.md has a `sources:` key in its frontmatter."""
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    return end != -1 and re.search(r"(?m)^sources:", text[3:end]) is not None


AGENT_READ_DIRS = ("00 Maps", "01 Permanent Notes")
AGENT_READ_FILES = ("Home.md",)


def _agent_may_read(root: Path, f: Path) -> bool:
    """Under the agent (ZR_AGENT=1): only the zettelkasten read roots, never a symlink
    (a link could lead to another zettelkasten or outside the vault)."""
    try:
        rel = f.relative_to(root)
    except ValueError:
        return False
    cur = root
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            return False
    return rel.as_posix() in AGENT_READ_FILES or (len(rel.parts) > 1 and rel.parts[0] in AGENT_READ_DIRS)


def load_vault(root: Path) -> list[tuple[Path, dict, str, str, bool]]:
    parsed = []
    agent = os.environ.get("ZR_AGENT") == "1"
    for f in sorted(root.rglob("*.md")):
        if agent and not _agent_may_read(root, f):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        fm, body = parse_frontmatter(text)
        h1 = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), "")
        parsed.append((f, fm, body, h1, _is_contract(text)))
    return parsed


# Contract failures that verify_claims finds without any transcript.
# Contract failures that verify_claims finds without any transcript.
FORM_FAILURES = {
    "frontmatter", "missing-section", "forbidden-section", "missing-h1", "missing-lead",
    "missing-source-tag", "unknown-source", "evidence-format", "mixed-speakers", "unknown-section",
    "multi-paragraph-lead", "title-mismatch", "disagreement-format", "timestamp-format",
}


def _lit_dirs(staging: str | None) -> list[Path]:
    import verify_claims  # local import: the plain form check does not need it

    root = staging or os.environ.get("ZR_STAGING")
    return verify_claims.lit_dirs(Path(root)) if root else []


def contract_errors(path: Path, rel: Path, lit_dirs: list[Path], text: str | None = None,
                    lit=None) -> list[str]:
    """NOTE_CONTRACT.md checks for one note, as ERROR lines."""
    import verify_claims

    lit = lit or verify_claims.LitIndex(lit_dirs)
    rep = verify_claims.verify_note(path, lit, text=text)
    out = []
    for f in rep["failures"]:
        if not lit_dirs and f["type"] not in FORM_FAILURES:
            continue  # needs a transcript; no lit folder was given
        out.append(f"{rel}: contract {f['type']} at {f['where']}: {f['detail']}")
    return out


class VaultIndex:
    """Titles, aliases and note metadata of one vault, read once per run.

    finalize builds one index and checks every shadow note against it, so a
    large vault is not read again for each note.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.parsed = load_vault(self.root)
        self.titles: set[str] = set()
        self.perm: list[tuple[Path, str, tuple[str, str]]] = []
        for f, fm, body, h1, contract in self.parsed:
            self._add(f, fm, h1)

    def _add(self, f: Path, fm: dict, h1: str) -> None:
        self.titles.add(f.stem)
        if h1:
            self.titles.add(h1)
        av = fm.get("aliases", [])
        alias_list = av if isinstance(av, list) else [x.strip() for x in re.split(r"[,\[\]]", str(av))]
        for a in alias_list:
            a = a.strip().strip("[]\"'")
            if a:
                self.titles.add(a)
        if _as_str(fm.get("type", "")).strip().strip('"') in PERMANENT_TYPES:
            self.perm.append((f.relative_to(self.root) if f.is_relative_to(self.root) else Path(f.name),
                              f.stem, _speaker_scope(fm)))

    def add_titles(self, names: set[str]) -> None:
        self.titles |= names


def _speaker_scope(fm: dict) -> tuple[str, str]:
    """(speakers, skill) of a note, for the near-duplicate report. Old notes: ('', '')."""
    raw = "\n".join(f"{k}: {v}" for k, v in fm.items())
    speakers = sorted(set(re.findall(r"speaker:\s*\"?([^\"\n]+)", raw)))
    skill = re.search(r"skill:\s*\"?([^\"\n]+)", raw)
    return ("|".join(speakers), skill.group(1).strip().lower() if skill else "")


def check_one(f: Path, fm: dict, body: str, h1: str, contract: bool, root: Path, index: VaultIndex,
              lit_dirs: list[Path], text: str | None = None, lit=None, rel: Path | None = None,
              contract_check: bool = True, pipeline_notes: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Form errors and warnings of one note. `text` lets a shadow file be checked
    under its future vault path."""
    errors: list[str] = []
    warnings: list[str] = []
    rel = rel or (f.relative_to(root) if f.is_relative_to(root) else Path(f.name))
    ntype = _as_str(fm.get("type", "")).strip().strip('"')
    if not fm:
        return [f"{rel}: missing YAML frontmatter"], warnings
    if ntype not in ALLOWED_TYPES:
        errors.append(f"{rel}: type '{ntype}' not in {sorted(ALLOWED_TYPES)}")
    if "created" not in fm:
        warnings.append(f"{rel}: no 'created' date")
    if not h1:
        errors.append(f"{rel}: no H1 title")
    elif h1 != f.stem:
        warnings.append(f"{rel}: H1 '{h1}' != filename '{f.stem}'")
    if fm.get("superseded_by") or _as_str(fm.get("status", "")).strip().strip('"') == "superseded":
        # A stub (harness or repair): an H1 and a link to the new note(s); no sections, tags or date.
        targets = WIKILINK.findall(_as_str(fm.get("superseded_by", "")))
        if not targets:
            errors.append(f"{rel}: superseded stub without a [[target]] in superseded_by")
        for t in targets:
            t = t.split("|")[0].split("#")[0].strip()
            if t and t not in index.titles:
                errors.append(f"{rel}: superseded_by target [[{t}]] does not exist")
        return errors, [w for w in warnings if "no 'created' date" not in w]
    if PROVENANCE.search(body):
        errors.append(f"{rel}: contains raw provenance backlink (x.com/twitter.com/t.co)")
    if BARE_URL.search(body):
        warnings.append(f"{rel}: contains a bare URL in body")
    links = WIKILINK.findall(body)
    if ntype in PERMANENT_TYPES:
        tags = _tag_list(fm)
        if h1 and not declarative_title_ok(h1):
            warnings.append(f"{rel}: title may not be a declarative claim")
        if contract and contract_check:
            errors.extend(contract_errors(f, rel, lit_dirs, text=text, lit=lit))
        required = REQUIRED_PERMANENT_SECTIONS + (REQUIRED_CONTRACT_SECTIONS if contract else ())
        for sec in required:
            if sec not in body:
                errors.append(f"{rel}: missing required section '{sec}'")
        if not contract:
            if "## Grounded Example" in body:
                warnings.append(f"{rel}: '## Grounded Example' is not allowed (NOTE_CONTRACT.md); "
                                "use '## Example From The Source' with a source tag, or no example")
            ver = _as_str(fm.get("verification", "")).strip()
            if ver and ver not in VERIFICATION_VALUES:
                warnings.append(f"{rel}: verification '{ver}' not in {sorted(VERIFICATION_VALUES)}")
        if not links:
            errors.append(f"{rel}: ORPHAN - permanent note has no wikilinks")
        if not any(t not in BROAD_DOMAINS for t in tags):
            warnings.append(f"{rel}: no fine topic tag (only domain tags) - MOCs need fine tags")
        is_tension = "tension" in (_as_str(fm.get("tags", "")))
        if h1 and not is_tension and not contract and re.search(r"\b(?:and|vs\.?|versus)\b|,", h1, re.I):
            warnings.append(f"{rel}: title joins multiple ideas (atomicity smell - split?)")
        details = _section_bullets(body, "## Details")
        if details > 6:
            warnings.append(f"{rel}: ## Details has {details} bullets (fat note - split?)")
        own_text = re.sub(r"\[src:[^\]]*\]", " ", _strip_sections(body, QUOTE_SECTIONS))
        wc = len(re.findall(r"\w+", own_text))
        if wc > 220:
            warnings.append(f"{rel}: body is {wc} words without quotes (atomic notes stay short - split?)")
        h1_count, in_fence = 0, False
        for ln in body.splitlines():
            if ln.lstrip().startswith("```"):
                in_fence = not in_fence
            elif not in_fence and re.match(r"^# \S", ln):
                h1_count += 1
        if h1_count > 1:
            warnings.append(f"{rel}: more than one H1 (atomicity smell)")
        # Near-duplicate titles, only between notes of the same speaker and skill
        # (a note of another speaker or scope is a sibling, not a duplicate).
        mine = _speaker_scope(fm)
        a_t = _title_tokens(f.stem)
        for rj, tj, other in index.perm:
            if tj == f.stem or other != mine:
                continue
            b_t = _title_tokens(tj)
            if a_t and b_t and len(a_t & b_t) / len(a_t | b_t) >= 0.7:
                warnings.append(f"{rel}: near-duplicate title of '{tj}' (same speaker and scope)")
    pipeline = pipeline_notes if pipeline_notes is not None else set()
    for t in _note_links(body):
        if not _resolves(t, index):
            if str(rel) in pipeline:  # round 16/18: a note this pipeline published may not have a dead link
                errors.append(f"{rel}: dead link [[{t}]] in a note published by this pipeline")
            else:
                warnings.append(f"{rel}: unresolved wikilink [[{t}]]")
    return errors, warnings


def check(root: Path, only: list[Path] | None = None, staging: str | None = None,
          index: VaultIndex | None = None) -> tuple[list[str], list[str], int]:
    """Return (errors, warnings, number of checked notes).

    `only`: check just these files (absolute, or relative to root). Links
    resolve against the whole vault in both modes. Pass `index` to reuse one
    VaultIndex for many calls.
    """
    index = index or VaultIndex(root)
    pipeline_notes: set[str] = set()
    if staging:
        import provenance  # noqa: PLC0415

        for r in provenance.read_records(Path(staging)):
            if r.get("event") == "note-published":
                pipeline_notes.add(str(r.get("note")))
            elif r.get("event") == "note-respeak":  # V48: renamed notes stay pipeline notes
                pipeline_notes.add(str(r.get("note")))
    lit_dirs = _lit_dirs(staging)
    lit = None
    if lit_dirs:
        import verify_claims

        lit = verify_claims.LitIndex(lit_dirs)
    if only is not None:
        wanted = {(p if p.is_absolute() else root / p).resolve() for p in only}
        parsed = [row for row in index.parsed if row[0].resolve() in wanted]
        missing = wanted - {row[0].resolve() for row in parsed}
    else:
        parsed, missing = index.parsed, set()
    errors: list[str] = [f"{m}: file not found in vault" for m in sorted(map(str, missing))]
    warnings: list[str] = []
    seen_dup: set[frozenset[str]] = set()
    for f, fm, body, h1, contract in parsed:
        e, w = check_one(f, fm, body, h1, contract, root, index, lit_dirs, lit=lit, pipeline_notes=pipeline_notes)
        errors += e
        for x in w:
            if "near-duplicate title of" in x:
                pair = frozenset({f.stem, x.split("near-duplicate title of '", 1)[1].split("'", 1)[0]})
                if pair in seen_dup:
                    continue
                seen_dup.add(pair)
            warnings.append(x)
    if staging and only is None:
        # Round 11 (D): an old note never stays unchanged beside a published replacement.
        import run_integrity  # noqa: PLC0415

        for b in run_integrity.replacement_beside_old(Path(staging), root):
            errors.append(f"{b['note']}: old note unchanged beside its published replacement(s) "
                          f"{b['published_replacements']} (it must be a stub)")
        errors += stub_errors(Path(staging), root, [row[0] for row in parsed])
        errors += two_state_errors(Path(staging), root)
    return errors, warnings, len(parsed)


STATUS_KEYS = re.compile(r"(?m)^(verification|unsupported_reason|unsupported_marked|repair_note)\s*:.*\n?")


def _only_links_added(old: str, new: str) -> bool:
    """Z18: `new` equals `old` plus link lines in `## Connected Ideas` (a pipeline backlink)."""
    o, n = old.split("\n"), new.split("\n")
    if len(n) < len(o):
        return False
    i = j = 0
    section = ""
    while j < len(n):
        if n[j].startswith("## ") or n[j].startswith("# "):
            section = n[j].strip()
        if i < len(o) and o[i] == n[j]:
            i += 1
        else:
            x = n[j]
            # Z-A3 (round 25): an added line is a link bullet or a blank line INSIDE the
            # `## Connected Ideas` section; an added heading only after every old line
            if x.strip() == "## Connected Ideas":
                if i < len(o):
                    return False
            elif not x.strip():
                if section != "## Connected Ideas" and i < len(o):
                    return False
            elif section != "## Connected Ideas" or not re.match(r"^\s*-\s.*\[\[[^\]]+\]\]", x):
                return False
        j += 1
    return i == len(o) and "## Connected Ideas" in new


def two_state_errors(staging: Path, root: Path) -> list[str]:
    """Round 22 (X2, X3): the two-state invariant.
    - Every note in the repair plan (repair_state.json) is byte-identical to its planned sha,
      or a pipeline stub (`old_bullets`; `stub_errors` checks its text).
    - Every vault note that the pipeline wrote (pipeline_writes.json) is a stub, a note that
      this pipeline published, a map/Home file, or differs from its archived version only
      in status lines (verification, unsupported_reason, repair_note).
    - A pipeline stub without `old_bullets` (written before round 21) is an ERROR."""
    import hashlib  # noqa: PLC0415

    import provenance  # noqa: PLC0415
    import run_integrity  # noqa: PLC0415
    import stub_check  # noqa: PLC0415

    out: list[str] = []

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def is_pipeline_stub(text: str) -> tuple[bool, bool]:
        fm, _ = stub_check.split(text)
        stub = bool(re.search(r"(?m)^superseded_by:|^status:\s*superseded", fm))
        return stub, bool(re.search(r"(?m)^old_bullets:", fm))

    st = json.loads((staging / "repair_state.json").read_text()) if (staging / "repair_state.json").is_file() else {}
    plan = st.get("notes") or {}
    renamed_away = {f"{run_integrity.PERMANENT_DIR}/{r.get('old')}" for r in provenance.read_records(staging)
                    if r.get("event") == "note-respeak" and r.get("old")}
    for rel, e in plan.items():
        p = root / rel
        if not p.is_file():
            if rel not in renamed_away and e.get("status") != "operator-closed":
                out.append(f"{rel}: repair-plan note is missing from the vault (removed or renamed?)")  # Z5
            continue
        if not e.get("sha") or sha(p) == e["sha"]:
            continue
        cur = run_integrity.read_raw(p)  # Z-B3 (round 25): line ends as they are
        stub, full = is_pipeline_stub(cur)
        if not stub:
            # round 24: state 3, "marked unsupported": the planned text with ONLY the three mark keys
            arch = next((a for a in run_integrity._archive_candidates(staging, rel) if sha(a) == e["sha"]), None)
            if arch is not None and run_integrity.is_unsupported_mark(run_integrity.read_raw(arch), cur):
                continue
            out.append(f"{rel}: repair-plan note changed but is neither a stub nor a clean unsupported mark "
                       "(old text left the vault?)")
        elif not full:
            out.append(f"{rel}: pipeline stub without old_bullets (written before round 21): no old text carried")
    reg = json.loads((staging / "pipeline_writes.json").read_text()) if (staging / "pipeline_writes.json").is_file() else {}
    published = {str(r.get("note")) for r in provenance.read_records(staging) if r.get("event") == "note-published"}
    stubbed = {str(r.get("note")) for r in provenance.read_records(staging) if r.get("event") in ("note-repair",)}
    for rel in sorted(set(reg) | stubbed):
        p = root / rel
        if not p.is_file() or rel in plan:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        stub, full = is_pipeline_stub(text)
        if stub:
            if not full:
                out.append(f"{rel}: pipeline stub without old_bullets (written before round 21): no old text carried")
            continue
        if rel not in reg or rel in published or not rel.startswith(run_integrity.PERMANENT_DIR + "/"):
            continue
        arch = run_integrity._archive_candidates(staging, rel)
        old = arch[-1].read_text(encoding="utf-8", errors="replace") if arch else None  # the first archived version
        if old is not None and _only_links_added(old, text):
            continue  # Z18 (round 23): a backlink line added to `## Connected Ideas` is allowed
        if re.search(r"(?m)^unsupported_marked:", text.split("\n---", 1)[0]):
            # round 24: a mark must be exactly the three keys over the archived text
            if old is None or not run_integrity.is_unsupported_mark(run_integrity.read_raw(arch[-1]),
                                                                    run_integrity.read_raw(p)):
                out.append(f"{rel}: marked unsupported, but the note changed in more than the three mark keys")
            continue
        if old is None or STATUS_KEYS.sub("", old) != STATUS_KEYS.sub("", text):
            out.append(f"{rel}: the pipeline changed this vault note, and it is neither a stub nor a note it published")
    return out


def stub_errors(staging: Path, root: Path, files: list[Path]) -> list[str]:
    """Round 21: every stub that the pipeline wrote (`old_bullets` in its frontmatter)
    passes `stub_check` against its archived old text (`repair_archive`)."""
    import stub_check  # noqa: PLC0415

    plan = ((json.loads((staging / "repair_state.json").read_text()) if (staging / "repair_state.json").is_file()
             else {}).get("notes") or {})
    out = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        fm_raw, _ = stub_check.split(text)
        if not re.search(r"(?m)^old_bullets:", fm_raw):
            continue
        m = re.search(r'(?m)^repair_archive:\s*"?([^"\n]*)"?\s*$', fm_raw)
        arch = (staging / m.group(1)) if m and m.group(1) else None
        rel = f.relative_to(root) if f.is_relative_to(root) else Path(f.name)
        if arch is None or not arch.is_file():
            out.append(f"{rel}: pipeline stub without its archived old text ({m.group(1) if m else 'no repair_archive'})")
            continue
        # Z4 (round 23): the archive belongs to THIS note: its path ends with the note's own
        # relative path (or a `.vN` version of it), and its sha is the planned sha when known
        rel_s = str(rel)
        a_rel = str(arch)
        stem_ok = a_rel.endswith("/" + rel_s) or re.search(re.escape("/" + str(Path(rel_s).with_suffix(""))) + r"\.v\d+\.md$", a_rel)
        if not stem_ok:
            out.append(f"{rel}: stub check: repair_archive {m.group(1)} is not an archive of this note")
            continue
        planned = (plan.get(rel_s) or {}).get("sha")
        if planned:
            import hashlib  # noqa: PLC0415

            if hashlib.sha256(arch.read_bytes()).hexdigest() != planned:
                out.append(f"{rel}: stub check: the archive's sha is not the planned sha of this note")
                continue
        for p in stub_check.check(arch.read_text(encoding="utf-8", errors="replace"), text, root):
            out.append(f"{rel}: stub check: {p}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--vault", required=True, help="path to the zettelkasten folder")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--squeeze", action="store_true", help="print topic counts + MOC squeeze points and exit")
    ap.add_argument("--files", nargs="+", type=Path, help="check only these notes (relative to --vault or absolute)")
    ap.add_argument("--staging", help="staging dir; enables the quote and number checks (all registered lit folders)")
    args = ap.parse_args(argv)

    root = Path(args.vault)
    if not root.exists():
        print(f"ERROR: vault not found: {root}")
        return 1
    if args.squeeze:
        parsed = [(f, fm, body, h1) for f, fm, body, h1, _c in load_vault(root)]
        return squeeze_report(parsed, root)
    errors, warnings, n = check(root, args.files, args.staging)
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"\n{n} notes | {len(errors)} errors | {len(warnings)} warnings")
    fail = bool(errors) or (args.strict and bool(warnings))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
