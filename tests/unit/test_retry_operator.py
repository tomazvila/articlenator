"""Offline coverage for `--entry-state` of `retry-open` / `retry-rejected`: the guarded retry of exact
open bullets of a repair entry that ended `needs-repair` or `operator` (the vault still holds the
UNCHANGED old note, no stub). Same preview/apply flow, other guards (see `retry_rejected_plan`).
The last tests drive the real repair unit, review and finalize with fake LLM scripts."""
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
import verify_claims as vc  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "retry_rejected"
NOTE = "01 Permanent Notes/Correct Muscle Activation Patterns Matter More Than Raw General Strength For Planche Performance.md"
NAME = Path(NOTE).name
V_REJ = "vid-qYQHJEH7zMA"    # b1 / b7 / b10: target-rejected
V_UNACC = "vid-yM5j7jHhKLA"  # b1: unaccounted
V_OTHER = "vid-G331KArPXnI"
WHY = "needs-repair: no published target, open bullets (old note unchanged)"
REJ_TITLE = "Old Rejected Title"


@pytest.fixture(params=["needs-repair", "operator"])
def case(request, tmp_path: Path):
    state_name = request.param
    staging = tmp_path / "staging"
    vault = tmp_path / "vault" / "Video Transcripts Zettelkasten"
    (staging / "lit").mkdir(parents=True)
    sources = {}
    for vid in (V_REJ, V_UNACC, V_OTHER):
        sources[vid] = staging / "lit" / f"{vid}.md"
        sources[vid].write_text(f"canonical transcript of {vid}\n", encoding="utf-8")
    note_path = vault / NOTE
    note_path.parent.mkdir(parents=True)
    original = (FIX / "qyq_original.md").read_bytes()
    note_path.write_bytes(original)  # the UNCHANGED old note: no stub, no archive
    entry = copy.deepcopy(json.loads((FIX / "qyq_parent_entry.json").read_text(encoding="utf-8"))["entry"])
    entry.update(status=state_name, titles=[], pos=len(entry["sources"]), replans=1)
    for key in ("reopened",):
        entry.pop(key, None)
    entry["bullets"][f"{NAME}#b1"]["answers"][V_REJ] = {"decision": "target-rejected", "titles": [REJ_TITLE],
                                                       "was": "corrected"}
    entry["bullets"][f"{NAME}#b10"]["answers"][V_UNACC] = {
        "decision": "unaccounted", "was": {"decision": "kept", "title": "Never Written", "titles": ["Never Written"]},
        "why": "one or more named notes were not written or reusable"}
    # every other bullet of the entry: settled by nothing but dropped (open), as in a real needs-repair entry
    for bid, b in entry["bullets"].items():
        if not bid.endswith(("#b1", "#b7", "#b10")):
            b["answers"] = {v: {"decision": "dropped", "reason": "not in this transcript"} for v in entry["sources"]}
    state_path = staging / "repair_state.json"
    ri._write_json(state_path, {"notes": {NOTE: entry}, "batches": {}})
    ri.needs_operator(staging, [ri.needs_repair_item(staging, NOTE, entry, [f"{NAME}#b1", f"{NAME}#b10"])], WHY)
    (staging / ".driver.lock").touch()
    (staging / ".statelock").touch()
    return {"staging": staging, "vault": vault, "sources": sources, "note_path": note_path, "original": original,
            "state_path": state_path, "entry": entry, "state_name": state_name}


def _plan(case, form, video, bullets, target="", state="same"):
    es = case["state_name"] if state == "same" else state
    if form == "target-rejected":
        return sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, video, target, bullets, entry_state=es)
    return sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, video, target, bullets, kind=form,
                                  entry_state=es)


def _apply(case, form, video, bullets, target="", plan=None):
    plan = plan or _plan(case, form, video, bullets, target)
    kw = {} if form == "target-rejected" else {"kind": form}
    return sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, video, target, bullets,
                                   expected=plan["guards"], entry_state=case["state_name"], **kw)


