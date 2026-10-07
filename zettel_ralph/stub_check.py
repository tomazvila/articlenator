"""Round 21: the stub safety check. Small on purpose, and independent of repair_state.json.

A stub that the pipeline writes for a replaced old note carries the old note's text: one
line per old Details bullet (verbatim, in the old order, with a status line under it) and
every other old body line under `## Old text`. This module parses an old note and checks a
stub against it:

- every non-empty line of the old body is in the stub, unchanged (stripped of indentation);
- the stub has one claim entry per old Details bullet, in the old order, and `old_bullets`
  equals that count;
- every `[[T]]` that the stub names as a target (frontmatter `superseded_by`, or a status
  line that starts with `→`) is a note in the vault that is not a stub.

A wrong ledger answer can then only give a wrong pointer next to visible text. Text can not
disappear without this check failing.
"""
from __future__ import annotations

import re
from pathlib import Path

CLAIMS_HEADING = "## Old claims and where they went"
OLD_TEXT_HEADING = "## Old text"
PERMANENT_DIR = "01 Permanent Notes"
_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_BULLET = re.compile(r"^[-*]\s+\S")


_FM_LINE = re.compile(r"^(?:[A-Za-z_][\w-]*\s*:.*|\s+\S.*|\s*-\s.*|\s*#.*|\s*)$")


def split(text: str) -> tuple[str, str]:
    """(frontmatter text, body). No frontmatter -> ("", text). Round 22 (X4): a first line
    `---` opens frontmatter only when every line up to the closing `---` is a YAML key line
    (or an indented/list continuation); a horizontal rule followed by text is body."""
    if text.startswith("---\n") or text.startswith("---\r\n"):
        end = text.find("\n---", 3)
        if end != -1:
            raw = text[3:end]
            lines = [ln for ln in raw.replace("\r", "").split("\n")[1:]]
            if lines and all(_FM_LINE.match(ln) for ln in lines) and any(":" in ln for ln in lines) \
                    and text[end + 4:end + 5] in ("", "\n", "\r"):
                body = text[end + 4:]
                return raw, body[1:] if body.startswith("\n") else body
    return "", text


def old_parts(old_text: str) -> tuple[str, list[str], list[str]]:
    """(H1 line, Details bullets (continuation lines joined with one space), the other
    non-empty body lines in order)."""
    _, body = split(old_text)
    h1, bullets, other = "", [], []
    in_details, fence = False, False
    for ln in body.splitlines():
        if ln.strip().startswith(("```", "~~~")):  # X17: a fence is plain text, never a heading or bullet
            fence = not fence
            if ln.strip():
                other.append(ln)
            continue
        if fence:
            if ln.strip():
                other.append(ln)
            continue
        if not h1 and ln.startswith("# "):
            h1 = ln
            continue
        if ln.startswith("## "):
            in_details = ln.strip() == "## Details"
            if in_details:
                continue
        if in_details:
            if _BULLET.match(ln):
                bullets.append(ln[1:].strip())
                continue
            if bullets and ln.startswith((" ", "\t")) and ln.strip():
                bullets[-1] += " " + ln.strip()
                continue
        if ln.strip():
            other.append(ln)
    return h1, bullets, other


def claim_entries(stub_text: str) -> list[tuple[str, str]]:
    """(bullet text, status) of each entry under the claims heading."""
    _, body = split(stub_text)
    out: list[tuple[str, str]] = []
    on = False
    for ln in body.splitlines():
        if ln.startswith("## "):
            on = ln.strip() == CLAIMS_HEADING
            continue
        if not on:
            continue
        if ln.startswith("- "):
            out.append((ln[2:], ""))
        elif ln.startswith("  - ") and out and not out[-1][1]:
            out[-1] = (out[-1][0], ln[4:])
    return out


def _fm_value(fm: str, key: str) -> str:
    m = re.search(rf"(?m)^{key}:\s*(.*)$", fm)
    return m.group(1).strip().strip('"') if m else ""


def _is_stub_file(p: Path) -> bool:
    fm, _ = split(p.read_text(encoding="utf-8", errors="replace"))
    return bool(_fm_value(fm, "superseded_by")) or _fm_value(fm, "status") == "superseded"


