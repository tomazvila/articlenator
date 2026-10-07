"""Harness-owned provenance store: append-only JSONL with backup rotation.

The agent never writes this file. Its file tools refuse the path (see
deepseek_agent.PROTECTED_NAMES). Only the run harness (run_integrity.py)
appends records, after it verifies a note.

File: <staging>/provenance.jsonl. One JSON object per line:

    {"ts": "...", "run_id": "...", "event": "note-verified",
     "note": "01 Permanent Notes/X.md", "sources": ["vid-abc"],
     "unit_ids": ["vid-abc"], "verification": "quote-checked",
     "settings": {"model": "...", "provider": "...", "prompt_sha256": {...}}}

Events: run-started, note-published (note-verified in round 1 files), note-quarantined,
note-removed, note-linked, note-retired.

The legacy file <staging>/provenance.json (note -> [ids]) stays readable.
`merged_map()` gives one view of both. Nothing in the harness writes the
legacy file any more.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable

from _lock import state_lock

JSONL_NAME = "provenance.jsonl"
LEGACY_NAME = "provenance.json"
BACKUPS = 5


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jsonl_path(staging: Path) -> Path:
    return Path(staging) / JSONL_NAME


def append_jsonl(path: Path, record: dict[str, Any], staging: Path | None = None,
                 lock: bool = True) -> None:
    """Append one record. Serialized by the staging lock; one write() per line.

    O_APPEND plus a single write keeps each line whole even if a second
    process ignores the lock. fsync makes the line durable before return.
    Pass lock=False only when the caller already holds the staging lock
    (flock does not nest across two open() calls in one process).
    """
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write() -> None:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    if not lock:
        _write()
        return
    with state_lock(staging if staging is not None else path.parent):
        _write()


def record(staging: Path, event: str, **fields: Any) -> dict[str, Any]:
    rec = {"ts": now_iso(), "event": event, **fields}
    append_jsonl(jsonl_path(staging), rec, staging)
    return rec


def read_records(staging: Path) -> list[dict[str, Any]]:
    p = jsonl_path(staging)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a damaged line never hides the other records
    return out


def rotate_backups(staging: Path, keep: int = BACKUPS) -> list[Path]:
    """Copy provenance.jsonl and provenance.json to .bak.1 and shift older copies.

    Call once at the start of each run. Copies never move the live file.
    """
    made = []
    with state_lock(Path(staging)):
        for name in (JSONL_NAME, LEGACY_NAME):
            live = Path(staging) / name
            if not live.exists():
                continue
            for n in range(keep - 1, 0, -1):
                src = live.with_name(f"{name}.bak.{n}")
                if src.exists():
                    os.replace(src, live.with_name(f"{name}.bak.{n + 1}"))
            dst = live.with_name(f"{name}.bak.1")
            shutil.copy2(live, dst)
            made.append(dst)
    return made


def merged_map(staging: Path) -> dict[str, list[str]]:
    """note path -> source ids, from provenance.json plus provenance.jsonl."""
    out: dict[str, list[str]] = {}
    legacy = Path(staging) / LEGACY_NAME
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    out[k] = [str(x) for x in v]
    for rec in read_records(staging):
        if rec.get("event") not in ("note-verified", "note-published") or not rec.get("note"):
            continue
        lst = out.setdefault(rec["note"], [])
        for s in rec.get("sources") or []:
            if s not in lst:
                lst.append(s)
    return out


# --------------------------------------------------------------------------- #
# Run settings: which model and which prompts wrote a note
# --------------------------------------------------------------------------- #

# Prompt texts plus the code the agent and the harness run (helpers, checks, publish).
PROMPT_GLOBS = ("AGENTS*.md", "NOTE_TEMPLATE.md", "NOTE_CONTRACT.md", "prompts/*.md",
                "deepseek_agent.py", "verify_claims.py", "validate.py", "run_integrity.py", "run_lib.sh",
                "index_*.py", "queue_*.py", "note_link.py", "provenance.py")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def provider_from_url(url: str) -> str:
    u = (url or "").lower()
    if "openrouter.ai" in u:
        return "openrouter"
    if "api.deepseek.com" in u:
        return "deepseek"
    if u.startswith("http"):
        return u.split("/")[2]
    return "unknown"


def settings_fingerprint(harness: Path, env: dict[str, str] | None = None,
                         extra_files: Iterable[Path] = ()) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    base_url = env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    agents_file = env.get("AGENTS_FILE", "")
    files: dict[str, str] = {}
    for pattern in PROMPT_GLOBS:
        for p in sorted(Path(harness).glob(pattern)):
            files[str(p.relative_to(harness))] = sha256_file(p)
    for p in extra_files:
        if Path(p).is_file():
            files[str(p)] = sha256_file(Path(p))
    return {
        "model": env.get("DEEPSEEK_MODEL") or env.get("MODEL") or "deepseek-v4-flash",
        "provider": provider_from_url(base_url),
        "base_url": base_url,
        "temperature": env.get("DEEPSEEK_TEMPERATURE", "0.2"),
        "max_tokens": env.get("DEEPSEEK_MAX_TOKENS", "12000"),
        "max_steps": env.get("DEEPSEEK_MAX_STEPS", "80"),
        "agents_file": agents_file,
        "agents_file_sha256": sha256_file(Path(agents_file)) if agents_file and Path(agents_file).is_file() else None,
        "prompt_sha256": files,
    }