def _tree_hash(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _mutate_state(case, fn):
    st = ri._read_json(case["state_path"], {})
    fn(st["notes"][NOTE])
    ri._write_json(case["state_path"], st)


def _operator_items(case):
    return [x for x in ri._read_json(case["staging"] / "needs_operator.json", []) if x.get("note") == NOTE]


def _answers(case):
    return ri._read_json(case["state_path"], {})["notes"][NOTE]["bullets"]


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #

def test_preview_is_read_only_and_uses_entry_guards(case):
    before_s, before_v = _tree_hash(case["staging"]), _tree_hash(case["vault"])
    plan = _plan(case, "unaccounted", V_UNACC, ["b10"])
    assert plan["entry_state"] == case["state_name"] and plan["entry_status"] == case["state_name"]
    assert plan["entry_kind"] == "known"
    assert plan["kind"] == "unaccounted" and plan["named_targets"] == ["Never Written"]
    original_sha = hashlib.sha256(case["original"]).hexdigest()
    assert plan["guards"]["vault_sha256"] == plan["guards"]["archive_sha256"] == original_sha == case["entry"]["sha"]
    assert plan["archive_path"] == ""  # no archive exists for an unchanged note: the vault is the original
    assert plan["preserved_answers"][f"{NAME}#b10"][V_REJ]["decision"] == "target-rejected"
    assert _tree_hash(case["staging"]) == before_s and _tree_hash(case["vault"]) == before_v


def test_preview_target_rejected_form(case):
    plan = _plan(case, "target-rejected", V_REJ, ["b1"], REJ_TITLE)
    assert plan["entry_state"] == case["state_name"] and "kind" not in plan
    with pytest.raises(ri.HarnessError, match="not target-rejected for the requested target"):
        _plan(case, "target-rejected", V_REJ, ["b1"], "Other Title")


def test_preview_uses_a_matching_archive_only(case):
    arch = case["staging"] / "retired" / "run" / "unit" / NOTE
    arch.parent.mkdir(parents=True)
    arch.write_bytes(case["original"])
    assert _plan(case, "unaccounted", V_UNACC, ["b10"])["archive_path"] == str(arch)
    arch.write_bytes(case["original"] + b"older edit\n")  # not the recorded original: ignored, not trusted
    assert _plan(case, "unaccounted", V_UNACC, ["b10"])["archive_path"] == ""


# --------------------------------------------------------------------------- #
# Refused cases
# --------------------------------------------------------------------------- #

def test_entry_state_is_never_inferred_and_must_match(case):
    with pytest.raises(ri.HarnessError, match="only accepts a done repair entry"):
        sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, V_UNACC, "", ["b10"], kind="unaccounted")
    other = "operator" if case["state_name"] == "needs-repair" else "needs-repair"
    with pytest.raises(ri.HarnessError, match=f"needs a {other} repair entry"):
        _plan(case, "unaccounted", V_UNACC, ["b10"], state=other)
    with pytest.raises(ri.HarnessError, match="unknown entry state"):
        _plan(case, "unaccounted", V_UNACC, ["b10"], state="done")


@pytest.mark.parametrize("status", ["done", "open", "failed", "done-unrepaired", "operator-closed", "done-unsupported"])
def test_other_entry_statuses_are_refused(case, status):
    _mutate_state(case, lambda e: e.__setitem__("status", status))
    with pytest.raises(ri.HarnessError, match="repair entry"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])


@pytest.mark.parametrize("form,video,bullets,target", [
    ("unaccounted", V_REJ, ["b1"], ""),        # that answer is target-rejected
    ("unaccounted", V_OTHER, ["b1"], ""),      # b1 answer of V_OTHER is unverified (claim line), not unaccounted
    ("unaccounted", V_UNACC, ["b10", "b7"], ""),  # b7 has no unaccounted answer of V_UNACC: wrong form
    ("evidence-removed", V_UNACC, ["b10"], ""),
    ("unaccounted", V_UNACC, ["b10"], "Some Title"),
    ("unaccounted", V_UNACC, ["b999"], ""),
    ("unaccounted", "vid-nope", ["b10"], ""),
    ("unaccounted", V_UNACC, ["b10", "b10"], ""),
    ("unaccounted", V_UNACC, ["10"], ""),
])
def test_refused_forms_bullets_and_sources(case, form, video, bullets, target):
    with pytest.raises(ri.HarnessError):
        _plan(case, form, video, bullets, target)


def test_video_must_be_a_recorded_source(case):
    sources = [v for v in case["entry"]["sources"] if v != V_UNACC]
    _mutate_state(case, lambda e: (e.__setitem__("sources", sources), e.__setitem__("recorded_sources", sources)))
    with pytest.raises(ri.HarnessError, match="not a recorded source"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])


def test_old_note_bytes_guard(case):
    for data in (case["original"] + b"\n", case["original"].replace(b"planche", b"Planche", 1)):
        case["note_path"].write_bytes(data)
        with pytest.raises(ri.HarnessError, match="not byte-identical"):
            _plan(case, "unaccounted", V_UNACC, ["b10"])
    case["note_path"].write_bytes(case["original"])
    _plan(case, "unaccounted", V_UNACC, ["b10"])  # the exact bytes are accepted again
    case["note_path"].unlink()
    with pytest.raises(ri.HarnessError, match="missing from the vault"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])


def test_a_stub_or_marked_vault_note_is_refused(case):
    stub = ri.repair_stub(Path(NOTE).stem, ["Some Target"])
    case["note_path"].write_text(stub, encoding="utf-8")
    _mutate_state(case, lambda e: e.__setitem__("sha", hashlib.sha256(stub.encode()).hexdigest()))
    with pytest.raises(ri.HarnessError, match="unchanged old note"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])


def test_recorded_bullets_must_match_the_vault_note(case):
    _mutate_state(case, lambda e: e["bullets"][f"{NAME}#b2"].__setitem__("text", "something else"))
    with pytest.raises(ri.HarnessError, match="do not exactly match"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])


