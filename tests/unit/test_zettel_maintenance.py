"""Tests for the reusable Zettel-Ralph maintenance CLI."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ZR = Path(__file__).resolve().parents[2] / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

import maintenance as maintenance  # noqa: E402


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_note(path: Path, title: str, links: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered_links = "\n".join(f"- [[{link}]]" for link in links)
    path.write_text(f"# {title}\n\n{rendered_links}\n", encoding="utf-8")


def test_provenance_report_accepts_a_note_title(tmp_path):
    staging = tmp_path / "staging"
    _write_json(
        staging / "provenance.json",
        {"01 Permanent Notes/An Atomic Claim.md": ["tw-1", "pdf-2"]},
    )

    report = maintenance.provenance_report(staging, "An Atomic Claim")

    assert report == {
        "note": "01 Permanent Notes/An Atomic Claim.md",
        "sources": ["tw-1", "pdf-2"],
        "source_count": 2,
    }


def test_links_report_finds_incoming_outgoing_and_reciprocal_links(tmp_path):
    vault = tmp_path / "vault"
    notes = vault / "01 Permanent Notes"
    _write_note(notes / "Target.md", "Target", ["Linked", "Missing"])
    _write_note(notes / "Linked.md", "Linked", ["Target"])
    _write_note(notes / "Incoming.md", "Incoming", ["Target"])

    report = maintenance.links_report(vault, "Target")

    assert report["incoming"] == [
        "01 Permanent Notes/Incoming.md",
        "01 Permanent Notes/Linked.md",
    ]
    assert report["unreferenced"] is False
    assert report["isolated"] is False
    assert report["outgoing"] == [
        {
            "target": "Linked",
            "resolved": "01 Permanent Notes/Linked.md",
            "reciprocal": True,
        },
        {"target": "Missing", "resolved": None, "reciprocal": False},
    ]


def test_squeeze_report_only_returns_topics_awaiting_a_moc(tmp_path):
    report_path = tmp_path / "squeeze.json"
    _write_json(
        report_path,
        {
            "agents": {"count": 8, "has_moc": False, "at_squeeze": True},
            "testing": {"count": 5, "has_moc": False, "at_squeeze": True},
            "python": {"count": 12, "has_moc": True, "at_squeeze": False},
        },
    )

    assert maintenance.squeeze_report(report_path) == {
        "topics": [
            {"topic": "agents", "count": 8, "has_moc": False},
            {"topic": "testing", "count": 5, "has_moc": False},
        ],
        "topic_count": 2,
    }


def test_retire_note_is_a_dry_run_unless_apply_is_set(tmp_path):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    note = vault / "01 Permanent Notes" / "Old Claim.md"
    _write_note(note, "Old Claim", [])
    index = {
        "version": 1,
        "concepts": [{"title": "Old Claim", "file": "01 Permanent Notes/Old Claim.md"}],
        "mocs": [],
    }
    provenance = {"01 Permanent Notes/Old Claim.md": ["tw-1"]}
    _write_json(staging / "concept-index.json", index)
    _write_json(staging / "provenance.json", provenance)

    plan = maintenance.retire_note(vault, staging, "Old Claim")

    assert plan == {
        "applied": False,
        "note": "01 Permanent Notes/Old Claim.md",
        "file_exists": True,
        "concept_entries": 1,
        "provenance_entries": 1,
        "retired_to": "retired/01 Permanent Notes/Old Claim.md.disabled",
    }
    assert note.exists()
    assert json.loads((staging / "concept-index.json").read_text()) == index
    assert json.loads((staging / "provenance.json").read_text()) == provenance


def test_retire_note_apply_removes_note_index_and_provenance(tmp_path):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    note = vault / "01 Permanent Notes" / "Old Claim.md"
    _write_note(note, "Old Claim", [])
    _write_json(
        staging / "concept-index.json",
        {
            "version": 1,
            "concepts": [
                {"title": "Old Claim", "file": "01 Permanent Notes/Old Claim.md"},
                {"title": "Keep", "file": "01 Permanent Notes/Keep.md"},
            ],
            "mocs": [],
        },
    )
    _write_json(
        staging / "provenance.json",
        {"01 Permanent Notes/Old Claim.md": ["tw-1"], "01 Permanent Notes/Keep.md": ["tw-2"]},
    )

    result = maintenance.retire_note(vault, staging, "Old Claim", apply=True)

    assert result["applied"] is True
    assert not note.exists()
    retired = staging / "retired" / "01 Permanent Notes" / "Old Claim.md.disabled"
    assert retired.read_text(encoding="utf-8").startswith("# Old Claim")
    index = json.loads((staging / "concept-index.json").read_text())
    assert [entry["title"] for entry in index["concepts"]] == ["Keep"]
    provenance = json.loads((staging / "provenance.json").read_text())
    assert provenance == {"01 Permanent Notes/Keep.md": ["tw-2"]}


def test_retire_note_rejects_an_index_path_outside_the_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    staging = tmp_path / "staging"
    _write_json(
        staging / "concept-index.json",
        {
            "version": 1,
            "concepts": [{"title": "Unsafe", "file": "../outside.md"}],
            "mocs": [],
        },
    )

    with pytest.raises(maintenance.MaintenanceError, match="escapes configured root"):
        maintenance.retire_note(vault, staging, "Unsafe", apply=True)


def test_retire_note_treats_glob_characters_literally(tmp_path):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    _write_note(vault / "01 Permanent Notes" / "Alpha.md", "Alpha", [])

    with pytest.raises(maintenance.MaintenanceError, match="note not found"):
        maintenance.retire_note(vault, staging, "A*")


def test_retire_note_rejects_a_traversing_selector_even_when_the_stem_matches(tmp_path):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    note = vault / "01 Permanent Notes" / "Alpha.md"
    _write_note(note, "Alpha", [])
    _write_json(
        staging / "concept-index.json",
        {
            "version": 1,
            "concepts": [{"title": "Alpha", "file": "01 Permanent Notes/Alpha.md"}],
            "mocs": [],
        },
    )

    with pytest.raises(maintenance.MaintenanceError, match="escapes configured root"):
        maintenance.retire_note(vault, staging, "../Alpha.md", apply=True)

    assert note.is_file()


def test_retire_note_normalizes_an_absolute_in_vault_index_path(tmp_path):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    note = vault / "01 Permanent Notes" / "Absolute.md"
    _write_note(note, "Absolute", [])
    _write_json(
        staging / "concept-index.json",
        {
            "version": 1,
            "concepts": [{"title": "Absolute", "file": str(note.resolve())}],
            "mocs": [],
        },
    )
    _write_json(staging / "provenance.json", {str(note.resolve()): ["pdf-1"]})

    result = maintenance.retire_note(vault, staging, "Absolute", apply=True)

    assert result["note"] == "01 Permanent Notes/Absolute.md"
    retired = staging / "retired" / "01 Permanent Notes" / "Absolute.md.disabled"
    assert retired.is_file()
    assert not list(vault.rglob("*.disabled"))
    assert json.loads((staging / "provenance.json").read_text()) == {}


def test_retire_note_rejects_a_symlinked_archive_outside_staging(tmp_path):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    outside = tmp_path / "outside"
    note = vault / "01 Permanent Notes" / "Target.md"
    _write_note(note, "Target", [])
    staging.mkdir()
    outside.mkdir()
    (staging / "retired").symlink_to(outside, target_is_directory=True)

    with pytest.raises(maintenance.MaintenanceError, match="escapes configured root"):
        maintenance.retire_note(vault, staging, "Target", apply=True)

    assert note.is_file()
    assert list(outside.iterdir()) == []


def test_retire_note_recomputes_the_target_after_acquiring_the_lock(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    first = vault / "01 Permanent Notes" / "First.md"
    second = vault / "01 Permanent Notes" / "Second.md"
    _write_note(first, "First", [])
    _write_note(second, "Second", [])
    index_path = staging / "concept-index.json"
    _write_json(
        index_path,
        {
            "version": 1,
            "concepts": [{"title": "Target", "file": "01 Permanent Notes/First.md"}],
            "mocs": [],
        },
    )

    @contextmanager
    def changing_lock(_staging):
        _write_json(
            index_path,
            {
                "version": 1,
                "concepts": [{"title": "Target", "file": "01 Permanent Notes/Second.md"}],
                "mocs": [],
            },
        )
        yield

    monkeypatch.setattr(maintenance, "state_lock", changing_lock)

    maintenance.retire_note(vault, staging, "Target", apply=True)

    assert first.is_file()
    assert not second.exists()
    assert (staging / "retired" / "01 Permanent Notes" / "Second.md.disabled").is_file()


def test_retire_note_resumes_an_interrupted_transaction(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    note = vault / "01 Permanent Notes" / "Interrupted.md"
    _write_note(note, "Interrupted", [])
    _write_json(
        staging / "concept-index.json",
        {
            "version": 1,
            "concepts": [{"title": "Interrupted", "file": "01 Permanent Notes/Interrupted.md"}],
            "mocs": [],
        },
    )
    _write_json(
        staging / "provenance.json",
        {"01 Permanent Notes/Interrupted.md": ["tw-1"]},
    )
    real_write = maintenance._atomic_write_json
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if path.name == "provenance.json" and not failed:
            failed = True
            raise OSError("simulated write failure")
        real_write(path, value)

    monkeypatch.setattr(maintenance, "_atomic_write_json", fail_once)
    with pytest.raises(maintenance.MaintenanceError, match="rerun the same command"):
        maintenance.retire_note(vault, staging, "Interrupted", apply=True)

    assert note.is_file()
    assert (staging / maintenance.RETIREMENT_JOURNAL).is_file()

    monkeypatch.setattr(maintenance, "_atomic_write_json", real_write)
    maintenance.retire_note(vault, staging, "Interrupted", apply=True)

    assert not note.exists()
    assert not (staging / maintenance.RETIREMENT_JOURNAL).exists()
    assert json.loads((staging / "concept-index.json").read_text())["concepts"] == []
    assert json.loads((staging / "provenance.json").read_text()) == {}


def test_retire_note_refuses_to_resume_after_the_source_changes(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    note = vault / "01 Permanent Notes" / "Changed.md"
    _write_note(note, "Changed", [])
    _write_json(
        staging / "concept-index.json",
        {
            "version": 1,
            "concepts": [{"title": "Changed", "file": "01 Permanent Notes/Changed.md"}],
            "mocs": [],
        },
    )
    real_write = maintenance._atomic_write_json

    def fail_provenance(path, value):
        if path.name == "provenance.json":
            raise OSError("simulated write failure")
        real_write(path, value)

    monkeypatch.setattr(maintenance, "_atomic_write_json", fail_provenance)
    with pytest.raises(maintenance.MaintenanceError, match="rerun the same command"):
        maintenance.retire_note(vault, staging, "Changed", apply=True)

    note.write_text("# Changed\n\nnew content\n", encoding="utf-8")
    monkeypatch.setattr(maintenance, "_atomic_write_json", real_write)

    with pytest.raises(maintenance.MaintenanceError, match="note changed"):
        maintenance.retire_note(vault, staging, "Changed", apply=True)

    assert note.is_file()


def test_retire_note_refuses_to_resume_without_source_or_archive(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    note = vault / "01 Permanent Notes" / "Missing.md"
    _write_note(note, "Missing", [])
    _write_json(
        staging / "concept-index.json",
        {
            "version": 1,
            "concepts": [{"title": "Missing", "file": "01 Permanent Notes/Missing.md"}],
            "mocs": [],
        },
    )
    real_write = maintenance._atomic_write_json

    def fail_provenance(path, value):
        if path.name == "provenance.json":
            raise OSError("simulated write failure")
        real_write(path, value)

    monkeypatch.setattr(maintenance, "_atomic_write_json", fail_provenance)
    with pytest.raises(maintenance.MaintenanceError, match="rerun the same command"):
        maintenance.retire_note(vault, staging, "Missing", apply=True)

    note.unlink()
    archive = staging / "retired" / "01 Permanent Notes" / "Missing.md.disabled"
    archive.unlink()
    monkeypatch.setattr(maintenance, "_atomic_write_json", real_write)

    with pytest.raises(maintenance.MaintenanceError, match="source and archive are missing"):
        maintenance.retire_note(vault, staging, "Missing", apply=True)

    assert (staging / maintenance.RETIREMENT_JOURNAL).is_file()


def test_review_mark_is_dry_run_idempotent_and_releases_claim(tmp_path):
    staging = tmp_path / "staging"
    queue_path = staging / "review_queue.json"
    _write_json(
        queue_path,
        {
            "version": 1,
            "items": [
                {
                    "file": "01 Permanent Notes/A.md",
                    "passes_done": ["atomicity"],
                    "claims": {"linking": "worker-1"},
                }
            ],
        },
    )

    plan = maintenance.mark_review(staging, "A", "linking")
    assert plan["applied"] is False
    assert maintenance.review_status(staging, "A")["passes_done"] == ["atomicity"]

    maintenance.mark_review(staging, "A", "linking", apply=True)
    maintenance.mark_review(staging, "A", "linking", apply=True)

    entry = maintenance.review_status(staging, "A")
    assert entry["passes_done"] == ["atomicity", "linking"]
    assert entry["claims"] == {}


def test_review_mark_rejects_an_unknown_pass(tmp_path):
    staging = tmp_path / "staging"
    _write_json(
        staging / "review_queue.json",
        {"version": 1, "items": [{"file": "01 Permanent Notes/A.md", "passes_done": []}]},
    )

    with pytest.raises(maintenance.MaintenanceError, match="unknown review pass"):
        maintenance.mark_review(staging, "A", "linknig", apply=True)


def test_cli_mutating_command_reports_dry_run(tmp_path, capsys):
    staging = tmp_path / "staging"
    _write_json(
        staging / "review_queue.json",
        {"version": 1, "items": [{"file": "01 Permanent Notes/A.md", "passes_done": []}]},
    )

    result = maintenance.main(
        ["review-mark", "--staging", str(staging), "--note", "A", "--pass", "linking"]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert maintenance.review_status(staging, "A")["passes_done"] == []
