"""Offline coverage for `retry-open --form unverified`: the guarded retry of a done entry's bullet whose
recorded answer is `unverified` with `why` "the claim line that carried the bullet changed" or a ledger
cause ("the named note quotes nothing near the bullet's passage ..." / "no candidate passage ...").
Same preview/apply flow as the other retry forms; the stub guard and the status guard are in
`retry_rejected_plan` (kind `unverified`)."""
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
V_A = "vid-G331KArPXnI"      # the retried source video (first recorded source)
V_B = "vid-yM5j7jHhKLA"
V_C = "vid-qYQHJEH7zMA"
T1 = "Radoslav Radev Says The Planche Needs Lats Core Glutes And Legs In One Tension Pattern"  # in entry titles
T2 = "Radoslav Radev Mentions A Weight Room Anecdote About Pressing"                          # published, not in titles
T3 = "Radoslav Radev Unpublished Target"                                                      # not published
CLAIM = "the claim line that carried the bullet changed"
QUOTES = "the named note quotes nothing near the bullet's passage that matches it"
NOCAND = "no candidate passage to check against"
PENDING = [f"b{i}" for i in range(1, 11)]


def _full(b: str) -> str:
    return f"{NAME}#{b}"


def _target_note(title: str, vid: str, claim: str, quote: str) -> str:
    return (f"---\ntype: permanent note\nsources:\n  - {vid}\n---\n\n# {title}\n\n{claim} [src: {vid} @ 01:24]\n\n"
            f"## Evidence\n\n- {vid} @ 01:24 (said): {quote}\n")


@pytest.fixture
def case(tmp_path: Path):
    staging = tmp_path / "staging"
    vault = tmp_path / "vault" / "Video Transcripts Zettelkasten"
    archive = staging / "retired" / "20261005T074358Z-1" / "20261005T074359-pending-unit-1" / NOTE
    archive.parent.mkdir(parents=True)
    shutil.copyfile(FIX / "qyq_original.md", archive)
    (staging / "lit").mkdir(parents=True)
    sources = {}
    for vid in (V_A, V_B, V_C):
        sources[vid] = staging / "lit" / f"{vid}.md"
        sources[vid].write_text(f"canonical transcript of {vid}\n", encoding="utf-8")
    note_path = vault / NOTE
    note_path.parent.mkdir(parents=True)
    entry = copy.deepcopy(json.loads((FIX / "qyq_parent_entry.json").read_text(encoding="utf-8"))["entry"])
    bullets = entry["bullets"]
    text1 = bullets[_full("b1")]["text"]
    entry["titles"] = list(entry["titles"]) + [T1]

    def was(decision, *titles):
        return {"decision": decision, "title": titles[0], "titles": list(titles)}

    # b1: claim line changed, T1 published and listed in the entry; other sources keep their answers
    bullets[_full("b1")]["answers"][V_A] = {"decision": "unverified", "why": CLAIM, "was": was("corrected", T1)}
    # b3: ledger cause; T2 published but NOT a title of the entry
    bullets[_full("b3")]["answers"][V_A] = {"decision": "unverified", "why": QUOTES, "was": was("kept", T2)}
    # b4: ledger cause (no candidate); T3 is not published
    bullets[_full("b4")]["answers"][V_A] = {"decision": "unverified", "why": NOCAND, "was": was("corrected", T3)}
    # b5: the other unverified form (a fix removed the Evidence)
    bullets[_full("b5")]["answers"][V_A] = {"decision": "unverified", "why": "a fix removed the Evidence that carried the bullet",
                                           "was": was("corrected", T1)}
    # b6: claim line changed, but another source already carries the bullet
    bullets[_full("b6")]["answers"][V_A] = {"decision": "unverified", "why": CLAIM, "was": was("corrected", T1)}
    bullets[_full("b6")]["answers"][V_B] = {"decision": "corrected", "title": "Other Published",
                                           "titles": ["Other Published"]}
    perm = vault / "01 Permanent Notes"
    perm.mkdir(parents=True, exist_ok=True)
    (perm / f"{T1}.md").write_text(_target_note(T1, V_A, text1, text1), encoding="utf-8")
    (perm / f"{T2}.md").write_text(_target_note(T2, V_A, "He tells a story about a gym.", "a story about a gym"),
                                   encoding="utf-8")
    stub = ("---\ntype: permanent note\nstatus: superseded\nverification: superseded\nsuperseded_pending:\n"
            + "".join(f'  - "bullet: {NAME}#{b}"\n' for b in PENDING) + "---\n\n# Original parent note\n")
    note_path.write_text(stub, encoding="utf-8")
    state_path = staging / "repair_state.json"
    ri._write_json(state_path, {"notes": {NOTE: entry}, "batches": {}})
    (staging / ".driver.lock").touch()
    (staging / ".statelock").touch()
    return {"staging": staging, "vault": vault, "archive": archive, "sources": sources, "perm": perm,
            "note_path": note_path, "state_path": state_path, "entry": entry}