def test_refused_when_another_source_already_settles_the_bullet(case, monkeypatch):
    _mutate_state(case, lambda e: e["bullets"][f"{NAME}#b10"]["answers"].__setitem__(
        V_OTHER, {"decision": "corrected", "title": "Published", "titles": ["Published"]}))
    monkeypatch.setattr(ri, "_final_targets", lambda _zk, title, depth=0: [title])
    with pytest.raises(ri.HarnessError, match="another answer already settles"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])
    # the target-rejected form is checked the same way here (the done-entry path does not)
    _mutate_state(case, lambda e: e["bullets"][f"{NAME}#b1"]["answers"].__setitem__(
        V_OTHER, {"decision": "corrected", "title": "Published", "titles": ["Published"]}))
    with pytest.raises(ri.HarnessError, match="another answer already settles"):
        _plan(case, "target-rejected", V_REJ, ["b1"], REJ_TITLE)


def test_refused_when_the_entry_names_a_target_or_the_replan_has_not_run(case):
    _mutate_state(case, lambda e: e.__setitem__("titles", ["A Title"]))
    with pytest.raises(ri.HarnessError, match="already names a target"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])
    _mutate_state(case, lambda e: (e.__setitem__("titles", []), e.__setitem__("linked_titles", ["X"])))
    with pytest.raises(ri.HarnessError, match="already names a target"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])
    _mutate_state(case, lambda e: (e.__setitem__("linked_titles", []), e.__setitem__("replans", 0)))
    if case["state_name"] == "needs-repair":  # the normal one-time re-plan still exists: use it first
        with pytest.raises(ri.HarnessError, match="re-plan"):
            _plan(case, "unaccounted", V_UNACC, ["b10"])
    else:
        _plan(case, "unaccounted", V_UNACC, ["b10"])


def test_refused_with_a_registered_partial_stub(case):
    ri._write_json(case["staging"] / ri.PARTIAL, {NOTE: {"pending_targets": ["T"], "open_bullets": []}})
    with pytest.raises(ri.HarnessError, match="partial stub"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])


def test_one_active_retry_batch_per_note_for_any_video(case):
    _apply(case, "unaccounted", V_UNACC, ["b10"])
    for form, video, bullets, target in [("unaccounted", V_UNACC, ["b10"], ""),
                                         ("target-rejected", V_REJ, ["b1"], REJ_TITLE)]:
        with pytest.raises(ri.HarnessError, match="active repair batch"):
            _plan(case, form, video, bullets, target)


def test_conflicts_with_a_pending_review_of_the_note(case):
    pdir = case["staging"] / "pending-review" / "20261006T000000Z-1--unit"
    pdir.mkdir(parents=True)
    ri._write_json(pdir / "meta.json", {"notes": [NOTE], "reasons": {}})
    with pytest.raises(ri.HarnessError, match="pending review"):
        _plan(case, "unaccounted", V_UNACC, ["b10"])


@pytest.mark.parametrize("stale", ["source", "vault", "state"])
def test_apply_rejects_stale_guard_without_writes(case, stale):
    plan = _plan(case, "unaccounted", V_UNACC, ["b10"])
    path = {"source": case["sources"][V_UNACC], "vault": case["note_path"], "state": case["state_path"]}[stale]
    if stale == "state":
        st = ri._read_json(path, {})
        st["notes"][NOTE]["revision"] = "intervening edit"
        ri._write_json(path, st)
    else:
        path.write_bytes(path.read_bytes() + b"\n")
    before_s, before_v = _tree_hash(case["staging"]), _tree_hash(case["vault"])
    with pytest.raises(ri.HarnessError):
        _apply(case, "unaccounted", V_UNACC, ["b10"], plan=plan)
    assert _tree_hash(case["staging"]) == before_s and _tree_hash(case["vault"]) == before_v


# --------------------------------------------------------------------------- #
# Apply, hand-out, revalidation
# --------------------------------------------------------------------------- #

def test_apply_queues_batch_that_the_planner_hands_out_and_leaves_state_alone(case):
    before = copy.deepcopy(case["entry"]["bullets"])
    queued = _apply(case, "unaccounted", V_UNACC, ["b10"])
    assert queued["entry_state"] == case["state_name"] and queued["kind"] == "unaccounted"
    st = ri._read_json(case["state_path"], {})
    batch = st["batches"][queued["batch"]]
    assert batch["status"] == "running" and batch["last"] == {NOTE: True}
    assert batch["notes"] == [NOTE] and batch["unknown"] == []
    assert batch["retry_rejected"]["entry_state"] == case["state_name"]
    assert "state_sha256" not in batch["retry_rejected"]["guards"]
    assert st["notes"][NOTE]["bullets"] == before and st["notes"][NOTE]["status"] == case["state_name"]
    # repair_loop.sh runs repair-groups first: a needs-repair/operator entry with a replan is not planned again
    plan = sc.repair_groups(case["staging"], case["vault"], [NOTE], False, case["state_path"])
    assert NOTE not in [r for k in plan["known"] for r in k["notes"]]
    assert ri._read_json(case["state_path"], {})["notes"][NOTE]["bullets"] == before
    handed = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    assert handed["key"] == queued["batch"] and handed["retry_rejected"]["entry_state"] == case["state_name"]
    assert ri._read_json(case["state_path"], {})["notes"][NOTE]["status"] == case["state_name"]


