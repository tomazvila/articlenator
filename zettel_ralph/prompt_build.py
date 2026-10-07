#!/usr/bin/env python3
"""Build a model prompt from a prompt file and NOTE_CONTRACT.md.

`NOTE_CONTRACT.md` is the one normative text. The single-call prompts
(`prompts/synth_single.md`, `prompts/fix_single.md`, `prompts/repair_single.md`) do
not copy its rules; they hold an include line that this module expands:

    <!-- include NOTE_CONTRACT.md#speaker,title,quote-match -->

Each name is a STABLE ANCHOR. In the contract, the line directly under a heading
(`## N. ...` or `### N.N ...`) is `<!-- anchor: <name> -->`. The include takes that
heading and everything up to the next heading of the same or a higher level, in the
listed order. Renumbering the headings changes nothing. An unknown anchor, a
duplicate anchor, or an anchor that is not directly under a heading is an error.

Mode blocks, each marker on its own line:

    <!-- agent-only -->  ... <!-- /agent-only -->    kept only in mode "agent"
    <!-- single-only --> ... <!-- /single-only -->   kept only in mode "single"

A small state machine reads them: an unbalanced or nested marker is an error with its
line number. All other HTML comments are removed from the built text.

Use from the harness:

    import prompt_build
    system_prompt = prompt_build.load("prompts/synth_single.md")   # mode "single"
    problems = prompt_build.check_manifest()                        # [] = up to date

Command line:

    python3 prompt_build.py prompts/synth_single.md [agent|single]   # print a built prompt
    python3 prompt_build.py --write-manifest                          # write prompts/build_manifest.json
    python3 prompt_build.py --check-manifest                          # exit 1 and list problems if stale
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INCLUDE = re.compile(r"^<!-- include ([\w./-]+)#([\w,\s-]+) -->[ \t]*$", re.M)
ANCHOR = re.compile(r"^<!-- anchor: ([a-z0-9-]+) -->[ \t]*$")
HEADING = re.compile(r"^(#{2,6}) (.+?)\s*$")
MARKER = re.compile(r"^<!-- (/?)(agent|single)-only -->[ \t]*$")
MODES = ("agent", "single")
MANIFEST = HERE / "prompts" / "build_manifest.json"
# The prompts that the harness builds, and the modes it builds them in.
BUILT = {
    "prompts/inventory_single.md": ("single",),
    "prompts/synth_single.md": ("single",),
    "prompts/synth_onecall.md": ("single",),
    "prompts/repair_drop_check.md": ("single",),
    "prompts/fix_single.md": ("single",),
    "prompts/repair_single.md": ("single",),
    "prompts/source_check.md": ("single",),
    "NOTE_CONTRACT.md": ("agent",),
}


class PromptBuildError(ValueError):
    """A broken include, anchor or mode block."""


def anchors(text: str, name: str = "NOTE_CONTRACT.md") -> dict[str, tuple[str, str]]:
    """Return {anchor: (heading title, section text)} of a markdown text."""
    lines = text.splitlines(keepends=True)
    out: dict[str, tuple[str, str]] = {}
    for i, ln in enumerate(lines):
        m = ANCHOR.match(ln.rstrip("\n"))
        if not m:
            continue
        a = m.group(1)
        h = HEADING.match(lines[i - 1].rstrip("\n")) if i > 0 else None
        if not h:
            raise PromptBuildError(f"{name}:{i + 1}: anchor '{a}' is not directly under a heading")
        if a in out:
            raise PromptBuildError(f"{name}:{i + 1}: duplicate anchor '{a}'")
        level = len(h.group(1))
        end = len(lines)
        in_fence = False
        for j in range(i + 1, len(lines)):
            s = lines[j].rstrip("\n")
            if s.lstrip().startswith("```"):
                in_fence = not in_fence
            hj = None if in_fence else HEADING.match(s)
            if hj and len(hj.group(1)) <= level:
                end = j
                break
        out[a] = (h.group(2), "".join(lines[i - 1 : end]).rstrip() + "\n")
    return out


def _include(m: re.Match, base: Path, used: list[tuple[str, str]]) -> str:
    path = (base / m.group(1)).resolve()
    if not path.is_file():
        raise PromptBuildError(f"include: file not found: {m.group(1)}")
    found = anchors(path.read_text(encoding="utf-8"), m.group(1))
    names = [x for x in re.split(r"[,\s]+", m.group(2).strip()) if x]
    unknown = [n for n in names if n not in found]
    if unknown:
        raise PromptBuildError(f"include: {m.group(1)} has no anchor(s) {unknown}")
    used.extend((n, found[n][0]) for n in names)
    return "\n".join(found[n][1] for n in names)


def apply_mode(text: str, mode: str, name: str = "<text>") -> str:
    """Keep the blocks of `mode`, drop the blocks of the other mode, drop comments.

    Markers are read line by line; a nested, unopened or unclosed block is an error."""
    if mode not in MODES:
        raise PromptBuildError(f"unknown mode {mode!r}")
    out: list[str] = []
    open_mode: str | None = None
    open_line = 0
    for n, ln in enumerate(text.splitlines(keepends=True), 1):
        m = MARKER.match(ln.rstrip("\n"))
        if not m:
            if re.search(r"<!-- /?(agent|single)-only -->", ln):
                raise PromptBuildError(f"{name}:{n}: a mode marker must stand alone on its line")
            if open_mode is None or open_mode == mode:
                out.append(ln)
            continue
        closing, which = m.group(1) == "/", m.group(2)
        if not closing:
            if open_mode is not None:
                raise PromptBuildError(
                    f"{name}:{n}: nested '{which}-only' inside '{open_mode}-only' "
                    f"opened at line {open_line}"
                )
            open_mode, open_line = which, n
        else:
            if open_mode != which:
                raise PromptBuildError(
                    f"{name}:{n}: '/{which}-only' without an open '{which}-only'"
                )
            open_mode = None
    if open_mode is not None:
        raise PromptBuildError(f"{name}:{open_line}: '{open_mode}-only' is never closed")
    text = re.sub(r"<!--.*?-->\n?", "", "".join(out), flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", text)


def build(name: str, mode: str = "single") -> tuple[str, list[tuple[str, str]]]:
    """Return (built text, [(anchor, heading)] of the includes) of a prompt file."""
    path = (HERE / name).resolve()
    text = path.read_text(encoding="utf-8")
    used: list[tuple[str, str]] = []
    text = INCLUDE.sub(lambda m: _include(m, HERE, used), text)
    if "<!-- include" in text:
        raise PromptBuildError(f"{name}: malformed include line")
    return apply_mode(text, mode, name), used


def expand(text: str, mode: str = "single", base: Path = HERE) -> str:
    """Expand include lines in `text`, then apply the mode blocks."""
    used: list[tuple[str, str]] = []
    return apply_mode(INCLUDE.sub(lambda m: _include(m, base, used), text), mode)


def load(name: str, mode: str = "single") -> str:
    """Load a prompt file (relative to zettel_ralph/) and build it."""
    return build(name, mode)[0]


def manifest() -> dict:
    """Manifest of every built prompt: per prompt and mode, sha256, words, includes."""
    prompts: dict[str, dict] = {}
    for name, modes in BUILT.items():
        for mode in modes:
            text, used = build(name, mode)
            prompts.setdefault(name, {})[mode] = {
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "words": len(text.split()),
                "includes": [{"anchor": a, "heading": h} for a, h in used],
            }
    return {"version": 1, "prompts": prompts}


def write_manifest(path: Path = MANIFEST) -> dict:
    data = manifest()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def check_manifest(path: Path = MANIFEST) -> list[str]:
    """Rebuild every prompt and compare with the manifest. [] = up to date."""
    if not path.is_file():
        return [f"{path.name} is missing; run: python3 prompt_build.py --write-manifest"]
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        new = manifest()
    except (ValueError, PromptBuildError) as e:
        return [str(e)]
    problems = []
    for name in sorted(set(old.get("prompts", {})) | set(new["prompts"])):
        for mode in sorted(
            set(old.get("prompts", {}).get(name, {})) | set(new["prompts"].get(name, {}))
        ):
            a = old.get("prompts", {}).get(name, {}).get(mode)
            b = new["prompts"].get(name, {}).get(mode)
            if a != b:
                problems.append(
                    f"{name} [{mode}]: built prompt differs from the manifest "
                    f"(sha {str((a or {}).get('sha256'))[:12]} -> {str((b or {}).get('sha256'))[:12]})"
                )
    if problems:
        problems.append("review the change, then run: python3 prompt_build.py --write-manifest")
    return problems


def main(argv: list[str]) -> int:
    args = argv[1:]
    try:
        if args == ["--write-manifest"]:
            data = write_manifest()
            print(f"wrote {MANIFEST.name}: {len(data['prompts'])} prompts")
            return 0
        if args == ["--check-manifest"]:
            problems = check_manifest()
            for p in problems:
                print(p, file=sys.stderr)
            return 1 if problems else 0
        if len(args) in (1, 2) and not args[0].startswith("-"):
            sys.stdout.write(load(args[0], args[1] if len(args) == 2 else "single"))
            return 0
    except PromptBuildError as e:
        print(f"prompt_build: {e}", file=sys.stderr)
        return 1
    print(
        "usage: prompt_build.py <prompt file> [agent|single] | --write-manifest | --check-manifest",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