def _plan(case, video, bullets, target=""):
    return sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, video, target, bullets, kind="unverified")


def _apply(case, video, bullets, target="", plan=None):
    plan = plan or _plan(case, video, bullets, target)
    return sc.apply_retry_rejected(case["staging"], case["vault"], NOTE, video, target, bullets,
                                   expected=plan["guards"], kind="unverified")


def _tree_hash(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _mutate_state(case, fn):
    st = ri._read_json(case["state_path"], {})
    fn(st["notes"][NOTE])
    ri._write_json(case["state_path"], st)


def _status(case, bid):
    ri.load_renames(case["staging"])
    e = ri._read_json(case["state_path"], {})["notes"][NOTE]
    return ri._bullet_status(case["vault"], e, _full(bid), set())[0]


def _handed(case, queued):
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    batch["key"] = queued["batch"]
    return batch


# ----------------------------------------------------------------------------- preview / accepted forms

def test_status_of_the_real_forms_before_the_retry(case):
    assert _status(case, "b1").endswith("(unverified)") and f"[[{T1}]]" in _status(case, "b1")
    assert _status(case, "b3").endswith("(unverified)")
    assert _status(case, "b4").startswith("open: target not published (")


@pytest.mark.parametrize("bid,why", [("b1", CLAIM), ("b3", QUOTES), ("b4", NOCAND)])
def test_preview_accepts_both_why_forms_read_only(case, bid, why):
    before_s, before_v = _tree_hash(case["staging"]), _tree_hash(case["vault"])
    plan = _plan(case, V_A, [bid])
    assert plan["kind"] == "unverified"
    assert plan["bullets"] == [bid]
    assert plan["entry_status"] == "done"
    assert plan["rejections"][bid]["why"] == why
    assert plan["archive_sha256"] == case["entry"]["sha"]
    assert plan["target"] == ""
    assert set(plan["named_targets"]) >= set(plan["rejections"][bid]["was"]["titles"]) & {T1, T2, T3}
    assert _tree_hash(case["staging"]) == before_s
    assert _tree_hash(case["vault"]) == before_v


def test_preview_with_an_exact_named_target_and_several_bullets(case):
    plan = _plan(case, V_A, ["b1", "b3", "b4"])
    assert plan["bullets"] == ["b1", "b3", "b4"]
    assert _plan(case, V_A, ["b1"], target=T1)["target"] == T1
    assert _plan(case, V_A, ["b1"], target=T1)["guards"] == _plan(case, V_A, ["b1"])["guards"]
    with pytest.raises(ri.HarnessError, match="does not name the requested target"):
        _plan(case, V_A, ["b1"], target=T2)


def test_preview_lists_other_source_answers_as_preserved(case):
    plan = _plan(case, V_A, ["b1"])
    assert set(plan["preserved_answers"][_full("b1")]) == {V_B, V_C}
    assert plan["preserved_answers"][_full("b1")][V_B]["decision"] == "unaccounted"


def test_published_target_outside_entry_titles_joins_prior_titles_only_for_this_form(case):
    p1 = _plan(case, V_A, ["b1"])
    assert p1["prior_titles"] == list(case["entry"]["titles"])           # T1 is already a title of the entry
    p3 = _plan(case, V_A, ["b3"])
    assert p3["prior_titles"] == list(case["entry"]["titles"]) + [T2]    # T2 published, not a title
    p4 = _plan(case, V_A, ["b4"])
    assert p4["prior_titles"] == list(case["entry"]["titles"])           # T3 is not published: nothing is added
    assert T2 in p3["named_targets"]


# ----------------------------------------------------------------------------- refused cases

@pytest.mark.parametrize("video,bullets,target", [
    (V_A, ["b5"], ""),            # evidence-removed answer is the other form
    (V_A, ["b2"], ""),            # corrected answer, not unverified
    (V_A, ["b7"], ""),            # dropped answer
    (V_A, ["b1", "b5"], ""),      # one bullet of the set is the wrong form
    (V_B, ["b1"], ""),            # unaccounted for that video
    (V_C, ["b1"], ""),            # target-rejected for that video
    (V_A, ["b6"], ""),            # another source already carries the bullet
    (V_A, ["b999"], ""),
    ("vid-nope", ["b1"], ""),
    (V_A, ["b1", "b1"], ""),
    (V_A, ["b10"], ""),           # no answer of this video
])
def test_unverified_retry_refused_cases(case, video, bullets, target):
    with pytest.raises(ri.HarnessError):
        _plan(case, video, bullets, target)


def test_claim_already_settled_by_another_source_is_named(case):
    with pytest.raises(ri.HarnessError, match="another answer already settles"):
        _plan(case, V_A, ["b6"])


def test_other_forms_do_not_accept_the_unverified_answers(case):
    for kind in ("evidence-removed", "unaccounted"):
        with pytest.raises(ri.HarnessError, match="not an open"):
            sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, V_A, "", ["b1"], kind=kind)
    with pytest.raises(ri.HarnessError):
        sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, V_A, T1, ["b1"])   # target-rejected form