def test_batch_key_space_differs_from_the_done_entry_retry(case):
    plan = _plan(case, "unaccounted", V_UNACC, ["b10"])
    queued = _apply(case, "unaccounted", V_UNACC, ["b10"], plan=plan)
    seed = json.dumps([plan["guards"]["state_sha256"], V_UNACC, "", [f"{NAME}#b10"], "unaccounted"],
                      ensure_ascii=False, separators=(",", ":"))
    assert queued["batch"] != "retry-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def test_unknown_kind_entry_keeps_the_caution_flag_split(case):
    _mutate_state(case, lambda e: e.__setitem__("kind", "unknown"))
    queued = _apply(case, "unaccounted", V_UNACC, ["b10"])
    batch = ri._read_json(case["state_path"], {})["batches"][queued["batch"]]
    assert batch["notes"] == [] and batch["unknown"] == [NOTE] and batch["mode"] == "unknown"
    handed = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    plan = sc._validate_queued_retry(case["staging"], case["vault"], handed["retry_rejected"])
    assert plan["entry_kind"] == "unknown"


@pytest.mark.parametrize("what", ["source", "answer", "vault", "status", "titles", "entry_state", "stub", "partial"])
def test_handout_revalidates_every_guard(case, what):
    queued = _apply(case, "unaccounted", V_UNACC, ["b10"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    assert batch["key"] == queued["batch"]
    retry = batch["retry_rejected"]
    sc._validate_queued_retry(case["staging"], case["vault"], retry)  # still valid
    if what == "source":
        case["sources"][V_UNACC].write_text("changed\n", encoding="utf-8")
        match = "source_sha256"
    elif what == "answer":
        _mutate_state(case, lambda e: e["bullets"][f"{NAME}#b10"]["answers"].__setitem__(
            V_UNACC, {"decision": "dropped", "reason": "not in this transcript"}))
        match = "not an open"
    elif what == "vault":
        case["note_path"].write_bytes(case["original"] + b"edit\n")
        match = "not byte-identical"
    elif what == "status":
        _mutate_state(case, lambda e: e.__setitem__("status", "done"))
        match = "repair entry"
    elif what == "titles":
        _mutate_state(case, lambda e: e.__setitem__("titles", ["X"]))
        match = "already names a target"
    elif what == "entry_state":
        retry = dict(retry, entry_state="done")
        match = "unknown entry state"
    elif what == "stub":
        stub = ri.repair_stub(Path(NOTE).stem, ["Some Target"])
        case["note_path"].write_text(stub, encoding="utf-8")
        match = "not byte-identical"
    else:
        ri._write_json(case["staging"] / ri.PARTIAL, {NOTE: {}})
        match = "partial stub"
    with pytest.raises(ri.HarnessError, match=match):
        sc._validate_queued_retry(case["staging"], case["vault"], retry)


def test_a_batch_without_entry_state_never_runs_against_an_operator_entry(case):
    queued = _apply(case, "unaccounted", V_UNACC, ["b10"])
    retry = dict(sc.next_batch(case["staging"], case["vault"], case["state_path"])["retry_rejected"])
    assert queued
    retry.pop("entry_state")  # a stripped marker falls back to the done-entry guards: refused
    with pytest.raises(ri.HarnessError, match="only accepts a done repair entry"):
        sc._validate_queued_retry(case["staging"], case["vault"], retry)


def test_repair_unit_refuses_a_changed_vault_note_before_any_call(case, monkeypatch):
    monkeypatch.setenv("ZR_LLM_BACKEND", "files")
    monkeypatch.setenv("ZR_STATE_DIR", str(case["staging"] / "state-home"))
    _apply(case, "unaccounted", V_UNACC, ["b10"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    case["note_path"].write_bytes(case["original"] + b"edit\n")
    unit = case["staging"] / "runs" / "r" / "units" / "u"
    unit.mkdir(parents=True)
    ri._write_json(unit / "unit.json", {"id": "u"})
    with pytest.raises(ri.HarnessError, match="not byte-identical"):
        sc.repair_unit(case["staging"], case["vault"], unit, batch["vid"], batch["notes"], batch["unknown"],
                       batch["bullets"], batch["prior_titles"], batch["last"], batch["prior_bullets"],
                       batch["video_notes"], batch["retry_rejected"])
    assert not (case["staging"] / "exchange" / "requests").exists()


def test_repair_unit_refuses_a_batch_that_hides_the_note_in_the_wrong_list(case):
    _mutate_state(case, lambda e: e.__setitem__("kind", "unknown"))
    _apply(case, "unaccounted", V_UNACC, ["b10"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    unit = case["staging"] / "runs" / "r" / "units" / "u"
    unit.mkdir(parents=True)
    ri._write_json(unit / "unit.json", {"id": "u"})
    with pytest.raises(ri.HarnessError, match="does not exactly match"):
        sc.repair_unit(case["staging"], case["vault"], unit, batch["vid"], batch["unknown"], [],
                       batch["bullets"], batch["prior_titles"], batch["last"], batch["prior_bullets"],
                       batch["video_notes"], batch["retry_rejected"])


def test_request_salt_is_operator_specific_and_stable(case, monkeypatch):
    monkeypatch.setenv("ZR_LLM_BACKEND", "files")
    monkeypatch.setenv("ZR_STATE_DIR", str(case["staging"] / "state-home"))
    queued = _apply(case, "unaccounted", V_UNACC, ["b10"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    keys = []
    for n in (1, 2):
        unit = case["staging"] / "runs" / "retry-run" / "units" / f"u{n}"
        unit.mkdir(parents=True)
        ri._write_json(unit / "batch.json", batch)
        ri._write_json(unit / "unit.json", {"id": unit.name})
        res = sc.repair_unit(case["staging"], case["vault"], unit, batch["vid"], batch["notes"], batch["unknown"],
                             batch["bullets"], batch["prior_titles"], batch["last"], batch["prior_bullets"],
                             batch["video_notes"], batch["retry_rejected"])
        assert res.get("stopped") == "waiting-for-reply"
        keys.append({p.name for p in (case["staging"] / "exchange" / "requests").glob("*.json")})
    assert keys[0] == keys[1] and len(keys[0]) == 1
    request = ri._read_json(next(iter((case["staging"] / "exchange" / "requests").glob("*.json"))), {})
    msgs = [{"role": "system", "content": request["system"]}, {"role": "user", "content": request["user"]}]
    assert request["key"] == sc.request_key(msgs, {**sc.settings("repair"),
                                                   "request_salt": f"retry-operator:{queued['batch']}"})
    assert request["key"] != sc.request_key(msgs, {**sc.settings("repair"),
                                                   "request_salt": f"retry-open:{queued['batch']}"})
    assert case["original"].decode("utf-8") in request["user"] and f"{NAME}#b10" in request["user"]


# --------------------------------------------------------------------------- #
# Results: answers, history, needs_operator.json
# --------------------------------------------------------------------------- #

def _run_result(case, form, video, bullets, target, result_bullets, written, applied=()):
    queued = _apply(case, form, video, bullets, target)
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    batch["key"] = queued["batch"]
    sc.record_answers(case["state_path"], batch, {"bullets": result_bullets, "written": written,
                                                  "applied": list(applied)})
    return queued, batch


def test_result_keeps_prior_answer_in_history_and_other_sources(case):
    full = f"{NAME}#b10"
    others = {v: copy.deepcopy(a) for v, a in case["entry"]["bullets"][full]["answers"].items() if v != V_UNACC}
    prior = copy.deepcopy(case["entry"]["bullets"][full]["answers"][V_UNACC])
    queued, _b = _run_result(case, "unaccounted", V_UNACC, ["b10"], "", {
        full: {"decision": "corrected", "title": "Fresh Target", "titles": ["Fresh Target"]}}, ["Fresh Target"])
    after = _answers(case)[full]
    assert after["answers"][V_UNACC]["decision"] == "corrected"
    assert {v: a for v, a in after["answers"].items() if v != V_UNACC} == others
    assert [h["phase"] for h in after["answer_history"]] == ["before-retry"]
    assert after["answer_history"][0]["answer"] == prior and after["answer_history"][0]["retry_id"] == queued["batch"]
    # the bullets that were not requested are untouched
    assert _answers(case)[f"{NAME}#b1"] == case["entry"]["bullets"][f"{NAME}#b1"]


def test_unaccounted_again_leaves_the_entry_and_the_listing_honest(case):
    full = f"{NAME}#b10"
    _run_result(case, "unaccounted", V_UNACC, ["b10"], "", {}, [])
    assert _answers(case)[full]["answers"][V_UNACC]["decision"] == "unaccounted"
    before_vault = _tree_hash(case["vault"])
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"]) is None  # settles, hands out nothing
    st = ri._read_json(case["state_path"], {})
    entry = st["notes"][NOTE]
    assert entry["status"] in ("needs-repair", "operator")  # never `done` without a target
    assert ri._bullet_status(case["vault"], entry, full, set())[0].startswith("open:")
    items = _operator_items(case)
    assert len(items) == 1 and items[0]["state"] == "needs-repair" and items[0]["why"] == WHY
    assert full in {f"{NAME}#{b['id'].rsplit('#', 1)[-1]}" for b in items[0]["bullets"]}
    assert [h["phase"] for h in _answers(case)[full]["answer_history"]] == ["before-retry"]
    assert _tree_hash(case["vault"]) == before_vault and case["note_path"].read_bytes() == case["original"]
    assert any(b.get("settled") for b in st["batches"].values())


def test_dropped_retry_result_is_recorded_and_stays_open_for_the_operator(case):
    full = f"{NAME}#b10"
    _run_result(case, "unaccounted", V_UNACC, ["b10"], "", {
        full: {"decision": "dropped", "reason": "not in this transcript"}}, [])
    sc.next_batch(case["staging"], case["vault"], case["state_path"])
    entry = ri._read_json(case["state_path"], {})["notes"][NOTE]
    assert entry["bullets"][full]["answers"][V_UNACC]["decision"] == "dropped"
    assert entry["bullets"][full]["answer_history"][0]["answer"]["decision"] == "unaccounted"
    # V_REJ still holds target-rejected for that bullet and V_OTHER a drop: the bullet stays open
    assert ri._bullet_status(case["vault"], entry, full, set())[0].startswith("open:")
    assert entry["status"] in ("needs-repair", "operator") and len(_operator_items(case)) == 1


def test_success_settles_the_entry_and_removes_the_open_listing(case):
    full = f"{NAME}#b10"
    _run_result(case, "unaccounted", V_UNACC, ["b10"], "", {
        full: {"decision": "corrected", "title": "Fresh Target", "titles": ["Fresh Target"]}}, ["Fresh Target"],
        applied=[{"old": NOTE, "new": ["Fresh Target"], "action": "repair"}])
    assert _operator_items(case)  # nothing changes before the unit is finalized and the planner runs again
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"]) is None
    entry = ri._read_json(case["state_path"], {})["notes"][NOTE]
    assert entry["status"] == "done" and entry["titles"] == ["Fresh Target"]
    assert _operator_items(case) == []
    assert not [x for x in ri.operator_work(case["staging"]) if "Planche" in x]
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"]) is None  # settles once only


def test_other_operator_items_of_the_note_survive_settling(case):
    ri.needs_operator(case["staging"], [{"note": NOTE, "unit": "u1"}], "stub refused by the stub check")
    full = f"{NAME}#b10"
    _run_result(case, "unaccounted", V_UNACC, ["b10"], "", {
        full: {"decision": "corrected", "title": "Fresh Target", "titles": ["Fresh Target"]}}, ["Fresh Target"],
        applied=[{"old": NOTE, "new": ["Fresh Target"], "action": "repair"}])
    sc.next_batch(case["staging"], case["vault"], case["state_path"])
    assert [x["why"] for x in _operator_items(case)] == ["stub refused by the stub check"]


def test_quarantined_result_returns_the_bullet_to_an_honest_open_state(case):
    full = f"{NAME}#b10"
    prior = copy.deepcopy(case["entry"]["bullets"][full]["answers"][V_UNACC])
    queued, batch = _run_result(case, "unaccounted", V_UNACC, ["b10"], "", {
        full: {"decision": "corrected", "title": "Bad Target", "titles": ["Bad Target"]}}, ["Bad Target"],
        applied=[{"old": NOTE, "new": ["Bad Target"], "action": "repair"}])
    unit = case["staging"] / "runs" / "r" / "units" / "u"
    unit.mkdir(parents=True)
    ri._write_json(unit / "unit.json", {"repair_video": V_UNACC, "retry_rejected": batch["retry_rejected"]})
    ri._write_json(unit / "repair_map.json", {NOTE: {"targets": ["Bad Target"]}})
    ri._write_json(unit / "claims.json", {"bullets": {full: {"decision": "corrected", "titles": ["Bad Target"]}}})
    ri.reopen_rejected_bullets(case["staging"], unit, {"Bad Target"})  # finalize does this for a quarantined target
    sc.next_batch(case["staging"], case["vault"], case["state_path"])
    entry = ri._read_json(case["state_path"], {})["notes"][NOTE]
    after = entry["bullets"][full]
    assert after["answers"][V_UNACC]["decision"] == "target-rejected"
    assert [h["phase"] for h in after["answer_history"]] == ["before-retry", "retry-result-quarantined"]
    assert after["answer_history"][0]["answer"] == prior
    assert entry["status"] in ("needs-repair", "operator") and not entry["titles"]
    assert len(_operator_items(case)) == 1
    assert ri._bullet_status(case["vault"], entry, full, set())[0].startswith("open:")


def test_failed_twice_keeps_the_entry_the_listing_and_allows_a_fresh_preview(case):
    queued = _apply(case, "unaccounted", V_UNACC, ["b10"])
    answers = copy.deepcopy(_answers(case))
    items = _operator_items(case)
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"])["key"] == queued["batch"]
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"])["key"] == queued["batch"]
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"]) is None
    st = ri._read_json(case["state_path"], {})
    assert st["notes"][NOTE]["status"] == case["state_name"] and st["notes"][NOTE]["bullets"] == answers
    assert st["batches"][queued["batch"]]["status"] == "failed" and st["batches"][queued["batch"]]["retry_failure"]
    assert _operator_items(case) == items and case["note_path"].read_bytes() == case["original"]
    assert _apply(case, "unaccounted", V_UNACC, ["b10"])["batch"] != queued["batch"]


def test_operator_done_closes_a_queued_retry_and_it_is_not_handed_out(case):
    queued = _apply(case, "unaccounted", V_UNACC, ["b10"])
    ri.operator_done(case["staging"], NOTE, "operator decided")
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"]) is None
    st = ri._read_json(case["state_path"], {})
    assert st["batches"][queued["batch"]]["status"] == "operator-closed"
    assert st["notes"][NOTE]["status"] == "operator-closed"


def _cli(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sc.main(list(args))
    return rc, buf.getvalue()


def test_cli_preview_then_apply_for_both_commands(case):
    base = ["--staging", str(case["staging"]), "--vault", str(case["vault"]), "--note", NOTE]
    open_args = ["retry-open", *base, "--video", V_UNACC, "--form", "unaccounted", "--bullet", "b10"]
    rc, _out = _cli(*open_args)  # without the flag: the old, done-only behavior
    assert rc != 0
    rc, out = _cli(*open_args, "--entry-state", case["state_name"])
    assert rc == 0
    g = json.loads(out)["guards"]
    flags = ["--apply", "--expect-state", g["state_sha256"], "--expect-entry", g["entry_sha256"],
             "--expect-vault", g["vault_sha256"], "--expect-archive", g["archive_sha256"],
             "--expect-source", g["source_sha256"], "--expect-source-path", g["source_path"]]
    rej = ["retry-rejected", *base, "--video", V_REJ, "--target", REJ_TITLE, "--bullet", "b1",
           "--entry-state", case["state_name"]]
    rc, out = _cli(*rej)
    assert rc == 0 and json.loads(out)["entry_state"] == case["state_name"]
    with pytest.raises(SystemExit):
        _cli(*rej[:-1], "done")
    rc, _out = _cli(*open_args, "--entry-state", case["state_name"], "--apply")
    assert rc != 0 and not ri._read_json(case["state_path"], {})["batches"]
    rc, out = _cli(*open_args, "--entry-state", case["state_name"], *flags)
    assert rc == 0 and json.loads(out)["entry_state"] == case["state_name"]


# --------------------------------------------------------------------------- #
# End to end through the real repair unit, review and finalize (fake LLM scripts)
# --------------------------------------------------------------------------- #

from tests.probes_v6.test_probe_A_crash import _Repair  # noqa: E402
from tests.unit.test_run_integrity import PN  # noqa: E402
from tests.unit.test_synth_single import V5  # noqa: E402

import review_call as rc  # noqa: E402
import stub_check  # noqa: E402
import validate  # noqa: E402


class OperatorRetryEndToEnd(_Repair):
    TITLE = "Old Operator Note"

    def _entry(self, state: str, kind: str = "known") -> tuple[str, Path]:
        rel = self.old(self.TITLE, 2)
        state_file = sc.state_path(self.staging)
        sc.repair_groups(self.staging, self.zk, [rel], state_file=state_file)
        st = json.loads(state_file.read_text())
        e = st["notes"][rel]
        name = f"{self.TITLE}.md"
        e.update(status=state, pos=len(e["sources"]), replans=1, kind=kind)
        e["bullets"][f"{name}#b1"]["answers"] = {V5: {
            "decision": "unaccounted", "why": "one or more named notes were not written or reusable",
            "was": {"decision": "kept", "title": "Never Written", "titles": ["Never Written"]}}}
        e["bullets"][f"{name}#b2"]["answers"] = {V5: {"decision": "dropped", "reason": "not in this transcript"}}
        state_file.write_text(json.dumps(st))
        ri.needs_operator(self.staging, [ri.needs_repair_item(self.staging, rel, e, [f"{name}#b1"])], WHY)
        return rel, state_file

    def _retry(self, rel: str, state_file: Path, state: str, reply: str):
        plan = sc.retry_rejected_plan(self.staging, self.zk, rel, V5, "", ["b1"], kind="unaccounted", entry_state=state)
        sc.apply_retry_rejected(self.staging, self.zk, rel, V5, "", ["b1"], plan["guards"], kind="unaccounted",
                                entry_state=state)
        b = sc.next_batch(self.staging, self.zk, state_file)
        self.assertIn("retry_rejected", b)
        self.fake(repair=[reply])
        u = self.new_unit("repair", [])
        res = sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                             b["prior_titles"], b["last"], b["prior_bullets"], b["video_notes"], b["retry_rejected"])
        sc.record_answers(state_file, b, res)
        rc.review_unit(self.staging, self.zk, u)
        ev = ri.finalize(self.staging, self.run_dir, u, [], 0, self.zk)
        return b, u, res, ev

    def _ok_reply(self) -> str:
        return (f"=====NOTE: {self.a} | repairs: {self.TITLE}.md | bullets: b1=====\n{self.ta.rstrip()}\n"
                f"=====BULLETS=====\n{self.TITLE}.md#b1 | kept in {self.a}\n")

    def _assert_success(self, state: str) -> None:
        rel, state_file = self._entry(state)
        old_bytes = self.vpath(self.TITLE).read_bytes()
        before_e = json.loads(state_file.read_text())["notes"][rel]
        self.assertEqual(before_e["status"], state)
        b, u, res, ev = self._retry(rel, state_file, state, self._ok_reply())
        self.assertEqual(res["bullets"][f"{self.TITLE}.md#b1"]["decision"], "kept")
        # (a) the old note became the normal stub, and only that: its original bytes are archived intact
        stub = self.vpath(self.TITLE).read_text()
        self.assertIn("status: superseded", stub)
        self.assertTrue(self.vpath(self.a).is_file())
        arch = self.staging / stub.split('repair_archive: "', 1)[1].split('"', 1)[0]
        self.assertEqual(arch.read_bytes(), old_bytes)
        self.assertEqual(stub_check.check(arch.read_text(), stub, self.zk), [])
        errors, _w, _n = validate.check(self.zk, None, str(self.staging))
        self.assertEqual([e for e in errors if "stub" in e or "beside" in e], [])
        # the unit reached publication through the normal finalize
        self.assertEqual([x["note"] for x in ev["repair_stubs"] if x.get("stub")], [rel])
        # (b) the recorded answer survived in the history; the other bullet is untouched
        st = json.loads(state_file.read_text())
        bullet = st["notes"][rel]["bullets"][f"{self.TITLE}.md#b1"]
        self.assertEqual([h["answer"]["decision"] for h in bullet["answer_history"]], ["unaccounted"])
        self.assertEqual(st["notes"][rel]["bullets"][f"{self.TITLE}.md#b2"], before_e["bullets"][f"{self.TITLE}.md#b2"])
        # (c) the planner settles the entry; needs_operator.json lists the note no more
        self.assertEqual(st["notes"][rel]["status"], state)
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state_file))
        st = json.loads(state_file.read_text())
        self.assertEqual(st["notes"][rel]["status"], "done")
        self.assertEqual(st["notes"][rel]["titles"], [self.a])
        ops = json.loads((self.staging / "needs_operator.json").read_text())
        self.assertEqual([o for o in ops if o["note"] == rel], [])
        self.assertEqual([x for x in ri.operator_work(self.staging, self.zk) if self.TITLE in x], [])
        # (d) nothing else was handed out
        self.assertIsNone(sc.next_batch(self.staging, self.zk, state_file))

    def test_needs_repair_entry_ends_with_target_and_stub(self) -> None:
        self._assert_success("needs-repair")

    def test_operator_entry_ends_with_target_and_stub(self) -> None:
        self._assert_success("operator")

    def test_keep_reply_leaves_the_old_note_and_an_honest_listing(self) -> None:
        for state in ("needs-repair", "operator"):
            with self.subTest(state=state):
                rel, state_file = self._entry(state)
                old_bytes = self.vpath(self.TITLE).read_bytes()
                reply = f"=====KEEP-NEEDS-REPAIR: {self.TITLE}.md=====\nno supporting passage\n"
                b, u, res, ev = self._retry(rel, state_file, state, reply)
                self.assertEqual(self.vpath(self.TITLE).read_bytes(), old_bytes)  # unchanged, no stub
                self.assertFalse((self.vpath(self.a)).is_file())
                self.assertIsNone(sc.next_batch(self.staging, self.zk, state_file))
                st = json.loads(state_file.read_text())
                e = st["notes"][rel]
                self.assertIn(e["status"], ("needs-repair", "operator"))
                self.assertEqual(e["bullets"][f"{self.TITLE}.md#b1"]["answers"][V5]["decision"], "unaccounted")
                self.assertEqual(e["bullets"][f"{self.TITLE}.md#b1"]["answer_history"][0]["phase"], "before-retry")
                ops = [o for o in json.loads((self.staging / "needs_operator.json").read_text()) if o["note"] == rel]
                self.assertEqual(len(ops), 1)
                self.assertEqual([x["id"] for x in ops[0]["bullets"]], [f"{self.TITLE}.md#b1"])
                self.assertTrue([x for x in ri.operator_work(self.staging, self.zk) if self.TITLE in x])
                errors, _w, _n = validate.check(self.zk, None, str(self.staging))
                self.assertEqual([x for x in errors if self.TITLE in x and ("stub" in x or "beside" in x)], [])
                shutil.rmtree(self.staging / "repair-cache", ignore_errors=True)
                (self.staging / "repair_state.json").unlink()
                (self.staging / "needs_operator.json").unlink(missing_ok=True)

    def test_a_vault_edit_between_apply_and_unit_stops_the_unit_without_a_call(self) -> None:
        rel, state_file = self._entry("needs-repair")
        plan = sc.retry_rejected_plan(self.staging, self.zk, rel, V5, "", ["b1"], kind="unaccounted",
                                      entry_state="needs-repair")
        sc.apply_retry_rejected(self.staging, self.zk, rel, V5, "", ["b1"], plan["guards"], kind="unaccounted",
                                entry_state="needs-repair")
        b = sc.next_batch(self.staging, self.zk, state_file)
        p = self.vpath(self.TITLE)
        p.write_bytes(p.read_bytes() + b"\nuser edit\n")
        self.fake(repair=[self._ok_reply()])
        u = self.new_unit("repair", [])
        with self.assertRaises(ri.HarnessError):
            sc.repair_unit(self.staging, self.zk, u, b["vid"], b["notes"], b["unknown"], b["bullets"],
                           b["prior_titles"], b["last"], b["prior_bullets"], b["video_notes"], b["retry_rejected"])
        self.assertEqual(self.log(), [])  # no model call
        self.assertFalse(self.vpath(self.a).is_file())


def test_the_module_under_test_is_the_proposal_copy():
    assert Path(sc.__file__).resolve() == (ZR / "synth_call.py").resolve()
    assert vc and PN