def check(old_text: str, stub_text: str, zk_dir: Path | None) -> list[str]:
    """The problems of `stub_text` as the stub of `old_text` ([] = the stub is safe)."""
    problems: list[str] = []
    fm, body = split(stub_text)
    _, bullets, other = old_parts(old_text)
    entries = claim_entries(stub_text)
    if [e[0] for e in entries] != bullets:
        problems.append(f"claim entries differ from the {len(bullets)} old Details bullets (order or text)")
    old_h1 = old_parts(old_text)[0]
    stub_h1 = next((ln for ln in body.splitlines() if ln.startswith("# ")), "")
    if old_h1 and stub_h1 != old_h1:  # X17 (round 22): the H1 is carried unchanged
        problems.append(f"the stub's H1 is not the old H1 ({old_h1[:80]})")
    if _fm_value(fm, "old_bullets") != str(len(bullets)):
        problems.append(f"old_bullets is '{_fm_value(fm, 'old_bullets')}', the old note has {len(bullets)}")
    have = {ln.strip() for ln in body.splitlines()} | {ln.strip()[1:].strip() for ln in body.splitlines()
                                                    if ln.strip().startswith(">")}
    for ln in other:
        if ln.strip() not in have:
            problems.append(f"old line not carried: {ln.strip()[:120]}")
    want_rest = [ln for ln in _rest_lines(old_text, old_parts(old_text)[0])]
    got_rest = []
    on = False
    for ln in body.splitlines():
        if ln.startswith("## "):
            on = ln.strip() == OLD_TEXT_HEADING
            continue
        if on and ln.startswith(">"):
            got_rest.append(ln[2:] if ln.startswith("> ") else "")
    if [x.rstrip() for x in got_rest] != [x.rstrip() for x in want_rest]:  # X19: ordered, complete
        problems.append("the `## Old text` block is not the old text in its order")
    for e, st in entries:
        if not st:
            problems.append(f"no status line for: {e[:80]}")
    if zk_dir is not None:
        targets = [m.group(1).strip() for m in _LINK.finditer(_fm_value(fm, "superseded_by"))]
        targets += [m.group(1).strip() for _e, st in entries if st.startswith("→") for m in _LINK.finditer(st)]
        for t in dict.fromkeys(targets):
            p = zk_dir / PERMANENT_DIR / f"{t}.md"
            if not p.is_file():
                problems.append(f"target [[{t}]] is not in the vault")
            elif _is_stub_file(p):
                problems.append(f"target [[{t}]] is a stub")
    return problems


def _rest_lines(old_text: str, h1: str) -> list[str]:
    """The old body lines that are not the H1 and not a Details bullet, in order (the
    `## Old text` block, without its `> ` prefix)."""
    _, body = split(old_text)
    rest: list[str] = []
    in_details, seen_h1, fence, n_b = False, False, False, 0
    for ln in body.splitlines():
        if ln.strip().startswith(("```", "~~~")):
            fence = not fence
            rest.append(ln)
            continue
        if fence:
            rest.append(ln)
            continue
        if not seen_h1 and ln.startswith("# ") and ln == h1:
            seen_h1 = True
            continue
        if ln.startswith("## "):
            in_details = ln.strip() == "## Details"
            if in_details:
                continue
        if in_details and _BULLET.match(ln):
            n_b += 1
            continue
        if in_details and n_b and ln.startswith((" ", "\t")) and ln.strip():
            continue  # a continuation line of a bullet (old_parts joins it)
        rest.append(ln)
    while rest and not rest[0].strip():
        rest.pop(0)
    while rest and not rest[-1].strip():
        rest.pop()
    return rest


def render(old_text: str, title: str, statuses: list[str], targets: list[str], pending: list[str],
           archive: str) -> str:
    """The stub text. `statuses[i]` is the status of old bullet i (same order as
    `old_parts`); a missing status is `open: not yet processed`."""
    import json
    h1, bullets, _other = old_parts(old_text)
    _, body = split(old_text)
    links = "; ".join(f"[[{t}]]" for t in targets)
    fm = ["---", "type: permanent note", "status: superseded"]
    if links:
        fm.append(f'superseded_by: "{links}"')
    if pending:
        fm.append("superseded_pending:")
        fm += [f"  - {json.dumps(p[:200], ensure_ascii=False)}" for p in pending]
    fm += ["verification: superseded", f"old_bullets: {len(bullets)}", f"repair_archive: {json.dumps(archive, ensure_ascii=False)}", "---"]
    named = " and ".join(f"[[{t}]]" for t in targets) or "no published note yet"
    out = fm + ["", h1 or f"# {title}", "",
                f"This note was replaced by {named}. The text below is the OLD text of this note, unchanged and "
                f"not source-checked. Each old claim shows where it went. Archive copy: `{archive}`.", "",
                CLAIMS_HEADING, ""]
    for i, b in enumerate(bullets):
        st = statuses[i] if i < len(statuses) and statuses[i] else "open: not yet processed"
        out += [f"- {b}", f"  - {st}"]
    rest = _rest_lines(old_text, h1)
    if rest:
        out += ["", OLD_TEXT_HEADING, ""] + [f"> {ln}" if ln.strip() else ">" for ln in rest]
    return "\n".join(out) + "\n"
