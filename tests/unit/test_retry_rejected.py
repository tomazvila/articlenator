"""Focused, offline coverage for the proposed targeted rejected-answer retry."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ZR = ROOT / "zettel_ralph"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

import run_integrity as ri  # noqa: E402
import synth_call as sc  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "retry_rejected"
NOTE = "01 Permanent Notes/Correct Muscle Activation Patterns Matter More Than Raw General Strength For Planche Performance.md"
VIDEO = "vid-qYQHJEH7zMA"
TARGET = "Radoslav Radev Predicts Strong Enough Planche Trainees With A Perfect Program Can Stay Stuck Forever If Attempt Technique And Muscle Activation Are Bad"
RETRY_IDS = ["b1", "b7", "b10"]
BULLET_NOTE = Path(NOTE).name


@pytest.fixture
def qyq_case(tmp_path: Path):
    staging = tmp_path / "staging"
    vault = tmp_path / "vault" / "Video Transcripts Zettelkasten"
    archive = staging / "retired" / "20261005T074358Z-3032081" / "20261005T074359-pending-unit-24" / NOTE
    archive.parent.mkdir(parents=True)
    shutil.copyfile(FIX / "qyq_original.md", archive)
    (staging / "lit").mkdir(parents=True)
    source = staging / "lit" / f"{VIDEO}.md"
    source.write_text("canonical qYQ test transcript\n", encoding="utf-8")
    note_path = vault / NOTE
    note_path.parent.mkdir(parents=True)
    state_fixture = json.loads((FIX / "qyq_parent_entry.json").read_text(encoding="utf-8"))
    entry = copy.deepcopy(state_fixture["entry"])
    stub = (
        "---\ntype: permanent note\nstatus: superseded\nverification: superseded\n"
        "superseded_pending:\n"
        + "".join(f'  - "bullet: {BULLET_NOTE}#{bid}"\n' for bid in RETRY_IDS)
        + "---\n\n# Original parent note\n"
    )
    note_path.write_text(stub, encoding="utf-8")
    state_path = staging / "repair_state.json"
    ri._write_json(state_path, {"notes": {NOTE: entry}, "batches": {}})
    # Match an already-initialized staging checkout so lock-file creation during a guarded
    # no-op attempt is not misreported as a content mutation.
    (staging / ".driver.lock").touch()
    (staging / ".statelock").touch()
    return {"staging": staging, "vault": vault, "archive": archive, "source": source,
            "note_path": note_path, "state_path": state_path, "entry": entry, "stub": stub}


def _plan(case, bullets=RETRY_IDS, target=TARGET):
    return sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, VIDEO, target, bullets)


def _tree_hash(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_qyq_done_superseded_rejections_preview_exact_full_archive_and_source(qyq_case):
    case = qyq_case
    before_state = case["state_path"].read_bytes()
    before_vault = case["note_path"].read_bytes()
    before_staging_tree = _tree_hash(case["staging"])
    before_vault_tree = _tree_hash(case["vault"])
    plan = _plan(case)

    assert plan["note"] == NOTE
    assert plan["video"] == VIDEO
    assert plan["target"] == TARGET
    assert plan["bullets"] == RETRY_IDS
    assert plan["entry_status"] == "done"
    assert plan["vault_status"] == "superseded"
    assert plan["archive_sha256"] == hashlib.sha256(case["archive"].read_bytes()).hexdigest()
    assert plan["archive_sha256"] == case["entry"]["sha"]
    assert plan["source_sha256"] == hashlib.sha256(case["source"].read_bytes()).hexdigest()
    assert plan["old_bullet_ids"] == [f"{BULLET_NOTE}#b{i}" for i in range(1, 11)]
    assert all(plan["rejections"][bid]["decision"] == "target-rejected" for bid in RETRY_IDS)
    assert case["state_path"].read_bytes() == before_state
    assert case["note_path"].read_bytes() == before_vault
    assert not list(case["staging"].glob("runs/*"))
    assert _tree_hash(case["staging"]) == before_staging_tree
    assert _tree_hash(case["vault"]) == before_vault_tree


def test_qyq_retry_allows_other_video_answer_and_preserves_it_in_plan(qyq_case):
    case = qyq_case
    other_vid = "vid-G331KArPXnI"
    answer = case["entry"]["bullets"][f"{BULLET_NOTE}#b1"]["answers"][other_vid]
    answer.update(decision="corrected", title="Already supported target", titles=["Already supported target"])
    ri._write_json(case["state_path"], {"notes": {NOTE: case["entry"]}, "batches": {}})

    plan = _plan(case, bullets=["b1"])

    assert plan["preserved_answers"][f"{BULLET_NOTE}#b1"][other_vid] == answer


@pytest.mark.parametrize("target,bullets,video", [
    ("another rejected title", RETRY_IDS, VIDEO),
    (TARGET, ["b999"], VIDEO),
    (TARGET, RETRY_IDS, "vid-other"),
])
def test_retry_rejects_wrong_target_bullet_or_source(qyq_case, target, bullets, video):
    with pytest.raises(ri.HarnessError):
        sc.retry_rejected_plan(qyq_case["staging"], qyq_case["vault"], NOTE, video, target, bullets)


def test_retry_rejects_source_answer_that_is_no_longer_target_rejected(qyq_case):
    case = qyq_case
    answer = case["entry"]["bullets"][f"{BULLET_NOTE}#b7"]["answers"][VIDEO]
    answer.update(decision="corrected", title="Published", titles=["Published"])
    ri._write_json(case["state_path"], {"notes": {NOTE: case["entry"]}, "batches": {}})

    with pytest.raises(ri.HarnessError, match="target-rejected"):
        _plan(case)


@pytest.mark.parametrize("stale", ["source", "vault", "state"])
def test_retry_apply_rejects_stale_preview_guard_without_writes(qyq_case, stale):
    case = qyq_case
    preview = _plan(case)
    path = {"archive": case["archive"], "source": case["source"], "vault": case["note_path"],
            "state": case["state_path"]}[stale]
    if stale == "state":
        state = ri._read_json(path, {})
        state["notes"][NOTE]["test_revision"] = "a valid intervening edit"
        ri._write_json(path, state)
    else:
        path.write_bytes(path.read_bytes() + b"\n")
    before_staging = _tree_hash(case["staging"])
    before_vault = _tree_hash(case["vault"])

    with pytest.raises(ri.HarnessError):
        sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, VIDEO, TARGET, RETRY_IDS,
                                expected=preview["guards"])
    assert _tree_hash(case["staging"]) == before_staging
    assert _tree_hash(case["vault"]) == before_vault


def test_retry_preview_rejects_archive_that_does_not_match_recorded_original(qyq_case):
    case = qyq_case
    case["archive"].write_bytes(case["archive"].read_bytes() + b"changed\n")

    with pytest.raises(ri.HarnessError, match="archive"):
        _plan(case)


def test_retry_rejects_duplicate_active_batch(qyq_case):
    case = qyq_case
    state = ri._read_json(case["state_path"], {})
    state["batches"]["active"] = {"status": "waiting", "bullets": {NOTE: [f"{BULLET_NOTE}#b7"]}, "vid": VIDEO}
    ri._write_json(case["state_path"], state)

    with pytest.raises(ri.HarnessError, match="active repair batch"):
        _plan(case)


def test_retry_rejects_pending_review_for_target(qyq_case):
    case = qyq_case
    pdir = case["staging"] / "pending-review" / "20261006T000000Z-1--unit"
    pdir.mkdir(parents=True)
    ri._write_json(pdir / "meta.json", {"notes": [TARGET], "reasons": {TARGET: "waiting-for-reply"}})

    with pytest.raises(ri.HarnessError, match="pending review"):
        _plan(case)


def test_retry_enqueue_survives_normal_repair_planner_and_next_batch(qyq_case):
    case = qyq_case
    before = copy.deepcopy(case["entry"]["bullets"])
    preview = _plan(case)
    queued = sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, VIDEO, TARGET, RETRY_IDS,
                                     expected=preview["guards"])
    state = ri._read_json(case["state_path"], {})
    batch = state["batches"][queued["batch"]]
    assert batch["status"] == "running"
    assert batch["bullets"] == {NOTE: [f"{BULLET_NOTE}#{bid}" for bid in RETRY_IDS]}
    assert state["notes"][NOTE]["bullets"] == before

    # repair_loop.sh runs repair-groups before next-batch. A normal non-force planning pass
    # may skip the done/superseded note, while retaining the explicit queued batch.
    sc.repair_groups(case["staging"], case["vault"], [NOTE], False, case["state_path"])
    handed = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    assert handed["key"] == queued["batch"]
    assert handed["retry_rejected"]["bullets"] == batch["bullets"][NOTE]
    assert handed["notes"] == [NOTE]
    assert handed["vid"] == VIDEO


def test_retry_rechecks_source_before_unit_and_appends_rejection_history(qyq_case):
    case = qyq_case
    preview = _plan(case, bullets=["b1"])
    queued = sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, VIDEO, TARGET, ["b1"],
                                     expected=preview["guards"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    case["source"].write_text("changed canonical source\n", encoding="utf-8")
    with pytest.raises(ri.HarnessError, match="source_sha256"):
        sc._validate_queued_retry(case["staging"], case["vault"], batch["retry_rejected"])

    # Restore the exact previewed source and exercise answer recording with an independent
    # supported target. The old rejected answer remains in append-only history.
    case["source"].write_text("canonical qYQ test transcript\n", encoding="utf-8")
    batch["key"] = queued["batch"]
    bid = f"{BULLET_NOTE}#b1"
    sc.record_answers(case["state_path"], batch, {
        "bullets": {bid: {"decision": "corrected", "title": "Retry target", "titles": ["Retry target"]}},
        "written": ["Retry target"], "applied": [],
    })
    after = ri._read_json(case["state_path"], {})["notes"][NOTE]["bullets"][bid]
    assert after["answers"][VIDEO]["decision"] == "corrected"
    assert after["answer_history"][0]["phase"] == "before-retry"
    assert after["answer_history"][0]["answer"]["decision"] == "target-rejected"


def test_retry_request_salt_bypasses_old_rejected_cache_and_is_stable(tmp_path, monkeypatch):
    unit_dir = tmp_path / "runs" / "r1" / "units" / "u1"
    unit_dir.mkdir(parents=True)
    cache_dir = tmp_path / "repair-cache"
    cache_dir.mkdir()
    messages = [{"role": "user", "content": "unchanged qYQ request"}]
    settings = {"model": "test-model", "max_tokens": 100, "reasoning": None,
                "request_salt": "retry-rejected:retry-a91f"}
    old_parts = [messages, settings["model"], settings["max_tokens"], settings["reasoning"]]
    old_key = hashlib.sha256(json.dumps(old_parts, sort_keys=True).encode()).hexdigest()[:32]
    old_cache = cache_dir / f"{old_key}.json"
    ri._write_json(old_cache, {"content": "old rejected answer", "finish": "stop"})
    calls = []

    def fake_call(_unit_dir, name, msgs, cfg):
        request_key = sc.request_key(msgs, cfg)
        calls.append(request_key)
        (_unit_dir / "calls").mkdir(exist_ok=True)
        ri._write_json(_unit_dir / "calls" / f"{name}.json", {"exchange_key": request_key, "worker": "worker-a"})
        return "=====BULLETS=====\nvalid fresh retry", "stop"

    monkeypatch.setattr(sc, "call", fake_call)
    monkeypatch.setattr(sc, "parse_reply", lambda *_: {"notes": [{}], "keeps": {}, "bullets": {}})
    monkeypatch.setitem(sc._REQ_CACHE, "dir", str(cache_dir))

    first = sc._cached_call(unit_dir, "repair-0", messages, settings)
    second = sc._cached_call(unit_dir, "repair-0", messages, settings)

    assert first == second == ("=====BULLETS=====\nvalid fresh retry", "stop")
    assert len(calls) == 1
    assert calls[0] != old_key
    assert calls[0] == sc.request_key(messages, settings)
    assert old_cache.read_text(encoding="utf-8")
    assert len(list(cache_dir.glob("*.json"))) == 2


def test_failed_retry_handouts_preserve_done_parent_and_allow_fresh_preview(qyq_case):
    case = qyq_case
    preview = _plan(case, bullets=["b7"])
    queued = sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, VIDEO, TARGET, ["b7"],
                                     expected=preview["guards"])
    initial = ri._read_json(case["state_path"], {})["notes"][NOTE]
    original_answers = copy.deepcopy(initial["bullets"])
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"])["key"] == queued["batch"]
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"])["key"] == queued["batch"]
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"]) is None

    state = ri._read_json(case["state_path"], {})
    assert state["notes"][NOTE]["status"] == "done"
    assert state["notes"][NOTE]["bullets"] == original_answers
    failed = state["batches"][queued["batch"]]
    assert failed["status"] == "failed"
    assert failed["retry_failure"]

    fresh = _plan(case, bullets=["b7"])
    second = sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, VIDEO, TARGET, ["b7"],
                                     expected=fresh["guards"])
    assert second["batch"] != queued["batch"]


def test_rejected_retry_result_keeps_both_prior_and_retry_answer_history(qyq_case):
    case = qyq_case
    preview = _plan(case, bullets=["b1"])
    queued = sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, VIDEO, TARGET, ["b1"],
                                     expected=preview["guards"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    batch["key"] = queued["batch"]
    bid = f"{BULLET_NOTE}#b1"
    sc.record_answers(case["state_path"], batch, {
        "bullets": {bid: {"decision": "corrected", "title": TARGET, "titles": [TARGET]}},
        "written": [TARGET], "applied": [],
    })

    unit_dir = case["staging"] / "runs" / "retry-run" / "units" / "retry-unit"
    unit_dir.mkdir(parents=True)
    ri._write_json(unit_dir / "unit.json", {"repair_video": VIDEO,
                  "retry_rejected": batch["retry_rejected"]})
    ri._write_json(unit_dir / "repair_map.json", {NOTE: {"targets": [TARGET]}})
    ri._write_json(unit_dir / "claims.json", {"bullets": {bid: {"decision": "corrected", "titles": [TARGET]}}})
    ri.reopen_rejected_bullets(case["staging"], unit_dir, {TARGET})
    after = ri._read_json(case["state_path"], {})["notes"][NOTE]["bullets"][bid]
    assert after["answers"][VIDEO]["decision"] == "target-rejected"
    assert [h["phase"] for h in after["answer_history"]] == ["before-retry", "retry-result-quarantined"]
    assert [h["answer"]["decision"] for h in after["answer_history"]] == ["target-rejected", "corrected"]


def test_retry_through_normal_repair_unit_emits_fresh_stable_files_request(qyq_case, monkeypatch):
    case = qyq_case
    monkeypatch.setenv("ZR_LLM_BACKEND", "files")
    monkeypatch.setenv("ZR_STATE_DIR", str(case["staging"] / "state-home"))
    preview = _plan(case)
    queued = sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, VIDEO, TARGET, RETRY_IDS,
                                     expected=preview["guards"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    run_dir = case["staging"] / "runs" / "retry-run"
    unit_dir = run_dir / "units" / "retry-unit-1"
    unit_dir.mkdir(parents=True)
    ri._write_json(unit_dir / "batch.json", batch)
    ri._write_json(unit_dir / "unit.json", {"id": unit_dir.name})

    first = sc.repair_unit(case["staging"], case["vault"], unit_dir, batch["vid"], batch["notes"],
                           batch["unknown"], batch["bullets"], batch["prior_titles"], batch["last"],
                           batch["prior_bullets"], batch["video_notes"], batch["retry_rejected"])
    assert first.get("stopped") == "waiting-for-reply"
    requests = list((case["staging"] / "exchange" / "requests").glob("*.json"))
    assert len(requests) == 1
    request_path = requests[0]
    request = ri._read_json(request_path, {})
    user = request["user"]
    archive_text = case["archive"].read_text(encoding="utf-8")
    assert archive_text in user
    assert all(f"{BULLET_NOTE}#{bid}" in user for bid in RETRY_IDS)
    assert "canonical qYQ test transcript" in user
    assert request["key"] == sc.request_key(
        [{"role": "system", "content": request["system"]}, {"role": "user", "content": user}],
        {**sc.settings("repair"), "request_salt": f"retry-rejected:{queued['batch']}"})
    old_key = sc.request_key(
        [{"role": "system", "content": request["system"]}, {"role": "user", "content": user}],
        sc.settings("repair"))
    assert request["key"] != old_key

    cache_dir = case["staging"] / "repair-cache"
    cache_dir.mkdir(exist_ok=True)
    ri._write_json(cache_dir / f"{old_key}.json", {"content": "=====KEEP-NEEDS-REPAIR: old rejected cached reply=====\n", "finish": "stop"})
    unit2 = run_dir / "units" / "retry-unit-2"
    unit2.mkdir()
    ri._write_json(unit2 / "batch.json", batch)
    ri._write_json(unit2 / "unit.json", {"id": unit2.name})
    second = sc.repair_unit(case["staging"], case["vault"], unit2, batch["vid"], batch["notes"],
                            batch["unknown"], batch["bullets"], batch["prior_titles"], batch["last"],
                            batch["prior_bullets"], batch["video_notes"], batch["retry_rejected"])
    assert second.get("stopped") == "waiting-for-reply"
    assert len(list((case["staging"] / "exchange" / "requests").glob("*.json"))) == 1
    assert request["key"] in second["detail"]
    assert not (unit2 / "calls" / "repair-0.json").exists()
    assert (cache_dir / f"{old_key}.json").is_file()
