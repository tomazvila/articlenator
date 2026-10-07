#!/usr/bin/env python3
"""Tiny DeepSeek-backed tool runner for the Ralph loops.

The Ralph harness needs an agent that can read a bounded set of files, edit notes, and run
the existing Python helper scripts. This script provides exactly those tools over the
OpenAI-compatible DeepSeek chat completions API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import secret_scan  # noqa: E402

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
HELPERS = {
    "index_query.py",
    "index_add.py",
    "index_rebuild.py",
    "queue_mark.py",
    "validate.py",
    "verify_claims.py",
    "note_link.py",
}
LIMIT_EXIT_CODE = 3
ENV_ALLOWLIST = {
    "ZR_SHADOW_DIR",
    "ZR_UNIT_IDS",
    "AGENTS_FILE",
    "HERE",
    "NWORKERS",
    "QUEUE",
    "STAGING",
    "VAULT",
    "WORKER",
    "ZK_DIR",
    "ZK_FOLDER",
    "ZR_STAGING",
}


class AgentError(RuntimeError):
    """Raised for model or tool errors that should stop the current iteration."""


class AgentLimit(AgentError):
    """Raised when the run stops at the token limit or the step limit."""


class AgentTimeout(AgentError):
    """Raised when the outer `timeout` sends SIGTERM."""


def _on_sigterm(signum, frame):  # noqa: ARG001 - signal handler signature
    raise AgentTimeout("terminated by SIGTERM (outer timeout)")


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def load_api_key() -> str:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"].strip()

    key_file = Path(os.environ.get("DEEPSEEK_API_KEY_FILE", REPO / "deepseek.txt"))
    if not key_file.is_file():
        raise AgentError(
            "DeepSeek API key not found. Set DEEPSEEK_API_KEY or "
            f"DEEPSEEK_API_KEY_FILE (looked for {key_file})."
        )
    key = key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise AgentError(f"DeepSeek API key file is empty: {key_file}")
    return key


def compact_output(text: str, limit: int | None = None) -> str:
    max_chars = limit or int(os.environ.get("DEEPSEEK_TOOL_OUTPUT_CHARS", "30000"))
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...[tool output truncated]...\n" + text[-half:]


# --------------------------------------------------------------------------- #
# Paths: the agent reads the harness, staging, lit folders and the vault; it
# writes ONLY into its shadow folder (ZR_SHADOW_DIR) and a few staging outputs.
# A vault path in a write is mapped to the same relative path in the shadow
# folder. run_integrity.py finalize verifies and publishes the shadow files.
# --------------------------------------------------------------------------- #

# Staging outputs the agent may write directly (file name, or folder + suffix).
# No verdict location is writable here: a review verdict goes only to the one file that
# the harness names for a read-only review agent (ZR_VERDICT_FILE).
AGENT_STAGING_FILES = {"state.md", "decisions.md"}
AGENT_STAGING_DIRS = {"repair": ".json"}
# Folders of the zettelkasten that an agent may read (not the rest of the personal vault).
ZK_READ_DIRS = ("00 Maps", "01 Permanent Notes")
ZK_READ_FILES = ("Home.md",)
# Harness files that the prompts tell the agent to read.
HARNESS_READ_GLOBS = ("AGENTS*.md", "NOTE_CONTRACT.md", "NOTE_TEMPLATE.md", "prompts/*.md", "state/*.md")
# Helpers that a read-only review agent may run.
READONLY_HELPERS = {"verify_claims.py", "validate.py", "index_query.py"}


def staging_dir() -> Path:
    return Path(os.environ.get("ZR_STAGING") or HERE / "staging").expanduser().resolve()


def shadow_dir() -> Path | None:
    value = os.environ.get("ZR_SHADOW_DIR")
    return Path(value).expanduser().resolve() if value else None


def zk_dir() -> Path | None:
    value = os.environ.get("ZK_DIR")
    return Path(value).expanduser().resolve() if value else None


def _lit_roots() -> list[Path]:
    try:
        import verify_claims  # noqa: PLC0415 - optional at import time

        return verify_claims.lit_dirs(staging_dir())
    except Exception:  # noqa: BLE001
        return []


def verdict_file() -> Path | None:
    value = os.environ.get("ZR_VERDICT_FILE")
    return Path(value).expanduser().resolve() if value else None


def allowed_roots() -> list[Path]:
    """Folders the agent may READ in full: its shadow folder and the registered lit
    folders. read_allowed() adds the zettelkasten folders and a few single files."""
    roots: list[Path] = []
    sh = shadow_dir()
    if sh:
        roots.append(sh)
    roots += _lit_roots()
    return [p.expanduser().resolve() for p in roots]


def read_allowed(resolved: Path) -> bool:
    """The read rule. `resolved` has symlinks and `..` resolved already, so a link or
    an escape that leads outside these places is refused."""
    if any(_under(resolved, r) for r in allowed_roots()):
        return True
    zk = zk_dir()
    if zk is not None and _under(resolved, zk):
        if resolved == zk:
            return True  # the folder itself, for list_dir; entries are filtered
        rel = resolved.relative_to(zk).as_posix()
        return rel in ZK_READ_FILES or rel.split("/", 1)[0] in ZK_READ_DIRS
    stg = staging_dir()
    if _under(resolved, stg) and resolved != stg:
        parts = resolved.relative_to(stg).as_posix().split("/")
        if len(parts) == 1 and parts[0].lower() in AGENT_STAGING_FILES:
            return True
        if parts[0] in AGENT_STAGING_DIRS:
            return True
    vf = verdict_file()
    if vf is not None and resolved == vf:
        return True
    for pattern in HARNESS_READ_GLOBS:
        if any(resolved == p.resolve() for p in HERE.glob(pattern)):
            return True
    return False


def _under(path: Path, root: Path) -> bool:
    """Case-sensitive prefix test (a path mapping must use the exact folder name)."""
    p, r = str(path), str(root)
    return p == r or p.startswith(r.rstrip("/") + "/")


def _under_ci(path: Path, root: Path) -> bool:
    """Case-insensitive prefix test, used only to REFUSE writes."""
    p, r = str(path).lower(), str(root).lower()
    return p == r or p.startswith(r.rstrip("/") + "/")


# The only variables the harness replaces in agent input (paths and helper arguments).
# Nothing else is expanded: an agent path such as "$DEEPSEEK_API_KEY" stays literal.
PATH_VARS = ("ZK_DIR", "ZR_STAGING")


def expand_allowed(raw: str) -> str:
    out = raw
    for name in PATH_VARS:
        value = os.environ.get(name, "")
        if value:
            out = out.replace("${" + name + "}", value).replace("$" + name, value)
    return out


def resolve_allowed_path(raw_path: str) -> Path:
    if not raw_path:
        raise AgentError("Path must not be empty.")
    if "\0" in raw_path:
        raise AgentError("Path holds a NUL character.")
    candidate = Path(expand_allowed(raw_path))
    if not candidate.is_absolute():
        candidate = HERE / candidate
    resolved = candidate.resolve()
    if read_allowed(resolved):
        return resolved
    raise AgentError(
        f"Path is outside what an agent may read: {resolved}. Readable: the zettelkasten folders "
        f"{', '.join(ZK_READ_DIRS)} and Home.md, your shadow folder, the transcript (lit) folders, "
        "STATE.md, DECISIONS.md and the prompt files.")


def _vault_rel(path: Path) -> str | None:
    """Path relative to ZK_DIR, also for a path inside the shadow folder."""
    for root in (shadow_dir(), zk_dir()):
        if root is not None and _under(path, root):
            return path.relative_to(root).as_posix() if path != root else ""
    for root in (shadow_dir(), zk_dir()):
        if root is not None and _under_ci(path, root):
            raise AgentError(f"{path}: the letter case differs from the vault folder {root}; use the exact name.")
    return None


def resolve_writable_path(raw_path: str) -> Path:
    """Map a write to its real target. Vault paths go to the shadow folder.

    Allowed: the shadow folder (or a ZK_DIR path, mapped into it), STATE.md and
    DECISIONS.md in staging, and JSON files in staging/review-verdicts/ and
    staging/repair/. Everything else is refused.
    """
    if os.environ.get("ZR_AGENT_READONLY") == "1":
        # A review agent writes exactly one file: its own verdict.
        cand = Path(expand_allowed(raw_path))
        cand = (cand if cand.is_absolute() else HERE / cand).resolve()
        vf = verdict_file()
        if vf is not None and cand == vf:
            return vf
        raise AgentError("Write refused: this is a source-check review. It writes only its verdict "
                         f"file {vf} and changes nothing else.")
    resolved = resolve_allowed_path(raw_path)
    sh = shadow_dir()
    rel = _vault_rel(resolved)
    if rel is not None:
        if sh is None:
            raise AgentError("Write refused: no shadow folder (ZR_SHADOW_DIR) for this run.")
        if not rel:
            raise AgentError("Write refused: give a file path, not the vault folder.")
        return (sh / rel).resolve()
    stg = staging_dir()
    if _under_ci(resolved, stg) and not _under(resolved, stg):
        raise AgentError(f"Write refused: {resolved}: letter case differs from the staging folder.")
    if _under(resolved, stg):
        rel_s = resolved.relative_to(stg).as_posix() if resolved != stg else ""
        parts = rel_s.split("/")
        if len(parts) == 1 and parts[0].lower() in AGENT_STAGING_FILES:
            return resolved
        if len(parts) == 2 and parts[0] in AGENT_STAGING_DIRS and parts[1].lower().endswith(AGENT_STAGING_DIRS[parts[0]]):
            return resolved
        raise AgentError(
            f"Write refused: {resolved}. In staging the agent writes only STATE.md, DECISIONS.md "
            "and repair/*.json; queue, provenance, index, verdicts, logs, lit and runs belong to the harness."
        )
    raise AgentError(f"Write refused: {resolved} is outside the vault folder and the staging outputs.")


def _run_dir() -> Path | None:
    value = os.environ.get("ZR_RUN_DIR")
    return Path(value).expanduser().resolve() if value else None


def _journal(name: str, record: dict[str, Any]) -> None:
    run_dir = _run_dir()
    if run_dir is None:
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"t": time.time(), **record}, ensure_ascii=False) + "\n"
    fd = os.open(run_dir / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _meta_path(name: str) -> Path | None:
    rd = _run_dir()
    return rd / name if rd else None


def _load_meta(name: str) -> dict[str, Any]:
    p = _meta_path(name)
    if p is None or not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _save_meta(name: str, data: dict[str, Any]) -> None:
    p = _meta_path(name)
    if p is None:
        return
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    if name == "marked.json":
        ri = sys.modules.get("run_integrity")
        if ri is not None:
            ri._invalidate_rename_index_for_marked(p)


def _record_base(rel: str) -> None:
    """Remember the vault version that this unit starts from (for conflict detection)."""
    bases = _load_meta("bases.json")
    if rel in bases:
        return
    zk = zk_dir()
    bases[rel] = _sha256(zk / rel) if zk else None
    _save_meta("bases.json", bases)


def _removed(rel: str) -> bool:
    return rel in _load_meta("removals.json")


def _overlay_source(target: Path) -> Path:
    """For reads: the shadow version of a vault path if there is one."""
    rel = _vault_rel(target)
    sh, zk = shadow_dir(), zk_dir()
    if rel and sh is not None:
        if _removed(rel):
            raise AgentError(f"{rel} is removed in this unit (see removals).")
        if (sh / rel).is_file():
            return sh / rel
        if zk is not None:
            return zk / rel
    return target


def read_file(path: str, start_line: int = 1, line_count: int = 400) -> dict[str, Any]:
    target = _overlay_source(resolve_allowed_path(path))
    if not target.is_file():
        raise AgentError(f"File does not exist: {target}")
    if start_line < 1:
        raise AgentError("start_line must be >= 1")
    if line_count < 1:
        raise AgentError("line_count must be >= 1")
    max_lines = int(os.environ.get("DEEPSEEK_READ_MAX_LINES", "20000"))
    line_count = min(line_count, max_lines)
    lines = target.read_text(encoding="utf-8").splitlines()
    start_index = start_line - 1
    selected = lines[start_index : start_index + line_count]
    body = "\n".join(f"{start_index + i + 1}: {line}" for i, line in enumerate(selected))
    return {
        "path": str(target),
        "start_line": start_line,
        "line_count": len(selected),
        "total_lines": len(lines),
        "content": compact_output(body, int(os.environ.get("DEEPSEEK_READ_MAX_CHARS", "220000"))),
    }


def _merged_children(target: Path) -> list[tuple[str, bool]]:
    """Directory entries of the vault and the shadow folder together."""
    rel = _vault_rel(target)
    dirs = [target]
    sh, zk = shadow_dir(), zk_dir()
    if rel is not None and sh is not None and zk is not None:
        dirs = [zk / rel, sh / rel]
    removals = _load_meta("removals.json")
    seen: dict[str, bool] = {}
    for d in dirs:
        if d.is_dir():
            for p in d.iterdir():
                r = f"{rel}/{p.name}".lstrip("/") if rel is not None else None
                if r is not None and r in removals:
                    continue
                if zk is not None and rel == "" and p.name not in ZK_READ_DIRS + ZK_READ_FILES:
                    continue
                seen[p.name] = seen.get(p.name, False) or p.is_dir()
    return sorted(seen.items(), key=lambda kv: (not kv[1], kv[0].lower()))


def list_dir(path: str = ".", max_entries: int = 200) -> dict[str, Any]:
    target = resolve_allowed_path(path)
    entries = _merged_children(target)
    if not entries and not target.is_dir():
        raise AgentError(f"Directory does not exist: {target}")
    max_entries = max(1, min(max_entries, 1000))
    return {
        "path": str(target),
        "entries": [{"name": n, "type": "dir" if d else "file"} for n, d in entries[:max_entries]],
        "truncated": len(entries) > max_entries,
    }


def find_files(root: str, pattern: str, max_entries: int = 200) -> dict[str, Any]:
    target = resolve_allowed_path(root)
    if ".." in Path(pattern).parts:
        raise AgentError("Pattern must not traverse upward.")
    rel = _vault_rel(target)
    sh, zk = shadow_dir(), zk_dir()
    bases = [target]
    if rel is not None and sh is not None and zk is not None:
        bases = [zk / rel, sh / rel]
    removals = _load_meta("removals.json")
    found: set[str] = set()
    for b in bases:
        if b.is_dir():
            for p in b.glob(pattern):
                if p.is_file():
                    r = p.relative_to(b).as_posix()
                    full = f"{rel}/{r}".lstrip("/") if rel is not None else None
                    if full is None or full not in removals:
                        if read_allowed((target / r).resolve()) or (b == bases[-1] and len(bases) > 1):
                            found.add(str(target / r))
    if not found and not any(b.is_dir() for b in bases):
        raise AgentError(f"Directory does not exist: {target}")
    matches = sorted(found)
    max_entries = max(1, min(max_entries, 1000))
    return {"root": str(target), "pattern": pattern, "matches": matches[:max_entries],
            "truncated": len(matches) > max_entries}


MAX_NAME_BYTES = 200


def _check_content(content: str) -> None:
    found = secret_scan.find_secrets(content)
    if found:
        raise AgentError(f"Write refused: the content holds a secret ({', '.join(found)}).")


def _write_target(path: str) -> tuple[Path, str | None]:
    target = resolve_writable_path(path)
    if len(target.name.encode("utf-8")) > MAX_NAME_BYTES:
        raise AgentError(f"Write refused: the file name is longer than {MAX_NAME_BYTES} bytes.")
    rel = _vault_rel(target)
    if rel is not None:
        _record_base(rel)
        removals = _load_meta("removals.json")
        if rel in removals:  # writing the path again cancels its removal
            removals.pop(rel)
            _save_meta("removals.json", removals)
    return target, rel


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


def _current_text(target: Path) -> str:
    src = _overlay_source(target)
    if not src.is_file():
        raise AgentError(f"File does not exist: {target}")
    return src.read_text(encoding="utf-8")


def write_file(path: str, content: str) -> dict[str, Any]:
    _check_content(content)
    target, rel = _write_target(path)
    _atomic_write(target, content)
    return {"path": str(target), "vault_path": rel, "bytes": len(content.encode("utf-8")),
            "note": "written to the shadow folder; the harness publishes it after verification" if rel else ""}


def append_file(path: str, content: str) -> dict[str, Any]:
    resolved = resolve_allowed_path(path)
    old = _current_text(resolved) if (_overlay_source(resolved).is_file()) else ""
    _check_content(content)
    target, rel = _write_target(path)
    _atomic_write(target, old + content)
    return {"path": str(target), "vault_path": rel, "bytes_appended": len(content.encode("utf-8"))}


def replace_in_file(path: str, old: str, new: str, expected_replacements: int = 1) -> dict[str, Any]:
    if not old:
        raise AgentError("old text must not be empty.")
    resolved = resolve_allowed_path(path)
    text = _current_text(resolved)
    count = text.count(old)
    if count != expected_replacements:
        raise AgentError(f"Expected {expected_replacements} replacement(s), found {count} in {resolved}.")
    _check_content(new)
    target, rel = _write_target(path)
    _atomic_write(target, text.replace(old, new, expected_replacements))
    return {"path": str(target), "vault_path": rel}


def _stub_text(old_title: str, new_title: str) -> str:
    """NOTE_CONTRACT.md section 13 stub: keeps wikilinks to the old title working."""
    return ("---\ntype: permanent note\nstatus: superseded\n"
            f'superseded_by: "[[{new_title}]]"\nverification: unverified\n---\n\n'
            f"# {old_title}\n\nThis note was replaced by [[{new_title}]].\n")


def _retarget(old_rel: str, new_rel: str) -> None:
    """A rename chain A->B->C: stubs and removal requests that point at B now point at C."""
    sh = shadow_dir()
    old_t, new_t = Path(old_rel).stem, Path(new_rel).stem
    if sh is not None:
        for p in sh.rglob("*.md"):
            text = p.read_text(encoding="utf-8")
            # One target or several ("[[A]]; [[B]]", a split): retarget the moved one.
            if any(ln.startswith("superseded_by:") and f"[[{old_t}]]" in ln for ln in text.splitlines()):
                p.write_text(text.replace(f"[[{old_t}]]", f"[[{new_t}]]"), encoding="utf-8")
    removals = _load_meta("removals.json")
    changed = False
    for req in removals.values():
        if req.get("replaced_by") == old_rel:
            req["replaced_by"] = new_rel
            changed = True
    if changed:
        _save_meta("removals.json", removals)


def remove_note(path: str, replaced_by: str) -> dict[str, Any]:
    """Ask the harness to remove a vault note. It applies only if `replaced_by`
    is published in the same unit; the old file is archived and replaced by a stub."""
    src = resolve_allowed_path(path)
    rel = _vault_rel(src)
    if rel is None:
        raise AgentError("remove_note works only on notes in the vault folder.")
    dst_rel = _vault_rel(resolve_allowed_path(replaced_by))
    if not dst_rel:
        raise AgentError("replaced_by must be a note path in the vault folder.")
    if dst_rel == rel:
        raise AgentError("replaced_by must be another note.")
    sh = shadow_dir()
    zk = zk_dir()
    in_vault = zk is not None and (zk / rel).is_file()
    if sh is not None and (sh / rel).is_file():
        (sh / rel).unlink()
    if not in_vault:
        _retarget(rel, dst_rel)  # a note written only in this unit: just drop it
        return {"removed_from_shadow": rel, "replaced_by": dst_rel}
    _record_base(rel)
    if os.environ.get("ZR_UNIT_KIND") == "repair":
        # Repair mode: the old file always becomes a stub that points at the new note.
        _atomic_write(sh / rel, _stub_text(Path(rel).stem, Path(dst_rel).stem))  # type: ignore[operator]
        return {"stub_written": rel, "replaced_by": dst_rel}
    removals = _load_meta("removals.json")
    removals[rel] = {"replaced_by": dst_rel}
    _save_meta("removals.json", removals)
    return {"removal_requested": rel, "replaced_by": dst_rel}


def delete_draft(path: str) -> dict[str, Any]:
    """Delete a note that exists ONLY in your shadow folder (a draft of this unit).
    A vault note is never deleted here: use move_file or remove_note."""
    res = resolve_allowed_path(path)
    rel = _vault_rel(res)
    sh, zk = shadow_dir(), zk_dir()
    if rel is None or sh is None:
        raise AgentError("delete_draft works only on notes of this unit (a vault path or a shadow path).")
    if zk is not None and (zk / rel).is_file():
        raise AgentError(f"{rel} exists in the vault; use move_file or remove_note, not delete_draft.")
    if not (sh / rel).is_file():
        raise AgentError(f"{rel} is not a draft of this unit.")
    (sh / rel).unlink()
    _retarget(rel, rel)
    _marked_update(Path(rel).stem, None)
    return {"deleted_draft": rel}


def _marked_update(old_title: str, new_title: str | None) -> None:
    """Keep the unit's note list (marked.json, written by queue_mark.py) in step with a
    delete_draft (new_title None) or a move_file, so a fix turn needs no new clock-out."""
    data = _load_meta("marked.json")
    if not data:
        data = {}
    notes = [str(t) for t in data.get("notes") or []]
    if new_title is None:
        data["deleted"] = sorted(set(data.get("deleted") or []) | {old_title})
        notes = [t for t in notes if t != old_title]
    else:
        ren = dict(data.get("renamed") or {})
        ren[old_title] = new_title
        data["renamed"] = ren
        if old_title in notes:
            notes = [new_title if t == old_title else t for t in notes]
        notes = list(dict.fromkeys(notes))
    data["notes"] = notes
    _save_meta("marked.json", data)


def move_file(src: str, dst: str) -> dict[str, Any]:
    """Rename a note: write the new path in the shadow folder. The old vault note
    becomes a stub (repair mode) or a removal request; both apply only if the new
    note passes. A note that exists only in this unit is simply renamed."""
    s_res = resolve_allowed_path(src)
    s_rel = _vault_rel(s_res)
    if s_rel is None:
        raise AgentError("move_file works only on notes in the vault folder.")
    text = _current_text(s_res)
    target, rel = _write_target(dst)
    if rel is None:
        raise AgentError("move_file target must be in the vault folder.")
    if rel == s_rel:
        raise AgentError("move_file: source and target are the same note.")
    old_t, new_t = Path(s_rel).stem, Path(rel).stem
    text = re.sub(rf"(?m)^# {re.escape(old_t)}\s*$", f"# {new_t}", text, count=1)
    _atomic_write(target, text)
    remove_note(src, dst)
    _marked_update(old_t, new_t)
    return {"src": str(s_res), "dst": str(target), "vault_path": rel}


# --------------------------------------------------------------------------- #
# Helper commands: one argument allowlist per helper.
# --------------------------------------------------------------------------- #

HELPER_FLAGS: dict[str, set[str] | None] = {
    # read-only; --index and --vault (arbitrary files) are not allowed
    "index_query.py": {"--k", "--kind", "--speaker", "--skill", "--level", "--equipment", "--basis", "--modality"},
    "index_rebuild.py": set(),
    "index_add.py": {"--title", "--file", "--gist", "--tags", "--aliases", "--moc", "--source",
                     "--source-url", "--speaker", "--channel", "--skill", "--level", "--equipment",
                     "--basis", "--modality", "--claim-inc"},
    "queue_mark.py": {"--ids", "--stage", "--notes", "--notes-replace", "--reason", "--note", "--skip-passage", "--at"},
    "validate.py": {"--vault", "--files", "--staging", "--strict", "--squeeze"},
    "verify_claims.py": {"--lit", "--staging", "--marker-tolerance", "--quiet", "--repair", "--out", "--vault-root"},
    "note_link.py": {"--from", "--to", "--kind"},
}
INDEX_ADD_FOLD_REFUSED = 3  # index_add.py: fold refused (NOTE_CONTRACT.md section 7.1)


def helper_path(raw: str) -> Path:
    candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() else (HERE / candidate).resolve()
    if resolved.parent != HERE or resolved.name not in HELPERS:
        raise AgentError(f"Only these Ralph helper scripts may be run: {', '.join(sorted(HELPERS))} "
                         f"(no 'python3 -c', no other scripts); got: {raw}")
    return resolved


# Arguments that name a file or folder the helper READS: they must be inside the read roots.
PATH_FLAGS = {
    "validate.py": {"--vault", "--files", "--staging"},
    "verify_claims.py": {"--lit", "--staging", "--vault-root", "<positional>"},
}


def _check_read_path(script: str, value: str) -> None:
    p = Path(value)
    p = p if p.is_absolute() else HERE / p
    rp = p.resolve()
    zk = zk_dir()
    if not p.exists() and zk is not None and not Path(value).is_absolute():
        rp = (zk / value).resolve()  # a vault-relative note path ("01 Permanent Notes/X.md")
    if zk is not None and rp == zk:
        return  # validate.py --vault "$ZK_DIR": the helper reads the zettelkasten only
    if not read_allowed(rp):
        raise AgentError(f"{script}: path '{value}' is outside what an agent may read.")


def check_helper_args(script: str, args: list[str]) -> list[str]:
    """Refuse any flag that is not on the helper's list (no prefixes, no abbreviations),
    and any path argument outside the read roots. `$ZK_DIR` and `$ZR_STAGING` are
    replaced; no other variable is expanded."""
    allowed = HELPER_FLAGS.get(script)
    args = [expand_allowed(str(a)) for a in args]
    if args in (["--help"], ["-h"]):
        return args  # read-only usage text
    paths = PATH_FLAGS.get(script, set())
    current = None
    for a in args:
        if a.startswith("-"):
            current = a.split("=", 1)[0]
            if "=" in a and current in paths:
                _check_read_path(script, a.split("=", 1)[1])
                current = None
            continue
        if current in paths or (current is None and "<positional>" in paths):
            _check_read_path(script, a)
        if current != "--files":
            current = None
    if script == "note_link.py":
        for i, a in enumerate(args):
            if a in ("--from", "--to") and i + 1 < len(args):
                v = args[i + 1]
                if Path(v).is_absolute():
                    v = _vault_rel(Path(v).resolve()) or ""
                if not v.startswith("01 Permanent Notes/") or "/" in v[len("01 Permanent Notes/"):] \
                        or ".." in Path(v).parts or not v.endswith(".md"):
                    raise AgentError("note_link.py: --from and --to must be notes in '01 Permanent Notes/'.")
    if script == "index_add.py":
        for i, a in enumerate(args):
            if a == "--file" and i + 1 < len(args):
                v = args[i + 1]
                if Path(v).is_absolute() or ".." in Path(v).parts or not v.startswith("01 Permanent Notes/"):
                    raise AgentError("index_add.py: --file must be '01 Permanent Notes/<name>.md'.")
    if script == "index_add.py" and os.environ.get("ZR_UNIT_KIND") == "repair":
        allowed = (allowed or set()) | {"--repair"}
    if allowed is None:
        return args
    for i, a in enumerate(args):
        if a.startswith("-"):
            flag = a.split("=", 1)[0]
            if flag not in allowed:
                raise AgentError(f"{script}: argument '{flag}' is not allowed for the agent. Allowed: {sorted(allowed)}")
            if script == "verify_claims.py" and flag == "--out":
                value = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
                sh = shadow_dir()
                if not value or sh is None or not _under(Path(value).resolve(), sh):
                    raise AgentError("verify_claims.py --out must point into the shadow folder.")
    return args


def run_command(command: str, args: list[str] | None = None, timeout_seconds: int = 120) -> dict[str, Any]:
    args = args or []
    if command == "pwd":
        if args:
            raise AgentError("pwd does not accept arguments.")
        return {"stdout": str(HERE) + "\n", "stderr": "", "returncode": 0}
    if command not in {"python", "python3"}:
        raise AgentError("Only pwd, python, and python3 commands are allowed.")
    if not args:
        raise AgentError("python command requires a helper script argument.")
    script = helper_path(expand_allowed(str(args[0])))
    if os.environ.get("ZR_AGENT_READONLY") == "1" and script.name not in READONLY_HELPERS:
        raise AgentError(f"{script.name} changes state; a review agent may run only "
                         f"{', '.join(sorted(READONLY_HELPERS))}.")
    rest = check_helper_args(script.name, list(args[1:]))
    env = secret_scan.safe_env()  # no API key, token or secret reaches a helper
    env["ZR_AGENT"] = "1"
    sh = shadow_dir()
    if script.name == "index_add.py" and sh is not None:
        # index_add.py reads the note it records; a new or changed note is in the shadow folder.
        f = next((rest[i + 1] for i, a in enumerate(rest[:-1]) if a == "--file"), None)
        if f and (sh / f).is_file():
            rest = [*rest, "--vault", str(sh)]
    cmd = [sys.executable, str(script), *rest]
    timeout_seconds = max(1, min(timeout_seconds, int(os.environ.get("DEEPSEEK_TOOL_TIMEOUT", "300"))))
    proc = subprocess.run(cmd, cwd=HERE, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout_seconds, check=False)
    result = {
        "command": " ".join(shlex.quote(part) for part in cmd),
        "returncode": proc.returncode,
        "stdout": compact_output(secret_scan.scrub(proc.stdout)),
        "stderr": compact_output(secret_scan.scrub(proc.stderr)),
    }
    if script.name in FULL_RESULT_HELPERS:
        # Round 8: the full output (not cut), secrets removed, for analysis of self-checks.
        out = secret_scan.scrub(proc.stdout)
        try:
            parsed: Any = json.loads(out)
        except ValueError:
            parsed = out
        _journal("helper_results.jsonl", {"helper": script.name, "args": [secret_scan.scrub(str(x)) for x in rest],
                                          "returncode": proc.returncode, "stdout": parsed,
                                          "stderr": secret_scan.scrub(proc.stderr)})
    if script.name == "index_add.py" and proc.returncode == INDEX_ADD_FOLD_REFUSED:
        f = next((rest[i + 1] for i, a in enumerate(rest[:-1]) if a == "--file"), "")
        _journal("events.jsonl", {"event": "fold-refused", "file": f})
        result["note"] = ("index_add.py refused the fold (exit 3): the speaker or scope differs. "
                          "Create a new note instead and link the two notes with note_link.py.")
    return result


def get_env(name: str) -> dict[str, Any]:
    if name not in ENV_ALLOWLIST:
        raise AgentError(f"Environment variable is not allowlisted: {name}")
    return {"name": name, "value": os.environ.get(name, "")}


def finish(summary: str = "") -> dict[str, Any]:
    return {"finished": True, "summary": summary}


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 file: notes in the zettelkasten folders (00 Maps, 01 Permanent Notes, Home.md), your shadow folder, the transcripts (lit), STATE.md, DECISIONS.md and the prompt files. Use line ranges for large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "default": 1},
                    "line_count": {"type": "integer", "default": 400},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories in an allowed directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "max_entries": {"type": "integer", "default": 200},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files below an allowed directory using a glob pattern, such as '01 Permanent Notes/*.md'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {"type": "string"},
                    "pattern": {"type": "string"},
                    "max_entries": {"type": "integer", "default": 200},
                },
                "required": ["root", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a UTF-8 file. A path in the zettelkasten folder goes to your shadow folder; the harness verifies and publishes it after the run. In staging only STATE.md, DECISIONS.md and repair/*.json are writable. A source-check reviewer writes only the one verdict file named in its prompt.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append UTF-8 content to an allowed file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace exact text in an allowed UTF-8 file. Fails unless the exact expected count is found.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_replacements": {"type": "integer", "default": 1},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Rename a vault note: the new path is written to your shadow folder and the old path is removed when the new note passes verification.",
            "parameters": {
                "type": "object",
                "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_draft",
            "description": "Delete a note that exists only in your shadow folder (an old draft of this unit). Use it when you rewrote a note under a new title with write_file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_note",
            "description": "Ask the harness to remove a vault note. It applies only when replaced_by (a note you wrote in this unit) passes verification.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "replaced_by": {"type": "string"}},
                "required": ["path", "replaced_by"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run only pwd or python/python3 with a Ralph helper script: index_query.py, index_add.py, queue_mark.py, validate.py, verify_claims.py (report only), note_link.py, or index_rebuild.py. Each helper accepts only its own listed flags.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "enum": ["pwd", "python", "python3"]},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer", "default": 120},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_env",
            "description": "Read an allowlisted Ralph environment variable.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call when the single work unit is complete. Include a concise verification summary.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": [],
            },
        },
    },
]

# Transient network faults (SSL EOF, resets) killed whole agent runs. Retry a few
# times with exponential backoff before giving up.
TRANSIENT_ATTEMPTS = max(1, int(os.environ.get("DEEPSEEK_RETRIES", "4")))
TRANSIENT_BACKOFF_CAP_S = 30

TOOL_HANDLERS = {
    "read_file": read_file,
    "list_dir": list_dir,
    "find_files": find_files,
    "write_file": write_file,
    "append_file": append_file,
    "replace_in_file": replace_in_file,
    "move_file": move_file,
    "remove_note": remove_note,
    "delete_draft": delete_draft,
    "run_command": run_command,
    "get_env": get_env,
    "finish": finish,
}

SYSTEM_PROMPT = f"""You are the tool-using worker inside the zettel_ralph Ralph harness.