def test_evidence_removed_still_works_and_is_refused_by_this_form(case):
    plan = sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, V_A, "", ["b5"], kind="evidence-removed")
    assert plan["kind"] == "evidence-removed"
    with pytest.raises(ri.HarnessError, match="not an open unverified answer"):
        _plan(case, V_A, ["b5"])


@pytest.mark.parametrize("status", ["operator", "needs-repair", "open", "done-unrepaired", "failed"])
def test_unverified_retry_requires_done_entry(case, status):
    _mutate_state(case, lambda e: e.__setitem__("status", status))
    with pytest.raises(ri.HarnessError, match="only accepts a done repair entry"):
        _plan(case, V_A, ["b1"])


def test_unverified_retry_refuses_entry_state(case):
    for st_ in ("needs-repair", "operator"):
        with pytest.raises(ri.HarnessError, match="only accepts a done repair entry"):
            sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, V_A, "", ["b1"], kind="unverified",
                                   entry_state=st_)


def test_unverified_retry_requires_stub_pending_bullet_and_archive(case):
    text = case["note_path"].read_text(encoding="utf-8")
    case["note_path"].write_text(text.replace(f'  - "bullet: {NAME}#b1"\n', ""), encoding="utf-8")
    with pytest.raises(ri.HarnessError, match="superseded_pending"):
        _plan(case, V_A, ["b1"])
    case["note_path"].write_text(text.replace("status: superseded", "status: published"), encoding="utf-8")
    with pytest.raises(ri.HarnessError, match="superseded vault stub"):
        _plan(case, V_A, ["b1"])
    case["note_path"].write_text(text, encoding="utf-8")
    case["archive"].write_bytes(case["archive"].read_bytes() + b"changed\n")
    with pytest.raises(ri.HarnessError, match="archive"):
        _plan(case, V_A, ["b1"])


