#!/usr/bin/env python3
"""Safe maintenance and diagnostics for a Zettel-Ralph run.

This CLI consolidates the reusable parts of the one-off repair scripts created during
the first large Twitter-bookmark run. Read-only commands emit JSON. Commands that change
the vault or staging state are dry-run by default and require ``--apply``.

Examples:

    python maintenance.py provenance --staging staging --note "01 Permanent Notes/A.md"
    python maintenance.py links --vault "$ZK_DIR" --note "A"
    python maintenance.py squeeze --report staging/squeeze.json
    python maintenance.py retire-note --vault "$ZK_DIR" --staging staging \
        --note "Obsolete title" --apply
    python maintenance.py review-status --staging staging --note "01 Permanent Notes/A.md"
    python maintenance.py review-mark --staging staging --note "01 Permanent Notes/A.md" \
        --pass linking --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from _lock import state_lock

HERE = Path(__file__).resolve().parent
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
REVIEW_PASSES = frozenset({"source-check", "atomicity", "linking", "source-free"})
RETIREMENT_JOURNAL = ".retire-note-transaction.json"


class MaintenanceError(RuntimeError):
    """Raised when a requested maintenance operation is unsafe or ambiguous."""


def _read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if default is not None:
            return default
        raise MaintenanceError(f"file not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"cannot read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MaintenanceError(f"expected a JSON object in {path}")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _note_key(keys: list[str], note: str) -> str:
    value = note.strip()
    direct = [
        value,
        f"{value}.md" if not value.endswith(".md") else value,
        f"01 Permanent Notes/{value}.md"
        if "/" not in value and not value.endswith(".md")
        else value,
    ]
    for candidate in direct:
        if candidate in keys:
            return candidate

    wanted_stem = Path(value).stem
    matches = [key for key in keys if Path(key).stem == wanted_stem]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise MaintenanceError(f"ambiguous note '{note}': {matches}")
    raise MaintenanceError(f"note not found: {note}")


def _inside(root: Path, candidate: Path) -> Path:
    resolved_root = root.expanduser().resolve()
    resolved = candidate.expanduser().resolve()
    if not resolved.is_relative_to(resolved_root):
        raise MaintenanceError(f"path escapes configured root: {candidate}")
    return resolved


def _vault_note(vault: Path, note: str, *, must_exist: bool = True) -> Path:
    root = vault.expanduser().resolve()
    value = Path(note).expanduser()
    candidates: list[Path] = []
    if value.is_absolute():
        candidates.append(value)
    elif "/" in note or note.endswith(".md"):
        candidates.append(root / value)
    else:
        candidates.append(root / "01 Permanent Notes" / f"{note}.md")
        candidates.extend(path for path in root.rglob("*.md") if path.stem == note)

    safe_candidates: list[Path] = []
    for candidate in candidates:
        safe = _inside(root, candidate)
        if safe.suffix != ".md":
            continue
        if safe not in safe_candidates:
            safe_candidates.append(safe)

    existing = [candidate for candidate in safe_candidates if candidate.is_file()]
    if len(existing) == 1:
        return existing[0]
    if len(existing) > 1:
        relative = [str(path.relative_to(root)) for path in existing]
        raise MaintenanceError(f"ambiguous note '{note}': {relative}")
    if not must_exist and len(safe_candidates) == 1:
        return safe_candidates[0]
    raise MaintenanceError(f"note not found under {root}: {note}")


def provenance_report(staging: Path, note: str) -> dict[str, Any]:
    """Sources of one note from provenance.json plus the harness log provenance.jsonl."""
    import provenance as provenance_store

    if not (staging / "provenance.json").exists() and not provenance_store.jsonl_path(staging).exists():
        raise MaintenanceError(f"no provenance.json or provenance.jsonl under {staging}")
    provenance = provenance_store.merged_map(staging)
    key = _note_key(list(provenance), note)
    sources = provenance.get(key, [])
    if not isinstance(sources, list):
        raise MaintenanceError(f"invalid provenance entry for {key}")
    return {"note": key, "sources": sources, "source_count": len(sources)}


def _link_names(path: Path, root: Path) -> set[str]:
    relative = path.relative_to(root).with_suffix("").as_posix()
    return {path.stem, relative}


def _links(path: Path) -> list[str]:
    return [
        match.strip()
        for match in WIKILINK.findall(path.read_text(encoding="utf-8", errors="replace"))
    ]


def links_report(vault: Path, note: str) -> dict[str, Any]:
    root = vault.expanduser().resolve()
    target = _vault_note(root, note)
    markdown = sorted(path for path in root.rglob("*.md") if path.is_file())
    names_by_path = {path: _link_names(path, root) for path in markdown}
    target_names = names_by_path[target]

    incoming = [
        str(path.relative_to(root))
        for path in markdown
        if path != target and target_names.intersection(_links(path))
    ]
    outgoing: list[dict[str, Any]] = []
    for link in _links(target):
        matches = [path for path, names in names_by_path.items() if link in names]
        resolved = matches[0] if len(matches) == 1 else None
        outgoing.append(
            {
                "target": link,
                "resolved": str(resolved.relative_to(root)) if resolved else None,
                "reciprocal": bool(resolved and target_names.intersection(_links(resolved))),
            }
        )

    return {
        "note": str(target.relative_to(root)),
        "incoming": incoming,
        "incoming_count": len(incoming),
        "outgoing": outgoing,
        "outgoing_count": len(outgoing),
        "unreferenced": not incoming,
        "isolated": not incoming and not outgoing,
    }


def squeeze_report(report_path: Path) -> dict[str, Any]:
    report = _read_json(report_path)
    topics = []
    for topic, details in report.items():
        if not isinstance(details, dict) or not details.get("at_squeeze"):
            continue
        topics.append(
            {
                "topic": topic,
                "count": int(details.get("count", 0)),
                "has_moc": bool(details.get("has_moc", False)),
            }
        )
    topics.sort(key=lambda entry: (-entry["count"], entry["topic"]))
    return {"topics": topics, "topic_count": len(topics)}


def _matches_note_selector(note: str, relative: str, title: str | None) -> bool:
    if note == relative or (title and note == title):
        return True
    if "/" in note or "\\" in note:
        return False
    return Path(note).stem == Path(relative).stem


def _matches_vault_path(vault: Path, configured: object, relative: str) -> bool:
    raw = Path(str(configured)).expanduser()
    try:
        resolved = _inside(vault, raw if raw.is_absolute() else vault / raw)
    except MaintenanceError:
        return False
    return resolved.relative_to(vault.resolve()).as_posix() == relative


def _retirement_target(
    vault: Path, index: dict[str, Any], note: str
) -> tuple[Path, str, str | None]:
    concepts = index.get("concepts", [])
    if not isinstance(concepts, list):
        raise MaintenanceError("concept-index.json has no concepts list")
    matches = [
        entry
        for entry in concepts
        if isinstance(entry, dict)
        and _matches_note_selector(
            note,
            str(entry.get("file", "")),
            str(entry.get("title", "")) or None,
        )
    ]
    if len(matches) > 1:
        raise MaintenanceError(f"ambiguous concept title: {note}")
    if matches:
        configured = str(matches[0].get("file", ""))
        if not configured:
            raise MaintenanceError(f"concept has no file path: {note}")
        configured_path = Path(configured).expanduser()
        path = _inside(
            vault, configured_path if configured_path.is_absolute() else vault / configured_path
        )
        if path.suffix != ".md":
            raise MaintenanceError(f"concept does not point to a Markdown note: {configured}")
        relative = path.relative_to(vault.resolve()).as_posix()
        title = str(matches[0].get("title", "")).strip()
        if not title:
            raise MaintenanceError(f"concept has no title: {configured}")
        return path, relative, title

    path = _vault_note(vault, note)
    return path, str(path.relative_to(vault.resolve())), None


def _retired_note_path(staging: Path, relative: str) -> Path:
    staging_root = staging.expanduser().resolve()
    retired_root = _inside(staging_root, staging_root / "retired")
    base = _inside(retired_root, retired_root / f"{relative}.disabled")
    candidate = base
    sequence = 1
    while candidate.exists():
        candidate = _inside(retired_root, base.with_name(f"{base.name}.{sequence}"))
        sequence += 1
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _retirement_state(
    vault: Path, staging: Path, note: str
) -> tuple[dict[str, Any], dict[str, Any], Path, str, str | None]:
    index = _read_json(staging / "concept-index.json", {"version": 1, "concepts": [], "mocs": []})
    provenance = _read_json(staging / "provenance.json", {})
    note_path, relative, title = _retirement_target(vault, index, note)
    return index, provenance, note_path, relative, title


def _retirement_result(
    *,
    apply: bool,
    staging: Path,
    index: dict[str, Any],
    provenance: dict[str, Any],
    note_path: Path,
    relative: str,
    title: str | None,
    retired_path: Path | None,
    vault: Path,
) -> dict[str, Any]:
    concepts = index.get("concepts", [])
    removed_concepts = [
        entry
        for entry in concepts
        if isinstance(entry, dict)
        and (
            _matches_vault_path(vault, entry.get("file", ""), relative)
            or (title and entry.get("title") == title)
        )
    ]
    provenance_entries = sum(1 for key in provenance if _matches_vault_path(vault, key, relative))
    return {
        "applied": apply,
        "note": relative,
        "file_exists": note_path.exists(),
        "concept_entries": len(removed_concepts),
        "provenance_entries": provenance_entries,
        "retired_to": str(retired_path.relative_to(staging)) if retired_path else None,
    }


def _resume_retirement(
    vault: Path, staging: Path, note: str, journal: dict[str, Any]
) -> tuple[Path, str, str | None, Path | None, str | None]:
    relative = str(journal.get("note", ""))
    title_value = journal.get("title")
    title = str(title_value) if title_value else None
    if not _matches_note_selector(note, relative, title):
        raise MaintenanceError(
            f"unfinished retirement exists for '{relative}'; rerun that retirement first"
        )
    note_path = _inside(vault, vault / relative)
    if note_path.suffix != ".md":
        raise MaintenanceError(f"invalid note path in retirement journal: {relative}")

    archive_value = journal.get("retired_to")
    retired_path = None
    if archive_value:
        retired_path = _inside(staging, staging / str(archive_value))
        retired_root = (staging / "retired").resolve()
        if not retired_path.is_relative_to(retired_root):
            raise MaintenanceError("retirement journal archive escapes staging/retired")
    source_sha256 = journal.get("source_sha256")
    expected_hash = str(source_sha256) if source_sha256 else None
    return note_path, relative, title, retired_path, expected_hash


def retire_note(vault: Path, staging: Path, note: str, *, apply: bool = False) -> dict[str, Any]:
    root = vault.expanduser().resolve()
    staging = staging.expanduser().resolve()
    index_path = staging / "concept-index.json"
    provenance_path = staging / "provenance.json"
    if not apply:
        index, provenance, note_path, relative, title = _retirement_state(root, staging, note)
        retired_path = _retired_note_path(staging, relative) if note_path.exists() else None
        return _retirement_result(
            apply=False,
            staging=staging,
            index=index,
            provenance=provenance,
            note_path=note_path,
            relative=relative,
            title=title,
            retired_path=retired_path,
            vault=root,
        )

    with state_lock(staging):
        journal_path = staging / RETIREMENT_JOURNAL
        if journal_path.exists():
            journal = _read_json(journal_path)
            note_path, relative, title, retired_path, expected_hash = _resume_retirement(
                root, staging, note, journal
            )
            index = _read_json(index_path, {"version": 1, "concepts": [], "mocs": []})
            provenance = _read_json(provenance_path, {})
        else:
            index, provenance, note_path, relative, title = _retirement_state(root, staging, note)
            retired_path = _retired_note_path(staging, relative) if note_path.exists() else None
            expected_hash = _sha256(note_path) if note_path.exists() else None
            _atomic_write_json(
                journal_path,
                {
                    "version": 1,
                    "note": relative,
                    "title": title,
                    "retired_to": str(retired_path.relative_to(staging)) if retired_path else None,
                    "source_sha256": expected_hash,
                },
            )

        result = _retirement_result(
            apply=True,
            staging=staging,
            index=index,
            provenance=provenance,
            note_path=note_path,
            relative=relative,
            title=title,
            retired_path=retired_path,
            vault=root,
        )
        current_index = _read_json(index_path, {"version": 1, "concepts": [], "mocs": []})
        current_index["concepts"] = [
            entry
            for entry in current_index.get("concepts", [])
            if not (
                isinstance(entry, dict)
                and (
                    _matches_vault_path(root, entry.get("file", ""), relative)
                    or (title and entry.get("title") == title)
                )
            )
        ]
        current_provenance = _read_json(provenance_path, {})
        for key in list(current_provenance):
            if _matches_vault_path(root, key, relative):
                current_provenance.pop(key, None)
        try:
            if expected_hash:
                if note_path.exists():
                    if not retired_path:
                        raise MaintenanceError("retirement journal has no archive path")
                    if _sha256(note_path) != expected_hash:
                        raise MaintenanceError(
                            "note changed after retirement started; inspect the journal and archive"
                        )
                    retired_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(note_path, retired_path)
                elif not retired_path or not retired_path.exists():
                    raise MaintenanceError(
                        "retirement source and archive are missing; metadata was not changed"
                    )
                elif _sha256(retired_path) != expected_hash:
                    raise MaintenanceError("retired archive does not match the transaction journal")
            elif note_path.exists():
                if not retired_path:
                    raise MaintenanceError("retirement journal has no archive path")
                retired_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(note_path, retired_path)
            _atomic_write_json(index_path, current_index)
            _atomic_write_json(provenance_path, current_provenance)
            # provenance.jsonl is append-only: record the retirement, never delete lines.
            import provenance as provenance_store

            provenance_store.append_jsonl(
                provenance_store.jsonl_path(staging),
                {"ts": provenance_store.now_iso(), "event": "note-retired", "note": str(relative)},
                staging,
                lock=False,  # this block already holds state_lock(staging)
            )
            if note_path.exists():
                note_path.unlink()
            journal_path.unlink()
        except (MaintenanceError, OSError, TypeError, ValueError) as exc:
            raise MaintenanceError(
                f"retirement is incomplete but recoverable; rerun the same command to resume: {exc}"
            ) from exc
    return result


def review_status(staging: Path, note: str) -> dict[str, Any]:
    queue = _read_json(staging / "review_queue.json")
    items = queue.get("items", [])
    if not isinstance(items, list):
        raise MaintenanceError("review_queue.json has no items list")
    keys = [str(item.get("file")) for item in items if isinstance(item, dict) and item.get("file")]
    key = _note_key(keys, note)
    return next(item for item in items if isinstance(item, dict) and item.get("file") == key)


def mark_review(
    staging: Path, note: str, review_pass: str, *, apply: bool = False
) -> dict[str, Any]:
    if review_pass not in REVIEW_PASSES:
        raise MaintenanceError(
            f"unknown review pass '{review_pass}'; expected one of {sorted(REVIEW_PASSES)}"
        )
    before = review_status(staging, note)
    passes_done = before.get("passes_done", [])
    if not isinstance(passes_done, list):
        raise MaintenanceError(f"invalid passes_done for {before['file']}")
    result = {
        "applied": apply,
        "note": before["file"],
        "pass": review_pass,
        "already_done": review_pass in passes_done,
    }
    if not apply:
        return result

    queue_path = staging / "review_queue.json"
    with state_lock(staging):
        queue = _read_json(queue_path)
        for item in queue.get("items", []):
            if isinstance(item, dict) and item.get("file") == before["file"]:
                passes = item.setdefault("passes_done", [])
                if not isinstance(passes, list):
                    raise MaintenanceError(f"invalid passes_done for {before['file']}")
                if review_pass not in passes:
                    passes.append(review_pass)
                claims = item.setdefault("claims", {})
                if not isinstance(claims, dict):
                    raise MaintenanceError(f"invalid claims for {before['file']}")
                claims.pop(review_pass, None)
                break
        _atomic_write_json(queue_path, queue)
    return result


def _staging(value: str | None) -> Path:
    return Path(value or os.environ.get("ZR_STAGING") or HERE / "staging").expanduser().resolve()


def _vault(value: str | None) -> Path:
    configured = value or os.environ.get("ZK_DIR")
    if not configured:
        raise MaintenanceError("provide --vault or set ZK_DIR")
    root = Path(configured).expanduser().resolve()
    if not root.is_dir():
        raise MaintenanceError(f"vault not found: {root}")
    return root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)

    provenance = commands.add_parser("provenance", help="show source IDs recorded for one note", allow_abbrev=False)
    provenance.add_argument("--staging")
    provenance.add_argument("--note", required=True)

    links = commands.add_parser("links", help="show incoming, outgoing, and reciprocal links", allow_abbrev=False)
    links.add_argument("--vault")
    links.add_argument("--note", required=True)

    squeeze = commands.add_parser("squeeze", help="list topics awaiting a Map of Content", allow_abbrev=False)
    squeeze.add_argument("--report", required=True, help="JSON emitted by validate.py --squeeze")

    retire = commands.add_parser("retire-note", help="archive a note and remove staging metadata", allow_abbrev=False)
    retire.add_argument("--vault")
    retire.add_argument("--staging")
    retire.add_argument("--note", required=True)
    retire.add_argument("--apply", action="store_true", help="perform the planned removal")

    status = commands.add_parser("review-status", help="show one review-queue entry", allow_abbrev=False)
    status.add_argument("--staging")
    status.add_argument("--note", required=True)

    mark = commands.add_parser("review-mark", help="mark one review pass complete", allow_abbrev=False)
    mark.add_argument("--staging")
    mark.add_argument("--note", required=True)
    mark.add_argument("--pass", dest="review_pass", required=True, choices=sorted(REVIEW_PASSES))
    mark.add_argument("--apply", action="store_true", help="perform the planned queue update")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "provenance":
            result = provenance_report(_staging(args.staging), args.note)
        elif args.command == "links":
            result = links_report(_vault(args.vault), args.note)
        elif args.command == "squeeze":
            result = squeeze_report(Path(args.report).expanduser().resolve())
        elif args.command == "retire-note":
            result = retire_note(
                _vault(args.vault), _staging(args.staging), args.note, apply=args.apply
            )
        elif args.command == "review-status":
            result = review_status(_staging(args.staging), args.note)
        else:
            result = mark_review(
                _staging(args.staging), args.note, args.review_pass, apply=args.apply
            )
    except (MaintenanceError, OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