You must complete exactly the one unit described by the user prompt, persist the result to
disk with the available tools, and stop. Read the requested AGENTS file, prompts, template,
state, decisions, and lit note through tools. Never assume unseen files.

Important operating rules:
- Use only the provided tools for filesystem work and command execution.
- Never try to run shell scripts, bash, sh, claude, codex, or nested agents.
- Never open the concept index directly; use index_query.py and index_add.py.
- Mark the unit with queue_mark.py before finishing, unless the user prompt is a review pass.
- Verify by rereading touched notes or running the helper requested by the driver.
- You never write into the vault. A write to a vault path goes to your shadow folder
  ($ZR_SHADOW_DIR); reads of that path then return your version. After the run the
  harness verifies each shadow note against its transcript and publishes only the notes
  that pass. To rename a note use move_file; to merge use remove_note with replaced_by.
- The harness directory is {HERE}.
"""


def chat_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout_seconds: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": False,
        "max_tokens": int(os.environ.get("DEEPSEEK_MAX_TOKENS", "32000")),
    }
    if "openrouter.ai" in base_url:
        body["usage"] = {"include": True}  # OpenRouter returns tokens and cost per call
    if os.environ.get("DEEPSEEK_TEMPERATURE", "0.2"):
        body["temperature"] = float(os.environ.get("DEEPSEEK_TEMPERATURE", "0.2"))

    endpoint = base_url.rstrip("/") + "/chat/completions"
    last_transient: Exception | None = None
    for attempt in range(TRANSIENT_ATTEMPTS):
        if attempt:
            time.sleep(min(2**attempt, TRANSIENT_BACKOFF_CAP_S))
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AgentError(
                f"DeepSeek API HTTP {exc.code}: {compact_output(detail, 4000)}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            # Transient network fault (SSL EOF, reset, timeout, DNS). One dropped
            # request must not kill a whole agent run; back off and try again.
            last_transient = exc
    raise AgentError(
        f"DeepSeek API connection error after {TRANSIENT_ATTEMPTS} attempts: "
        f"{getattr(last_transient, 'reason', last_transient)}"
    ) from last_transient


def parse_tool_args(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    return json.loads(raw)


def _log_args(raw: Any) -> Any:
    """Tool arguments for the tool log, cut to 300 characters, secrets removed."""
    try:
        args = parse_tool_args(raw)
    except (ValueError, TypeError):
        return secret_scan.scrub(str(raw)[:300])
    return {k: (secret_scan.scrub(v[:300]) + ("...[cut]" if len(v) > 300 else "") if isinstance(v, str) else v)
            for k, v in args.items()}


FULL_RESULT_HELPERS = {"verify_claims.py", "validate.py"}


def run_tool(call: dict[str, Any]) -> tuple[str, bool]:
    function = call.get("function") or {}
    name = function.get("name", "")
    if name not in TOOL_HANDLERS:
        _journal("tools.jsonl", {"tool": name, "ok": False, "error": "unknown tool"})
        return _json({"error": f"Unknown tool: {name}"}), False
    try:
        args = parse_tool_args(function.get("arguments"))
        result = TOOL_HANDLERS[name](**args)
        finished = name == "finish" and bool(result.get("finished"))
        out = secret_scan.scrub(_json({"ok": True, "result": result}))
        _journal("tools.jsonl", {"tool": name, "args": _log_args(args), "ok": True,
                                 "result": out[:2000] + ("...[cut]" if len(out) > 2000 else "")})
        return out, finished
    except Exception as exc:  # noqa: BLE001 - tool errors should go back to the model.
        err = secret_scan.scrub(str(exc))
        _journal("tools.jsonl", {"tool": name, "args": _log_args(function.get("arguments")),
                                 "ok": False, "error": err[:500]})
        return _json({"ok": False, "error": err}), False


def assistant_message_for_history(message: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        clean["tool_calls"] = message["tool_calls"]
    return clean


RUN_STATE: dict[str, Any] = {"steps": 0, "models_served": [], "usage": {
    "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
    "cost": 0.0, "calls_with_usage": 0}}


def _add_usage(usage: Any) -> None:
    """Sum the token usage and cost that the API returns (OpenRouter: `usage.cost`)."""
    u = RUN_STATE["usage"]
    u["calls"] += 1
    if not isinstance(usage, dict):
        return
    u["calls_with_usage"] += 1
    u["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
    u["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    u["reasoning_tokens"] += int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    u["cached_tokens"] += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    import pricing  # noqa: PLC0415

    cost, src = pricing.call_cost(str(RUN_STATE.get("model_requested") or ""), usage)
    u["cost"] = round(u["cost"] + cost, 6)
    if src != "provider":
        key = "calls_cost_from_price_table" if src == "price-table" else "calls_cost_unknown"
        u[key] = u.get(key, 0) + 1


def run_agent(prompt: str) -> int:
    api_key = load_api_key()
    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("DEEPSEEK_MODEL") or os.environ.get("MODEL") or DEFAULT_MODEL
    max_steps = int(os.environ.get("DEEPSEEK_MAX_STEPS", "80"))
    api_timeout = int(os.environ.get("DEEPSEEK_API_TIMEOUT", "300"))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    RUN_STATE.update(model_requested=model, base_url=base_url, max_steps=max_steps)
    for step in range(1, max_steps + 1):
        RUN_STATE["steps"] = step
        response = chat_completion(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=messages,
            timeout_seconds=api_timeout,
        )
        choices = response.get("choices") or []
        if not choices:
            raise AgentError(f"DeepSeek response had no choices: {_json(response)}")

        _add_usage(response.get("usage"))
        served = response.get("model")
        if served and served not in RUN_STATE["models_served"]:
            RUN_STATE["models_served"].append(served)
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if content:
            print(secret_scan.scrub(content), flush=True)

        tool_calls = message.get("tool_calls") or []
        finish_reason = choice.get("finish_reason")
        if not tool_calls:
            if finish_reason == "length":
                raise AgentLimit("DeepSeek response hit max_tokens before finishing "
                                 f"(DEEPSEEK_MAX_TOKENS={os.environ.get('DEEPSEEK_MAX_TOKENS', '32000')}).")
            return 0

        messages.append(assistant_message_for_history(message))
        finished = False
        for call in tool_calls:
            tool_result, tool_finished = run_tool(call)
            finished = finished or tool_finished
            if tool_finished:
                try:
                    summary = json.loads(tool_result)["result"].get("summary", "")
                except (KeyError, TypeError, json.JSONDecodeError):
                    summary = ""
                if summary:
                    print(secret_scan.scrub(summary), flush=True)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": tool_result,
                }
            )
        if finished:
            return 0

        if step % 10 == 0:
            print(f"[deepseek-agent] completed {step} tool rounds", file=sys.stderr, flush=True)

    raise AgentLimit(f"DeepSeek agent exceeded DEEPSEEK_MAX_STEPS={max_steps}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", help="Prompt text. If omitted, stdin is used.")
    args = parser.parse_args()
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        print("No prompt supplied.", file=sys.stderr)
        return 2

    started = time.time()
    result: dict[str, Any] = {"rc": 1, "end": "error", "detail": ""}
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        rc = run_agent(prompt)
        result.update(rc=rc, end="finished" if rc == 0 else "error")
    except AgentTimeout as exc:
        print(f"deepseek_agent timeout: {exc}", file=sys.stderr)
        result.update(rc=124, end="timeout", detail=str(exc))
        rc = 124
    except AgentLimit as exc:
        # Exit code 3 tells the harness that the run stopped at a limit.
        print(f"deepseek_agent limit: {exc}", file=sys.stderr)
        result.update(rc=LIMIT_EXIT_CODE, end="limit", detail=str(exc))
        rc = LIMIT_EXIT_CODE
    except AgentError as exc:
        print(f"deepseek_agent error: {exc}", file=sys.stderr)
        result.update(rc=1, end="error", detail=str(exc)[:2000])
        rc = 1
    finally:
        elapsed = time.time() - started
        print(f"[deepseek-agent] elapsed={elapsed:.1f}s", file=sys.stderr, flush=True)
        result.update(elapsed_s=round(elapsed, 1), **RUN_STATE)
        run_dir = _run_dir()
        if run_dir is not None:
            run_dir.mkdir(parents=True, exist_ok=True)
            result = _accumulate(run_dir / "agent_result.json", result)
            (run_dir / "agent_result.json").write_text(_json(result) + "\n", encoding="utf-8")
            # One record per turn as well (synthesis = turn 1, fix turns after it).
            turn = len(result.get("turns") or []) + 1
            own = result.get("last_turn") or {k: result.get(k) for k in ("rc", "end", "detail", "elapsed_s", "steps",
                                                                          "usage", "models_served")}
            (run_dir / f"agent_result.turn-{turn}.json").write_text(
                _json({**own, "turn": turn, "base_url": result.get("base_url"), "model": result.get("model_requested")})
                + "\n", encoding="utf-8")
    return rc


def _accumulate(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Fix turns run in the same unit folder: keep the earlier turns, and sum usage,
    steps and time over all turns. `rc`, `end` and `detail` are those of the last turn."""
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    if not isinstance(prev, dict):
        return result
    keys = ("rc", "end", "detail", "elapsed_s", "steps", "usage", "models_served")
    # "last_turn" holds the earlier run's own values (the top level holds totals).
    turns = list(prev.get("turns") or []) + [prev.get("last_turn") or {k: prev.get(k) for k in keys}]
    out = dict(result)
    out["last_turn"] = {k: result.get(k) for k in keys}
    out["turns"] = turns
    usage = dict(result.get("usage") or {})
    for t in turns:
        for k, v in (t.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] = usage.get(k, 0) + v
    out["usage"] = usage
    out["steps"] = int(result.get("steps") or 0) + sum(int(t.get("steps") or 0) for t in turns)
    out["elapsed_s"] = round(float(result.get("elapsed_s") or 0) + sum(float(t.get("elapsed_s") or 0) for t in turns), 1)
    out["models_served"] = sorted({m for t in [*turns, result] for m in (t.get("models_served") or [])})
    return out


if __name__ == "__main__":
    raise SystemExit(main())