def test_status_guard_refuses_a_bullet_whose_pointer_is_unmarked_or_waiting(case):
    # a published target that strongly carries the bullet and a non-claim-line cause reads as a plain
    # pointer (no mark): the guard refuses it
    _mutate_state(case, lambda e: e["bullets"][_full("b1")]["answers"].__setitem__(
        V_A, {"decision": "unverified", "why": QUOTES, "was": {"decision": "corrected", "titles": [T1]}}))
    assert not _status(case, "b1").endswith("(unverified)")
    with pytest.raises(ri.HarnessError, match="status is"):
        _plan(case, V_A, ["b1"])
    assert not sc._retry_unverified_status_ok("→ [[T]]")
    assert not sc._retry_unverified_status_ok("open: target waiting (T)")
    assert not sc._retry_unverified_status_ok("open: target rejected (T)")
    assert not sc._retry_unverified_status_ok("open: evidence removed (T)")
    assert not sc._retry_unverified_status_ok("unsupported: no source video states this")
    assert sc._retry_unverified_status_ok("→ [[T]] (unverified)")
    assert sc._retry_unverified_status_ok("open: target not published (T)")


def test_conflicts_active_batch_pending_review_and_published_target(case):
    st = ri._read_json(case["state_path"], {})
    st["batches"]["active"] = {"status": "running", "bullets": {NOTE: [_full("b1")]}, "vid": V_A}
    ri._write_json(case["state_path"], st)
    with pytest.raises(ri.HarnessError, match="active repair batch"):
        _plan(case, V_A, ["b1"])
    st["batches"].clear()
    ri._write_json(case["state_path"], st)
    pdir = case["staging"] / "pending-review" / "20261006T000000Z-1--unit"
    pdir.mkdir(parents=True)
    ri._write_json(pdir / "meta.json", {"notes": [T2], "reasons": {}})   # touches the published target of b3
    with pytest.raises(ri.HarnessError, match="pending review"):
        _plan(case, V_A, ["b3"])
    _plan(case, V_A, ["b1"])   # b1 names another title: no conflict


@pytest.mark.parametrize("stale", ["source", "vault", "state"])
def test_apply_rejects_stale_guard_without_writes(case, stale):
    plan = _plan(case, V_A, ["b1"])
    path = {"source": case["sources"][V_A], "vault": case["note_path"], "state": case["state_path"]}[stale]
    if stale == "state":
        st = ri._read_json(path, {})
        st["notes"][NOTE]["revision"] = "intervening edit"
        ri._write_json(path, st)
    else:
        path.write_bytes(path.read_bytes() + b"\n")
    before_s, before_v = _tree_hash(case["staging"]), _tree_hash(case["vault"])
    with pytest.raises(ri.HarnessError):
        _apply(case, V_A, ["b1"], plan=plan)
    assert _tree_hash(case["staging"]) == before_s
    assert _tree_hash(case["vault"]) == before_v


# ----------------------------------------------------------------------------- queue and hand-out

def test_apply_queues_batch_with_kind_prior_titles_and_one_active_batch(case):
    before_bullets = copy.deepcopy(case["entry"]["bullets"])
    queued = _apply(case, V_A, ["b3"])
    assert queued["kind"] == "unverified"
    st = ri._read_json(case["state_path"], {})
    batch = st["batches"][queued["batch"]]
    assert batch["status"] == "running" and batch["vid"] == V_A
    assert batch["bullets"] == {NOTE: [_full("b3")]}
    assert batch["retry_rejected"]["kind"] == "unverified"
    assert "state_sha256" not in batch["retry_rejected"]["guards"]
    assert batch["prior_titles"][NOTE] == list(case["entry"]["titles"]) + [T2]
    assert st["notes"][NOTE]["titles"] == case["entry"]["titles"]          # the entry itself is unchanged
    assert st["notes"][NOTE]["bullets"] == before_bullets
    with pytest.raises(ri.HarnessError, match="active repair batch"):
        _plan(case, V_A, ["b3"])
    handed = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    assert handed["key"] == queued["batch"] and handed["retry_rejected"]["kind"] == "unverified"


def test_batch_key_has_its_own_key_space(case):
    plan = _plan(case, V_A, ["b1"])
    queued = _apply(case, V_A, ["b1"], plan=plan)
    seed = json.dumps([plan["guards"]["state_sha256"], V_A, "", [_full("b1")], "unverified"],
                      ensure_ascii=False, separators=(",", ":"))
    assert queued["batch"] == "retry-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


