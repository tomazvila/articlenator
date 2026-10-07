"""Offline coverage for `retry-open`: the guarded retry of open `unaccounted` and
`evidence removed` answers (same preview/apply flow as `retry-rejected`)."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
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
NAME = Path(NOTE).name
V_REJ = "vid-qYQHJEH7zMA"
V_UNACC = "vid-yM5j7jHhKLA"   # b1: recorded `unaccounted` (real fixture answer)
V_OTHER = "vid-G331KArPXnI"
WAS_TITLE = "Radoslav Radev Evidence Removed Target"
IDS = ["b1", "b7", "b10"]


@pytest.fixture
def case(tmp_path: Path):
    staging = tmp_path / "staging"
    vault = tmp_path / "vault" / "Video Transcripts Zettelkasten"
    archive = staging / "retired" / "20261005T074358Z-1" / "20261005T074359-pending-unit-1" / NOTE
    archive.parent.mkdir(parents=True)
    shutil.copyfile(FIX / "qyq_original.md", archive)
    (staging / "lit").mkdir(parents=True)
    sources = {}
    for vid in (V_REJ, V_UNACC, V_OTHER):
        sources[vid] = staging / "lit" / f"{vid}.md"
        sources[vid].write_text(f"canonical transcript of {vid}\n", encoding="utf-8")
    note_path = vault / NOTE
    note_path.parent.mkdir(parents=True)
    entry = copy.deepcopy(json.loads((FIX / "qyq_parent_entry.json").read_text(encoding="utf-8"))["entry"])
    # b7 / V_REJ: a fix removed the Evidence; b10 / V_REJ: plain unaccounted (no `was`)
    entry["bullets"][f"{NAME}#b7"]["answers"][V_REJ] = {
        "decision": "unverified", "why": "a fix removed the Evidence that carried the bullet",
        "was": {"decision": "corrected", "title": WAS_TITLE, "titles": [WAS_TITLE]}}
    entry["bullets"][f"{NAME}#b10"]["answers"][V_REJ] = {"decision": "unaccounted"}
    stub = ("---\ntype: permanent note\nstatus: superseded\nverification: superseded\nsuperseded_pending:\n"
            + "".join(f'  - "bullet: {NAME}#{b}"\n' for b in IDS) + "---\n\n# Original parent note\n")
    note_path.write_text(stub, encoding="utf-8")
    state_path = staging / "repair_state.json"
    ri._write_json(state_path, {"notes": {NOTE: entry}, "batches": {}})
    (staging / ".driver.lock").touch()
    (staging / ".statelock").touch()
    return {"staging": staging, "vault": vault, "archive": archive, "sources": sources,
            "note_path": note_path, "state_path": state_path, "entry": entry}


def _plan(case, kind, video, bullets, target=""):
    return sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, video, target, bullets, kind=kind)


def _apply(case, kind, video, bullets, target="", plan=None):
    plan = plan or _plan(case, kind, video, bullets, target)
    return sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, video, target, bullets,
                                   expected=plan["guards"], kind=kind)


def _tree_hash(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _mutate_state(case, fn):
    st = ri._read_json(case["state_path"], {})
    fn(st["notes"][NOTE])
    ri._write_json(case["state_path"], st)


def test_unaccounted_preview_is_read_only_and_exact(case):
    before_s, before_v = _tree_hash(case["staging"]), _tree_hash(case["vault"])
    plan = _plan(case, "unaccounted", V_UNACC, ["b1"])
    assert plan["kind"] == "unaccounted"
    assert plan["target"] == ""
    assert plan["bullets"] == ["b1"]
    assert plan["entry_status"] == "done"
    assert plan["rejections"]["b1"]["decision"] == "unaccounted"
    assert plan["archive_sha256"] == case["entry"]["sha"]
    assert plan["source_sha256"] == hashlib.sha256(case["sources"][V_UNACC].read_bytes()).hexdigest()
    # other-source answers are listed as preserved, byte for byte
    assert plan["preserved_answers"][f"{NAME}#b1"][V_REJ]["decision"] == "target-rejected"
    assert set(plan["preserved_answers"][f"{NAME}#b1"]) == {V_OTHER, V_REJ}
    assert _tree_hash(case["staging"]) == before_s
    assert _tree_hash(case["vault"]) == before_v


def test_unaccounted_without_was_and_evidence_removed_preview(case):
    p1 = _plan(case, "unaccounted", V_REJ, ["b10"])
    assert p1["named_targets"] == []
    p2 = _plan(case, "evidence-removed", V_REJ, ["b7"])
    assert p2["named_targets"] == [WAS_TITLE]
    assert p2["target"] == ""
    p3 = _plan(case, "evidence-removed", V_REJ, ["b7"], target=WAS_TITLE)
    assert p3["target"] == WAS_TITLE
    assert p3["guards"] == p2["guards"]  # the guards cover content, not the label


@pytest.mark.parametrize("kind,video,bullets,target", [
    ("unaccounted", V_REJ, ["b7"], ""),             # evidence-removed answer is not unaccounted
    ("evidence-removed", V_UNACC, ["b1"], ""),      # unaccounted answer is not evidence-removed
    ("evidence-removed", V_REJ, ["b7", "b10"], ""),  # one bullet of the set is the wrong form
    ("unaccounted", V_REJ, ["b1"], ""),             # that answer is target-rejected
    ("evidence-removed", V_REJ, ["b7"], "Some Other Title"),  # target not named
    ("unaccounted", V_REJ, ["b10"], "Some Title"),  # no named target exists
    ("unaccounted", V_UNACC, ["b999"], ""),
    ("unaccounted", "vid-nope", ["b1"], ""),
    ("bogus-kind", V_UNACC, ["b1"], ""),
])
def test_open_retry_refused_cases(case, kind, video, bullets, target):
    with pytest.raises(ri.HarnessError):
        _plan(case, kind, video, bullets, target)


def test_unverified_claim_line_and_other_open_forms_are_not_accepted(case):
    _mutate_state(case, lambda e: e["bullets"][f"{NAME}#b10"]["answers"].__setitem__(
        V_REJ, {"decision": "unverified", "why": "the claim line that carried the bullet changed",
                "was": {"decision": "corrected", "titles": ["x"]}}))
    with pytest.raises(ri.HarnessError, match="not an open"):
        _plan(case, "evidence-removed", V_REJ, ["b10"])
    with pytest.raises(ri.HarnessError, match="not an open"):
        _plan(case, "unaccounted", V_REJ, ["b10"])


@pytest.mark.parametrize("status", ["operator", "needs-repair", "open", "done-unrepaired", "failed"])
def test_open_retry_requires_done_entry(case, status):
    _mutate_state(case, lambda e: e.__setitem__("status", status))
    with pytest.raises(ri.HarnessError, match="only accepts a done repair entry"):
        _plan(case, "unaccounted", V_UNACC, ["b1"])


def test_open_retry_requires_superseded_stub_and_pending_bullet(case):
    text = case["note_path"].read_text(encoding="utf-8")
    case["note_path"].write_text(text.replace(f'  - "bullet: {NAME}#b1"\n', ""), encoding="utf-8")
    with pytest.raises(ri.HarnessError, match="superseded_pending"):
        _plan(case, "unaccounted", V_UNACC, ["b1"])
    case["note_path"].write_text(text.replace("status: superseded", "status: published"), encoding="utf-8")
    with pytest.raises(ri.HarnessError, match="superseded vault stub"):
        _plan(case, "unaccounted", V_UNACC, ["b1"])


def test_open_retry_requires_matching_full_archive(case):
    case["archive"].write_bytes(case["archive"].read_bytes() + b"changed\n")
    with pytest.raises(ri.HarnessError, match="archive"):
        _plan(case, "unaccounted", V_UNACC, ["b1"])


def test_open_retry_refused_when_another_source_already_settles_the_bullet(case, monkeypatch):
    _mutate_state(case, lambda e: e["bullets"][f"{NAME}#b1"]["answers"].__setitem__(
        V_OTHER, {"decision": "corrected", "title": "Published", "titles": ["Published"]}))
    monkeypatch.setattr(ri, "_final_targets", lambda _zk, title, depth=0: [title])
    with pytest.raises(ri.HarnessError, match="another answer already settles"):
        _plan(case, "unaccounted", V_UNACC, ["b1"])


def test_open_retry_conflicts_active_batch_pending_review_and_named_target(case):
    st = ri._read_json(case["state_path"], {})
    st["batches"]["active"] = {"status": "running", "bullets": {NOTE: [f"{NAME}#b10"]}, "vid": V_REJ}
    ri._write_json(case["state_path"], st)
    with pytest.raises(ri.HarnessError, match="active repair batch"):
        _plan(case, "unaccounted", V_REJ, ["b10"])
    st["batches"].clear()
    ri._write_json(case["state_path"], st)
    pdir = case["staging"] / "pending-review" / "20261006T000000Z-1--unit"
    pdir.mkdir(parents=True)
    # the pending review touches the title that the `evidence removed` answer names (no --target given)
    ri._write_json(pdir / "meta.json", {"notes": [WAS_TITLE], "reasons": {}})
    with pytest.raises(ri.HarnessError, match="pending review"):
        _plan(case, "evidence-removed", V_REJ, ["b7"])


@pytest.mark.parametrize("stale", ["source", "vault", "state"])
def test_open_apply_rejects_stale_guard_without_writes(case, stale):
    plan = _plan(case, "unaccounted", V_UNACC, ["b1"])
    path = {"source": case["sources"][V_UNACC], "vault": case["note_path"], "state": case["state_path"]}[stale]
    if stale == "state":
        st = ri._read_json(path, {})
        st["notes"][NOTE]["revision"] = "intervening edit"
        ri._write_json(path, st)
    else:
        path.write_bytes(path.read_bytes() + b"\n")
    before_s, before_v = _tree_hash(case["staging"]), _tree_hash(case["vault"])
    with pytest.raises(ri.HarnessError):
        _apply(case, "unaccounted", V_UNACC, ["b1"], plan=plan)
    assert _tree_hash(case["staging"]) == before_s
    assert _tree_hash(case["vault"]) == before_v


def test_open_apply_queues_batch_with_kind_and_one_active_batch_per_note(case):
    before_bullets = copy.deepcopy(case["entry"]["bullets"])
    queued = _apply(case, "unaccounted", V_UNACC, ["b1"])
    assert queued["kind"] == "unaccounted"
    st = ri._read_json(case["state_path"], {})
    batch = st["batches"][queued["batch"]]
    assert batch["status"] == "running"
    assert batch["vid"] == V_UNACC
    assert batch["bullets"] == {NOTE: [f"{NAME}#b1"]}
    assert batch["retry_rejected"]["kind"] == "unaccounted"
    assert batch["retry_rejected"]["target"] == ""
    assert "state_sha256" not in batch["retry_rejected"]["guards"]
    assert st["notes"][NOTE]["bullets"] == before_bullets      # nothing recorded yet
    # no second retry for the same note and video while the first batch is active
    with pytest.raises(ri.HarnessError, match="active repair batch"):
        _plan(case, "unaccounted", V_UNACC, ["b1"])
    with pytest.raises(ri.HarnessError, match="active repair batch"):
        _plan(case, "unaccounted", V_UNACC, ["b1"])
    # the normal planner and next-batch hand it out
    sc.repair_groups(case["staging"], case["vault"], [NOTE], False, case["state_path"])
    handed = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    assert handed["key"] == queued["batch"]
    assert handed["retry_rejected"]["kind"] == "unaccounted"


def test_rejected_kind_batch_and_key_are_unchanged_by_the_extension(case):
    """A target-rejected retry carries no `kind`; its key seed has no kind part."""
    # make b1 target-rejected for a title and queue the old kind
    title = "Old Rejected Title"
    _mutate_state(case, lambda e: e["bullets"][f"{NAME}#b1"]["answers"].__setitem__(
        V_REJ, {"decision": "target-rejected", "titles": [title], "was": "corrected"}))
    p = sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, V_REJ, title, ["b1"])
    assert "kind" not in p and "named_targets" not in p
    q = sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, V_REJ, title, ["b1"], expected=p["guards"])
    assert "kind" not in q
    batch = ri._read_json(case["state_path"], {})["batches"][q["batch"]]
    assert "kind" not in batch["retry_rejected"]
    seed = json.dumps([p["guards"]["state_sha256"], V_REJ, title, [f"{NAME}#b1"]],
                      ensure_ascii=False, separators=(",", ":"))
    assert q["batch"] == "retry-" + hashlib.sha256(seed.encode()).hexdigest()[:16]
    # the old kind refuses an `unaccounted` answer (exact old behavior)
    with pytest.raises(ri.HarnessError):
        sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, V_UNACC, "x", ["b1"])


@pytest.mark.parametrize("what", ["source", "answer", "vault", "archive_state"])
def test_open_handout_revalidates_guards(case, what):
    queued = _apply(case, "evidence-removed", V_REJ, ["b7"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    assert batch["key"] == queued["batch"]
    retry = batch["retry_rejected"]
    sc._validate_queued_retry(case["staging"], case["vault"], retry)   # still valid
    if what == "source":
        case["sources"][V_REJ].write_text("changed\n", encoding="utf-8")
        match = "source_sha256"
    elif what == "answer":
        _mutate_state(case, lambda e: e["bullets"][f"{NAME}#b7"]["answers"].__setitem__(
            V_REJ, {"decision": "dropped", "reason": "not in this transcript"}))
        match = "not an open"
    elif what == "vault":
        case["note_path"].write_text(case["note_path"].read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
        match = "vault_sha256"
    else:
        _mutate_state(case, lambda e: e.__setitem__("status", "needs-repair"))
        match = "done repair entry"
    with pytest.raises(ri.HarnessError, match=match):
        sc._validate_queued_retry(case["staging"], case["vault"], retry)


def test_open_handout_rejects_unknown_kind_in_queued_dict(case):
    queued = _apply(case, "unaccounted", V_UNACC, ["b1"])
    retry = dict(sc.next_batch(case["staging"], case["vault"], case["state_path"])["retry_rejected"],
                 kind="anything")
    assert queued
    with pytest.raises(ri.HarnessError, match="unknown retry kind"):
        sc._validate_queued_retry(case["staging"], case["vault"], retry)


@pytest.mark.parametrize("kind,video,bid,prior", [
    ("unaccounted", V_UNACC, "b1", "unaccounted"),
    ("evidence-removed", V_REJ, "b7", "unverified"),
])
def test_open_retry_result_keeps_prior_answer_in_history_and_other_sources(case, kind, video, bid, prior):
    full = f"{NAME}#{bid}"
    others_before = {v: copy.deepcopy(a) for v, a in case["entry"]["bullets"][full]["answers"].items() if v != video}
    prior_answer = copy.deepcopy(case["entry"]["bullets"][full]["answers"][video])
    queued = _apply(case, kind, video, [bid])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    batch["key"] = queued["batch"]
    sc.record_answers(case["state_path"], batch, {
        "bullets": {full: {"decision": "corrected", "title": "Fresh Target", "titles": ["Fresh Target"]}},
        "written": ["Fresh Target"], "applied": []})
    after = ri._read_json(case["state_path"], {})["notes"][NOTE]["bullets"][full]
    assert after["answers"][video]["decision"] == "corrected"
    assert {v: a for v, a in after["answers"].items() if v != video} == others_before
    hist = after["answer_history"]
    assert [h["phase"] for h in hist] == ["before-retry"]
    assert hist[0]["answer"] == prior_answer
    assert hist[0]["answer"]["decision"] == prior
    assert hist[0]["retry_id"] == queued["batch"]


def test_open_retry_unaccounted_again_stays_honestly_open(case):
    queued = _apply(case, "unaccounted", V_UNACC, ["b1"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    batch["key"] = queued["batch"]
    full = f"{NAME}#b1"
    sc.record_answers(case["state_path"], batch, {"bullets": {}, "written": [], "applied": []})
    st = ri._read_json(case["state_path"], {})
    b = st["notes"][NOTE]["bullets"][full]
    assert b["answers"][V_UNACC] == {"decision": "unaccounted"}
    assert b["answer_history"][0]["phase"] == "before-retry"
    status = ri._bullet_status(case["vault"], st["notes"][NOTE], full, set())[0]
    assert status.startswith("open:")


@pytest.mark.parametrize("kind,video,bid", [("unaccounted", V_UNACC, "b1"), ("evidence-removed", V_REJ, "b7")])
def test_open_retry_quarantined_result_returns_bullet_to_honest_open_state(case, kind, video, bid):
    full = f"{NAME}#{bid}"
    prior_answer = copy.deepcopy(case["entry"]["bullets"][full]["answers"][video])
    queued = _apply(case, kind, video, [bid])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    batch["key"] = queued["batch"]
    result_title = "Retry Result Target"
    sc.record_answers(case["state_path"], batch, {
        "bullets": {full: {"decision": "corrected", "title": result_title, "titles": [result_title]}},
        "written": [result_title], "applied": []})
    unit_dir = case["staging"] / "runs" / "r" / "units" / "u"
    unit_dir.mkdir(parents=True)
    ri._write_json(unit_dir / "unit.json", {"repair_video": video, "retry_rejected": batch["retry_rejected"]})
    ri._write_json(unit_dir / "repair_map.json", {NOTE: {"targets": [result_title]}})
    ri._write_json(unit_dir / "claims.json", {"bullets": {full: {"decision": "corrected", "titles": [result_title]}}})
    ri.reopen_rejected_bullets(case["staging"], unit_dir, {result_title})
    st = ri._read_json(case["state_path"], {})
    after = st["notes"][NOTE]["bullets"][full]
    assert after["answers"][video]["decision"] == "target-rejected"
    assert [h["phase"] for h in after["answer_history"]] == ["before-retry", "retry-result-quarantined"]
    assert after["answer_history"][0]["answer"] == prior_answer
    assert after["answer_history"][1]["answer"]["decision"] == "corrected"
    assert st["notes"][NOTE]["status"] == "done"
    assert ri._bullet_status(case["vault"], st["notes"][NOTE], full, set())[0].startswith("open:")


def test_open_retry_failed_twice_keeps_done_entry_and_allows_fresh_preview(case):
    queued = _apply(case, "unaccounted", V_REJ, ["b10"])
    answers = copy.deepcopy(ri._read_json(case["state_path"], {})["notes"][NOTE]["bullets"])
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"])["key"] == queued["batch"]
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"])["key"] == queued["batch"]
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"]) is None
    st = ri._read_json(case["state_path"], {})
    assert st["notes"][NOTE]["status"] == "done"
    assert st["notes"][NOTE]["bullets"] == answers
    assert st["batches"][queued["batch"]]["status"] == "failed"
    assert st["batches"][queued["batch"]]["retry_failure"]
    again = _apply(case, "unaccounted", V_REJ, ["b10"])
    assert again["batch"] != queued["batch"]


def test_open_retry_salt_is_stable_and_differs_from_rejected_salt(case, monkeypatch):
    monkeypatch.setenv("ZR_LLM_BACKEND", "files")
    monkeypatch.setenv("ZR_STATE_DIR", str(case["staging"] / "state-home"))
    queued = _apply(case, "unaccounted", V_UNACC, ["b1"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    run_dir = case["staging"] / "runs" / "retry-run"
    keys = []
    for n in (1, 2):
        unit_dir = run_dir / "units" / f"u{n}"
        unit_dir.mkdir(parents=True)
        ri._write_json(unit_dir / "batch.json", batch)
        ri._write_json(unit_dir / "unit.json", {"id": unit_dir.name})
        res = sc.repair_unit(case["staging"], case["vault"], unit_dir, batch["vid"], batch["notes"],
                             batch["unknown"], batch["bullets"], batch["prior_titles"], batch["last"],
                             batch["prior_bullets"], batch["video_notes"], batch["retry_rejected"])
        assert res.get("stopped") == "waiting-for-reply"
        reqs = list((case["staging"] / "exchange" / "requests").glob("*.json"))
        keys.append({p.name for p in reqs})
    assert keys[0] == keys[1] and len(keys[0]) == 1          # replay hits the same request
    request = ri._read_json(next(iter((case["staging"] / "exchange" / "requests").glob("*.json"))), {})
    msgs = [{"role": "system", "content": request["system"]}, {"role": "user", "content": request["user"]}]
    assert request["key"] == sc.request_key(msgs, {**sc.settings("repair"),
                                                   "request_salt": f"retry-open:{queued['batch']}"})
    assert request["key"] != sc.request_key(msgs, sc.settings("repair"))
    assert request["key"] != sc.request_key(msgs, {**sc.settings("repair"),
                                                   "request_salt": f"retry-rejected:{queued['batch']}"})
    assert f"{NAME}#b1" in request["user"]
    assert case["archive"].read_text(encoding="utf-8") in request["user"]


def _cli(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sc.main(list(args))
    return rc, buf.getvalue()


def test_retry_open_cli_preview_then_apply_needs_all_guards(case):
    base = ["retry-open", "--staging", str(case["staging"]), "--vault", str(case["vault"]),
            "--note", NOTE, "--video", V_UNACC, "--form", "unaccounted", "--bullet", "b1"]
    rc, out = _cli(*base)
    assert rc == 0
    plan = json.loads(out)
    assert plan["kind"] == "unaccounted"
    g = plan["guards"]
    rc, _out = _cli(*base, "--apply")
    assert rc != 0
    assert not ri._read_json(case["state_path"], {})["batches"]
    flags = ["--apply", "--expect-state", g["state_sha256"], "--expect-entry", g["entry_sha256"],
             "--expect-vault", g["vault_sha256"], "--expect-archive", g["archive_sha256"],
             "--expect-source", g["source_sha256"], "--expect-source-path", g["source_path"]]
    rc, out = _cli(*base, *flags)
    assert rc == 0
    assert json.loads(out)["queued"] is True
    with pytest.raises(SystemExit):
        _cli(*base[:-4], "--form", "target-rejected", "--bullet", "b1")