@pytest.mark.parametrize("what", ["source", "answer", "vault", "archive_state", "other_source"])
def test_handout_revalidates_guards(case, what):
    queued = _apply(case, V_A, ["b1"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    assert batch["key"] == queued["batch"]
    retry = batch["retry_rejected"]
    sc._validate_queued_retry(case["staging"], case["vault"], retry)   # still valid
    if what == "source":
        case["sources"][V_A].write_text("changed\n", encoding="utf-8")
        match = "source_sha256"
    elif what == "answer":
        _mutate_state(case, lambda e: e["bullets"][_full("b1")]["answers"].__setitem__(
            V_A, {"decision": "dropped", "reason": "not in this transcript"}))
        match = "not an open unverified answer"
    elif what == "vault":
        case["note_path"].write_text(case["note_path"].read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
        match = "vault_sha256"
    elif what == "other_source":
        _mutate_state(case, lambda e: e["bullets"][_full("b1")]["answers"].__setitem__(
            V_B, {"decision": "corrected", "title": "Other Published", "titles": ["Other Published"]}))
        match = "entry_sha256|another answer already settles"
    else:
        _mutate_state(case, lambda e: e.__setitem__("status", "needs-repair"))
        match = "done repair entry"
    with pytest.raises(ri.HarnessError, match=match):
        sc._validate_queued_retry(case["staging"], case["vault"], retry)


def test_handout_request_names_the_existing_published_targets(case, monkeypatch):
    """The normal repair request lists every prior title with its CURRENT text (H_WRITTEN_OLD), so the author
    can write `kept in <existing target>`; T2 (not a title of the entry) is listed because of prior_titles."""
    monkeypatch.setenv("ZR_LLM_BACKEND", "files")
    monkeypatch.setenv("ZR_STATE_DIR", str(case["staging"] / "state-home"))
    queued = _apply(case, V_A, ["b3"])
    batch = sc.next_batch(case["staging"], case["vault"], case["state_path"])
    unit_dir = case["staging"] / "runs" / "retry-run" / "units" / "u1"
    unit_dir.mkdir(parents=True)
    ri._write_json(unit_dir / "batch.json", batch)
    ri._write_json(unit_dir / "unit.json", {"id": unit_dir.name})
    res = sc.repair_unit(case["staging"], case["vault"], unit_dir, batch["vid"], batch["notes"],
                         batch["unknown"], batch["bullets"], batch["prior_titles"], batch["last"],
                         batch["prior_bullets"], batch["video_notes"], batch["retry_rejected"])
    assert res.get("stopped") == "waiting-for-reply"
    request = ri._read_json(next(iter((case["staging"] / "exchange" / "requests").glob("*.json"))), {})
    user = request["user"]
    assert sc.H_WRITTEN_OLD in user
    assert f"[[{T2}]]" in user and "He tells a story about a gym." in user
    assert f"[[{T1}]]" in user
    assert f"{_full('b3')} |" in user and f"{_full('b1')} |" not in user.split("BULLETS OF")[-1]
    msgs = [{"role": "system", "content": request["system"]}, {"role": "user", "content": user}]
    assert request["key"] == sc.request_key(msgs, {**sc.settings("repair"),
                                                   "request_salt": f"retry-open:{queued['batch']}"})


# ----------------------------------------------------------------------------- the four results

def test_result_a_kept_in_existing_published_target_settles_when_the_target_carries_the_claim(case):
    """Real ledger check (`_verify_ledger`) of a `kept in <existing published target>` line, then the
    real `record_answers`. T1 is a title of the entry and its text carries the bullet."""
    full = _full("b1")
    text1 = case["entry"]["bullets"][full]["text"]
    others_before = {v: copy.deepcopy(a) for v, a in case["entry"]["bullets"][full]["answers"].items() if v != V_A}
    prior = copy.deepcopy(case["entry"]["bullets"][full]["answers"][V_A])
    queued = _apply(case, V_A, ["b1"])
    batch = _handed(case, queued)
    parsed = {"notes": [], "problems": [],
              "bullets": {full: {"decision": "kept", "title": T1, "titles": [T1]}}}
    sc._verify_ledger(parsed, {full: [{"at": "[01:24]", "text": text1}]}, case["vault"], {full: text1}, V_A, {})
    ledger = parsed["bullets"][full]
    assert ledger["decision"] == "kept" and ledger["cited"] and ledger["claim_lines"]
    sc.record_answers(case["state_path"], batch, {"bullets": {full: ledger}, "written": [], "applied": []})
    st = ri._read_json(case["state_path"], {})
    after = st["notes"][NOTE]["bullets"][full]
    assert after["answers"][V_A]["decision"] == "kept"                       # not turned into `unaccounted`
    assert {v: a for v, a in after["answers"].items() if v != V_A} == others_before
    assert [h["phase"] for h in after["answer_history"]] == ["before-retry"]
    assert after["answer_history"][0]["answer"] == prior
    assert after["answer_history"][0]["retry_id"] == queued["batch"]
    assert _status(case, "b1") == f"→ [[{T1}]]"                              # strong pointer, no mark
    assert full not in st["notes"][NOTE]["unverified_bullets"]
    assert st["notes"][NOTE]["status"] == "done"


def test_result_a_target_outside_entry_titles_needs_the_prior_titles_extension(case):
    """T2 is published but is not a title of the entry. `known` of `record_answers` is written + entry titles
    + batch prior_titles: the extension makes it a valid ledger target; without it the line is `unaccounted`."""
    full = _full("b3")
    line = {"decision": "kept", "title": T2, "titles": [T2], "cited": [[V_A, 84, "abc"]]}
    queued = _apply(case, V_A, ["b3"])
    batch = _handed(case, queued)
    assert T2 in batch["prior_titles"][NOTE]
    # control: the same result with the batch as the plain retry-open would have built it
    plain = copy.deepcopy(batch)
    plain["prior_titles"][NOTE] = [t for t in plain["prior_titles"][NOTE] if t != T2]
    state_copy = case["state_path"].parent / "repair_state_copy.json"
    shutil.copyfile(case["state_path"], state_copy)
    sc.record_answers(state_copy, plain, {"bullets": {full: dict(line)}, "written": [], "applied": []})
    bad = ri._read_json(state_copy, {})["notes"][NOTE]["bullets"][full]["answers"][V_A]
    assert bad["decision"] == "unaccounted" and "not written or reusable" in bad["why"]
    sc.record_answers(case["state_path"], batch, {"bullets": {full: dict(line)}, "written": [], "applied": []})
    good = ri._read_json(case["state_path"], {})["notes"][NOTE]["bullets"][full]["answers"][V_A]
    assert good["decision"] == "kept"


def test_result_a_corrected_in_a_new_written_note(case):
    full = _full("b4")
    queued = _apply(case, V_A, ["b4"])
    batch = _handed(case, queued)
    new = "Radoslav Radev Fresh Retry Target"
    sc.record_answers(case["state_path"], batch, {
        "bullets": {full: {"decision": "corrected", "title": new, "titles": [new]}},
        "written": [new], "applied": [{"old": NOTE, "new": [new], "action": "split"}]})
    st = ri._read_json(case["state_path"], {})["notes"][NOTE]
    assert st["bullets"][full]["answers"][V_A]["titles"] == [new]
    assert new in st["titles"]
    assert [h["phase"] for h in st["bullets"][full]["answer_history"]] == ["before-retry"]
    assert st["bullets"][full]["answer_history"][0]["answer"]["why"] == NOCAND


def test_result_b_again_unverified_stays_honest_and_not_worse(case):
    full = _full("b1")
    prior = copy.deepcopy(case["entry"]["bullets"][full]["answers"][V_A])
    before_status = _status(case, "b1")
    queued = _apply(case, V_A, ["b1"])
    batch = _handed(case, queued)
    again = {"decision": "unverified", "why": CLAIM, "was": {"decision": "kept", "title": T1, "titles": [T1]}}
    sc.record_answers(case["state_path"], batch, {"bullets": {full: again}, "written": [], "applied": []})
    e = ri._read_json(case["state_path"], {})["notes"][NOTE]
    assert e["bullets"][full]["answers"][V_A] == again
    assert e["bullets"][full]["answer_history"][0]["answer"] == prior
    assert _status(case, "b1") == before_status                  # same marked pointer
    assert full in e["unverified_bullets"]
    again_plan = _plan(case, V_A, ["b1"])                        # a fresh preview is possible
    assert again_plan["rejections"]["b1"]["why"] == CLAIM


def test_result_b_ledger_unverified_is_read_by_the_existing_pointer_rule(case):
    """Not a retry feature: for a ledger cause `_bullet_status` lets the published target's own text decide
    (X16 `pointer_supported`). T1 carries the bullet text, so the pointer reads unmarked; a weak target stays
    marked. The old claim-line cause was always marked, so this can only improve, never worsen."""
    queued = _apply(case, V_A, ["b1"])
    batch = _handed(case, queued)
    full = _full("b1")
    again = {"decision": "unverified", "why": QUOTES, "was": {"decision": "kept", "title": T1, "titles": [T1]}}
    sc.record_answers(case["state_path"], batch, {"bullets": {full: again}, "written": [], "applied": []})
    assert _status(case, "b1") == f"→ [[{T1}]]"


def test_result_c_dropped_is_recorded_and_the_status_changes_honestly(case):
    """A drop removes the marked pointer: this is the one result besides (d) that changes the status line."""
    _mutate_state(case, lambda e: e.__setitem__("sources", [V_A]))   # one planned source: a drop ends the bullet
    full = _full("b3")
    prior = copy.deepcopy(case["entry"]["bullets"][full]["answers"][V_A])
    others = {b: copy.deepcopy(x["answers"]) for b, x in case["entry"]["bullets"].items() if b != full}
    assert _status(case, "b3").endswith("(unverified)")
    queued = _apply(case, V_A, ["b3"])
    batch = _handed(case, queued)
    drop = {"decision": "dropped", "reason": "not in this transcript"}
    sc.record_answers(case["state_path"], batch, {"bullets": {full: drop}, "written": [], "applied": []})
    e = ri._read_json(case["state_path"], {})["notes"][NOTE]
    assert e["bullets"][full]["answers"][V_A] == drop
    assert e["bullets"][full]["answer_history"][0]["answer"] == prior
    assert _status(case, "b3") == "unsupported: no source video states this"
    # the class lists of the done entry follow the retried bullet, and only that bullet
    assert full in e["unsupported_bullets"] and full not in e["unverified_bullets"]
    assert e["status"] == "done"
    assert {b: x["answers"] for b, x in e["bullets"].items() if b != full} == others
    assert set(e["unverified_bullets"]) == set(case["entry"]["unverified_bullets"]) - {full}


def test_result_d_quarantined_new_note_can_make_the_status_worse_and_history_undoes_it(case):
    full = _full("b1")
    prior = copy.deepcopy(case["entry"]["bullets"][full]["answers"][V_A])
    before = _status(case, "b1")
    assert before.endswith("(unverified)") and f"[[{T1}]]" in before
    queued = _apply(case, V_A, ["b1"])
    batch = _handed(case, queued)
    new = "Retry Result Target"
    sc.record_answers(case["state_path"], batch, {
        "bullets": {full: {"decision": "corrected", "title": new, "titles": [new]}},
        "written": [new], "applied": [{"old": NOTE, "new": [new], "action": "split"}]})
    unit_dir = case["staging"] / "runs" / "r" / "units" / "u"
    unit_dir.mkdir(parents=True)
    ri._write_json(unit_dir / "unit.json", {"repair_video": V_A, "retry_rejected": batch["retry_rejected"]})
    ri._write_json(unit_dir / "repair_map.json", {NOTE: {"targets": [new]}})
    ri._write_json(unit_dir / "claims.json", {"bullets": {full: {"decision": "corrected", "titles": [new]}}})
    ri.reopen_rejected_bullets(case["staging"], unit_dir, {new})
    st = ri._read_json(case["state_path"], {})
    after = st["notes"][NOTE]["bullets"][full]
    assert after["answers"][V_A]["decision"] == "target-rejected"
    assert [h["phase"] for h in after["answer_history"]] == ["before-retry", "retry-result-quarantined"]
    assert after["answer_history"][0]["answer"] == prior
    assert st["notes"][NOTE]["status"] == "done"
    assert T1 in st["notes"][NOTE]["titles"]                            # the old target stays listed
    worse = _status(case, "b1")
    assert worse.startswith("open: target rejected")                    # WORSE: the pointer to T1 is gone
    # operator undo by hand: put the `before-retry` answer back from the history
    st["notes"][NOTE]["bullets"][full]["answers"][V_A] = copy.deepcopy(after["answer_history"][0]["answer"])
    ri._write_json(case["state_path"], st)
    assert _status(case, "b1") == before
    # or retry again with the normal target-rejected form for the new title
    st["notes"][NOTE]["bullets"][full]["answers"][V_A] = {"decision": "target-rejected", "titles": [new],
                                                           "was": "corrected"}
    ri._write_json(case["state_path"], st)
    assert _plan_rejected_ok(case, new)


def _plan_rejected_ok(case, title):
    return sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, V_A, title, ["b1"])["bullets"] == ["b1"]


def test_failed_twice_keeps_done_entry_and_allows_fresh_preview(case):
    queued = _apply(case, V_A, ["b1"])
    answers = copy.deepcopy(ri._read_json(case["state_path"], {})["notes"][NOTE]["bullets"])
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"])["key"] == queued["batch"]
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"])["key"] == queued["batch"]
    assert sc.next_batch(case["staging"], case["vault"], case["state_path"]) is None
    st = ri._read_json(case["state_path"], {})
    assert st["notes"][NOTE]["status"] == "done"
    assert st["notes"][NOTE]["bullets"] == answers
    assert st["batches"][queued["batch"]]["status"] == "failed"
    assert _apply(case, V_A, ["b1"])["batch"] != queued["batch"]


def test_other_forms_leave_the_class_lists_alone(case):
    """The class-list refresh of `record_answers` belongs to --form unverified only."""
    queued = sc.apply_retry_rejected(
        case["staging"], case["vault"], NOTE, V_A, "", ["b5"],
        expected=sc.retry_rejected_plan(case["staging"], case["vault"], NOTE, V_A, "", ["b5"],
                                        kind="evidence-removed")["guards"], kind="evidence-removed")
    batch = _handed(case, queued)
    sc.record_answers(case["state_path"], batch, {"bullets": {_full("b5"): {"decision": "dropped",
                                                                           "reason": "not in this transcript"}},
                                                  "written": [], "applied": []})
    e = ri._read_json(case["state_path"], {})["notes"][NOTE]
    assert e["unverified_bullets"] == case["entry"]["unverified_bullets"]
    assert e["unsupported_bullets"] == case["entry"]["unsupported_bullets"]


# ----------------------------------------------------------------------------- CLI

def _cli(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sc.main(list(args))
    return rc, buf.getvalue()


def test_retry_open_cli_unverified_preview_then_apply_needs_all_guards(case):
    base = ["retry-open", "--staging", str(case["staging"]), "--vault", str(case["vault"]),
            "--note", NOTE, "--video", V_A, "--form", "unverified", "--bullet", "b1"]
    rc, out = _cli(*base)
    assert rc == 0
    plan = json.loads(out)
    assert plan["kind"] == "unverified"
    g = plan["guards"]
    rc, _out = _cli(*base, "--apply")
    assert rc != 0
    assert not ri._read_json(case["state_path"], {})["batches"]
    flags = ["--apply", "--expect-state", g["state_sha256"], "--expect-entry", g["entry_sha256"],
             "--expect-vault", g["vault_sha256"], "--expect-archive", g["archive_sha256"],
             "--expect-source", g["source_sha256"], "--expect-source-path", g["source_path"]]
    rc, out = _cli(*base, *flags)
    assert rc == 0 and json.loads(out)["queued"] is True and json.loads(out)["kind"] == "unverified"
    rc, _out = _cli(*base[:-4], "--form", "evidence-removed", "--bullet", "b6")
    assert rc != 0
