#!/usr/bin/env python3
"""Run integrity for the Ralph loops: shadow publication with review, recovery, summary.

The agent never writes into the vault. Each unit has a shadow folder
`<staging>/runs/<run_id>/units/<unit>/out/` with the vault layout; the agent's
file tools write only there. A note reaches the vault only through `finalize`.

Flow of one video unit (run_lib.sh `zr_synth_unit`):

    unit-start -> agent -> check (mechanical; up to 2 fix turns for the agent)
      -> review-targets -> source-check review agent per note (reads the shadow
         note and the cited passages with 90 s of context, writes a verdict file)
      -> finalize (publish / quarantine / pending-review) -> optional git commit

`finalize` publishes a note only when the mechanical check passes AND its
source-check verdict is `supported` for every item. A published note carries
`verification: source-checked`. A note whose review did not run (limit, error,
no verdict file) goes to `<staging>/pending-review/` (outside the vault);
`pending-units` turns it into a unit of the next run. Text units (tweets,
articles) and clustering units have no review step.

Other rules (README "Verification and run integrity"):
- path rule, safe file names, no duplicate of an existing name after Unicode
  normalization, no secret in any shadow file;
- a stub (`superseded_by`) is published only when its target is published in
  the same finalize; an overwritten or removed vault file is archived first to
  `<staging>/retired/<run_id>/<unit>/`; a removal leaves a stub;
- publish records an intent (path, sha) before the rename, so recovery after a
  crash keeps notes that are already in the vault;
- `start` recovers dead units: their output is not trusted and goes to
  quarantine (except already-published notes), the queue item is `incomplete`;
- a review never removes a published note: it sets `verification: needs-repair`;
- the publish lock lives outside the vault (`<staging parent>/.zr-locks/`);
- git commits only the published paths; a failed commit is retried at `start`.
"""
from __future__ import annotations

import argparse
import copy
import contextlib
import contextvars
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pricing  # noqa: E402
import provenance  # noqa: E402
import secret_scan  # noqa: E402
import stub_check  # noqa: E402
import validate  # noqa: E402
import verify_claims  # noqa: E402
from _lock import state_lock  # noqa: E402

UNIT_NOT_OK = 10  # precheck: nothing to run; check/finalize: a note failed
UNIT_INCOMPLETE = 11  # finalize: the agent run ended by limit, timeout, error or kill
UNIT_ALREADY_FINAL = 12  # finalize: the unit has a finalize record already
RECOVERED_RC = -1000
PERMANENT_DIR = "01 Permanent Notes"
MAPS_DIR = "00 Maps"
TRANSIENT = re.compile(
    r"HTTP 429|HTTP 5\d\d|rate.?limit|quota|overloaded|connection error|temporarily unavailable", re.I)
MAX_ATTEMPTS = int(os.environ.get("ZR_MAX_UNIT_ATTEMPTS", "3"))
LLM_BACKENDS = {"openrouter", "files"}
RUN_HISTORY_CAP = 20
VERDICT_OK = {"supported"}
SCOPE_FIELDS = ("skill", "level", "equipment", "basis", "modality")
# Note file names: ASCII letters, digits and a few signs; no leading/trailing space or dot.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.'()&+!%=-]*[A-Za-z0-9)!%']$")
MAX_NAME_BYTES = 180
REVIEW_KINDS = {"synth", "review", "repair", "pending"}
REVIEW_CONTEXT_S = 90
# Neutral backlink lines (the harness does not judge which note replaces which).
LINK_LINE = {
    "disagreement": "Other side of a disagreement: [[{src}]]",
    "supersedes-candidate": "Related newer note: [[{src}]]",
    "related": "Related newer note: [[{src}]]",
}


class HarnessError(RuntimeError):
    """A harness precondition failed (missing queue, missing registry, finalized twice)."""


def _now() -> str:
    return provenance.now_iso()


def _append(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


_JSON_SNAPSHOT_CACHE: dict[Path, tuple[tuple[int, int, int, int, int], Any]] = {}
_JSON_SNAPSHOT_CACHE_LIMIT = 1024


def _file_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _read_json_snapshot(path: Path, default: Any) -> Any:
    """Read-only JSON snapshot for repeated finalize reads; callers must not mutate the result.

    File identity and timestamps make external atomic replacements visible. Harness writes also
    evict the entry directly, so a later read sees the newly published state immediately.
    """
    if path.name not in {"repair_state.json", "marked.json"}:
        return _read_json(path, default)
    key = path.absolute()
    signature = _file_signature(path)
    if signature is None:
        _JSON_SNAPSHOT_CACHE.pop(key, None)
        return default
    cached = _JSON_SNAPSHOT_CACHE.get(key)
    if cached and cached[0] == signature:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _JSON_SNAPSHOT_CACHE.pop(key, None)
        return default
    if signature == _file_signature(path):
        if key not in _JSON_SNAPSHOT_CACHE and len(_JSON_SNAPSHOT_CACHE) >= _JSON_SNAPSHOT_CACHE_LIMIT:
            _JSON_SNAPSHOT_CACHE.pop(next(iter(_JSON_SNAPSHOT_CACHE)))
        _JSON_SNAPSHOT_CACHE[key] = (signature, data)
    return data


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    _JSON_SNAPSHOT_CACHE.pop(path.absolute(), None)
    _invalidate_rename_index_for_marked(path)


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256(p: Path) -> str | None:
    return _sha256_bytes(p.read_bytes()) if p.is_file() else None


def _queue_items(staging: Path) -> list[dict[str, Any]]:
    q = staging / "queue.json"
    if not q.exists():
        return []
    return _read_json(q, {}).get("items", [])


# --------------------------------------------------------------------------- #
# Locks and process identity
# --------------------------------------------------------------------------- #


def state_dir() -> Path:
    """Per-user state of the harness (locks, registry): $ZR_STATE_DIR, else
    $XDG_STATE_HOME/zettel_ralph, else ~/.local/state/zettel_ralph. Never in a vault."""
    if os.environ.get("ZR_STATE_DIR"):
        return Path(os.environ["ZR_STATE_DIR"])
    if os.environ.get("ZR_TEST_RUN") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        # Round 19 (pilot 7, item 8): a test never writes the owner's state folder, even when
        # a subprocess environment dropped ZR_STATE_DIR.
        import tempfile  # noqa: PLC0415

        d = Path(tempfile.gettempdir()) / f"zr-test-state-{os.getuid()}"
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "zettel_ralph"


def _vault_key(zk_dir: Path) -> str:
    return hashlib.sha1(str(zk_dir.resolve()).encode("utf-8")).hexdigest()[:16]


def lock_file(staging: Path, zk_dir: Path) -> Path:
    """Publish lock: one per vault (resolved path), shared by every staging that feeds it."""
    return state_dir() / "locks" / f"vault-{_vault_key(zk_dir)}.lock"


@contextlib.contextmanager
def vault_lock(staging: Path, zk_dir: Path):
    path = lock_file(staging, zk_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def proc_start(pid: int) -> str | None:
    """Start time of a process (field 22 of /proc/<pid>/stat); defeats pid reuse."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return None
    rest = stat.rsplit(")", 1)[-1].split()
    return rest[19] if len(rest) > 19 else None


def owner_alive(info: dict[str, Any]) -> bool:
    pid = info.get("owner_pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    want = info.get("owner_start")
    have = proc_start(int(pid))
    return not (want and have and want != have)


# --------------------------------------------------------------------------- #
# Transcript registry (several stagings feed one vault)
# --------------------------------------------------------------------------- #


def _registry_file(staging: Path) -> Path:
    """One registry for all vaults, keyed by the resolved vault path (any staging parent)."""
    return state_dir() / "vault_registry.json"


def registry_add(zk_dir: Path, staging: Path) -> list[str]:
    """Register a staging for a vault and write lit_dirs.txt so that every staging of
    that vault finds the lit folders of the others."""
    reg_path = _registry_file(staging)
    reg = _read_json(reg_path, {})
    key = str(zk_dir.resolve())
    stagings = sorted(set(reg.get(key, [])) | {str(staging.resolve())})
    reg[key] = stagings
    _write_json(reg_path, reg)
    for s in stagings:
        parent = Path(s).parent
        listing = parent / "lit_dirs.txt"
        lines = listing.read_text(encoding="utf-8").splitlines() if listing.exists() else []
        have = {(parent / ln.split("#")[0].strip()).resolve() for ln in lines if ln.split("#")[0].strip()}
        for other in stagings:
            lit = Path(other) / "lit"
            if lit.resolve() not in have:
                lines.append(os.path.relpath(lit, parent) if Path(other).parent == parent else str(lit))
                have.add(lit.resolve())
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stagings


def registry_check(staging: Path, zk_dir: Path) -> list[str]:
    """Record this staging for the vault. Fail when another staging feeds the vault
    and its lit folder is not in this staging's registry (lit_dirs)."""
    reg_path = _registry_file(staging)
    reg = _read_json(reg_path, {})
    key = str(zk_dir.resolve())
    stagings = sorted(set(reg.get(key, [])) | {str(staging.resolve())})
    if stagings != reg.get(key):
        reg[key] = stagings
        _write_json(reg_path, reg)
    known = {d.resolve() for d in verify_claims.lit_dirs(staging)}
    missing = [s for s in stagings if (Path(s) / "lit").is_dir() and (Path(s) / "lit").resolve() not in known]
    if missing:
        raise HarnessError(
            f"{len(stagings)} stagings feed the vault {zk_dir}, but the transcript registry of {staging} "
            f"misses {missing}. Run: python3 run_integrity.py registry --vault '{zk_dir}' --add '<staging>' "
            "for each staging (it writes lit_dirs.txt).")
    return stagings


# --------------------------------------------------------------------------- #
# start, unit-start, recovery, vault audit
# --------------------------------------------------------------------------- #


def start(staging: Path, kind: str, zk_dir: Path | None = None, harness: Path = HERE) -> Path:
    if os.environ.get("ZR_TEST_RUN") == "1" and (staging.resolve() == HERE or HERE in staging.resolve().parents):
        raise HarnessError(f"test run: refusing a staging inside the code tree ({staging}); use a temp folder")
    try:
        import synth_call  # noqa: PLC0415 - synth_call imports this module

        import prompt_build  # noqa: PLC0415

        # M7 (round 10): the built prompts must match prompts/build_manifest.json; a prompt
        # that changed without a reviewed manifest update stops the run before any call.
        problems = prompt_build.check_manifest()
        if problems and os.environ.get("ZR_TEST_RUN") == "1" and os.environ.get("PYTEST_CURRENT_TEST") \
                and os.environ.get("ZR_TEST_MANIFEST") != "1":
            problems = []  # unit tests test the code; `prompt_build.py --check-manifest` tests the prompts
        if problems:
            raise HarnessError("prompt manifest is not current: " + "; ".join(problems))
        built_prompts = synth_call.prompt_hashes()  # PromptBuildError here stops the run before any call
    except ImportError:
        built_prompts = {}
    except ValueError as exc:
        raise HarnessError(f"model prompt cannot be built: {exc}") from exc
    be = os.environ.get("ZR_LLM_BACKEND", "openrouter").strip() or "openrouter"
    if be not in LLM_BACKENDS:  # X13 (round 22): an unknown value is a hard error in every driver
        raise HarnessError(f"ZR_LLM_BACKEND={os.environ.get('ZR_LLM_BACKEND')!r} is not one of {sorted(LLM_BACKENDS)}")
    unknown = cost_unknown_models() if be != "files" else []
    if unknown and os.environ.get("ZR_ALLOW_UNKNOWN_COST") != "1":
        raise HarnessError(f"no cost source for {unknown}: the provider reports no cost and no price is known. "
                           "Set ZR_PRICE_IN and ZR_PRICE_OUT (USD per million tokens), or ZR_ALLOW_UNKNOWN_COST=1 "
                           "to run without a budget")
    if not (staging / "queue.json").exists() and kind.startswith(("synth", "repair")):
        raise HarnessError(f"no queue.json in {staging}: run ingest first")
    if zk_dir:
        registry_check(staging, zk_dir)
    commits = retry_pending_commits(staging)
    recovered = recover(staging, zk_dir) if zk_dir else []
    home = ensure_home(staging, zk_dir) if zk_dir else None
    base = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
    run_id, k = base, 1
    while True:
        run_dir = staging / "runs" / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            k += 1
            run_id = f"{base}-{k}"
    (staging / "logs" / run_id).mkdir(parents=True, exist_ok=True)
    settings = provenance.settings_fingerprint(harness)
    audit = vault_audit(staging, zk_dir) if zk_dir else {}
    meta = {"run_id": run_id, "kind": kind, "started": _now(), "staging": str(staging),
            "vault": str(zk_dir or ""), "lit_dirs": [str(d) for d in verify_claims.lit_dirs(staging)],
            "settings": settings, "recovered_units": recovered, "vault_audit": audit,
            "commits_retried": commits, "checker_sha256": checker_hash(), "home_created": home,
            "prompt_sha256": built_prompts,
            # round 15 (item 9): which backend answers the calls of this run
            "llm_backend": os.environ.get("ZR_LLM_BACKEND", "openrouter").strip() or "openrouter"}
    _write_json(run_dir / "run.json", meta)
    provenance.rotate_backups(staging)
    provenance.record(staging, "run-started", run_id=run_id, kind=kind, settings=settings)
    release_waiting(staging, kind)
    return run_dir


def release_waiting(staging: Path, kind: str = "") -> list[str]:
    """Round 13: at run start, items that waited for a reply in the last pass go back to
    their old stage (the new pass consumes the replies), and repair batches that waited
    are handed out again. W19 (round 21): only the waits of THIS driver kind (a synthesis
    run releases queue items, a repair run releases repair batches)."""
    out = []
    q_path = staging / "queue.json"
    do_queue = not kind or not kind.startswith(("repair", "review"))
    do_repair = not kind or kind.startswith("repair")
    if q_path.exists() and do_queue:
        with state_lock(staging):
            q = json.loads(q_path.read_text(encoding="utf-8"))
            for it in q.get("items", []):
                if it.get("stage") == "waiting-for-reply":
                    it["stage"] = it.pop("claimed_from", None) or "extracted"
                    out.append(it["id"])
            if out:
                _write_json(q_path, q)
    rs = staging / "repair_state.json"
    st = _read_json(rs, None) if do_repair else None
    if st:
        changed = False
        for b in (st.get("batches") or {}).values():
            if b.get("status") == "waiting":
                b["status"] = "running"
                b["released"] = True  # W21: a released wait is not a failed hand-out
                changed = True
        if changed:
            _write_json(rs, st)
    return out


def run_cost(run_dir: Path) -> float:
    """Summed cost (USD) of every agent turn and review call of the run so far."""
    return round(sum(float(_unit_usage(u).get("cost") or 0) for u in (run_dir / "units").glob("*") if u.is_dir()), 6)


def next_call_estimate() -> float:
    """The reservation of a typical next call: ZR_BUDGET_NEXT_CALL_USD, else the most a
    synthesis call can cost (40,000 prompt tokens plus ZR_SYNTH_MAX_TOKENS at the price of
    ZR_SYNTH_MODEL)."""
    raw = os.environ.get("ZR_BUDGET_NEXT_CALL_USD")
    if raw:
        return float(raw)
    model = os.environ.get("ZR_SYNTH_MODEL") or "anthropic/claude-sonnet-5.5"
    p = pricing.price(model) or (3.0, 15.0)
    return round(40000 / 1e6 * p[0] + int(os.environ.get("ZR_SYNTH_MAX_TOKENS", "24000")) / 1e6 * p[1], 6)


def _budget_decision(run_dir: Path, est: float) -> dict[str, Any]:
    """Round 11 (G): THE budget rule, for every call path and for the driver. Over when a
    stop is already recorded (sticky: the run makes no further call), or when the cost plus
    the open reservations plus this call's reservation would pass ZR_MAX_COST_USD. The
    first call of a run is checked like any other."""
    if os.environ.get("ZR_LLM_BACKEND", "openrouter").strip() == "files":
        # Round 13: the files backend costs nothing; ZR_MAX_CALLS (requests emitted in this
        # run) is the limit, counted where the requests are written.
        lim = int(os.environ.get("ZR_MAX_CALLS", "0") or 0)
        n = len(_read_json(run_dir / "exchange_emitted.json", {}) or {})
        if lim > 0 and n >= lim and not (run_dir / "budget-stop.json").exists():  # S10: recorded
            _write_json(run_dir / "budget-stop.json", {"at": _now(), "limit_calls": lim, "emitted": n, "backend": "files"})
        return {"cost": 0.0, "reserved": 0.0, "limit": float(lim), "over": lim > 0 and n >= lim, "next_call": 0.0,
                "res": {}, "backend": "files", "requests_emitted": n}
    limit = float(os.environ.get("ZR_MAX_COST_USD", "5"))
    res = _read_json(run_dir / "budget_reservations.json", {}) or {}
    reserved = round(sum(float(v) for v in res.values()), 6)
    cost = run_cost(run_dir) if run_dir.is_dir() else 0.0
    stopped = (run_dir / "budget-stop.json").exists()
    over = limit > 0 and (stopped or cost + reserved + est > limit)
    if over and not stopped:
        _write_json(run_dir / "budget-stop.json", {"at": _now(), "cost": cost, "reserved": reserved,
                                                   "next_call": est, "limit": limit})
    return {"cost": cost, "reserved": reserved, "limit": limit, "over": over, "next_call": est, "res": res}


def budget_status(run_dir: Path, est: float | None = None) -> dict[str, Any]:
    """The driver's check (before each unit, review or repair call): the same rule as
    `budget_reserve`, with the reservation of a typical next call."""
    est = next_call_estimate() if est is None else est
    with _run_lock(run_dir):
        d = _budget_decision(run_dir, est)
    d.pop("res")
    return d


@contextlib.contextmanager
def _run_lock(run_dir: Path):
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / ".budget.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def budget_reserve(run_dir: Path, est: float) -> dict[str, Any]:
    """Reserve the most a call can cost under the run lock (parallel workers cannot pass
    ZR_MAX_COST_USD together). Over the limit: nothing is reserved and the stop is recorded."""
    with _run_lock(run_dir):
        d = _budget_decision(run_dir, est)
        res = d.pop("res")
        if not d["over"]:
            res[f"{os.getpid()}-{time.time_ns()}"] = est
            _write_json(run_dir / "budget_reservations.json", res)
    return d


def budget_release(run_dir: Path, est: float) -> None:
    with _run_lock(run_dir):
        res = _read_json(run_dir / "budget_reservations.json", {}) or {}
        mine = [k for k, v in res.items() if k.startswith(f"{os.getpid()}-") and abs(float(v) - est) < 1e-9]
        if mine:
            del res[mine[0]]
            _write_json(run_dir / "budget_reservations.json", res)


HOME_TEXT = """---
type: index
status: draft
tags: [zettelkasten, index]
---

# Home

The index of this zettelkasten. The clustering pass adds one line for each map.
"""


def cost_unknown_models() -> list[str]:
    """Models of this run (synthesis and review) whose call cost is unknown: the provider
    reports none and no price is set (pricing.py)."""
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    models = {os.environ.get("DEEPSEEK_MODEL") or os.environ.get("MODEL") or "deepseek-v4-flash",
              os.environ.get("ZR_REVIEW_MODEL") or "anthropic/claude-sonnet-5.5"}
    if os.environ.get("ZR_SYNTH_MODE", "single") == "single":
        models.add(os.environ.get("ZR_SYNTH_MODEL") or "anthropic/claude-sonnet-5.5")
    return sorted(m for m in models if not pricing.price_known(m, base))


def ensure_home(staging: Path, zk_dir: Path) -> str | None:
    """A zettelkasten has `Home.md` from the first unit on: the fallback link target of
    `## Connected Ideas` (contract 5.1). The harness creates a minimal one when it is missing."""
    home = zk_dir / "Home.md"
    if home.exists() or not zk_dir.is_dir():
        return None
    with vault_lock(staging, zk_dir):
        if home.exists():
            return None
        tmp = home.with_name(".Home.md.zr-publish.tmp")
        tmp.write_text(HOME_TEXT, encoding="utf-8")
        os.replace(tmp, home)
        provenance.record(staging, "note-published", note="Home.md", by="harness", created=True,
                          verification="harness")
        if os.environ.get("ZR_GIT_COMMIT") == "1":
            _git_commit(zk_dir, ["Home.md"], "zettel: create Home.md (harness)", staging)
    return str(home)


def unit_start(run_dir: Path, kind: str, ids: list[str], review_note: str | None,
               owner_pid: int | None, label: str = "", options: dict[str, Any] | None = None) -> Path:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    first = re.sub(r"[^A-Za-z0-9_.-]", "_", (ids[0] if ids else Path(review_note or "unit").stem))[:60]
    base = f"{stamp}-{label or kind}-{first}"
    unit_dir = run_dir / "units" / base
    n = 1
    while True:
        try:
            unit_dir.mkdir(parents=True)
            break
        except FileExistsError:
            n += 1
            unit_dir = run_dir / "units" / f"{base}-{n}"
    (unit_dir / "out").mkdir()
    pid = owner_pid or os.getppid()
    _write_json(unit_dir / "unit.json", {"kind": kind, "ids": ids, "review_note": review_note,
                                         "owner_pid": pid, "owner_start": proc_start(pid), "started": _now(),
                                         "options": options or {}})
    (unit_dir / "started").write_text(str(_dt.datetime.now().timestamp()))
    return unit_dir


def recover(staging: Path, zk_dir: Path) -> list[dict[str, Any]]:
    """Finalize every dead unit that has no finalize record. Its output is not
    trusted: notes go to quarantine, except notes that the crashed finalize already
    published (intent record + vault sha); recovered paths are committed."""
    out = []
    reconcile_pipeline_writes(staging, zk_dir)  # X10
    for tmp in zk_dir.rglob(".*.zr-publish.tmp"):  # Z29 (round 23): a kill between copy and replace
        tmp.unlink(missing_ok=True)
    for unit_dir in sorted(staging.glob("runs/*/units/*")):
        if not unit_dir.is_dir() or (unit_dir / "finalized.json").exists():
            continue
        info = _read_json(unit_dir / "unit.json", {})
        if owner_alive(info):
            continue
        released = _release_cached_unit(staging, unit_dir, info)
        if released:
            out.append(released)
            continue
        try:
            ev = finalize(staging, unit_dir.parent.parent, unit_dir, list(info.get("ids") or []),
                          agent_rc=RECOVERED_RC, zk_dir=zk_dir, review_note=info.get("review_note"),
                          commit_message=f"zettel: recovered unit {unit_dir.name}", recovered=True)
        except HarnessError as exc:
            out.append({"unit": str(unit_dir), "error": str(exc)})
            continue
        ev.setdefault("stubs_healed", [])  # X1: no heal; list_beside below only lists
        out.append({"unit": str(unit_dir), "end": ev["end"],
                    "published": [n["note"] for n in ev["notes"] if n.get("action") == "published"],
                    "quarantined": [n["note"] for n in ev["notes"] if n.get("action") == "quarantined"],
                    "owner_pid": info.get("owner_pid")})
    list_beside(staging, zk_dir)  # X1 (round 22): list only, never write
    return out


def _release_cached_unit(staging: Path, unit_dir: Path, info: dict[str, Any]) -> dict[str, Any] | None:
    """Round 10 (m8): a dead single-mode synthesis unit whose complete reply is in the
    reply cache, and that published nothing, is not quarantined. Its item goes back to
    its old stage (no attempt counted); the next run takes the saved reply at no cost,
    and the mechanical check and the review run again on the notes."""
    if str(info.get("kind") or "synth") != "synth" or (unit_dir / "publish.jsonl").exists():
        return None
    try:
        import synth_call  # noqa: PLC0415 - synth_call imports this module
    except ImportError:
        return None
    if synth_call.cache_for_unit(staging, unit_dir) is None:
        return None
    ids = list(info.get("ids") or [])
    q_path = staging / "queue.json"
    if q_path.exists():
        with state_lock(staging):
            q = json.loads(q_path.read_text(encoding="utf-8"))
            for it in q["items"]:
                # The unit may have set the stage (synthesized, skipped) before it died.
                if it["id"] in ids and it.get("stage") in ("claimed", "synthesized", "skipped", "needs-attention"):
                    it["stage"] = it.pop("claimed_from", None) or "extracted"
                    it.pop("worker", None)
            _write_json(q_path, q)
    _write_json(unit_dir / "finalized.json", {"at": _now(), "end": "recovered-released",
                                              "detail": "complete reply in the cache; the item runs again"})
    provenance.record(staging, "unit-released", unit=unit_dir.name, ids=ids, reason="reply cache complete")
    return {"unit": str(unit_dir), "end": "recovered-released", "released": ids, "published": [], "quarantined": []}


def vault_audit(staging: Path, zk_dir: Path | None) -> dict[str, Any]:
    if zk_dir is None or not (zk_dir / PERMANENT_DIR).is_dir():
        return {}
    published = {r.get("note") for r in provenance.read_records(staging)
                 if r.get("event") in ("note-published", "note-verified")}
    unpublished, old_format, failed, needs_repair = [], 0, [], []
    for p in sorted((zk_dir / PERMANENT_DIR).glob("*.md")):
        fm, _, _ = verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        rel = p.relative_to(zk_dir).as_posix()
        ver = str(fm.get("verification") or "")
        if ver == "failed":
            failed.append(rel)
        if ver == "needs-repair":
            needs_repair.append(rel)
        if verify_claims.is_contract_note(fm):
            if rel not in published:
                unpublished.append(rel)
        else:
            old_format += 1
    return {"contract_notes_without_publish_record": unpublished, "old_format_notes": old_format,
            "notes_with_verification_failed": failed, "notes_needs_repair": needs_repair,
            "note": "provenance of other stagings that feed this vault is not read; check them too"}


# --------------------------------------------------------------------------- #
# precheck
# --------------------------------------------------------------------------- #


def _save_queue(q_path: Path, q: dict) -> None:
    tmp = q_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(q, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, q_path)


def _lit_path(staging: Path, item: dict[str, Any]) -> Path | None:
    raw = item.get("lit_note")
    candidates = []
    if raw:
        p = Path(raw)
        candidates += [p] if p.is_absolute() else [HERE / p, staging.parent / p, staging / p]
    candidates.append(staging / "lit" / f"{item.get('id')}.md")
    return next((c for c in candidates if c.is_file()), None)


def precheck(staging: Path, run_dir: Path, ids: list[str]) -> list[str]:
    """Set units with a degraded transcript to `held` (ingest's stage name).
    `partial` and `unknown` transcripts are runnable."""
    q_path = staging / "queue.json"
    if not q_path.exists():
        raise HarnessError(f"no queue.json in {staging}")
    keep, held = [], []
    with state_lock(staging):
        q = json.loads(q_path.read_text(encoding="utf-8"))
        unknown = sorted(set(ids) - {it["id"] for it in q["items"]})
        if unknown:
            raise HarnessError(f"ids {unknown} are not in {q_path}")
        for it in q["items"]:
            if it["id"] not in ids:
                continue
            lit = _lit_path(staging, it)
            quality = "unknown"
            if lit is not None:
                fm, _, _ = verify_claims.split_frontmatter(lit.read_text(encoding="utf-8", errors="replace"))
                quality = str(fm.get("asr_quality") or "unknown")
            if quality == "degraded":
                it["stage"] = "held"
                it.pop("claimed_from", None)
                it.pop("worker", None)
                reason = "asr_quality degraded: no note (NOTE_CONTRACT.md section 1)"
                it.setdefault("hold_reasons", [])
                if reason not in it["hold_reasons"]:
                    it["hold_reasons"].append(reason)
                held.append(it["id"])
            else:
                keep.append(it["id"])
        if held:
            _save_queue(q_path, q)
    if held:
        _append(run_dir / "events.jsonl", {"t": _now(), "event": "held-degraded", "ids": held})
    return keep


# --------------------------------------------------------------------------- #
# Checks of one shadow file
# --------------------------------------------------------------------------- #


def classify_end(unit_dir: Path, agent_rc: int) -> tuple[str, str, bool]:
    """Return (end, detail, transient). end: finished | limit | timeout | killed | error | crashed."""
    res = _read_json(unit_dir / "agent_result.json", {})
    end, detail = res.get("end"), str(res.get("detail", ""))
    if agent_rc == RECOVERED_RC:
        end = "crashed"
    elif agent_rc == 124:
        end = "timeout"
    elif agent_rc in (137, -9):
        end = "killed"  # SIGKILL: not a timeout
    elif agent_rc == 143:
        end = "timeout" if end == "timeout" else "killed"
    elif agent_rc == 3:
        end = "limit"
    elif agent_rc != 0:
        end = end if end in ("limit", "timeout", "error") else "error"
    elif end is None:
        end = "finished"
    if agent_rc == 0 and end == "limit" and (unit_dir / "fix-turn-limit.json").exists():
        # The synthesis turn finished; only a fix or review-repair turn hit its own token
        # limit. The unit is finished; notes that still fail are judged as they are.
        end = "finished"
        detail = (detail + " " if detail else "") + "(a fix turn ended at its token limit)"
    log_tail = ""
    log = unit_dir / "agent.log"
    if log.exists():
        with open(log, "rb") as f:
            f.seek(max(0, log.stat().st_size - 4000))
            log_tail = f.read().decode("utf-8", errors="replace")
    transient = end == "error" and bool(TRANSIENT.search(detail + "\n" + log_tail))
    return end, secret_scan.scrub(detail or log_tail[-500:].strip()), transient


def _queue_kinds(staging: Path, ids: list[str]) -> set[str]:
    return {str(it.get("kind") or "") for it in _queue_items(staging) if it.get("id") in ids}


def _path_allowed(rel: str, kind: str) -> str | None:
    parts = rel.split("/")
    name = parts[-1]
    if not name.endswith(".md"):
        return "only lower-case .md note files are published" + (" (got .MD)" if name.lower().endswith(".md") else "")
    if name.startswith("."):
        return "hidden file"
    if kind == "clustering":
        if rel == "Home.md" or (len(parts) == 2 and parts[0] == MAPS_DIR):
            return None
        return f"a clustering unit publishes only '{MAPS_DIR}/<name>.md' and 'Home.md'"
    if len(parts) == 2 and parts[0] == PERMANENT_DIR:
        return None
    return f"this unit publishes only '{PERMANENT_DIR}/<name>.md' (no subfolders, no other folders)"


def name_key(stem: str) -> str:
    """Name after NFKC, case folding, whitespace collapse and trailing dot/space strip."""
    s = unicodedata.normalize("NFKC", stem).casefold()
    s = " ".join(s.split())
    return s.rstrip(" .")


def _name_problem(stem: str) -> str | None:
    if len(stem.encode("utf-8")) > MAX_NAME_BYTES:
        return f"the name is longer than {MAX_NAME_BYTES} bytes"
    if not SAFE_NAME.match(stem) or "  " in stem:
        bad = sorted({c for c in stem if not re.match(r"[A-Za-z0-9 ,.'()&+!%=-]", c)})
        return ("unsafe characters " + ", ".join(f"U+{ord(c):04X}" for c in bad) if bad
                else "the name must start and end with a letter or digit and have single spaces")
    return None


def _scope_of(fm: dict[str, Any]) -> dict[str, str]:
    sc = fm.get("scope") if isinstance(fm.get("scope"), dict) else {}
    return {k: " ".join(str(sc.get(k) or "").lower().split()) for k in SCOPE_FIELDS}


def _speakers_of(fm: dict[str, Any]) -> set[str]:
    return {str(s.get("speaker") or "") for s in fm.get("sources") or [] if isinstance(s, dict)}


def _fold_failures(old_fm: dict[str, Any], new_fm: dict[str, Any], kind: str) -> list[dict[str, str]]:
    out = []
    if not verify_claims.is_contract_note(old_fm):
        if kind != "repair":
            out.append({"type": "old-note-edited", "where": "note",
                        "detail": "an old-format note may change only in a repair unit (contract 7.4)"})
        return out
    if kind == "repair" or verify_claims.is_stub(new_fm):
        return out
    if _speakers_of(new_fm) != _speakers_of(old_fm):
        out.append({"type": "fold-scope", "where": "sources",
                    "detail": f"speakers {_speakers_of(old_fm)} -> {_speakers_of(new_fm)}"})
    if _scope_of(new_fm) != _scope_of(old_fm):
        out.append({"type": "fold-scope", "where": "scope",
                    "detail": f"scope changed {_scope_of(old_fm)} -> {_scope_of(new_fm)}"})
    added = set(verify_claims._source_ids(new_fm)) - set(verify_claims._source_ids(old_fm))
    if added and ("not stated" in _scope_of(old_fm).values() or "not stated" in _speakers_of(old_fm)):
        out.append({"type": "fold-scope", "where": "sources",
                    "detail": f"new source {sorted(added)} folded into a note with a 'not stated' value"})
    return out


# Verdict values of NOTE_CONTRACT.md section 9.
KNOWN_VERDICTS = {"transcript-missing", "no-tag", "quote-mismatch", "invented-correction", "not-in-source",
                  "wrong-speaker", "changed-number", "changed-unit", "changed-period", "changed-modality",
                  "dropped-qualifier", "changed-scope", "supported"}


def _norm_verdict(v: Any) -> str:
    return re.sub(r"[\s_]+", "-", str(v or "").strip().lower())


def review_items(text: str) -> list[dict[str, str]]:
    """The items a source-check verdict must cover (prompts/review.md): title, scope,
    lead, each Details bullet, the example, each reason (Why This Matters) and each
    Evidence item."""
    fm, body, _ = verify_claims.split_frontmatter(text)
    b = verify_claims.parse_body_full(body)
    items = [{"item": "title", "text": b.h1}, {"item": "scope", "text": json.dumps(fm.get("scope"), ensure_ascii=False)},
             {"item": "lead", "text": b.lead}]
    sec = b.sections.get("Details")
    for n, bullet in enumerate(sec.bullets() if sec else [], 1):
        items.append({"item": f"details-{n}", "text": bullet})
    sec = b.sections.get("Example From The Source")
    if sec is not None and sec.text().strip():
        items.append({"item": "example", "text": sec.text().strip()})
    sec = b.sections.get("Why This Matters")
    if sec is not None and sec.text().strip():
        reasons = sec.bullets() or [sec.text().strip()]
        for n, r in enumerate(reasons, 1):
            items.append({"item": f"reason-{n}", "text": r})
    sec = b.sections.get("Evidence")
    for n, bullet in enumerate(sec.bullets() if sec else [], 1):
        items.append({"item": f"evidence-{n}", "text": bullet})
    return items


def review_skeleton(rel: str, text: str) -> dict[str, Any]:
    return {"note": rel, "scope_complete": None,
            "items": [{"item": it["item"], "text": it["text"], "tag": _item_tags(it["text"]), "verdict": "",
                       "also": [], "transcript_text": "", "problem": ""}
                      for it in review_items(text)]}


def _item_tags(text: str) -> str:
    """The source tags of an item ("vid-x @ 01:50"), for the reviewer to find its passage."""
    tags = [f"{m.group(1)}" + (f" @ {m.group(2)}" if m.group(2) else "") for m in verify_claims.SRC_TAG.finditer(text)]
    m = verify_claims.EVIDENCE_ITEM.match("- " + text)
    if m:
        tags.append(m.group(1) + (f" @ {m.group(2)}" if m.group(2) else ""))
    return "; ".join(dict.fromkeys(tags))


def review_record(review_dir: Path, end: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The harness record of one review call (round 6): how it ended and the sha256 of
    the verdict file that the harness wrote."""
    req = _read_json(review_dir / "request.json", {})
    rec = {"note": req.get("note"), "sha256": req.get("sha256"), "agent_rc": 0 if end == "finished" else 1,
           "end": end, "verdict_sha256": _sha256(review_dir / "verdict.json"), "recorded_at": _now(), "by": "harness",
           **(extra or {})}
    _write_json(review_dir / "review.json", rec)
    return rec


def review_begin(unit_dir: Path, rel: str, text: bytes, source: str = "shadow") -> Path:
    """Create the folder of ONE review agent for ONE note: request.json (note, sha256 of
    the reviewed text, the item skeleton). Only the harness writes it."""
    n = 0
    while (unit_dir / f"review-{n}").exists():
        n += 1
    d = unit_dir / f"review-{n}"
    d.mkdir(parents=True)
    t = text.decode("utf-8", errors="replace")
    sk = review_skeleton(rel, t)
    _write_json(d / "request.json", {"note": rel, "sha256": _sha256_bytes(text), "source": source,
                                     "expected_items": [it["item"] for it in sk["items"]], "started": _now(),
                                     "content_hash": reviewed_hash(t)})
    _write_json(d / "skeleton.json", sk)
    return d


def reviewed_hash(text: str) -> str:
    """Round 16 (6c): a hash over the content that the review judged: the title's claim
    text (the speaker prefix normalized), the lead and Details paragraphs and every other
    section except Connected Ideas (links reduced to their text), the Evidence quotes with
    their source tags, scope (incl. modality, basis, quantities) and the speakers. A change of
    links, title spelling of the speaker, or other frontmatter keeps the hash."""
    fm, body, _ = verify_claims.split_frontmatter(text)
    vb = verify_claims.parse_body_full(body)
    speakers = sorted(str(s.get("speaker") or "") for s in fm.get("sources") or [] if isinstance(s, dict))
    title = re.sub(r"[^a-z0-9 ]", "", (vb.h1 or "").replace("_", " ").lower())
    for sp in sorted(speakers, key=len, reverse=True):
        sp_n = re.sub(r"[^a-z0-9 ]", "", sp.replace("_", " ").lower()).strip()
        if sp_n and title.startswith(sp_n + " "):
            title = title[len(sp_n):]
            break
    unlink = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
    parts = [" ".join(title.split()), unlink.sub(lambda m: m.group(2) or m.group(1), vb.lead or "")]
    for name, sec in sorted(vb.sections.items()):
        if name == "Connected Ideas":
            continue
        parts.append(name + "\n" + unlink.sub(lambda m: m.group(2) or m.group(1), "\n".join(sec.lines)))
    parts.append(json.dumps(fm.get("scope") or {}, sort_keys=True, ensure_ascii=False))
    parts.append(json.dumps(speakers))
    return hashlib.sha256("\n\x00".join(parts).encode("utf-8")).hexdigest()


def review_end(review_dir: Path, agent_rc: int) -> dict[str, Any]:
    """The harness records how the review agent ended and which verdict file it left."""
    req = _read_json(review_dir / "request.json", {})
    vf = review_dir / "verdict.json"
    end = classify_end(review_dir, agent_rc)[0] if (review_dir / "agent_result.json").exists() else "no-agent-result"
    rec = {"note": req.get("note"), "sha256": req.get("sha256"), "agent_rc": agent_rc, "end": end,
           "verdict_sha256": _sha256(vf), "recorded_at": _now(), "by": "harness"}
    _write_json(review_dir / "review.json", rec)
    return rec


def verdict_obj_unusable(obj: Any, expected: list[str]) -> bool:
    """Z22 (round 23): True only when the full verdict parse (`_parse_verdict`) calls the
    verdict unusable (a usable rejection is never counted as invalid)."""
    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(obj, fh)
        path = Path(fh.name)
    try:
        return bool(_parse_verdict(path, expected).get("unusable"))
    finally:
        path.unlink(missing_ok=True)


def parse_verdict_usable(obj: Any) -> str | None:
    """Pilot 9: None when a well-formed verdict is usable, else why not (transcript-missing,
    scope not complete). Item coverage is checked by the caller."""
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return None
    verdicts = [str(it.get("verdict")) for it in obj["items"] if isinstance(it, dict)]
    also = [str(x) for it in obj["items"] if isinstance(it, dict) for x in (it.get("also") or [])]
    if any(v not in VERDICT_OK and v != "transcript-missing" for v in verdicts + also):
        return None  # Z22 (round 23): a usable rejection (the same rule as _parse_verdict)
    if "transcript-missing" in verdicts + also:
        return "the reviewer could not read the transcript"
    if obj.get("scope_complete") is not True:
        return "scope_complete is not true"
    return None


def _parse_verdict(vf: Path, expected: list[str]) -> dict[str, Any]:
    """Coverage rules: one dict item per expected item, verdicts from the contract list,
    `also` empty, `scope_complete` true. Returns bad (non-supported verdicts present) or
    unusable (incomplete or malformed)."""
    data = _read_json(vf, None)
    if not isinstance(data, dict):
        return {"unusable": "the verdict file is not a JSON object", "bad": [], "counts": {}}
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return {"unusable": "no 'items' list", "bad": [], "counts": {}}
    if not all(isinstance(it, dict) for it in items):
        return {"unusable": "an item is not an object", "bad": [], "counts": {}}
    ids = [str(it.get("item") or "") for it in items]
    if sorted(ids) != sorted(expected):
        missing = sorted(set(expected) - set(ids))
        extra = sorted(set(ids) - set(expected))
        dup = sorted({i for i in ids if ids.count(i) > 1})
        return {"unusable": f"items do not match the note: missing {missing}, unknown {extra}, twice {dup}",
                "bad": [], "counts": {}}
    verdicts = [_norm_verdict(it.get("verdict")) for it in items]
    also = [_norm_verdict(a) for it in items for a in (it.get("also") or [])]
    unknown = sorted({v for v in verdicts + also if v not in KNOWN_VERDICTS})
    counts = dict(Counter(verdicts))
    if unknown:
        return {"unusable": f"unknown verdict values {unknown}", "bad": [], "counts": counts}
    bad = sorted({v for v in verdicts + also if v not in VERDICT_OK and v != "transcript-missing"})
    sc = data.get("scope_complete")
    if sc is False or str(sc).lower() == "false":
        bad.append("scope-incomplete")
    if bad:
        return {"bad": bad, "counts": counts}
    if "transcript-missing" in verdicts + also:
        return {"unusable": "the reviewer could not read the transcript", "bad": [], "counts": counts}
    if sc is not True and str(sc).lower() != "true":
        return {"unusable": "scope_complete is not true", "bad": [], "counts": counts}
    return {"bad": [], "counts": counts}


REVIEW_INVALID = "review_invalid.json"
REVIEW_INVALID_MAX = 2


def review_invalid_count_any(staging: Path, rel: str) -> int:
    """The highest unusable-review count of any text version of `rel`."""
    reg = _read_json(staging / REVIEW_INVALID, {}) or {}
    return max([int(v or 0) for k, v in reg.items() if k.split("#", 1)[0] == rel] or [0])


def review_invalid_count(staging: Path, rel: str, text: bytes) -> int:
    """Pilot 9: the unusable review replies (files backend) recorded for exactly this text."""
    return int(((_read_json(staging / REVIEW_INVALID, {}) or {}).get(f"{rel}#{_sha256_bytes(text)}") or 0))


def record_review_invalid(staging: Path, rel: str, text: bytes) -> int:
    reg = _read_json(staging / REVIEW_INVALID, {}) or {}
    k = f"{rel}#{_sha256_bytes(text)}"
    reg[k] = int(reg.get(k) or 0) + 1
    _write_json(staging / REVIEW_INVALID, reg)
    return reg[k]


def verdict_for(unit_dir: Path, rel: str, text: bytes) -> dict[str, Any] | None:
    """The verdict of a harness-recorded review of exactly this note text, or None.

    A review counts only when the harness wrote review.json for it: the review agent of
    this unit and this note ran to a normal end, the reviewed text has the same sha256 as
    the text that is published, and verdict.json is the file it left (same sha256)."""
    sha = _sha256_bytes(text)
    reasons: list[str] = []
    codes: list[str] = []
    for d in sorted(unit_dir.glob("review-*"), key=lambda p: int(p.name.split("-")[-1]) if p.name.split("-")[-1].isdigit() else -1,
                    reverse=True):
        rec = _read_json(d / "review.json", None)
        req = _read_json(d / "request.json", {})
        if not isinstance(rec, dict) or rec.get("by") != "harness" or rec.get("note") != rel:
            continue
        same_content = req.get("content_hash") and req.get("content_hash") == reviewed_hash(
            text.decode("utf-8", errors="replace"))
        if (rec.get("sha256") != sha or req.get("sha256") != sha) and not same_content:
            reasons.append(f"{d.name}: the note changed after its review")
            codes.append("review-stale")
            continue
        if rec.get("agent_rc") != 0 or rec.get("end") != "finished":
            reasons.append(f"{d.name}: the review ended by {rec.get('end')} (rc {rec.get('agent_rc')})"
                           + (f": {rec.get('detail')}" if rec.get("detail") else ""))
            codes.append(str(rec.get("end")) if str(rec.get("end", "")).startswith(("review-", "budget", "waiting"))
                         else "review-error")
            continue
        vf = d / "verdict.json"
        if not vf.is_file() or _sha256(vf) != rec.get("verdict_sha256"):
            reasons.append(f"{d.name}: no verdict file, or it changed after the review ended")
            codes.append("review-invalid")
            continue
        v = _parse_verdict(vf, list(req.get("expected_items") or []))
        v["path"] = str(vf)
        if v.get("unusable"):
            v["code"] = "review-invalid"
        if rec.get("sha256") != sha and (v.get("bad") or v.get("unusable")):
            # round 16 (6c): only a SUPPORTED review carries over to a text with the same
            # reviewed content; a rejection always gets a new review of the new text
            reasons.append(f"{d.name}: the note changed after its review")
            codes.append("review-stale")
            continue
        if rec.get("sha256") != sha:
            v["carried_over"] = True
        return v
    if reasons:
        return {"unusable": "; ".join(reasons), "code": codes[0], "bad": [], "counts": {}, "path": None}
    return None


INJECTION = re.compile(
    r"note to (the )?([\w-]+ )?(reviewer|checker|verifier|model|assistant|reader)|dear reader|"
    r"\bto the ([\w-]+ )?(reviewer|checker|verifier)\b|dear (reviewer|checker|model|assistant)|"
    r"\b(reviewer|checker|verifier|source[- ]?check(er)?)\s*:|"
    r"set (every|all|each|the) (\w+ )?verdicts?|verdicts? (to|as) [\"']?supported|scope_complete|"
    r"answer (with )?[\"']?supported|mark (every|all|each) (\w+ )?(item|items) (as )?[\"']?supported|"
    r"ignore (all |any |the |your )?(previous|prior|above|earlier|other)?\s*(instructions|rules|prompt)|"
    r"ignore (all |any |the )?(previous|prior|above|earlier)|disregard (all |the )?(previous|prior|above)|"
    r"system prompt|you are (an?|the) (ai|assistant|language model|reviewer|model)|as an ai\b", re.I)


def _reader_texts(vb: Any) -> list[tuple[str, str]]:
    """Every non-quote text of the note: title, lead, every line of every section; in
    `## Evidence` the text around the quote (the quote is transcript text)."""
    out = [("title", vb.h1 or ""), ("lead", vb.lead or "")]
    for name, sec in vb.sections.items():
        for n, ln in enumerate([x for x in sec.lines if x.strip()], 1):
            if name == "Evidence":
                ln = re.sub(r'"[^"]*"', '""', ln.translate(verify_claims.QUOTE_CHARS))
            out.append((f"{name}[{n}]", ln))
    return out


SHAPE = {
    # section -> (allowed line pattern, what the line must be)
    "Details": (re.compile(r"^\s*[-*]\s+\S|^\s{2,}\S"), "a bullet"),
    "Evidence": (re.compile(r"^\s*[-*]\s*" + verify_claims.VID + r"(\s*@\s*\S+)?\s*\([^)]*\)\s*:\s*\"|^\s{2,}\S"),
                 "an evidence item: - vid-<id> @ MM:SS (<speaker>): \"<quote>\""),
    "Disagreement": (re.compile(r"^\s*[-*]\s+(This note|Other side)\s*:"), "a '- This note:' or '- Other side:' line"),
    "Connected Ideas": (re.compile(r"^\s*[-*]\s+.*\[\[[^\]]+\]\]"), "a bullet with a [[link]]"),
}


def marker_lines(body: str) -> list[dict[str, Any]]:
    """B item 4 (contract "Allowed lines"): no line of a note starts with `=====` (a reply
    marker that leaked into the note)."""
    return [{"type": "marker-line", "where": f"line {n}", "detail": ln[:120]}
            for n, ln in enumerate(body.split("\n"), 1) if ln.lstrip().startswith("=====")]


def shape_failures(vb: Any) -> list[dict[str, Any]]:
    """Round 10 (M5): each section of a contract note holds only its allowed line shapes."""
    out = []
    for name, (pat, what) in SHAPE.items():
        sec = vb.sections.get(name)
        for ln in (sec.lines if sec else []):
            if ln.strip() and not pat.match(ln):
                out.append({"type": "evidence-format" if name == "Evidence" else "section-shape", "where": name,
                            "detail": f"'{ln.strip()[:120]}' is not {what}"})
    return out


def _waiting_titles(staging: Path, unit_dir: Path) -> set[str]:
    """Titles that can still be published: shadow notes of the other unfinalized units of
    this run, and notes that wait in pending-review."""
    out: set[str] = set()
    run_units = unit_dir.parent
    for u in run_units.glob("*") if run_units.is_dir() else []:
        if u == unit_dir or not u.is_dir() or (u / "finalized.json").exists():
            continue
        out |= {p.stem for p in (u / "out" / PERMANENT_DIR).glob("*.md")}
    for p in (staging / "pending-review").glob(f"*/out/{PERMANENT_DIR}/*.md"):
        out.add(p.stem)
    return out


WAIT_TYPES = {"link-waiting"}
# Checker warnings that go to the reviewer as hints. Findings that the mechanical check
# already enforces (quote and bracket form, numbers) are never hints (round 8).
REVIEW_HINT_TYPES = ("low-quote-support", "added-qualifier", "dropped-hedge", "title-drops-frame")
MAX_PENDING_CYCLES = int(os.environ.get("ZR_MAX_PENDING_CYCLES", "5"))


def _unit_started(unit_dir: Path) -> float:
    """Unit start time minus 2 s (file mtimes have a coarser clock than time())."""
    try:
        return float((unit_dir / "started").read_text().strip()) - 2.0
    except (OSError, ValueError):
        return 0.0


class _Checker:
    """Mechanical checks of shadow files against the current vault."""

    def __init__(self, staging: Path, unit_dir: Path, zk_dir: Path, kind: str, item_kinds: set[str]):
        self.staging, self.unit_dir, self.zk, self.kind = staging, unit_dir, zk_dir, kind
        self.item_kinds = item_kinds
        self.lit = verify_claims.LitIndex(verify_claims.lit_dirs(staging))
        self.index = validate.VaultIndex(zk_dir)
        self.bases = _read_json(unit_dir / "bases.json", {})
        self.refused_folds = {str(e.get("file") or "") for e in _read_jsonl(unit_dir / "events.jsonl")
                              if e.get("event") == "fold-refused"}
        shadow = unit_dir / "out"
        self.shadow_files = sorted(p for p in shadow.rglob("*") if p.is_file()) if shadow.is_dir() else []
        self.index.add_titles({p.stem for p in self.shadow_files})
        if (zk_dir / "Home.md").is_file():
            self.index.add_titles({"Home"})
        self.options = _read_json(unit_dir / "unit.json", {}).get("options") or {}
        self.waiting_titles = _waiting_titles(staging, unit_dir)
        self.waiting_links: dict[str, list[str]] = _read_json(unit_dir / "unit.json", {}).get("waiting_links") or {}
        self.is_pending = bool(_read_json(unit_dir / "unit.json", {}).get("pending"))
        # Single mode (round 9): links only to [[Home]], a note of this unit, or a title of
        # the candidate list that the call was given.
        self.link_allowed: set[str] | None = None
        cand = _read_json(unit_dir / "candidates.json", None)
        if self.options.get("synth_mode") == "single" and isinstance(cand, dict):
            self.link_allowed = {"Home"} | {p.stem for p in self.shadow_files} | set(cand.get("titles") or [])
        self.vault_keys = {name_key(p.stem): p.stem for p in (zk_dir / PERMANENT_DIR).glob("*.md")} \
            if (zk_dir / PERMANENT_DIR).is_dir() else {}

    @property
    def video(self) -> bool:
        if self.kind == "repair":
            return True
        if self.item_kinds:
            return "video" in self.item_kinds
        return os.environ.get("ZR_CONTENT", "video") == "video"

    def needs_review(self, rel: str, fm: dict[str, Any]) -> bool:
        return (self.video and self.kind in REVIEW_KINDS and rel.startswith(PERMANENT_DIR + "/")
                and verify_claims.is_contract_note(fm) and not verify_claims.is_stub(fm)
                and str(fm.get("verification") or "") != "unsupported"
                # the old text kept for the operator (repair found no supporting passage)
                and not (str(fm.get("verification") or "") == "needs-repair" and fm.get("repair_note")))

    def check_text(self, rel: str, text: str, contract_required: bool, existed: bool) -> dict[str, Any]:
        target = self.zk / rel
        fm, body, _ = verify_claims.split_frontmatter(text)
        vb = verify_claims.parse_body_full(body)
        stub = verify_claims.is_stub(fm)
        unsupported = str(fm.get("verification") or "") == "unsupported"
        contract = verify_claims.is_contract_note(fm) or stub or unsupported
        failures: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        drop_links: list[str] = []
        status, report = "form-only", {}
        if rel.startswith(PERMANENT_DIR + "/") and (contract or contract_required):
            opts = verify_claims.Options(repair=self.kind == "repair",
                                         title_number_form=not (self.kind == "repair" and existed))
            report = verify_claims.verify_note(target, self.lit, opts, text=text)
            status = report["status"]
            warnings = report.get("warnings", [])
            if status == "not-contract":
                failures.append({"type": "not-contract", "where": "note",
                                 "detail": "a video unit publishes only notes that follow NOTE_CONTRACT.md: "
                                           + "; ".join(report.get("missing", []))})
            else:
                failures += report["failures"]
        val_errors, _w = validate.check_one(target, validate.parse_frontmatter(text)[0], body, vb.h1,
                                            verify_claims.is_contract_note(fm), self.zk, self.index,
                                            self.lit.dirs, text=text, rel=Path(rel), contract_check=False)
        if stub or unsupported:
            val_errors = [e for e in val_errors if "missing required section" not in e and "ORPHAN" not in e]
        failures += [{"type": "validate", "where": "form", "detail": e} for e in val_errors]
        if verify_claims.is_contract_note(fm) and not stub and not unsupported:
            failures += shape_failures(vb)
            failures += marker_lines(body)
        for where_i, txt in _reader_texts(vb):
            m = INJECTION.search(txt)
            if m:
                failures.append({"type": "injection-suspect", "where": where_i,
                                 "detail": f"the text addresses the reviewer or the model ('{m.group(0)}'); a note "
                                           "states claims of the speaker only"})
        own = {Path(rel).stem, vb.h1}
        for m in verify_claims.WIKILINK.finditer(body):
            if m.group(1).strip() in own and not stub:
                failures.append({"type": "self-link", "where": "links",
                                 "detail": f"[[{m.group(1).strip()}]] links the note to itself; it does not count as a link"})
                break
        if verify_claims.is_contract_note(fm) and not stub:
            # V29: a dead link in the lead or any other body section (validate's resolution)
            import validate as _v  # noqa: PLC0415
            other = body.split("\n## Connected Ideas")[0].split("\n## Disagreement")[0]
            for target in _v._note_links(other):
                if target in self.waiting_titles or _v._resolves(target, self.index):
                    continue
                if self.link_allowed is not None and target in self.link_allowed and target in self.index.titles:
                    continue
                failures.append({"type": "dead-link", "where": "body",
                                 "detail": f"[[{target}]] (lead or Details) is neither in the vault nor in this unit"})
            for sec_name in ("Connected Ideas", "Disagreement"):
                sec = vb.sections.get(sec_name)
                for m in verify_claims.WIKILINK.finditer(sec.text() if sec else ""):
                    target = m.group(1).strip()
                    if target and (target not in self.index.titles
                                   or (self.link_allowed is not None and target not in self.link_allowed)):
                        if self.kind == "repair" and existed and sec_name == "Connected Ideas":
                            # Repair in place: drop the old dead link and report it.
                            drop_links.append(target)
                            warnings.append({"type": "dead-link-dropped", "where": sec_name,
                                             "detail": f"[[{target}]] does not exist; the line is removed"})
                            continue
                        if target in self.waiting_titles:
                            # A note of another unit of this run, or a pending note: it can
                            # be published later. The note waits (pending, link-waiting).
                            failures.append({"type": "link-waiting", "where": sec_name, "target": target,
                                             "detail": f"[[{target}]] is not published yet (another unit or pending-review)"})
                            continue
                        if sec_name == "Connected Ideas" and (target in self.waiting_links.get(rel, []) or self.is_pending):
                            # The sibling that this note waited for failed (quarantined or
                            # deleted): drop the link at publish, [[Home]] when none is left.
                            drop_links.append(target)
                            warnings.append({"type": "link-target-failed", "where": sec_name,
                                             "detail": f"[[{target}]] was not published; the line is removed"})
                            continue
                        failures.append({"type": "dead-link", "where": sec_name,
                                         "detail": f"[[{target}]] is neither in the vault nor written in this unit"})
        return {"ok": not failures, "status": status, "failures": failures, "warnings": warnings,
                "report": report, "fm": fm, "stub": stub, "drop_links": drop_links}

    def check_file(self, p: Path, other_shadow_keys: dict[str, str]) -> dict[str, Any]:
        rel = p.relative_to(self.unit_dir / "out").as_posix()
        data = p.read_bytes()
        current = self.zk / rel
        existed = current.is_file()
        res: dict[str, Any] = {"rel": rel, "existed_before": existed, "data": data}
        why = _path_allowed(rel, self.kind)
        if why:
            res.update(ok=False, failures=[{"type": "path-not-allowed", "where": rel, "detail": why}],
                       fm={}, stub=False, status="rejected", warnings=[], report={}, drop_links=[])
            return res
        text = data.decode("utf-8", errors="replace")
        fails: list[dict[str, Any]] = []
        found = secret_scan.find_secrets(text)
        if found:
            fails.append({"type": "secret-in-note", "where": rel, "detail": ", ".join(found)})
        stem = Path(rel).stem
        prob = None if existed else _name_problem(stem)  # an existing name stays usable in place
        if prob:
            fails.append({"type": "unsafe-name", "where": rel, "detail": prob})
        key = name_key(stem)
        if not existed and rel.startswith(PERMANENT_DIR + "/"):
            clash = self.vault_keys.get(key) or (other_shadow_keys.get(key) if other_shadow_keys.get(key) != stem else None)
            if clash and clash != stem:
                fails.append({"type": "duplicate-name", "where": rel,
                              "detail": f"'{stem}' equals the existing note '{clash}' after Unicode, case and space normalization"})
        status_fails, keep_text = self._status_rules(rel, text, existed)
        fails += status_fails
        if keep_text:
            # The old text kept as it is (needs-repair for the operator, or the harness's own
            # `unsupported` mark): only the status lines changed; no contract check applies.
            fm_k, _, _ = verify_claims.split_frontmatter(text)
            chk = {"failures": [], "fm": fm_k, "stub": False, "status": str(fm_k.get("verification")),
                   "warnings": [], "report": {}, "drop_links": []}
        else:
            chk = self.check_text(rel, text, contract_required=self.video, existed=existed)
        fails += chk["failures"]
        base = self.bases.get(rel, "missing")
        if base != "missing" and base != _sha256(current):
            fails.append({"type": "conflict-stale-base", "where": rel,
                          "detail": "the vault file changed after this unit read it (another unit published first)"})
        if existed and rel.startswith(PERMANENT_DIR + "/"):
            old_fm, _, _ = verify_claims.split_frontmatter(current.read_text(encoding="utf-8", errors="replace"))
            if self.video or verify_claims.is_contract_note(old_fm):
                fails += _fold_failures(old_fm, chk["fm"], self.kind)
        if self.kind == "repair" and existed and rel.startswith(PERMANENT_DIR + "/") \
                and not self.options.get("allow_other_sources"):
            fails += self._other_source_failures(rel, text)
        if rel in self.refused_folds and existed:
            fails.append({"type": "fold-refused-but-written", "where": rel,
                          "detail": "index_add.py refused this fold (exit 3), but the note was changed"})
        res.update(ok=not fails, failures=fails, fm=chk["fm"], stub=chk["stub"], status=chk["status"],
                   warnings=chk["warnings"], report=chk["report"], drop_links=chk.get("drop_links", []))
        return res

    def _status_rules(self, rel: str, text: str, existed: bool) -> tuple[list[dict[str, Any]], bool]:
        """Round 7 (F1). `verification: unsupported` is allowed only where the harness's
        repair plan says `mark_unsupported` (no known source has a transcript), and then
        only with the old body unchanged. `verification: needs-repair` with a
        `repair_note` (no bullet has a supporting passage) keeps the old text too and goes
        to the operator. Returns (failures, keep_text)."""
        fm, _, _ = verify_claims.split_frontmatter(text)
        ver = str(fm.get("verification") or "")
        if ver not in ("unsupported", "needs-repair") or not rel.startswith(PERMANENT_DIR + "/"):
            return [], False
        if ver == "needs-repair" and not str(fm.get("repair_note") or "").strip():
            return [], False
        old = (self.zk / rel).read_text(encoding="utf-8", errors="replace") if existed else None
        fails: list[dict[str, Any]] = []
        if ver == "unsupported":
            plan = repair_plan(self.staging, self.zk, rel) if (self.kind == "repair" and existed) else None
            if not plan or not plan["mark_unsupported"]:
                fails.append({"type": "unsupported-not-allowed", "where": "verification",
                              "detail": "verification: unsupported is set only by the harness, when no known source of "
                                        "the old note has a transcript (repair-plan mark_unsupported). A source "
                                        "transcript exists: repair the note from it, or keep the old text with "
                                        "verification: needs-repair and repair_note: \"no supporting passage found\""})
                return fails, False
        elif self.kind != "repair" or not existed:
            fails.append({"type": "needs-repair-not-allowed", "where": "verification",
                          "detail": "verification: needs-repair with repair_note is a repair outcome for an existing note"})
            return fails, False
        if old is None or _strip_status_lines(old) != _strip_status_lines(text):
            fails.append({"type": "unsupported-body-changed" if ver == "unsupported" else "needs-repair-body-changed",
                          "where": "body",
                          "detail": f"a note marked verification: {ver} keeps the old text byte for byte; only the "
                                    "status lines (verification, unsupported_reason, repair_note) may change"})
        return fails, True

    def _other_source_failures(self, rel: str, text: str) -> list[dict[str, Any]]:
        """Repair: when no known source of the old note has a transcript, Evidence from
        other videos is refused (the note becomes `unsupported`), unless the unit was
        started with --allow-other-sources."""
        plan = repair_plan(self.staging, self.zk, rel)
        if not plan["mark_unsupported"]:
            return []
        known = plan["origin_sources"] if plan["origin_sources"] else plan["sources"]
        fm, _, _ = verify_claims.split_frontmatter(text)
        if verify_claims.is_stub(fm) or str(fm.get("verification") or "") == "unsupported":
            return []
        cited = set(verify_claims._source_ids(fm)) | {m.group(1) for m in verify_claims.SRC_TAG.finditer(text)}
        cited |= {m.group(1) for m in re.finditer(r"^\s*[-*]\s*(" + verify_claims.VID + r")", text, re.M)}
        other = sorted(cited - set(known))
        if not other:
            return []
        return [{"type": "other-source-evidence", "where": rel,
                 "detail": f"the source {known} of this note has no transcript; Evidence from other videos {other} "
                           "is refused without --allow-other-sources. The harness marks this note "
                           "verification: unsupported (repair-plan mark_unsupported); write nothing for it"}]

    def check_all(self) -> list[dict[str, Any]]:
        keys = {name_key(p.stem): p.stem for p in self.shadow_files}
        return [self.check_file(p, keys) for p in self.shadow_files]


def check_unit(staging: Path, unit_dir: Path, zk_dir: Path, write: bool = True) -> dict[str, Any]:
    """Mechanical check of every shadow file (no vault change). Writes check.json."""
    info = _read_json(unit_dir / "unit.json", {})
    ck = _Checker(staging, unit_dir, zk_dir, str(info.get("kind") or "synth"),
                  _queue_kinds(staging, list(info.get("ids") or [])))
    out = {}
    for r in ck.check_all():
        out[r["rel"]] = {"ok": r["ok"], "failures": r["failures"],
                         "warnings": [w for w in r["warnings"] if w["type"] in REVIEW_HINT_TYPES],
                         "needs_review": r["ok"] and ck.needs_review(r["rel"], r["fm"])}
    if write:
        _write_json(unit_dir / "check.json", out)
    return out


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #


PIPELINE_WRITES = "pipeline_writes.json"
PIPELINE_JOURNAL = "pipeline_writes.journal.jsonl"


def reconcile_pipeline_writes(staging: Path, zk_dir: Path) -> list[str]:
    """X10 (round 22): a write whose intent is journaled and whose file holds the intended sha
    (a kill before the registry update) gets its registry entry. The journal is then empty."""
    j = staging / PIPELINE_JOURNAL
    done = []
    if not j.is_file():
        return done
    reg = _read_json(staging / PIPELINE_WRITES, {}) or {}
    for rec in _read_jsonl(j):
        p = zk_dir / str(rec.get("rel") or "")
        if not p.is_file():
            continue
        cur = _sha256(p)
        if cur == rec.get("sha256") and reg.get(rec["rel"]) != rec["sha256"]:
            reg[rec["rel"]] = rec["sha256"]
            done.append(rec["rel"])
        elif cur != rec.get("sha256") and cur != reg.get(rec["rel"]):
            # Z13 (round 23): the file matches neither the intent nor the registry (a kill
            # after the replace, then a user edit): the intent becomes the registry entry, so
            # the guard sees the user edit and refuses the next write
            reg[rec["rel"]] = rec["sha256"]
            done.append(rec["rel"])
    _write_json(staging / PIPELINE_WRITES, reg)
    j.unlink()
    return done


def _journal_done(staging: Path, rel: str, sha: str) -> None:
    j = staging / PIPELINE_JOURNAL
    if not j.is_file():
        return
    keep = [r for r in _read_jsonl(j) if not (r.get("rel") == rel and r.get("sha256") == sha)]
    if keep:
        j.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
    else:
        j.unlink()


def move_pipeline_write(staging: Path, old_rel: str, new_rel: str, sha: str) -> None:
    """X9: the registry follows a rename (respeak), so the user-edit protection moves with it."""
    reg = _read_json(staging / PIPELINE_WRITES, {}) or {}
    reg.pop(old_rel, None)
    reg[new_rel] = sha
    _write_json(staging / PIPELINE_WRITES, reg)


def record_pipeline_write(staging: Path, rel: str, sha: str) -> None:
    """Round 21: the sha of the pipeline's last write of each vault note."""
    reg = _read_json(staging / PIPELINE_WRITES, {}) or {}
    reg[rel] = sha
    _write_json(staging / PIPELINE_WRITES, reg)


def guard_vault_write(staging: Path, zk_dir: Path, rel: str, text: str) -> str:
    """Round 21 (V34, W13): '' when the write may go on, else the reason to refuse.
    - A note that the pipeline wrote before (a stub or a published target) and that
      changed since (a user edit): refused.
    - An old note in repair_state.json that is not a stub yet and changed since it was
      planned (a user edit during the run): refused."""
    p = zk_dir / rel
    if not p.is_file() or not rel.startswith(PERMANENT_DIR + "/"):
        return ""
    cur = _sha256(p)
    last = (_read_json(staging / PIPELINE_WRITES, {}) or {}).get(rel)
    if last and cur != last:
        return "edited since the pipeline's last write (user edit?): not overwritten"
    if not last:
        e = ((_read_json_snapshot(staging / "repair_state.json", {}) or {}).get("notes") or {}).get(rel) or {}
        fm, _, _ = verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        if e.get("sha") and cur != e["sha"] and not verify_claims.is_stub(fm):
            return "old note edited since repair planned it (user edit?): not overwritten"
    return ""


def stub_old_text(staging: Path, zk_dir: Path, rel: str) -> tuple[str | None, str | None]:
    """(old note text, archive path or None) for the stub of `rel`: the vault file when it is
    not a stub yet (it is archived just before the write), else the newest archived non-stub
    version."""
    p = zk_dir / rel
    if p.is_file():
        text = p.read_text(encoding="utf-8", errors="replace")
        if not verify_claims.is_stub(verify_claims.split_frontmatter(text)[0]):
            return text, None
    return archived_old_text(staging, rel), archived_old_path(staging, rel)


_PSTOP = frozenset("that this with from have will when your they them then than what which there their about "
                   "into just also only very more most some such each other been were does done should".split())


def _pstems(text: str) -> set[str]:
    """Content stems (the first 4 letters of words with 4+ letters, without stop words); the
    same rule as synth_call._stems."""
    return {w[:4] for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _PSTOP}


def pointer_supported(bullet: str, zk_dir: Path, title: str) -> bool:
    """X16 (round 22): `→ [[T]]` without `(unverified)` needs one claim line or one Evidence
    quote of T that shares 3 content stems with the bullet, or 2 when the bullet has fewer
    than 5 stems AND a number of the bullet (with its unit word) is in T."""
    p = zk_dir / PERMANENT_DIR / f"{title}.md"
    if not bullet or not p.is_file():
        return False
    text = p.read_text(encoding="utf-8", errors="replace")
    want = _pstems(bullet)
    if not want:
        return False
    _, body, _ = verify_claims.split_frontmatter(text)
    pieces = [ln for ln in body.split("## Connected Ideas", 1)[0].splitlines() if ln.strip() and not ln.startswith("#")]
    best = max([len(want & _pstems(x)) for x in pieces] or [0])
    if best >= 3:
        return True
    pairs = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z%]+)", bullet.lower())
    if best >= 2 and len(want) < 5:
        if not pairs:
            return True
        low = text.lower()
        return any(re.search(r"(?<![\d.])" + re.escape(n) + r"(?![\d.])", low) and u[:4] in low for n, u in pairs)
    return False


_RENAMES: dict[str, Any] = {}  # compatibility for callers outside a finalize scope
_RENAME_INDEX: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "zr_finalize_rename_index", default=None)


@contextlib.contextmanager
def _rename_index_scope(staging: Path):
    """Keep marked paths and their rename map only for this finalize."""
    root = staging.resolve()
    state: dict[str, Any] = {"root": root, "map": None, "active_root": root, "active_map": None}
    token = _RENAME_INDEX.set(state)
    try:
        yield
    finally:
        _RENAME_INDEX.reset(token)


def _invalidate_rename_index(staging: Path) -> None:
    state = _RENAME_INDEX.get()
    if state and state["root"] == staging.resolve():
        state["map"] = None


def _invalidate_rename_index_for_marked(path: Path) -> None:
    if path.name != "marked.json":
        return
    parts = path.absolute().parts
    if len(parts) < 5 or parts[-5] != "runs" or parts[-3] != "units":
        return
    staging = Path(*parts[:-5])
    _invalidate_rename_index(staging)


def _rename_chain(zk_dir: Path, title: str) -> str:
    """Follow marked.json rename chains, using the active finalize-local map when present."""
    state = _RENAME_INDEX.get()
    ren = state["active_map"] if state and state["active_map"] is not None else (_RENAMES.get("map") or {})
    x, seen = title, set()
    while x in ren and x not in seen:
        seen.add(x)
        x = ren[x]
    return x


def load_renames(staging: Path) -> None:
    state = _RENAME_INDEX.get()
    root = staging.resolve()
    if state and state["root"] == root and state["map"] is not None:
        ren = state["map"]
        _RENAMES.update(staging=str(staging), map=ren)
        state["active_root"], state["active_map"] = root, ren
        return
    ren: dict[str, str] = {}
    for p_ in sorted(staging.glob("runs/*/units/*/marked.json")):
        ren.update((_read_json_snapshot(p_, {}) or {}).get("renamed") or {})
    _RENAMES.update(staging=str(staging), map=ren)
    if state:
        if state["root"] == root:
            state["map"] = ren
        state["active_root"], state["active_map"] = root, ren


def _rename_map(staging: Path) -> dict[str, str]:
    load_renames(staging)
    state = _RENAME_INDEX.get()
    if state and state["active_root"] == staging.resolve():
        return state["active_map"] or {}
    if _RENAMES.get("staging") and Path(_RENAMES["staging"]).resolve() == staging.resolve():
        return _RENAMES.get("map") or {}
    return {}


def _load_renames_uncached(staging: Path) -> dict[str, str]:
    """Read every marked file directly for legacy audit calls outside finalize."""
    renamed: dict[str, str] = {}
    for marked_path in sorted(staging.glob("runs/*/units/*/marked.json")):
        renamed.update((_read_json(marked_path, {}) or {}).get("renamed") or {})
    return renamed


def _bullet_status(zk_dir: Path, e: dict[str, Any], bid: str, waiting: set[str]) -> tuple[str, list[str], list[str]]:
    """(status line, published targets, waiting targets) of one old bullet from its answers.
    Round 21: the status is shown next to the old text; when in doubt it is open or
    (unverified)."""
    b = (e.get("bullets") or {}).get(bid) or {}
    answers = list((b.get("answers") or {}).items())
    order = {v: i for i, v in enumerate(e.get("sources") or [])}
    answers.sort(key=lambda x: order.get(x[0], 99))

    def pub(titles: list[str]) -> list[str]:
        return sorted({y for x in titles if x for y in _final_targets(zk_dir, x)})

    def titles_of(a: dict[str, Any]) -> list[str]:
        # pilot 9 (round 22): a title that a fix call renamed resolves through the rename chain
        return [_rename_chain(zk_dir, x) for x in (a.get("titles") or [a.get("title")]) if x]

    for _vid, a in answers:  # 1. carried and confirmed
        if a.get("decision") in ("kept", "corrected"):
            p_ = pub(titles_of(a))
            if p_:
                # X16 (round 22): an unmarked pointer needs the bullet's content in a target
                strong = any(pointer_supported(b.get("text") or "", zk_dir, x) for x in p_)
                return "→ " + "; ".join(f"[[{x}]]" for x in p_) + ("" if strong else " (unverified)"), p_, []
    for _vid, a in answers:  # 2. carried, the ledger check at the repair call could not confirm
        if a.get("decision") == "unverified" and not str(a.get("why") or "").startswith(("a fix removed",
                                                                                       "the claim line")):
            p_ = pub(titles_of(a.get("was") or {}))
            if p_:
                # pilot 9 (round 22): the measured cause of 8 false (unverified) marks was the time
                # window of that check; the published target's own text decides (X16 rule)
                strong = any(pointer_supported(b.get("text") or "", zk_dir, x) for x in p_)
                return "→ " + "; ".join(f"[[{x}]]" for x in p_) + ("" if strong else " (unverified)"), p_, []
        if a.get("decision") == "unverified" and str(a.get("why") or "").startswith("the claim line"):
            p_ = pub(titles_of(a.get("was") or {}))
            if p_:  # W3: the claim line changed after the check: always marked
                return "→ " + "; ".join(f"[[{x}]]" for x in p_) + " (unverified)", p_, []
    for _vid, a in answers:  # 3. open
        d = a.get("decision")
        if d == "unverified" and str(a.get("why") or "").startswith("a fix removed"):
            return f"open: evidence removed ({'; '.join(titles_of(a.get('was') or {}))})", [], []
        if d == "target-rejected":
            return f"open: target rejected ({'; '.join(titles_of(a))})", [], []
        if d in ("kept", "corrected", "unverified"):
            ts = titles_of(a) or titles_of(a.get("was") or {})
            w = [x for x in ts if x in waiting]
            return (f"open: target waiting ({'; '.join(ts)})" if w else
                    f"open: target not published ({'; '.join(ts)})"), [], w
        if d == "dropped" and a.get("reason") == "damaged transcript":
            return "open: damaged transcript", [], []
    drops = [(v, a) for v, a in answers if a.get("decision") == "dropped"]
    planned = list(e.get("sources") or [])
    by_v = dict(answers)
    contra = [(v, a) for v, a in drops if "contradict" in str(a.get("reason") or "")]
    if contra:
        # pilot 9 (Rest b11): a "contradicts" drop ends the bullet (later videos are not asked)
        v, a = contra[-1]
        return f"dropped: {a.get('reason')} ({v})", [], []
    if drops and planned and all((by_v.get(v) or {}).get("decision") == "dropped" for v in planned):
        return "unsupported: no source video states this", [], []
    if e.get("status") == "open":
        return "open: not yet processed", [], []
    if drops:
        return "open: not every source video answered", [], []
    if not answers:
        return "open: no answer recorded", [], []
    return "open: " + ", ".join(sorted({str(a.get('decision')) for _v, a in answers})), [], []


def render_pipeline_stub(staging: Path, zk_dir: Path, rel: str, stub_in: str, old_text: str, archive: str) -> str:
    """Round 21: the full stub of `rel` from the old text and repair_state.json. The
    targets are the published targets of the caller's stub plus those of the statuses;
    `superseded_pending` keeps the caller's `note:` lines of waiting targets and lists
    every open bullet."""
    st = _read_json_snapshot(staging / "repair_state.json", {}) or {}
    e = (st.get("notes") or {}).get(rel) or {}
    load_renames(staging)
    fm_in, _, _ = verify_claims.split_frontmatter(stub_in)
    waiting = waiting_titles(staging)  # round 26 (b): no dead-request wait
    _h1, bullets, _o = stub_check.old_parts(old_text)
    name = Path(rel).name
    ids = [f"{name}#b{i}" for i in range(1, len(bullets) + 1)]
    known = list((e.get("bullets") or {}))
    statuses, targets, pend_notes, pend_bullets = [], [], [], []
    same = len(known) == len(bullets) and all(((e["bullets"][k].get("text") or "").strip() == bullets[i].strip())
                                              for i, k in enumerate(ids) if k in e["bullets"])
    for i, bid in enumerate(ids):
        if not e or not same or bid not in (e.get("bullets") or {}):
            statuses.append("open: no repair record for this bullet")
            pend_bullets.append(bid)
            continue
        s, p_, w = _bullet_status(zk_dir, e, bid, waiting)
        statuses.append(s)
        targets += p_
        pend_notes += w
        if s.startswith("open:") or s.endswith("(unverified)"):  # the operator reads these
            pend_bullets.append(bid)
    for x in _stub_targets(fm_in):
        targets += _final_targets(zk_dir, Path(x).stem)
    for p in fm_in.get("superseded_pending") or []:
        if str(p).startswith("note: ") and str(p)[6:] not in pend_notes:
            pend_notes.append(str(p)[6:])
    targets = list(dict.fromkeys(targets))
    pending = [f"note: {x}" for x in dict.fromkeys(pend_notes) if x not in targets] + \
        [f"bullet: {b}" for b in pend_bullets]
    return stub_check.render(old_text, Path(rel).stem, statuses, targets, pending, archive)


class _Publisher:
    def __init__(self, staging: Path, run_id: str, unit_dir: Path, zk_dir: Path, settings: dict[str, Any]):
        self.staging, self.run_id, self.unit_dir, self.zk, self.settings = staging, run_id, unit_dir, zk_dir, settings
        self.published: list[str] = []
        self.commit_paths: list[str] = []
        self.refused: list[dict[str, Any]] = []
        self.unchanged = False

    def archive(self, rel: str, why: str) -> str | None:
        cur = self.zk / rel
        if not cur.is_file():
            return None
        dst = self.staging / "retired" / self.run_id / self.unit_dir.name / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while dst.exists():  # round 15: a second write of one note in one unit keeps the first archive
            n += 1
            dst = dst.with_name(f"{Path(rel).stem}.v{n}.md")
        shutil.copy2(cur, dst)
        _record_archive_candidate(self.staging, rel, dst)
        provenance.record(self.staging, "note-archived", run_id=self.run_id, note=rel, unit=self.unit_dir.name,
                          archive=str(dst), reason=why, sha256=_sha256(cur))
        return str(dst)

    def quarantine(self, rel: str, data: bytes, failures: list[dict[str, Any]], vault_action: str) -> str:
        dst = self.staging / "quarantine" / self.run_id / self.unit_dir.name / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = data.decode("utf-8", errors="replace")
        try:
            text = verify_claims.set_verification_text(text, "failed")
        except ValueError:
            pass
        dst.write_text(secret_scan.scrub(text), encoding="utf-8")
        _write_json(dst.parent / (dst.name + ".reason.json"), {
            "note": rel, "run_id": self.run_id, "unit": self.unit_dir.name,
            "failure_types": sorted({f["type"] for f in failures}), "failures": failures,
            "vault_action": vault_action, "quarantined_at": _now()})
        return str(dst)

    def write(self, rel: str, text: str, why: str) -> bool:
        """Atomic write into the vault with an intent record (crash-safe, idempotent).
        Round 21: a stub of an old note is rendered with the old text and checked
        (`stub_check`); a stub that fails, a user edit of an old note, a stub or a published
        note since the pipeline's last write: no write, `needs_operator.json`. Returns
        False when the write is refused."""
        target = self.zk / rel
        refuse = guard_vault_write(self.staging, self.zk, rel, text)
        if refuse:
            self.refused.append({"note": rel, "why": refuse})
            needs_operator(self.staging, [{"note": rel, "unit": self.unit_dir.name}], refuse)
            provenance.record(self.staging, "note-write-refused", run_id=self.run_id, note=rel,
                              unit=self.unit_dir.name, reason=refuse)
            return False
        is_stub_text = verify_claims.is_stub(verify_claims.split_frontmatter(text)[0]) and rel.startswith(PERMANENT_DIR + "/")
        old_text = None
        if is_stub_text:
            old_text, old_archive = stub_old_text(self.staging, self.zk, rel)
            if old_text is None:
                why_r = "stub refused: no old text (vault or archive) to carry"
                self.refused.append({"note": rel, "why": why_r})
                needs_operator(self.staging, [{"note": rel, "unit": self.unit_dir.name}], why_r)
                return False
        self.unchanged = False
        if is_stub_text and old_archive is not None and target.is_file():
            # X7 (round 22): render first and compare; an unchanged stub is not archived again
            want = render_pipeline_stub(self.staging, self.zk, rel, text, old_text or "",
                                        os.path.relpath(old_archive, self.staging))
            if target.read_text(encoding="utf-8", errors="replace") == want:
                self.unchanged = True
                self.published.append(rel)
                return True
        if target.is_file():
            # X3 (round 22): a stub is compared as the FULL rendered text (above), never as
            # the bare text the caller gives
            if not is_stub_text and target.read_text(encoding="utf-8", errors="replace") == text:
                self.unchanged = True
                self.published.append(rel)
                return True
            arch = self.archive(rel, why)
            if is_stub_text and old_archive is None:
                old_archive = arch
        if is_stub_text:
            text = render_pipeline_stub(self.staging, self.zk, rel, text, old_text or "",
                                        os.path.relpath(old_archive, self.staging) if old_archive else "")
            probs = stub_check.check(old_text or "", text, self.zk)
            if probs:
                why_r = "stub refused by the stub check: " + "; ".join(probs[:3])
                self.refused.append({"note": rel, "why": why_r, "problems": probs})
                needs_operator(self.staging, [{"note": rel, "unit": self.unit_dir.name, "problems": probs[:10]}],
                               "stub refused by the stub check")
                provenance.record(self.staging, "note-write-refused", run_id=self.run_id, note=rel,
                                  unit=self.unit_dir.name, reason=why_r[:300])
                return False
            if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == text:
                self.unchanged = True
                self.published.append(rel)
                return True
        sha = _sha256_bytes(text.encode("utf-8"))
        _append(self.unit_dir / "publish.jsonl", {"phase": "intent", "rel": rel, "sha256": sha, "why": why})
        # X10 (round 22): the intent is journaled before the file; recover() completes the
        # registry entry when the file's sha equals the intended sha
        _append(self.staging / PIPELINE_JOURNAL, {"rel": rel, "sha256": sha, "unit": self.unit_dir.name, "at": _now()})
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.staging / ".zr-publish-tmp" / f"{self.unit_dir.name}-{len(self.published)}.tmp"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        # Same file system check: the temp file lives outside the vault; copy then rename.
        staged = target.with_name(f".{target.name}.zr-publish.tmp")
        shutil.copyfile(tmp, staged)
        os.replace(staged, target)
        tmp.unlink()
        _append(self.unit_dir / "publish.jsonl", {"phase": "done", "rel": rel, "sha256": sha})
        record_pipeline_write(self.staging, rel, sha)
        _journal_done(self.staging, rel, sha)  # Z13: the journal keeps only unfinished writes
        self.published.append(rel)
        self.commit_paths.append(rel)
        return True


def index_rollback(staging: Path, zk_dir: Path, rels: list[str]) -> list[str]:
    """Quarantined and discarded notes must be invisible to later agents (round 8): remove
    their concept-index.json and provenance.json entries. A note that exists in the vault
    (a fold that failed) gets its index entry back from the vault file."""
    rels = sorted({r for r in rels if r.startswith(PERMANENT_DIR + "/")})
    if not rels:
        return []
    import index_rebuild  # noqa: PLC0415

    idx_p, prov_p = staging / "concept-index.json", staging / "provenance.json"
    done: list[str] = []
    with state_lock(staging):
        idx = _read_json(idx_p, None)
        prov = _read_json(prov_p, None)
        for rel in rels:
            stem = Path(rel).stem
            in_vault = (zk_dir / rel).is_file()
            if isinstance(idx, dict) and isinstance(idx.get("concepts"), list):
                keep = []
                for c in idx["concepts"]:
                    hit = c.get("file") == rel or (not c.get("file") and c.get("title") == stem)
                    if not hit:
                        keep.append(c)
                    elif in_vault:
                        fm, body = index_rebuild.fm_and_body((zk_dir / rel).read_text(encoding="utf-8", errors="replace"))
                        srcs = index_rebuild.note_sources(fm)
                        c.update(sources=srcs, speakers=index_rebuild.speakers_of(srcs), scope=index_rebuild.note_scope(fm))
                        keep.append(c)
                    if hit:
                        done.append(rel)
                idx["concepts"] = keep
            if isinstance(prov, dict) and rel in prov and not in_vault:
                del prov[rel]
                done.append(rel)
        if isinstance(idx, dict):
            _write_json(idx_p, idx)
        if isinstance(prov, dict):
            _write_json(prov_p, prov)
    return sorted(set(done))


def index_publish(staging: Path, zk_dir: Path, rels: list[str]) -> list[str]:
    """Add or update the index and provenance entries of published notes (single mode):
    title, file, gist (the lead), tags, sources, speakers, scope."""
    import index_rebuild  # noqa: PLC0415

    rels = [r for r in rels if r.startswith(PERMANENT_DIR + "/") and (zk_dir / r).is_file()]
    if not rels:
        return []
    idx_p, prov_p = staging / "concept-index.json", staging / "provenance.json"
    with state_lock(staging):
        idx = _read_json(idx_p, None) or {"version": 1, "concepts": [], "mocs": []}
        prov = _read_json(prov_p, None) or {}
        for rel in rels:
            text = (zk_dir / rel).read_text(encoding="utf-8", errors="replace")
            fm, body = index_rebuild.fm_and_body(text)
            vfm, vbody, _ = verify_claims.split_frontmatter(text)
            if verify_claims.is_stub(vfm):
                continue
            srcs = index_rebuild.note_sources(fm)
            tags = vfm.get("tags") if isinstance(vfm.get("tags"), list) else []
            entry = {"title": Path(rel).stem, "file": rel, "aliases": [],
                     "gist": verify_claims.parse_body_full(vbody).lead[:300], "tags": tags, "mocs": [],
                     "sources": srcs, "speakers": index_rebuild.speakers_of(srcs), "scope": index_rebuild.note_scope(fm),
                     "by": "harness"}
            idx["concepts"] = [c for c in idx.get("concepts", []) if c.get("file") != rel] + [entry]
            prov[rel] = sorted({str(s.get("id")) for s in srcs if s.get("id")})
        _write_json(idx_p, idx)
        _write_json(prov_p, prov)
    return rels


def discard_drafts(staging: Path, unit_dir: Path, zk_dir: Path) -> list[dict[str, str]]:
    """Before a fix turn: drafts that are not in the unit's final note list leave the
    shadow folder (copied to <staging>/discarded/), so the fix turn sees only its notes."""
    info = _read_json(unit_dir / "unit.json", {})
    ck = _Checker(staging, unit_dir, zk_dir, str(info.get("kind") or "synth"),
                  _queue_kinds(staging, list(info.get("ids") or [])))
    results = ck.check_all()
    drafts = _drafts(unit_dir, zk_dir, results)
    run_id = unit_dir.parent.parent.name
    rec = _read_json(unit_dir / "discarded.json", {}) or {"notes": []}
    for r in results:
        if r["rel"] not in drafts:
            continue
        dst = staging / "discarded" / run_id / unit_dir.name / r["rel"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(r["data"])
        (unit_dir / "out" / r["rel"]).unlink()
        rec["notes"].append({"note": r["rel"], "path": str(dst)})
    _write_json(unit_dir / "discarded.json", rec)
    return rec["notes"]


def _lit_paths(staging: Path, text: str) -> list[str]:
    fm, _, _ = verify_claims.split_frontmatter(text)
    lit = verify_claims.LitIndex(verify_claims.lit_dirs(staging))
    out = []
    for sid in verify_claims._source_ids(fm):
        p = lit.paths.get(sid)
        out.append(f"{sid}: {p}" if p else f"{sid}: transcript missing")
    return out


def fix_report(staging: Path, unit_dir: Path, zk_dir: Path, max_chars: int = 60000) -> str:
    """The body of a mechanical fix turn: only notes of the unit's final list that fail,
    each with its failures, the lit file path(s) and the cited passages (marked)."""
    res = check_unit(staging, unit_dir, zk_dir)
    listed = set(listed_notes(unit_dir))
    blocks = []
    for rel, c in sorted(res.items()):
        if c["ok"] or (listed and Path(rel).stem not in listed):
            continue
        fails = [f for f in c["failures"] if f["type"] not in WAIT_TYPES]
        if not fails:
            continue
        p = unit_dir / "out" / rel
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = [f"NOTE: {rel}", "TRANSCRIPT FILE(S): " + "; ".join(_lit_paths(staging, text)), "FAILURES:"]
        lines += [f"- {f['type']} at {f['where']}: {f['detail'][:400]}" for f in fails]
        lines += ["CITED PASSAGES:", passages(staging, zk_dir, rel, note_file=p, max_chars=8000)]
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    return text if len(text) <= max_chars else text[:max_chars] + "\n...[report cut]"


def review_rejections(staging: Path, unit_dir: Path, zk_dir: Path) -> list[dict[str, Any]]:
    """Shadow notes whose current text has a review verdict with a defect (and that are
    in the unit's final list), with the rejected items."""
    out = []
    listed = set(listed_notes(unit_dir))
    for p in sorted((unit_dir / "out").rglob("*.md")):
        rel = p.relative_to(unit_dir / "out").as_posix()
        if listed and p.stem not in listed:
            continue
        v = verdict_for(unit_dir, rel, p.read_bytes())
        if not v or v.get("unusable") or not v["bad"]:
            continue
        data = _read_json(Path(v["path"]), {}) or {}
        items = [it for it in data.get("items") or [] if isinstance(it, dict)
                 and (_norm_verdict(it.get("verdict")) != "supported" or it.get("also"))]
        out.append({"rel": rel, "bad": v["bad"], "items": items, "scope_complete": data.get("scope_complete"),
                    "sha256": _sha256(p)})
    return out


def review_repair_message(staging: Path, unit_dir: Path, zk_dir: Path) -> str:
    """The targeted repair turn after a review rejection (round 8). Empty when no note
    was rejected, or when the unit had its repair turn already (one per unit)."""
    if (unit_dir / "review-repair.json").exists():
        return ""
    rej = review_rejections(staging, unit_dir, zk_dir)
    if not rej:
        return ""
    _write_json(unit_dir / "review-repair.json", {"at": _now(), "notes": {r["rel"]: r["sha256"] for r in rej}})
    blocks = []
    for r in rej:
        p = unit_dir / "out" / r["rel"]
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = [f"NOTE: {r['rel']}", "TRANSCRIPT FILE(S): " + "; ".join(_lit_paths(staging, text)),
                 "REJECTED ITEMS:"]
        for it in r["items"]:
            also = f" (also {', '.join(map(str, it.get('also') or []))})" if it.get("also") else ""
            lines.append(f"- {it.get('item')}: {it.get('verdict')}{also}. Problem: {it.get('problem') or '-'}")
            lines.append(f"  Item text: {str(it.get('text') or '')[:400]}")
            lines.append(f"  Transcript words: {str(it.get('transcript_text') or '')[:600]}")
        if r["scope_complete"] is False:
            lines.append("- scope: scope_complete is false (a scope value is not in the passages)")
        lines += ["CITED PASSAGES (the quoted sentences are between >>> and <<<):",
                  passages(staging, zk_dir, r["rel"], note_file=p, max_chars=8000)]
        blocks.append("\n".join(lines))
    head = ("REVIEW REPAIR TURN. The source-check review rejected the notes below. Rewrite ONLY these notes, "
            "in place (same file, same title). If an item `title` was rejected, rename the note with "
            "move_file first, then rewrite it at the new path. Fix exactly the rejected items: use the "
            "speaker's words, keep every hedge, condition, limit word and frame of the marked sentences, "
            "and remove what the passage does not say. Do not change any other note, do not create new "
            "notes, do not run queue_mark.py --notes. The harness checks each rewritten note and sends it "
            "to ONE new review; a note that is rejected again is quarantined. Then stop.")
    return head + "\n\n" + "\n\n".join(blocks)


def rereview(staging: Path, zk_dir: Path, note: str, model: str | None = None) -> dict[str, Any]:
    """Operator: review one published or quarantined note again (a new run folder; the
    vault does not change)."""
    rel = note if note.endswith(".md") else f"{PERMANENT_DIR}/{note}.md"
    src = zk_dir / rel
    if not src.is_file():
        cands = sorted((staging / "quarantine").glob(f"*/*/{rel}"), key=lambda p: p.stat().st_mtime)
        if not cands:
            raise HarnessError(f"{rel} is neither in the vault nor in {staging / 'quarantine'}")
        src = cands[-1]
    if model:
        os.environ["ZR_REVIEW_MODEL"] = model
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = staging / "runs" / f"rereview-{stamp}-{os.getpid()}"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"run_id": run_dir.name, "kind": "rereview", "started": _now(),
                                       "staging": str(staging), "vault": str(zk_dir), "settings": {}})
    unit = unit_start(run_dir, "review", [], rel, os.getpid(), "rereview")
    dst = unit / "out" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    import review_call  # noqa: PLC0415 - review_call imports this module

    rec = review_call.review_note(staging, zk_dir, unit, rel, dst, [])
    v = verdict_for(unit, rel, dst.read_bytes()) or {}
    return {"note": rel, "source": str(src), "review": rec, "bad": v.get("bad"), "unusable": v.get("unusable"),
            "verdict": v.get("path"), "unit": str(unit)}


def listed_notes(unit_dir: Path) -> list[str]:
    return [str(t) for t in (_read_json(unit_dir / "marked.json", {}) or {}).get("notes") or []]


def missing_listed(unit_dir: Path, zk_dir: Path) -> list[str]:
    """Listed titles that exist nowhere. A title that delete_draft removed or move_file
    renamed (recorded in marked.json by the agent tools) does not count."""
    shadow = unit_dir / "out"
    marked = _read_json(unit_dir / "marked.json", {}) or {}
    gone = set(marked.get("deleted") or []) | set((marked.get("renamed") or {}).keys())
    return [t for t in listed_notes(unit_dir) if t not in gone
            and not (shadow / PERMANENT_DIR / f"{t}.md").is_file() and not (zk_dir / PERMANENT_DIR / f"{t}.md").is_file()]


def _drafts(unit_dir: Path, zk_dir: Path, results: list[dict[str, Any]]) -> set[str]:
    """Shadow notes that the agent left behind: not in its final queue_mark --notes list,
    not a stub, not part of a rename. Only when every listed note exists."""
    listed = set(listed_notes(unit_dir))
    if not listed or missing_listed(unit_dir, zk_dir):
        return set()
    removals = _read_json(unit_dir / "removals.json", {})
    linked = set(removals) | {(v or {}).get("replaced_by") for v in removals.values()}
    out = set()
    for r in results:
        rel = r["rel"]
        if not rel.startswith(PERMANENT_DIR + "/") or r["stub"] or rel in linked or r["existed_before"]:
            continue  # an edit of an existing note is never a draft
        if Path(rel).stem not in listed:
            out.add(rel)
    return out


def _unit_times(unit_dir: Path) -> dict[str, float]:
    """Wall time per stage: the first agent turn (synthesis or review edit), the fix
    turns, and the review calls."""
    res = _read_json(unit_dir / "agent_result.json", {}) or {}
    turns = list(res.get("turns") or [])
    last = res.get("last_turn") or res
    all_turns = turns + [last] if res else []
    first = float((all_turns[0] or {}).get("elapsed_s") or 0) if all_turns else 0.0
    fixes = sum(float((t or {}).get("elapsed_s") or 0) for t in all_turns[1:])
    review = sum(float((_read_json(p, {}) or {}).get("elapsed_s") or 0) for p in unit_dir.glob("review-*/agent_result.json"))
    # P9: single mode has no agent turns; its calls are calls/<name>.json (synth, cont,
    # coverage, title-fix and repair calls are synthesis; fix-* calls are fix turns).
    for p in unit_dir.glob("calls/*.json"):
        if p.name.endswith((".reply.json", ".request.json")):
            continue
        rec = _read_json(p, {}) or {}
        el = float(rec.get("elapsed_s") or 0)
        if str(rec.get("name") or p.stem).startswith("fix-"):
            fixes += el
        else:
            first += el
    return {"synthesis": round(first, 1), "fix_turns": round(fixes, 1), "review": round(review, 1)}


def _calls_by_kind(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Round 13: calls per kind and backend, with their cost (0 for the files backend)."""
    out: dict[str, dict[str, Any]] = {}
    for p in run_dir.glob("units/*/calls/*.json"):
        if p.name.endswith((".reply.json", ".request.json")):
            continue
        r = _read_json(p, {}) or {}
        if r.get("kind") == "review":
            continue  # counted from the review folders below
        k = f"{r.get('kind') or p.stem.split('-')[0]}/{r.get('backend') or 'openrouter'}"
        e = out.setdefault(k, {"calls": 0, "cost": 0.0})
        e["calls"] += 1
        e["cost"] = round(e["cost"] + float((r.get("usage") or {}).get("cost") or 0), 6)
    for p in run_dir.glob("units/*/review-*/agent_result.json"):
        r = _read_json(p, {}) or {}
        k = f"review/{r.get('backend') or 'openrouter'}"
        e = out.setdefault(k, {"calls": 0, "cost": 0.0})
        e["calls"] += int(r.get("attempts") or 0)
        e["cost"] = round(e["cost"] + float((r.get("usage") or {}).get("cost") or 0), 6)
    return out


def _note_models(unit_dir: Path, rel: str) -> dict[str, Any]:
    """Round 13: which model (and, with the files backend, which worker) wrote and reviewed
    a note: `synth_model`, `synth_worker`, `review_model`, `review_worker`."""
    calls = [(_read_json(p, {}) or {}) for p in sorted(unit_dir.glob("calls/*.json"))
             if not p.name.endswith((".reply.json", ".request.json"))]
    writers = [c for c in calls if c.get("kind") != "review"]
    out: dict[str, Any] = {"synth_model": sorted({str(c.get("model")) for c in writers if c.get("model")}),
                           "synth_worker": sorted({str(c.get("worker")) for c in writers if c.get("worker")})}
    for d in sorted(unit_dir.glob("review-*"), reverse=True):
        if (_read_json(d / "request.json", {}) or {}).get("note") != rel:
            continue
        res = _read_json(d / "agent_result.json", {}) or {}
        out["review_model"] = res.get("model")
        workers = [(_read_json(p, {}) or {}).get("worker") for p in sorted(d.glob("reply-*.json"))]
        out["review_worker"] = next((w for w in reversed(workers) if w), None)
        break
    return out


def _unit_models(unit_dir: Path) -> list[str]:
    """P9: the models that the single-mode calls of the unit used (calls/<name>.json)."""
    return sorted({str((_read_json(p, {}) or {}).get("model")) for p in unit_dir.glob("calls/*.json")
                   if not p.name.endswith((".reply.json", ".request.json")) and (_read_json(p, {}) or {}).get("model")})


def _unit_base_urls(unit_dir: Path) -> list[str]:
    urls = set()
    for p in [unit_dir / "agent_result.json", *unit_dir.glob("review-*/agent_result.json")]:
        u = (_read_json(p, {}) or {}).get("base_url")
        if u:
            urls.add(str(u))
    return sorted(urls)


def _unit_usage(unit_dir: Path) -> dict[str, Any]:
    """Token use and cost of the unit's agents (synthesis, fix turns, reviews)."""
    total: Counter = Counter()
    calls = [p for p in sorted(unit_dir.glob("calls/*.json")) if not p.name.endswith((".reply.json", ".request.json"))]
    for res in [unit_dir / "agent_result.json", *sorted(unit_dir.glob("review-*/agent_result.json")),
                *sorted(unit_dir.glob("review-*/late-reply-*.json")), *calls]:
        u = (_read_json(res, {}) or {}).get("usage") or {}
        for k, v in u.items():
            if isinstance(v, (int, float)):
                total[k] += v
    out = dict(total)
    if "cost" in out:
        out["cost"] = round(out["cost"], 6)
    return out


CHECKER_FILES = ("verify_claims.py", "run_integrity.py", "validate.py", "secret_scan.py")


def checker_hash() -> dict[str, str]:
    return {name: provenance.sha256_file(HERE / name) for name in CHECKER_FILES if (HERE / name).is_file()}


def _stub_targets(fm: dict[str, Any]) -> list[str]:
    """Every target of a stub: `superseded_by: "[[A]]"` or `"[[A]]; [[B]]"` (a split)."""
    return [f"{PERMANENT_DIR}/{m.group(1).strip()}.md"
            for m in verify_claims.WIKILINK.finditer(str(fm.get("superseded_by") or ""))]


def _published_contract(zk_dir: Path, rel: str) -> bool:
    p = zk_dir / rel
    if not p.is_file():
        return False
    fm, _, _ = verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
    return verify_claims.is_contract_note(fm) and not verify_claims.is_stub(fm)


def _stub_target(fm: dict[str, Any]) -> str | None:
    t = _stub_targets(fm)
    return t[0] if t else None


STATUS_LINE = re.compile(r"^(verification|unsupported_reason|repair_note)\s*:.*\n?", re.M)


def _strip_status_lines(text: str) -> str:
    """The text without the status lines of its frontmatter (for the unchanged-body rule)."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return STATUS_LINE.sub("", text[:end + 1]) + text[end + 1:]


def _drop_link_lines(text: str, targets: list[str]) -> str:
    """Remove each line of `## Connected Ideas` that links a dead target (repair)."""
    out, in_ci = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            in_ci = line.strip() == "## Connected Ideas"
        if in_ci and any(f"[[{t}]]" in line or f"[[{t}|" in line or f"[[{t}#" in line for t in targets):
            continue
        out.append(line)
    return "".join(out)


def _ensure_a_link(text: str, staging: Path, zk_dir: Path) -> str:
    """After dropped links: a contract note keeps one link (`[[Home]]` when none is left)."""
    m = re.search(r"(?m)^## Connected Ideas\s*$", text)
    if m is None:
        return text
    nxt = re.search(r"(?m)^## ", text[m.end():])
    sec_end = m.end() + (nxt.start() if nxt else len(text) - m.end())
    if verify_claims.WIKILINK.search(text[m.end():sec_end]):
        return text
    ensure_home(staging, zk_dir)
    line = "\n- [[Home]] — index (the linked note was not published).\n"
    return text[:sec_end].rstrip("\n") + "\n" + line + ("\n" + text[sec_end:] if text[sec_end:] else "")


def _harness_stub(old_title: str, new_title: str) -> str:
    return ("---\ntype: permanent note\nstatus: superseded\n"
            f'superseded_by: "[[{new_title}]]"\nverification: unverified\n---\n\n'
            f"# {old_title}\n\nThis note was replaced by [[{new_title}]].\n")


def safe_note_rel(zk_dir: Path, rel: str) -> str | None:
    """Round 14 (R1): a note path relative to the zettelkasten that resolves inside
    `<zk>/01 Permanent Notes`, else None (absolute paths, `..`, links out of it)."""
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        return None
    rel = Path(rel).as_posix()  # V32: "./01 Permanent Notes/X.md" -> "01 Permanent Notes/X.md"
    if rel.startswith("./"):
        rel = rel[2:]
    base = (zk_dir / PERMANENT_DIR).resolve()
    try:
        p = (zk_dir / rel).resolve()
    except OSError:
        return None
    return rel if base in p.parents else None


def repair_stub(old: str, targets: list[str], pending: list[str] | None = None) -> str:
    """The stub of a repaired old note. Round 11 (D): `superseded_pending` lists what is
    still open (target notes not yet published, bullets still with a later video)."""
    links = "; ".join(f"[[{t}]]" for t in targets)
    named = " and ".join(f"[[{t}]]" for t in targets)
    pend = ""
    if pending:
        pend = "superseded_pending:\n" + "".join(f"  - {json.dumps(p[:200])}\n" for p in pending)
    return ("---\ntype: permanent note\nstatus: superseded\n"
            f'superseded_by: "{links}"\n{pend}verification: unverified\n---\n\n'
            f"# {old}\n\nThis note was replaced by {named}.\n")


PARTIAL = "repair_partial.json"


_ARCHIVE_CANDIDATE_INDEX: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "archive_candidate_index", default=None)


def _add_archive_index_entry(index: dict[str, list[tuple[tuple[str, str, int, int], Path]]],
                             rel: str, order: tuple[str, str, int, int], path: Path) -> None:
    entries = index.setdefault(rel, [])
    if not any(existing == path for _key, existing in entries):
        entries.append((order, path))


def _sort_archive_index(index: dict[str, list[tuple[tuple[str, str, int, int], Path]]]) -> None:
    for rel, entries in index.items():
        # Exact unit archives precede a matching .v1 archive when their legacy sort keys tie.
        index[rel] = sorted(entries, key=lambda entry: (entry[0][:3], -entry[0][3]), reverse=True)


def _walk_archive_files(root: Path):
    """Yield archive files below one notes folder, following directory symlinks without loops."""
    if not root.is_dir():
        return
    pending = [(root, frozenset())]
    while pending:
        directory, ancestors = pending.pop()
        st = directory.stat()
        identity = (st.st_dev, st.st_ino)
        if identity in ancestors:
            continue
        children = list(directory.iterdir())
        next_ancestors = ancestors | {identity}
        for child in children:
            if child.is_dir():
                pending.append((child, next_ancestors))
            elif child.is_file():
                yield child


def _build_archive_candidate_index(staging: Path) -> dict[str, list[tuple[tuple[str, str, int, int], Path]]]:
    """Index exact note paths and unit-version archives once for one finalize."""
    index: dict[str, list[tuple[tuple[str, str, int, int], Path]]] = {}
    base = staging / "retired"
    if not base.is_dir():
        return index
    versioned_name = re.compile(r"(.+)\.v(\d+)\.md")
    for run in sorted(base.iterdir(), key=lambda p: p.name):
        if not run.is_dir():
            continue
        for path in _walk_archive_files(run / PERMANENT_DIR):
            rel = path.relative_to(run).as_posix()
            _add_archive_index_entry(index, rel, (run.name, "", 0, 0), path)
        for unit in sorted(run.iterdir(), key=lambda p: p.name):
            if not unit.is_dir():
                continue
            for path in _walk_archive_files(unit / PERMANENT_DIR):
                rel = path.relative_to(unit).as_posix()
                _add_archive_index_entry(index, rel, (run.name, unit.name, 1, 0), path)
                match = versioned_name.fullmatch(path.name)
                if match:
                    base_rel = (Path(rel).parent / f"{match.group(1)}.md").as_posix()
                    _add_archive_index_entry(index, base_rel,
                                             (run.name, unit.name, int(match.group(2)), 1), path)
    _sort_archive_index(index)
    return index


@contextlib.contextmanager
def _archive_candidate_scope(staging: Path):
    """Scope the read-only archive index to one finalize; no index escapes the call."""
    root = staging.resolve()
    state: dict[str, Any] = {"root": root, "index": None}
    token = _ARCHIVE_CANDIDATE_INDEX.set(state)
    try:
        yield
    finally:
        _ARCHIVE_CANDIDATE_INDEX.reset(token)


def _record_archive_candidate(staging: Path, rel: str, path: Path) -> None:
    state = _ARCHIVE_CANDIDATE_INDEX.get()
    if not state or state["root"] != staging.resolve() or state["index"] is None:
        return
    index = state["index"]
    try:
        parts = path.relative_to(staging / "retired").parts
    except ValueError:
        _invalidate_archive_candidate_index(staging)
        return
    if len(parts) >= 2 and parts[1] == PERMANENT_DIR:
        key = Path(*parts[1:]).as_posix()
        _add_archive_index_entry(index, key, (parts[0], "", 0, 0), path)
    elif len(parts) >= 3 and parts[2] == PERMANENT_DIR:
        unit_rel = Path(*parts[2:]).as_posix()
        _add_archive_index_entry(index, unit_rel, (parts[0], parts[1], 1, 0), path)
        match = re.fullmatch(r"(.+)\.v(\d+)\.md", Path(unit_rel).name)
        if match:
            base_rel = (Path(unit_rel).parent / f"{match.group(1)}.md").as_posix()
            _add_archive_index_entry(index, base_rel,
                                     (parts[0], parts[1], int(match.group(2)), 1), path)
    else:
        _invalidate_archive_candidate_index(staging)
        return
    _sort_archive_index(index)


def _invalidate_archive_candidate_index(staging: Path) -> None:
    """Call after out-of-band archive file or directory metadata changes during finalize."""
    state = _ARCHIVE_CANDIDATE_INDEX.get()
    if state and state["root"] == staging.resolve():
        state["index"] = None


def _archive_candidates_uncached(staging: Path, rel: str) -> list[Path]:
    """Legacy exact-path traversal used outside finalize and as the correctness oracle."""
    base = staging / "retired"
    if not base.is_dir():
        return []
    stem, parent = Path(rel).stem, Path(rel).parent
    out: list[tuple[tuple[str, str, int], Path]] = []
    for run in base.iterdir():
        if not run.is_dir():
            continue
        p = run / rel
        if p.is_file():
            out.append(((run.name, "", 0), p))
        for unit in run.iterdir():
            if not unit.is_dir():
                continue
            p = unit / rel
            if p.is_file():
                out.append(((run.name, unit.name, 1), p))
            d = unit / parent
            if d.is_dir():
                for q in d.iterdir():
                    m = re.fullmatch(re.escape(stem) + r"\.v(\d+)\.md", q.name)
                    if m and q.is_file():
                        out.append(((run.name, unit.name, int(m.group(1))), q))
    return [p for _k, p in sorted(out, key=lambda x: x[0], reverse=True)]


def _archive_candidates(staging: Path, rel: str) -> list[Path]:
    """Return exact-path archive candidates newest-first, using the active finalize index."""
    state = _ARCHIVE_CANDIDATE_INDEX.get()
    requested = Path(rel)
    canonical_permanent_rel = (
        not requested.is_absolute()
        and len(requested.parts) > 1
        and requested.parts[0] == PERMANENT_DIR
        and requested.as_posix() == rel
    )
    if state and state["root"] == staging.resolve() and canonical_permanent_rel:
        if state["index"] is None:
            state["index"] = _build_archive_candidate_index(staging)
        return [path for _key, path in state["index"].get(rel, [])]
    return _archive_candidates_uncached(staging, rel)

def _archived_old(staging: Path, rel: str) -> Path | None:
    """The archived old (non-stub) version of `rel`: the one with the sha that the repair
    plan recorded (X6: keyed by path + planned sha), else the newest non-stub version."""
    planned = (((_read_json_snapshot(staging / "repair_state.json", {}) or {}).get("notes") or {}).get(rel) or {}).get("sha")
    cands = _archive_candidates(staging, rel)
    if planned:
        for p in cands:
            if _sha256(p) == planned:
                return p
    for p in cands:
        if not verify_claims.is_stub(verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))[0]):
            return p
    return None


def archived_old_text(staging: Path, rel: str) -> str | None:
    """The archived old text of a note (see `_archived_old`)."""
    p = _archived_old(staging, rel)
    return p.read_text(encoding="utf-8", errors="replace") if p else None


def archived_old_path(staging: Path, rel: str) -> str | None:
    p = _archived_old(staging, rel)
    return str(p) if p else None


def reopen_rejected_bullets(staging: Path, unit_dir: Path, rejected: set[str]) -> dict[str, list[str]]:
    """Round 12: the bullets that this unit's video kept or corrected in a target note that
    ends quarantined (mechanical check or review) are open again in repair_state.json: the
    old note's later source videos get them; with no later video they stay open for the
    operator (`unsupported_bullets`, summary `repair_bullets_open`)."""
    sp = staging / "repair_state.json"
    st = _read_json(sp, None)
    vid = str((_read_json(unit_dir / "unit.json", {}) or {}).get("repair_video") or "")
    rmap = _read_json(unit_dir / "repair_map.json", {}) or {}
    retry = (_read_json(unit_dir / "unit.json", {}) or {}).get("retry_rejected") or {}
    out: dict[str, list[str]] = {}
    if not st or not vid:
        return out
    for rel in rmap:
        e = (st.get("notes") or {}).get(rel)
        if not e:
            continue
        this_call = set(((_read_json(unit_dir / "claims.json", {}) or {}).get("bullets") or {}))
        for bid, b in e.get("bullets", {}).items():
            if this_call and bid not in this_call:
                continue  # V7: only the bullets that THIS call settled in the rejected note
            a = b.get("answers", {}).get(vid) or {}
            src = a.get("was") if a.get("decision") == "unverified" and isinstance(a.get("was"), dict) else a
            titles = {x for x in (src.get("titles") or [src.get("title")]) if x}
            # pilot 8: an unverified answer whose named target is rejected is target-rejected too
            if src.get("decision") in ("kept", "corrected") and titles and titles <= rejected:  # R13: every title
                if retry and bid in set(retry.get("bullets") or []):
                    history = b.setdefault("answer_history", [])
                    retry_id = str(retry.get("id") or "")
                    if not any(x.get("retry_id") == retry_id and x.get("phase") == "retry-result-quarantined"
                               for x in history):
                        history.append({"retry_id": retry_id, "phase": "retry-result-quarantined", "video": vid,
                                        "target": retry.get("target"), "answer": copy.deepcopy(a)})
                b["answers"][vid] = {"decision": "target-rejected", "titles": sorted(titles & rejected),
                                     "was": src.get("decision")}
                out.setdefault(rel, []).append(bid)
        e["titles"] = [x for x in e.get("titles", []) if x not in rejected]  # round 20: always
        if out.get(rel):
            e["reopened"] = sorted(set(e.get("reopened", [])) | set(out[rel]))
            if e.get("status") in ("done", "done-unrepaired", "done-unsupported") \
                    and e.get("pos", 0) >= len(e.get("sources", [])) - 1:
                # no later video: OPEN (target rejected), never unsupported (pilot 8)
                cls = classify_bullets(e)
                e["rejected_bullets"] = cls["rejected"]
                e["unsupported_bullets"] = cls["unsupported"]
                if not e.get("titles"):
                    e["status"] = "needs-repair"
                    open_ids = cls["damaged"] + cls["unverified"] + cls["rejected"] + cls["open"]
                    needs_operator(staging, [needs_repair_item(staging, rel, e, open_ids)],
                                   "needs-repair: no published target, open bullets (old note unchanged)")
    _write_json(sp, st)
    return out


def reopen_uncited_bullets(staging: Path, zk_dir: Path, texts: dict[str, str]) -> dict[str, list[str]]:
    """Round 20 (pilot 7, "Unassisted" b3): a bullet settled in a target note stays settled only
    while that note keeps one of the Evidence items that carried it (`cited`, recorded at the
    repair call). A review fix that removes all of them opens the bullet again (`unverified`):
    the stub lists it, and the operator decides. `texts`: title -> text that this finalize
    publishes (or keeps waiting)."""
    sp = staging / "repair_state.json"
    st = _read_json(sp, None)
    out: dict[str, list[str]] = {}
    changed_ = False
    if not st or not texts:
        return out
    for rel, e in (st.get("notes") or {}).items():
        for bid, b in (e.get("bullets") or {}).items():
            for vid, a in list((b.get("answers") or {}).items()):
                if a.get("decision") not in ("kept", "corrected") or not a.get("cited"):
                    continue
                titles = [x for x in (a.get("titles") or [a.get("title")]) if x]
                if not any(x in texts for x in titles):
                    continue  # not touched by this finalize
                items: set[tuple[str, int | None, str]] = set()
                bodies: dict[str, set[str]] = {}
                for x in titles:
                    tx = texts.get(x)
                    if tx is None and (zk_dir / PERMANENT_DIR / f"{x}.md").is_file():
                        tx = (zk_dir / PERMANENT_DIR / f"{x}.md").read_text(encoding="utf-8", errors="replace")
                    items |= _evidence_items(tx or "")
                    bodies[x] = {ln.strip() for ln in (tx or "").splitlines()}
                have = {(v_, t_) for v_, t_, _q in items} | {(v_, q_) for v_, _t, q_ in items}

                def kept(c: list[Any]) -> bool:  # W1/W11: the best-matching item itself
                    if len(c) >= 3:
                        return ((c[0], c[1]) in have) if c[1] is not None else ((c[0], c[2]) in have)
                    return (c[0], c[1]) in have
                if not any(kept(c) for c in a["cited"]):
                    b["answers"][vid] = {"decision": "unverified", "was": a,
                                         "why": "a fix removed the Evidence that carried the bullet"}
                    e["unverified_bullets"] = sorted(set(e.get("unverified_bullets") or []) | {bid})
                    out.setdefault(rel, []).append(bid)
                elif a.get("claim_lines") and not all(cl in set().union(*bodies.values()) for _tt, cl in a["claim_lines"]):
                    # W3: the Evidence stays, the claim line that carried the bullet is gone
                    b["answers"][vid] = {"decision": "unverified", "was": a,
                                         "why": "the claim line that carried the bullet changed"}
                    e["unverified_bullets"] = sorted(set(e.get("unverified_bullets") or []) | {bid})
                    changed_ = True
    if out or changed_:
        _write_json(sp, st)
    return out


def classify_bullets(e: dict[str, Any]) -> dict[str, list[str]]:
    """Pilot 8 (round 21): each bullet of an old note without a kept/corrected answer, by class:
    `unsupported` only when every planned source video answered with an explicit drop (not
    a damaged transcript); else `damaged`, `unverified`, `rejected` (target rejected) or
    `open` (no answer, unaccounted, left)."""
    out: dict[str, list[str]] = {"unsupported": [], "damaged": [], "unverified": [], "rejected": [], "open": []}
    planned = list(e.get("sources") or [])
    for bid, b in (e.get("bullets") or {}).items():
        ans = b.get("answers") or {}
        if any(a.get("decision") in ("kept", "corrected") for a in ans.values()):
            continue
        decs = [a.get("decision") for a in ans.values()]
        if any(a.get("reason") == "damaged transcript" for a in ans.values()):
            out["damaged"].append(bid)
        elif "unverified" in decs:
            out["unverified"].append(bid)
        elif "target-rejected" in decs:
            out["rejected"].append(bid)
        elif planned and all((ans.get(v) or {}).get("decision") == "dropped" for v in planned):
            out["unsupported"].append(bid)
        elif any(a.get("decision") == "dropped" and "contradict" in str(a.get("reason") or "") for a in ans.values()):
            out["unsupported"].append(bid)  # pilot 9: a "contradicts" drop is final (shown as `dropped:`)
        else:
            out["open"].append(bid)
    return out


def needs_repair_item(staging: Path, rel: str, e: dict[str, Any], open_ids: list[str]) -> dict[str, Any]:
    """Pilot 8: the operator entry of a note that stays unchanged: its open bullets (text and
    reason) and the quarantined versions of the targets that its answers named."""
    named: set[str] = set()
    for b in (e.get("bullets") or {}).values():
        for a in (b.get("answers") or {}).values():
            was = a.get("was") if isinstance(a.get("was"), dict) else {}
            for x in (a.get("titles") or [a.get("title")] or []) + list(was.get("titles") or []):
                if x:
                    named.add(x)
    quar = sorted(str(p.relative_to(staging)) for x in named for p in staging.glob(f"quarantine/**/{PERMANENT_DIR}/{x}.md"))
    return {"note": rel, "state": "needs-repair", "named_targets": sorted(named),
            "bullets": [{"id": b, "text": ((e.get("bullets") or {}).get(b) or {}).get("text", "")[:300],
                         "answers": sorted({str(a.get("decision")) for a in (((e.get("bullets") or {}).get(b) or {})
                                                                          .get("answers") or {}).values()})}
                        for b in open_ids],
            "quarantined_targets": quar}


def repair_bullet_counts(staging: Path, zk_dir: Path) -> dict[str, Any]:
    """Pilot 8 (round 21): every old bullet of every note in repair_state.json in exactly one
    class (the status that its stub line shows): `bullets_settled`, `bullets_unverified`,
    `bullets_open` (by reason), `bullets_dropped` (by reason), `bullets_unsupported`. The
    classes add up to `bullets_total` (asserted)."""
    st = _read_json_snapshot(staging / "repair_state.json", {}) or {}
    load_renames(staging)
    waiting = waiting_titles(staging)  # round 26 (b): no dead-request wait
    out: dict[str, Any] = {"bullets_total": 0, "bullets_settled": [], "bullets_unverified": [],
                           "bullets_open": {}, "bullets_dropped": {}, "bullets_unsupported": []}
    for _rel, e in sorted((st.get("notes") or {}).items()):
        for bid in (e.get("bullets") or {}):
            out["bullets_total"] += 1
            s, _p, _w = _bullet_status(zk_dir, e, bid, waiting)
            if s.startswith("→") and s.endswith("(unverified)"):
                out["bullets_unverified"].append(bid)
            elif s.startswith("→"):
                out["bullets_settled"].append(bid)
            elif s.startswith("unsupported:"):
                out["bullets_unsupported"].append(bid)
            elif s.startswith("dropped:"):
                reason = s[len("dropped: "):].rsplit(" (", 1)[0]
                out["bullets_dropped"].setdefault(reason, []).append(bid)
            else:
                reason = s[len("open: "):].split(" (", 1)[0]
                out["bullets_open"].setdefault(reason, []).append(bid)
    n = (len(out["bullets_settled"]) + len(out["bullets_unverified"]) + len(out["bullets_unsupported"])
         + sum(len(v) for v in out["bullets_open"].values()) + sum(len(v) for v in out["bullets_dropped"].values()))
    assert n == out["bullets_total"], f"bullet classes add up to {n}, not {out['bullets_total']}"
    out["counts"] = {"settled": len(out["bullets_settled"]), "unverified": len(out["bullets_unverified"]),
                     "open": sum(len(v) for v in out["bullets_open"].values()),
                     "dropped": sum(len(v) for v in out["bullets_dropped"].values()),
                     "unsupported": len(out["bullets_unsupported"]), "total": out["bullets_total"]}
    return out


def repair_open_bullets(staging: Path) -> list[dict[str, Any]]:
    """Bullets of old notes that no published note holds: per note, the open ids and why
    (no video supports it, its target note was rejected, its transcript was damaged, or the
    note that the ledger named cites no time near its passage)."""
    st = _read_json_snapshot(staging / "repair_state.json", {}) or {}
    out = []
    for rel, e in (st.get("notes") or {}).items():
        unsup = e.get("unsupported_bullets") or []
        damaged = e.get("damaged_bullets") or []
        unver = e.get("unverified_bullets") or []
        rej = e.get("rejected_bullets") or []  # pilot 8: open, target rejected
        other = e.get("open_bullets") or []
        ids = sorted(set(unsup) | set(damaged) | set(unver) | set(rej) | set(other))
        if e.get("status") != "open" and ids:
            out.append({"note": rel, "bullets": ids,
                        "target_rejected": sorted(set(rej) | {b for b in ids if b in e.get("reopened", [])}),
                        "damaged_transcript": list(damaged), "ledger_unverified": list(unver),
                        "unsupported": list(unsup), "open_other": list(other)})
    return out


DUP_TIME_S = 20
DUP_LEAD = 0.7


def _dup_sig(text: str) -> dict[str, Any]:
    fm, body, _ = verify_claims.split_frontmatter(text)
    vb = verify_claims.parse_body_full(body)
    times = {(m.group(1), verify_claims.ts_to_seconds(m.group(2))) for m in verify_claims.SRC_TAG.finditer(body)}
    lead = re.sub(r"\[src:[^\]]*\]", "", vb.lead or "")
    ev = vb.sections.get("Evidence")
    sc_ = fm.get("scope") if isinstance(fm.get("scope"), dict) else {}
    nums = {(v, q.unit) for q in verify_claims.extract_quantities((vb.h1 or "") + ". " + lead) for v in q.values}
    speakers = sorted(str(s.get("speaker") or "").lower() for s in fm.get("sources") or [] if isinstance(s, dict))
    return {"nums": nums, "skill": " ".join(str(sc_.get("skill") or "").lower().split()),
            "modality": " ".join(str(sc_.get("modality") or "").lower().split()),
            "speaker": (tuple(speakers), str(fm.get("speaker_label") or "")),
            "vids": set(verify_claims._source_ids(fm)), "times": {x for x in times if x[1] is not None},
            "lead": set(re.findall(r"[a-z0-9]{3,}", lead.lower())), "key": name_key(vb.h1 or ""),
            "evidence": len(ev.bullets()) if ev else 0, "evidence_times": {
                (m.group(1), verify_claims.ts_to_seconds(m.group(2)))
                for raw in (ev.bullets() if ev else []) for m in [verify_claims.EVIDENCE_ITEM.match("- " + raw)] if m}}


def same_note(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Round 16 (item 1): equal normalized title, or the same video with cited times within
    20 s and a lead-sentence word overlap of 0.7 or more."""
    if a.get("speaker") != b.get("speaker"):  # round 17 (c): another speaker is another note
        return False
    if a["key"] and a["key"] == b["key"]:
        return True
    if not a["vids"] & b["vids"]:
        return False
    # Round 17: conservative. (a) the same numbers with units in title and lead, (b) the
    # same stated skill (two steps of a progression are two notes), (d) the same modality.
    if a.get("nums") != b.get("nums"):
        return False
    ns = {"", "not stated"}
    if a.get("skill") not in ns and b.get("skill") not in ns and a.get("skill") != b.get("skill"):
        return False
    if a.get("modality") != b.get("modality"):
        return False
    near = any(va == vb_ and abs(ta - tb) <= DUP_TIME_S for va, ta in a["times"] for vb_, tb in b["times"])
    if not near or not a["lead"] or not b["lead"]:
        return False
    return len(a["lead"] & b["lead"]) / min(len(a["lead"]), len(b["lead"])) >= DUP_LEAD


def possible_duplicates(staging: Path, zk_dir: Path | None) -> list[dict[str, Any]]:
    """Pilot 9 (round 22): the report, one entry per pair, each marked with whether both notes
    are published (in the vault, not stubs) now."""
    out, seen = [], set()
    for x in _read_json(staging / "possible_duplicates.json", []) or []:
        k = frozenset((x.get("a"), x.get("b")))
        if k in seen or x.get("a") == x.get("b"):
            continue
        seen.add(k)
        both = zk_dir is not None and all(_published_contract(zk_dir, str(x.get(s))) for s in ("a", "b"))
        out.append(dict(x, both_published=both))
    return out


def report_pair(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Pilot 8 (round 21): the REPORT-only rule for possible_duplicates.json (no merge):
    `same_note`, or the same speaker and video with cited times within 20 s and a lead
    overlap of 0.7 or more, whatever the numbers, skill or modality (those guard the merge,
    not the report)."""
    if same_note(a, b):
        return True
    if a.get("speaker") != b.get("speaker") or not (a["vids"] & b["vids"]):
        return False
    near = any(va == vb_ and abs(ta - tb) <= DUP_TIME_S for va, ta in a["times"] for vb_, tb in b["times"])
    if not near or not a["lead"] or not b["lead"]:
        return False
    return len(a["lead"] & b["lead"]) / min(len(a["lead"]), len(b["lead"])) >= DUP_LEAD


def pipeline_published(staging: Path, zk_dir: Path, vids: set[str]) -> list[str]:
    """Notes that this pipeline published for these videos and that are in the vault."""
    out = []
    for r in provenance.read_records(staging):
        if r.get("event") == "note-published" and set(r.get("sources") or []) & vids:
            rel = str(r.get("note"))
            if rel not in out and _published_contract(zk_dir, rel):
                out.append(rel)
    return out


def _title_claim(text: str) -> str:
    """The title without its speaker prefix, normalized (round 18)."""
    fm, body, _ = verify_claims.split_frontmatter(text)
    h1 = (verify_claims.parse_body_full(body).h1 or "").replace("_", " ").lower()
    h1 = re.sub(r"[^a-z0-9 ]", "", h1)
    for s in fm.get("sources") or []:
        sp = re.sub(r"[^a-z0-9 ]", "", str((s or {}).get("speaker") or "").replace("_", " ").lower()).strip()
        if sp and h1.startswith(sp + " "):
            h1 = h1[len(sp):]
            break
    return " ".join(h1.split())


def _evidence_tags(text: str) -> set[tuple[str, Any]]:
    """(video, time) of each Evidence item; W11 (round 21): an untimed item is
    (video, "q:<quote hash>"), so keeping 1 of N untimed items is not "all kept"."""
    return {(v, t_ if t_ is not None else f"q:{q}") for v, t_, q in _evidence_items(text)}


def _tags_missing(old: list[Any] | set[Any], new: set[tuple[str, Any]]) -> set[tuple[str, Any]]:
    """Old tags that the new tag set lacks; an old record's (video, None) is kept by any
    untimed item of that video."""
    out = set()
    for v, t_ in old:
        if t_ is None:
            if not any(v2 == v and isinstance(x, str) for v2, x in new):
                out.add((v, t_))
        elif (v, t_) not in new:
            out.add((v, t_))
    return out


def _evidence_items(text: str) -> set[tuple[str, int | None, str]]:
    """Round 21 (W11): (video, time, quote hash) of each Evidence item; an untimed item is
    told apart by its quote."""
    _, body, _ = verify_claims.split_frontmatter(text)
    ev = verify_claims.parse_body_full(body).sections.get("Evidence")
    out = set()
    for raw in ev.bullets() if ev else []:
        m = verify_claims.EVIDENCE_ITEM.match("- " + raw)
        if m:
            q = hashlib.sha256(" ".join(m.group(4).split()).encode()).hexdigest()[:12]
            out.add((m.group(1), verify_claims.ts_to_seconds(m.group(2)) if m.group(2) else None, q))
    return out


def _bullet_record(staging: Path, rel: str, text: str) -> dict[str, Any]:
    """V7: the bullets that this published note settles (from repair_state.json), and the
    Evidence tags (video, time) that it holds."""
    title = Path(rel).stem
    st = _read_json_snapshot(staging / "repair_state.json", {}) or {}
    bullets = sorted({bid for e in (st.get("notes") or {}).values() for bid, b in (e.get("bullets") or {}).items()
                      for a in (b.get("answers") or {}).values() if a.get("decision") in ("kept", "corrected")
                      and title in (a.get("titles") or [a.get("title")])})
    rec: dict[str, Any] = {"evidence_tags": sorted(([v, t_] for v, t_ in _evidence_tags(text)), key=str)}
    if bullets:
        rec["bullets"] = bullets
    return rec


def last_published(staging: Path, rel: str) -> dict[str, Any] | None:
    recs = [r for r in provenance.read_records(staging) if r.get("event") == "note-published" and r.get("note") == rel]
    return recs[-1] if recs else None


def extension_loss(staging: Path, rel: str, new_text: str) -> dict[str, Any] | None:
    """V7: a later version of a published repair note must keep every Evidence tag of the
    published version (quotes may grow, tags may not disappear)."""
    prev = last_published(staging, rel)
    if not prev or not prev.get("bullets"):
        return None
    missing = _tags_missing([tuple(x) for x in prev.get("evidence_tags") or []], _evidence_tags(new_text))
    if missing:
        return {"type": "extend-lost-evidence", "where": "Evidence",
                "detail": f"the published version settles {prev['bullets'][:5]}; its Evidence tags {sorted(missing, key=str)[:4]} "
                          "are missing in the new version (the published version stays)"}
    return None


def merge_duplicates(staging: Path, zk_dir: Path, unit_dir: Path, results: list[dict[str, Any]],
                     decisions: dict[str, str], kind: str = "synth") -> list[dict[str, Any]]:
    """Round 18 (narrowed): merge only the one safe case: two NEW notes of THIS unit in THIS
    finalize, both in the publish set, with the same title claim (speaker prefix removed),
    the same non-empty number set, the same speaker, and a symmetric lead overlap of 0.9 or
    more. Never in repair units (unless ZR_MERGE_IN_REPAIR=1), never against a vault note,
    never settling repair bullets. Every other look-alike pair (also against this
    pipeline's published notes) is only listed in `possible_duplicates.json`."""
    okd = ("publish", "publish-reviewed")
    live = [r for r in results if decisions.get(r["rel"]) in okd + ("pending",) and not r["stub"]
            and not r["existed_before"] and r["rel"].startswith(PERMANENT_DIR + "/")]
    texts = {r["rel"]: r["data"].decode("utf-8", errors="replace") for r in live}
    sigs = {rel: _dup_sig(tx) for rel, tx in texts.items()}
    allow = kind != "repair" or os.environ.get("ZR_MERGE_IN_REPAIR") == "1"
    out, possible = [], []
    kept_new: list[str] = []
    for r in sorted(live, key=lambda x: x["rel"]):
        s = sigs[r["rel"]]
        into = None
        for k in kept_new:
            ks = sigs[k]
            sym = len(s["lead"] & ks["lead"]) / max(1, max(len(s["lead"]), len(ks["lead"])))
            if (allow and decisions.get(r["rel"]) in okd and decisions.get(k) in okd
                    and _title_claim(texts[r["rel"]]) == _title_claim(texts[k]) and s["nums"] and s["nums"] == ks["nums"]
                    and s.get("speaker") == ks.get("speaker") and sym >= 0.9):
                into = k
                break
            if report_pair(s, ks):
                possible.append({"a": k, "b": r["rel"], "unit": unit_dir.name})
        if into is None:
            kept_new.append(r["rel"])
            continue
        decisions[r["rel"]] = "merged"
        r["merged_into"] = into
        out.append({"dropped": r["rel"], "kept": into, "rule": "same-unit-equal-title-numbers-lead0.9"})
        provenance.record(staging, "duplicate-merged", dropped=r["rel"], kept=into, unit=unit_dir.name)
    # report-only: look-alikes of this pipeline's published notes
    vids = set().union(*[s["vids"] for s in sigs.values()]) if sigs else set()
    for prel in pipeline_published(staging, zk_dir, vids):
        if prel in sigs:
            continue
        ps = _dup_sig((zk_dir / prel).read_text(encoding="utf-8", errors="replace"))
        for rel, s in sigs.items():
            if report_pair(s, ps):
                possible.append({"a": prel, "b": rel, "unit": unit_dir.name})
    for m in out:
        a, b = Path(m["dropped"]).stem, Path(m["kept"]).stem
        pat = re.compile(r"\[\[" + re.escape(a) + r"(?=[\]|#])")
        for r in results:
            if r["rel"] == m["kept"]:
                continue  # V28: never rewrite inside the kept note (no self-link)
            txt = r["data"].decode("utf-8", errors="replace")
            new = pat.sub("[[" + b, txt)
            if new != txt:
                r["data"] = new.encode("utf-8")
        cl = _read_json(unit_dir / "claims.json", {}) or {}
        for cov in (cl.get("coverage") or {}).values():
            if cov.get("note") == a:
                cov["note"], cov["merged_from"] = b, a
        if cl:
            _write_json(unit_dir / "claims.json", cl)
    if out:
        _write_json(unit_dir / "merged.json", out)
    if possible:
        for p in possible:
            for side in ("a", "b"):
                src = zk_dir / p[side] if (zk_dir / p[side]).is_file() and p[side] not in texts else None
                tx = src.read_text(encoding="utf-8", errors="replace") if src else texts.get(p[side], "")
                p[side + "_lead"] = (verify_claims.parse_body_full(verify_claims.split_frontmatter(tx)[1]).lead or "")[:300]
        reg = _read_json(staging / "possible_duplicates.json", []) or []
        seen = {frozenset((x.get("a"), x.get("b"))) for x in reg}
        for p in possible:  # pilot 9 (round 22): one entry per pair
            k = frozenset((p["a"], p["b"]))
            if p["a"] != p["b"] and k not in seen:
                seen.add(k)
                reg.append(p)
        _write_json(staging / "possible_duplicates.json", reg)
    return out


UNLINKED = "unlinked.json"
LINK_ANY = re.compile(r"!?\[\[([^\]|#]+)((?:#[^\]|]*)?)(?:\|([^\]]+))?\]\]")  # V45: embeds too


def unlink_waiting_siblings(staging: Path, results: list[dict[str, Any]], decisions: dict[str, str],
                            zk_dir: Path | None = None) -> list[dict[str, Any]]:
    """Round 16/18 (item C): a note that publishes now keeps no wiki link to a NEW sibling of
    this unit (any folder) that is quarantined, pending, merged away or a draft and that is
    not in the vault: the link becomes plain text, and `unlinked.json` lists it for the
    operator (no automatic restore). Only body text is touched: not code blocks or inline
    code, not Evidence lines, not tables. A note left without any link gets the link the
    contract's orphan rule accepts (`_ensure_a_link`)."""
    gone = {Path(r["rel"]).stem for r in results if decisions.get(r["rel"]) in ("quarantine", "pending", "merged", "draft")
            and r["rel"].endswith(".md") and not r["existed_before"]
            and not (zk_dir is not None and (zk_dir / r["rel"]).is_file())}
    if not gone:
        return []
    reg = _read_json(staging / UNLINKED, []) or []
    out = []
    for r in results:
        if decisions.get(r["rel"]) not in ("publish", "publish-reviewed"):
            continue
        lines = r["data"].decode("utf-8", errors="replace").split("\n")
        changed, fence, section = False, False, ""
        for i, ln in enumerate(lines):
            if ln.strip().startswith("```"):
                fence = not fence
                continue
            if ln.startswith("## "):
                section = ln[3:].strip()
            if fence or section == "Evidence" or ln.lstrip().startswith("|"):
                continue
            parts = re.split(r"(`[^`]*`)", ln)  # inline code stays as it is
            for j in range(0, len(parts), 2):
                parts[j] = LINK_ANY.sub(lambda m: (m.group(3) or m.group(1)) if m.group(1).strip() in gone else m.group(0),
                                        parts[j])
            new = "".join(parts)
            if new != ln:
                if section == "Connected Ideas" and not LINK_ANY.search(new):
                    # Each Connected Ideas bullet must carry a link, even when its original
                    # target is waiting. Keep the title text and make the offline fallback
                    # explicit; relink records the final line below so it can match exactly.
                    if zk_dir is not None:
                        ensure_home(staging, zk_dir)
                    new = new.rstrip() + " — index: [[Home]]"
                for m in LINK_ANY.finditer(ln):
                    if m.group(1).strip() in gone:
                        # Record the post-unlink line (including any Home fallback) for offline relink.
                        rec = {"note": r["rel"], "target": m.group(1).strip(), "at": _now(), "line": new,
                               "link": m.group(0), "plain": m.group(3) or m.group(1)}
                        reg.append(rec)
                        out.append(rec)
                lines[i] = new
                changed = True
        if changed:
            text = "\n".join(lines)
            if zk_dir is not None:
                text = _ensure_a_link(text, staging, zk_dir)  # V25: never an orphan
            r["data"] = text.encode("utf-8")
            r["entry"]["links_unlinked"] = sorted({x["target"] for x in out if x["note"] == r["rel"]})
    _write_json(staging / UNLINKED, reg)
    return out

def relink(staging: Path, zk_dir: Path, apply: bool = False) -> dict[str, Any]:
    """Pilot 8 (round 21): offline maintenance, never called by finalize or a loop. For each
    `unlinked.json` entry whose target is now a published non-stub note and whose plain text
    is still present exactly once on the recorded line (the line itself present exactly once
    in the note), the link comes back. The publisher archives first and refuses a note
    that changed since the pipeline's last write. Any mismatch: skipped and listed. Dry run
    by default."""
    reg = _read_json(staging / UNLINKED, []) or []
    out: dict[str, Any] = {"restored": [], "skipped": [], "apply": apply}
    pub = None
    if apply:
        unit = staging / "maintenance" / f"relink-{_now().replace(':', '')}"
        unit.mkdir(parents=True, exist_ok=True)
        pub = _Publisher(staging, unit.name, unit, zk_dir, {})
    keep = []
    for rec in reg:
        why = ""
        p = zk_dir / str(rec.get("note") or "")
        if not rec.get("line") or not rec.get("link") or not rec.get("plain"):
            why = "no recorded line (entry from before round 21)"
        elif not _published_contract(zk_dir, f"{PERMANENT_DIR}/{rec['target']}.md"):
            why = "target is not a published non-stub note"
        elif not p.is_file():
            why = "note not in the vault"
        elif verify_claims.is_stub(verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))[0]):
            why = "the note is a stub now (its old text is never changed)"  # X26
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.split("\n")
            hits = [i for i, ln in enumerate(lines) if ln == rec["line"]]
            if len(hits) != 1:
                why = f"recorded line found {len(hits)} times"
            elif lines[hits[0]].count(rec["plain"]) != 1:
                why = "plain text not exactly once on the line"
            else:
                lines[hits[0]] = lines[hits[0]].replace(rec["plain"], rec["link"], 1)
                new = "\n".join(lines)
                if apply and pub is not None:
                    if not pub.write(rec["note"], new, f"relink [[{rec['target']}]]"):
                        why = "write refused (see needs_operator.json)"
                if not why:
                    out["restored"].append({"note": rec["note"], "target": rec["target"]})
                    if apply:
                        continue  # the entry ends
        if why:
            out["skipped"].append({"note": rec.get("note"), "target": rec.get("target"), "why": why})
        keep.append(rec)
    if apply:
        _write_json(staging / UNLINKED, keep)
        provenance.record(staging, "relink", restored=out["restored"], skipped=len(out["skipped"]))
        if pub is not None and pub.commit_paths:  # X25: the vault git state as after a finalize
            out["commit"] = _git_commit(zk_dir, pub.commit_paths, "zettel: relink", staging).get("ok")
    return out


def _repair_view(staging: Path, zk_dir: Path, e: dict[str, Any]) -> dict[str, Any]:
    """Round 15 (item 2): one old note from the persistent repair state: its target titles
    (published / pending / other), its bullets (settled in a PUBLISHED note / open /
    unsupported)."""
    pending_titles = {p.stem for p in staging.glob(f"pending-review/*--*/out/{PERMANENT_DIR}/*.md")}
    titles: set[str] = set(e.get("titles") or [])
    settled_by: dict[str, set[str]] = {}
    for bid, b in (e.get("bullets") or {}).items():
        for a in (b.get("answers") or {}).values():
            if a.get("decision") in ("kept", "corrected"):
                for x in a.get("titles") or [a.get("title")]:
                    if x:
                        titles.add(x)
                        settled_by.setdefault(bid, set()).add(x)
    # V10: a target that became a stub resolves to the published notes at the end of its chain
    final_of = {x: _final_targets(zk_dir, x) for x in titles}
    published = sorted({y for x in titles for y in final_of[x]})
    pending = sorted(x for x in titles if not final_of[x] and x in pending_titles)
    settled_by = {bid: {y for x in ts for y in final_of.get(x, [])} | ts for bid, ts in settled_by.items()}
    unsupported = set(e.get("unsupported_bullets") or [])
    settled = {bid for bid, ts in settled_by.items() if ts & set(published)}
    open_b = [bid for bid in (e.get("bullets") or {}) if bid not in settled and bid not in unsupported]
    return {"published": published, "pending": pending, "settled": sorted(settled), "open": open_b,
            "unsupported": sorted(unsupported)}


def rename_in_state(staging: Path, renamed: dict[str, str], unit_dir: Path | None = None) -> None:
    """Round 19 (pilot 7): a title that a fix call renamed is renamed in repair_state.json
    too (titles and answers), so stubs and the invariant follow it. W17 (round 21): with
    `unit_dir`, only the old notes of that unit (`repair_map.json`) and only the answers of
    its video change; another old note's answer that names the same title keeps it."""
    if not renamed:
        return
    scope: set[str] | None = None
    uvid = ""
    if unit_dir is not None:
        scope = set((_read_json(unit_dir / "repair_map.json", {}) or {}))
        uvid = str((_read_json(unit_dir / "unit.json", {}) or {}).get("repair_video") or "")
        if not scope:
            return
    sp = staging / "repair_state.json"
    st = _read_json(sp, None)
    if not st:
        return

    def fin(x: str) -> str:
        for _ in range(10):
            if x not in renamed:
                break
            x = renamed[x]
        return x
    changed = False
    for rel_, e in (st.get("notes") or {}).items():
        if scope is not None and rel_ not in scope:
            continue
        new = [fin(x) for x in e.get("titles") or []]
        if new != e.get("titles"):
            e["titles"], changed = new, True
        for b in (e.get("bullets") or {}).values():
            for v_, a in (b.get("answers") or {}).items():
                if uvid and v_ != uvid:
                    continue
                if a.get("title") and fin(a["title"]) != a["title"]:
                    a["title"], changed = fin(a["title"]), True
                if a.get("titles"):
                    nt = [fin(x) for x in a["titles"]]
                    if nt != a["titles"]:
                        a["titles"], changed = nt, True
    if changed:
        _write_json(sp, st)


def _final_targets(zk_dir: Path, title: str, depth: int = 0) -> list[str]:
    """V10: a published non-stub note -> [title]; a stub -> the final targets of its
    superseded_by chain; else []."""
    p = zk_dir / PERMANENT_DIR / f"{title}.md"
    if depth > 8 or not p.is_file():
        return []
    fm, _, _ = verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
    if verify_claims.is_stub(fm):
        return sorted({y for x in _stub_targets(fm) for y in _final_targets(zk_dir, Path(x).stem, depth + 1)})
    return [title] if verify_claims.is_contract_note(fm) else []


def recompute_repair_stubs(staging: Path, zk_dir: Path, pub: Any) -> list[dict[str, Any]]:
    """Round 15 (item 2): at every finalize, the stub of every old note in repair_state.json
    is rebuilt from the persistent state: published targets are linked, pending targets are
    `note: <Title>`, and every bullet that no published note holds and that is not recorded
    as unsupported is `bullet: <id>`. Written only when it differs (the publisher archives)."""
    st = _read_json_snapshot(staging / "repair_state.json", {}) or {}
    out = []
    for rel, e in (st.get("notes") or {}).items():
        if safe_note_rel(zk_dir, rel) is None or not (zk_dir / rel).is_file():
            continue
        cur0 = (zk_dir / rel).read_text(encoding="utf-8", errors="replace")
        fm0, _, _ = verify_claims.split_frontmatter(cur0)
        if mark_eligible(e) and _sha256(zk_dir / rel) == e.get("sha") and not verify_claims.is_stub(fm0):
            # round 24: every planned video said "not in this transcript" for every bullet;
            # round 25 (Z-B5): and every recorded source was asked, else operator work
            if mark_or_list(staging, zk_dir, rel, e, pub) == "marked":
                out.append({"note": rel, "marked_unsupported": True})
            continue
        if verify_claims.is_stub(fm0) and "old_bullets" not in fm0:
            # X3 (round 22): a stub written before round 21 is upgraded from its archive
            # (rendered with the old text), or refused and listed (no archive)
            if pub.write(rel, cur0, why="legacy stub upgraded with the old text") and not pub.unchanged:
                out.append({"note": rel, "upgraded": True})
            continue
        v = _repair_view(staging, zk_dir, e)
        if not v["published"] or Path(rel).stem in v["published"]:
            continue
        cur = (zk_dir / rel).read_text(encoding="utf-8", errors="replace")
        fm, _, _ = verify_claims.split_frontmatter(cur)
        if not verify_claims.is_stub(fm) and ri_old_is_unchanged(e, zk_dir / rel) is False:
            continue  # the user changed the old note: leave it (R15)
        pending_l = [f"note: {x}" for x in v["pending"]] + [f"bullet: {b}" for b in v["open"]]
        want = repair_stub(Path(rel).stem, v["published"], pending_l)
        # X3 (round 22): the publisher renders the FULL stub and compares it with the vault
        # file (a stub without old_bullets is upgraded from its archive, or refused and listed)
        if pub.write(rel, want, why="stub recomputed from the repair state") and not pub.unchanged:
            out.append({"note": rel, "published": v["published"], "pending": pending_l})
    return out


def _repair_written_targets(staging: Path, zk_dir: Path) -> set[str]:
    """Repair-plan targets whose latest actual pipeline publication is still repair-owned.

    A note may continue across repair runs/videos, but any later actual synthesis write takes
    ownership away. The unit publish journal attributes the write and its SHA to a unit; the
    current vault bytes must still match that recorded SHA. Legacy events are usable only when
    their unit can be uniquely located and its journal proves the publication.
    """
    st = _read_json_snapshot(staging / "repair_state.json", {}) or {}
    titles: set[str] = set()
    for e in (st.get("notes") or {}).values():
        titles |= {str(t) for t in (e.get("titles") or []) if t}
        for b in (e.get("bullets") or {}).values():
            for a in (b.get("answers") or {}).values():
                if a.get("decision") in ("kept", "corrected"):
                    titles |= {str(t) for t in (a.get("titles") or [a.get("title")]) if t}

    # Resolve each actual note-published record to the unit journal that proves its write.
    # A legacy unit name can repeat across runs, so resolve by rel/hash evidence and require
    # one semantic owner (kind + SHA); ambiguous historical attribution fails closed.
    def unit_candidates(rec: dict[str, Any]) -> list[Path]:
        unit_name = str(rec.get("unit") or "")
        if not unit_name or Path(unit_name).name != unit_name:
            return []
        if rec.get("run_id"):
            run_id = str(rec["run_id"])
            if Path(run_id).name != run_id:
                return []
            candidate = staging / "runs" / run_id / "units" / unit_name
            return [candidate] if candidate.is_dir() else []
        return [p for p in (staging / "runs").glob(f"*/units/{unit_name}") if p.is_dir()]

    latest_actual: dict[str, tuple[str, str] | None] = {}
    for rec in provenance.read_records(staging):
        if rec.get("event") != "note-published":
            continue
        rel = str(rec.get("note") or "")
        if not rel.startswith(PERMANENT_DIR + "/"):
            continue
        if rec.get("unchanged") is True:
            continue  # validation of existing bytes does not transfer write ownership
        event_sha = str(rec.get("sha256") or "")
        owners: set[tuple[str, str]] = set()
        for unit_dir in unit_candidates(rec):
            info = _read_json(unit_dir / "unit.json", {}) or {}
            journal = [x for x in _read_jsonl(unit_dir / "publish.jsonl")
                       if x.get("phase") == "done" and x.get("rel") == rel and x.get("sha256")]
            if not journal:
                continue
            journal_sha = str(journal[-1]["sha256"])
            if event_sha and event_sha != journal_sha:
                continue
            kind = str(info.get("kind") or "")
            if kind:
                owners.add((kind, event_sha or journal_sha))
        # An unresolvable later publication is an ownership boundary. It cannot inherit an
        # older repair's authorization just because that older content happens to reappear.
        latest_actual[rel] = next(iter(owners)) if len(owners) == 1 else None

    published: set[str] = set()
    for rel, owner in latest_actual.items():
        if owner is None:
            continue
        kind, sha = owner
        if kind == "repair" and _sha256(zk_dir / rel) == sha:
            published.add(rel)
    return {f"{PERMANENT_DIR}/{t}.md" for t in titles} & published


def ri_old_is_unchanged(e: dict[str, Any], p: Path) -> bool:
    return _sha256(p) == e.get("sha")


def bullet_invariant(staging: Path, zk_dir: Path) -> list[dict[str, Any]]:
    """Round 15 (item 2): for every old note, each bullet is settled in a PUBLISHED note, or
    open (listed in the stub, or the note is still open), or recorded as unsupported."""
    st = _read_json_snapshot(staging / "repair_state.json", {}) or {}
    out = []
    for rel, e in (st.get("notes") or {}).items():
        v = _repair_view(staging, zk_dir, e)
        p = zk_dir / rel
        listed, linked, is_stub = set(), set(), False
        if p.is_file():
            fm, _, _ = verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            is_stub = verify_claims.is_stub(fm)
            listed = {str(x).split("bullet: ", 1)[-1] for x in fm.get("superseded_pending") or [] if str(x).startswith("bullet: ")}
            linked = {Path(x).stem for x in _stub_targets(fm)} if is_stub else set()
        if e.get("status") != "open":
            lost = [b for b in v["open"] if b not in listed]
            if lost and v["published"]:
                out.append({"note": rel, "bullets": lost, "why": "in no published note, not in the stub, not unsupported"})
        waiting_ = waiting_titles(staging)  # round 26 (b): no dead-request wait
        for b in sorted(listed & set(v["settled"])):
            if _bullet_status(zk_dir, e, b, waiting_)[0].endswith("(unverified)"):
                continue  # round 22: an (unverified) pointer is listed for the operator on purpose
            out.append({"note": rel, "bullets": [b], "why": "listed open in the stub but held by a published note"})
        for b in sorted(set(e.get("unsupported_bullets") or []) & set(v["settled"])):
            out.append({"note": rel, "bullets": [b], "why": "recorded unsupported but held by a published note"})
        quarantined_t = {q.stem for q in staging.glob(f"quarantine/**/{PERMANENT_DIR}/*.md")}
        state = _RENAME_INDEX.get()
        renamed_map = (_rename_map(staging) if state and state["root"] == staging.resolve()
                       else _load_renames_uncached(staging))
        renamed_t = set(renamed_map)
        pending_t = {q.stem for q in staging.glob(f"pending-review/*--*/out/{PERMANENT_DIR}/*.md")}
        unlinked_ren: set[tuple[str, str]] = set()
        for bid, b in (e.get("bullets") or {}).items():  # pilot 7: a fix call renamed the target
            if bid in set(v["settled"]):
                continue  # held by a published note
            for a in (b.get("answers") or {}).values():
                if a.get("decision") not in ("kept", "corrected"):
                    continue
                for x in a.get("titles") or [a.get("title")]:
                    fx, seen = x, set()  # round 20: follow the rename chain (state with old titles)
                    while fx in renamed_map and fx not in seen:
                        seen.add(fx)
                        fx = renamed_map[fx]
                    if x and fx != x and _published_contract(zk_dir, f"{PERMANENT_DIR}/{fx}.md"):
                        if is_stub and fx not in linked:
                            unlinked_ren.add((fx, x))
                        if bid in listed:
                            out.append({"note": rel, "bullets": [bid],
                                        "why": f"listed open in the stub but held by [[{fx}]] (renamed from [[{x}]])"})
                        continue
                    if x and not _final_targets(zk_dir, x) and x not in pending_t and x not in quarantined_t \
                            and (x in renamed_t or bid not in listed):
                        out.append({"note": rel, "bullets": [bid], "why": f"settled in [[{x}]], which exists nowhere (renamed?)"})
        for fx, x in sorted(unlinked_ren):
            out.append({"note": rel, "targets": [fx], "why": f"published replacement not linked by the stub (renamed from [[{x}]])"})
        if is_stub:  # round 20 (pilot 7, "Unassisted" b3): "unsupported" needs a drop answer
            for bid in sorted(set(e.get("unsupported_bullets") or []) - listed - set(v["settled"])):
                ans = ((e.get("bullets") or {}).get(bid) or {}).get("answers") or {}
                if not any(a.get("decision") == "dropped" for a in ans.values()):
                    out.append({"note": rel, "bullets": [bid],
                                "why": "recorded unsupported with no drop answer from any video, not in the stub"})
            for bid in sorted((set(e.get("unverified_bullets") or []) | set(e.get("damaged_bullets") or []))
                              - listed - set(v["settled"])):
                out.append({"note": rel, "bullets": [bid], "why": "unverified or damaged, not in the stub"})
        if is_stub:  # round 19 (pilot 7): every published replacement is linked by the stub
            unlinked_t = [x for x in v["published"] if x not in linked]
            if unlinked_t:
                out.append({"note": rel, "targets": unlinked_t, "why": "published replacement not linked by the stub"})
    for rel in (st.get("notes") or {}):  # V9/V10: a stub links only published non-stub notes
        p = zk_dir / rel
        if p.is_file():
            fm2, _, _ = verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            if verify_claims.is_stub(fm2):
                bad = [x for x in _stub_targets(fm2) if _final_targets(zk_dir, Path(x).stem) != [Path(x).stem]]
                if bad:
                    out.append({"note": rel, "targets": bad, "why": "superseded_by names a missing note or a stub"})
    for rel, e in (st.get("notes") or {}).items():  # V7: the published file still records each settled bullet
        for bid, b in (e.get("bullets") or {}).items():
            for a in (b.get("answers") or {}).values():
                if a.get("decision") not in ("kept", "corrected"):
                    continue
                for x in a.get("titles") or [a.get("title")]:
                    trel = f"{PERMANENT_DIR}/{x}.md"
                    if not x or not _published_contract(zk_dir, trel):
                        continue
                    prev = last_published(staging, trel)
                    if prev and prev.get("bullets") is not None and bid not in prev["bullets"]:
                        out.append({"note": rel, "bullets": [bid], "why": f"[[{x}]] does not record this bullet"})
                    elif prev and prev.get("evidence_tags"):
                        miss = _tags_missing([tuple(x) for x in prev["evidence_tags"]], _evidence_tags(
                            (zk_dir / trel).read_text(encoding="utf-8", errors="replace")))
                        if miss:
                            out.append({"note": rel, "bullets": [bid], "why": f"[[{x}]] lost Evidence {sorted(miss, key=str)[:3]}"})
    return out


def _exchange_unconsumed(staging: Path) -> list[str]:
    """Answered replies that no unit took (listed for the operator, not counted as open)."""
    x = staging / "exchange"
    if not (x / "replies").is_dir():
        return []
    consumed = {p.stem for p in (x / "consumed").glob("*.json")} if (x / "consumed").is_dir() else set()
    return sorted(p.stem for p in (x / "replies").glob("*.txt") if ".refused" not in p.stem and p.stem not in consumed)


def needs_operator(staging: Path, items: list[dict[str, Any]], why: str) -> None:
    """Round 18: what the pipeline leaves to the operator (`needs_operator.json`)."""
    if not items:
        return
    reg = _read_json(staging / "needs_operator.json", []) or []
    seen = {(x.get("note"), x.get("why")) for x in reg}
    for it in items:
        if (it.get("note"), why) not in seen:
            reg.append(dict(it, why=why, at=_now()))
    _write_json(staging / "needs_operator.json", reg)


def repair_status_rows(staging: Path, zk_dir: Path) -> list[dict[str, Any]]:
    """Round 22/23 (one source of truth with `operator_work`): per old note of the repair
    plan: its state, the vault form (stub / unchanged / waiting / changed / missing), the
    stub-check problems, open and contradicts bullets, and the operator reasons. `unlisted`
    marks a finished, unchanged note with open bullets that needs_operator.json does not
    name and whose targets do not wait (Z17)."""
    import stub_check  # noqa: PLC0415
    import validate  # noqa: PLC0415

    st = _read_json_snapshot(staging / "repair_state.json", {}) or {}
    ops: dict[str, list[str]] = {}
    for x in _read_json(staging / "needs_operator.json", []) or []:
        ops.setdefault(str(x.get("note")), []).append(str(x.get("why") or ""))
    waiting = waiting_titles(staging)  # round 26 (b): no dead-request wait
    load_renames(staging)
    rows = []
    for rel, e in sorted((st.get("notes") or {}).items()):
        p = zk_dir / rel
        problems: list[str] = []
        if not p.is_file():
            vault = "missing"
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
            fm, _ = stub_check.split(text)
            if re.search(r"(?m)^old_bullets:", fm):
                problems = [x.split(": ", 1)[-1] for x in validate.stub_errors(staging, zk_dir, [p])]
                vault = "stub (check failed)" if problems else "stub"  # Z3 (round 23)
            elif _sha256(p) == e.get("sha"):
                vault = "unchanged"
            elif re.search(r"(?m)^unsupported_marked:", fm or ""):
                vault = "marked-unsupported"  # round 24 (validate checks the three-key rule)
            else:
                vault = "changed (not a pipeline stub)"
        stats = {b: _bullet_status(zk_dir, e, b, waiting)[0] for b in (e.get("bullets") or {})}
        open_b = [b for b, s in stats.items() if s.startswith("open:")]
        contra = [b for b, s in stats.items() if s.startswith("dropped: contradicts")]
        waits = any(s.startswith("open: target waiting") for s in stats.values()) or e.get("status") == "open"
        if vault == "unchanged" and waits:
            vault = "waiting"
        reasons = list(ops.get(rel, []))
        if vault == "missing" and e.get("status") != "operator-closed":
            reasons.append("repair-plan note is missing from the vault (removed or renamed?)")  # Z5
        if problems:
            reasons.append("stub check failed: " + "; ".join(problems)[:200])  # Z3
        row = {"note": rel, "state": e.get("status"), "vault": vault, "bullets": len(stats), "open_bullets": len(open_b),
               "contradicts": contra, "stub_problems": problems, "operator": reasons}
        row["unlisted"] = (vault == "unchanged" and e.get("status") not in ("open", "operator-closed")
                           and not ops.get(rel) and bool(open_b or e.get("status") == "done-unsupported"))
        rows.append(row)
    for m in marked_unsupported_notes(zk_dir, staging):  # round 24: marked notes outside the plan
        if m["note"] not in (st.get("notes") or {}):
            rows.append({"note": m["note"], "state": "marked", "vault": "marked-unsupported", "bullets": 0,
                         "open_bullets": 0, "contradicts": [], "stub_problems": [],
                         "operator": ops.get(m["note"], []), "unlisted": False, "reason": m.get("reason")})
    for rel, why in sorted(ops.items()):  # Z30: listed notes outside the plan (unsupported, operator)
        if rel not in (st.get("notes") or {}):
            rows.append({"note": rel, "state": "listed", "vault": "unchanged" if (zk_dir / rel).is_file() else "missing",
                         "bullets": 0, "open_bullets": 0, "contradicts": [], "stub_problems": [], "operator": why,
                         "unlisted": False})
    return rows


def operator_work(staging: Path, zk_dir: Path | None = None) -> list[str]:
    """Round 22/23: the ONE list of operator work (exit 7, `repair-status`, the summary):
    needs_operator.json entries; `needs-repair` and `done-unsupported` notes; UNLISTED notes;
    missing plan notes (Z5); stub-check and two-state errors of validate (Z3); old notes
    beside a replacement; pending folders that wait for no reply; `failed` queue items (Z9).
    `operator-done` clears an entry."""
    out = [f"needs_operator: {Path(str(x.get('note'))).stem} - {str(x.get('why'))[:100]}"
           for x in (_read_json(staging / "needs_operator.json", []) or [])]
    for p in sorted((staging / "pending-review").glob("*--*/meta.json")):
        m = _read_json(p, {}) or {}
        rs = sorted(set((m.get("reasons") or {}).values()))
        dead_k = folder_dead(staging, p.parent)
        if dead_k:  # round 26 (b): never silent; operator-done quarantines its notes
            if (p.parent / OPERATOR_DEAD).is_file():
                out.append(f"pending: {p.parent.name} (operator-done: its notes are quarantined at the next pass): "
                           "run the driver again")
                continue
            for n_ in sorted(r_ for r_, why in (m.get("reasons") or {}).items() if why == "waiting-for-reply"):
                out.append(f"pending target {Path(n_).stem} waits on a dead review request {', '.join(dead_k)}")
            continue
        if "waiting-for-reply" in rs or int(m.get("pending_cycles") or 0) >= MAX_PENDING_CYCLES:
            continue
        if any(review_invalid_count_any(staging, n) == 1 for n in (m.get("notes") or [])):
            continue  # Z23: one unusable review reply: the next pass sends a new request (a wait)
        out.append(f"pending: {p.parent.name} ({', '.join(rs) or 'no reason'}): run the driver again")
    q = _read_json(staging / "queue.json", {}) or {}
    out += [f"failed: {it['id']} ({str(it.get('error') or '')[:80]})" for it in q.get("items", []) if it.get("stage") == "failed"]
    out += [f"waiting: {it['id']} waits on a dead request {', '.join(it['wait_keys'])}" for it in q.get("items", [])
            if it.get("stage") == "waiting-for-reply" and it.get("wait_keys")
            and all(request_dead(staging, k) for k in it["wait_keys"])]  # round 26 (c)
    listed = {str(x.get("note")) for x in (_read_json(staging / "needs_operator.json", []) or [])}
    st = _read_json_snapshot(staging / "repair_state.json", {}) or {}
    marked = {x["note"] for x in marked_unsupported_notes(zk_dir, staging)} if zk_dir is not None else set()
    out += [f"{e.get('status')}: {Path(rel).stem}" for rel, e in (st.get("notes") or {}).items()
            if e.get("status") in ("needs-repair", "done-unsupported") and rel not in listed and rel not in marked]
    # Z-A2 (round 25): a done-unsupported note stays operator work until it is marked
    for rel, e in (st.get("notes") or {}).items():  # Z8 (round 23): one video's "contradicts" ends a bullet
        if e.get("status") in ("operator-closed", "open"):
            continue
        con = [bid for bid, b in (e.get("bullets") or {}).items()
               if not any(a.get("decision") in ("kept", "corrected") for a in (b.get("answers") or {}).values())
               and any(a.get("decision") == "dropped" and "contradict" in str(a.get("reason") or "")
                       for a in (b.get("answers") or {}).values())]
        if con:
            out.append(f"contradicts: {Path(rel).stem} ({', '.join(b.split('#')[-1] for b in con)}): "
                       "read against the other videos (operator-done closes it)")
    if zk_dir is not None:
        import validate  # noqa: PLC0415

        out += [f"validate: {e}" for e in validate.two_state_errors(staging, zk_dir)]
        out += [f"validate: {e}" for e in validate.stub_errors(staging, zk_dir, sorted((zk_dir / PERMANENT_DIR).glob("*.md")))
                if (zk_dir / PERMANENT_DIR).is_dir()]
        out += [f"validate: {b['note']} beside its published replacement" for b in replacement_beside_old(staging, zk_dir)
                if b["note"] not in listed]
        if (st.get("notes") or {}):
            for r in repair_status_rows(staging, zk_dir):
                if r["unlisted"] and r["state"] != "done-unsupported":
                    out.append(f"unlisted: {Path(r['note']).stem} ({r['state']}, unchanged, {r['open_bullets']} open)")

    return out


MARK_KEYS = ("verification", "unsupported_reason", "unsupported_marked")
MARK_REASONS = ("source transcript missing", "no source video states any claim of this note")


_FM_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")


def _fm_and_body(text: str) -> tuple[str | None, str]:
    """(frontmatter lines between an opening `---` line and the first closing `---` line,
    body after the closing line) or (None, text). Z-B2 (round 25): only a line that is
    exactly `---` opens or closes; an LF-only file is expected (see `mark_shape`)."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end == -1 and text.endswith("\n---"):
            end = len(text) - 4
        if end != -1:
            return text[4:end], text[end + 4:]
    return None, text


def _fm_lines_without(fm: str, keys: tuple[str, ...]) -> list[str]:
    return [ln for ln in fm.split("\n") if not re.match(r"^(" + "|".join(keys) + r")\s*:", ln)]


def read_raw(p: Path) -> str:
    """The file text with its line ends unchanged (`read_text` turns CRLF into LF)."""
    return p.read_bytes().decode("utf-8", errors="replace")


def mark_shape(text: str) -> str:
    """Z-B1..Z-B4 (round 25): "" when the harness may mark this note, else the shape that
    blocks the mark. The mark needs: no BOM, LF line ends only, a frontmatter opened and
    closed by a `---` line (no `...` closer), every frontmatter line a top-level
    `key: value` line or an indented continuation of a key that is not a mark key, each
    mark key at most once, with a single-line value, and a frontmatter that parses."""
    if text.startswith("﻿"):
        return "byte order mark"
    if "\r" in text:
        return "CRLF line endings"
    fm, _body = _fm_and_body(text)
    if fm is None:
        return "no frontmatter closed by a --- line"
    lines = fm.split("\n")
    if any(ln.strip() == "..." for ln in lines):
        return "frontmatter closed by ..."
    cur, seen = "", []
    for ln in lines:
        if not ln.strip():
            continue
        m = _FM_KEY.match(ln)
        if m:
            cur = m.group(1)
            seen.append(cur)
            if cur in MARK_KEYS and not m.group(2).strip():
                return f"multi-line {cur} value"
        elif ln[:1] in (" ", "\t", "-"):
            if not cur:
                return "frontmatter does not start with a key"
            if cur in MARK_KEYS:
                return f"multi-line {cur} value"
        else:
            return "frontmatter line that is not a key"
    dup = sorted({k for k in seen if k in MARK_KEYS and seen.count(k) > 1})
    if dup:
        return f"duplicate {dup[0]} key"
    if not verify_claims.split_frontmatter(text)[0]:
        return "frontmatter does not parse"
    return ""


def _mark_fm(text: str) -> dict[str, Any]:
    return verify_claims.split_frontmatter(text)[0]


def is_unsupported_mark(old: str, new: str) -> bool:
    """Round 24: `new` is `old` with ONLY the mark keys set (verification: unsupported,
    unsupported_reason, unsupported_marked): body byte-identical, every other frontmatter
    line unchanged and in order. Round 25: both texts have the mark shape (Z-B1..Z-B4), the
    parsed key sets agree (new = old + the mark keys, every other value equal), and the
    reason is one of the two harness reasons (Z-B7)."""
    if mark_shape(old) or mark_shape(new):
        return False
    fo, bo = _fm_and_body(old)
    fn, bn = _fm_and_body(new)
    if fo is None or fn is None or bo != bn:
        return False
    if _fm_lines_without(fo, MARK_KEYS) != _fm_lines_without(fn, MARK_KEYS):
        return False
    po, pn = _mark_fm(old), _mark_fm(new)
    if set(pn) != set(po) | set(MARK_KEYS) or any(po[k] != pn[k] for k in po if k not in MARK_KEYS):
        return False
    return str(pn.get("verification")).strip() == "unsupported" and pn.get("unsupported_reason") in MARK_REASONS \
        and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(pn.get("unsupported_marked") or "").strip()))


def unsupported_mark_text(old: str, reason: str) -> str | None:
    """The old note with only the three mark keys set; None when its shape blocks the mark
    (`mark_shape`) or the reason is not a harness reason."""
    if reason not in MARK_REASONS or mark_shape(old):
        return None
    fo, bo = _fm_and_body(old)
    if fo is None:
        return None
    lines = _fm_lines_without(fo, MARK_KEYS)
    lines += ["verification: unsupported", f"unsupported_reason: {json.dumps(reason, ensure_ascii=False)}",
              f"unsupported_marked: {_now()[:10]}"]
    return "---\n" + "\n".join(lines) + "\n---" + bo


def mark_unsupported_note(staging: Path, zk_dir: Path, rel: str, reason: str, pub: Any = None) -> bool:
    """Round 24 (the owner's "mark"): the HARNESS marks an old note that no source supports:
    only the frontmatter keys `verification: unsupported`, `unsupported_reason`,
    `unsupported_marked`, through the publisher (archive first, write guard). The body stays
    byte-identical (checked before the write). Anything else: no write, needs_operator.json."""
    p = zk_dir / rel
    if reason not in MARK_REASONS or not p.is_file():
        return False
    old = read_raw(p)  # Z-B3 (round 25): bytes as they are (no newline translation)
    if verify_claims.is_stub(verify_claims.split_frontmatter(old)[0]):
        return False
    shape = mark_shape(old)
    if not shape and re.search(r"(?m)^unsupported_marked:", _fm_and_body(old)[0] or ""):
        return True  # marked already
    new = None if shape else unsupported_mark_text(old, reason)
    if new is None or not is_unsupported_mark(old, new):
        # Z-B1..Z-B4 (round 25): the mark is refused; the old note stays byte-identical
        needs_operator(staging, [{"note": rel, "state": "unsupported", "unsupported_reason": reason}],
                       f"mark refused: {shape or 'the mark would change more than the three keys'}")
        return False
    if pub is None:
        mu = staging / "maintenance" / f"mark-{_now().replace(':', '')}"
        mu.mkdir(parents=True, exist_ok=True)
        pub = _Publisher(staging, mu.name, mu, zk_dir, {})
    if not pub.write(rel, new, why=f"marked unsupported: {reason}"):
        return False  # the guard listed it (user edit)
    provenance.record(staging, "note-marked-unsupported", note=rel, reason=reason)
    if pub.commit_paths and not (pub.unit_dir / "unit.json").exists():
        _git_commit(zk_dir, pub.commit_paths, "zettel: mark unsupported", staging)
    return True


def mark_eligible(e: dict[str, Any]) -> bool:
    """Round 24: a finished plan note with no published target whose EVERY bullet got an
    explicit "not in this transcript" drop from EVERY planned source video (no open,
    unverified, contradicts-dropped or settled bullet)."""
    if e.get("status") in ("open", "operator-closed") or e.get("titles") or not e.get("bullets"):
        return False
    if e.get("kind") != "known":
        return False  # candidate videos of an unknown-source note are not its sources
    planned = list(e.get("sources") or [])
    if not planned:
        return False
    for b in (e.get("bullets") or {}).values():
        ans = b.get("answers") or {}
        for v in planned:
            a = ans.get(v) or {}
            if a.get("decision") != "dropped" or a.get("reason") != "not in this transcript":
                return False
    return True


def unasked_sources(staging: Path, zk_dir: Path, rel: str, e: dict[str, Any]) -> list[str]:
    """Z-B5 (round 25): the RECORDED sources of the note (frontmatter, legacy provenance,
    known_sources.json, and the sources recorded at planning) that were not asked: no
    transcript, so not planned. A note is marked "no source video states any claim" only
    when this list is empty."""
    recorded = list(dict.fromkeys(list(e.get("recorded_sources") or []) + repair_sources(staging, rel, zk_dir)))
    planned = set(e.get("sources") or [])
    return [v for v in recorded if v not in planned]


def mark_or_list(staging: Path, zk_dir: Path, rel: str, e: dict[str, Any], pub: Any = None) -> str:
    """Round 25 (Z-A2, Z-B5): the end of a finished repair whose every bullet every planned
    video dropped ("not in this transcript"). Every recorded source asked: the harness
    marks the note (state 3). A recorded source without a transcript: no mark, the note
    stays unchanged, and needs_operator.json names it ("source <id> has no transcript").
    Returns "marked", "listed", "refused" or "" (not eligible)."""
    p = zk_dir / rel
    if not mark_eligible(e) or not p.is_file() or _sha256(p) != e.get("sha"):
        return ""
    if verify_claims.is_stub(verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))[0]):
        return ""
    missing = unasked_sources(staging, zk_dir, rel, e)
    if missing:
        for v in missing:
            needs_operator(staging, [{"note": rel, "state": "unsupported", "source": v}], f"source {v} has no transcript")
        return "listed"
    return "marked" if mark_unsupported_note(staging, zk_dir, rel, MARK_REASONS[1], pub) else "refused"


def mark_finished(staging: Path, zk_dir: Path) -> list[dict[str, Any]]:
    """Z-A2 (round 25): at the end of every repair pass (single mode), each finished plan
    note that `mark_or_list` accepts is marked or listed; a note that a later pass would
    never finalize again is no longer left unmarked."""
    st = _read_json(staging / "repair_state.json", {}) or {}
    out = []
    for rel, e in sorted((st.get("notes") or {}).items()):
        if e.get("status") != "done-unsupported" or safe_note_rel(zk_dir, rel) is None:
            continue
        res = mark_or_list(staging, zk_dir, rel, e)
        if res:
            out.append({"note": rel, "result": res})
    return out


def marked_unsupported_notes(zk_dir: Path, staging: Path) -> list[dict[str, Any]]:
    """Round 24: the vault notes that the harness marked unsupported (summary key)."""
    out = []
    for r in provenance.read_records(staging):
        if r.get("event") == "note-marked-unsupported":
            p = zk_dir / str(r.get("note"))
            if p.is_file() and re.search(r"(?m)^unsupported_marked:", p.read_text(encoding="utf-8", errors="replace")[:4000]):
                if all(x["note"] != r["note"] for x in out):
                    out.append({"note": r["note"], "reason": r.get("reason")})
    return out


def operator_done(staging: Path, note: str, reason: str) -> dict[str, Any]:
    """Z15 (round 23): the operator's decision on a note: it is recorded
    (`operator_decisions.jsonl`, provenance `operator-done`), every needs_operator.json entry
    of the note is removed, and a repair-plan note is closed (`operator-closed`: never planned
    again, never operator work again unless its file changes)."""
    rel = note if note.endswith(".md") else f"{PERMANENT_DIR}/{note}.md"
    reg = _read_json(staging / "needs_operator.json", []) or []
    # Z-C2 (round 25): an entry matches by its note (path, title, or the name of an
    # `exchange request <key>` entry) or by its request key
    key = note.strip().removeprefix("exchange request ").strip()
    keep = [x for x in reg if str(x.get("note")) not in (rel, note) and not (key and str(x.get("request") or "") == key)
            and str(x.get("note")) != f"exchange request {key}"]
    sp = staging / "repair_state.json"
    st = _read_json(sp, None)
    # round 26 (b): a pending folder that waits only for a dead review request matches by its
    # waiting note, by an old note of its repair map, or by the request key: its waiting
    # notes are quarantined `review-invalid` at the next pass (OPERATOR_DEAD marker)
    dead_dirs = []
    for pdir in sorted((staging / "pending-review").glob("*--*")):
        dk = folder_dead(staging, pdir)
        if not dk:
            continue
        m = _read_json(pdir / "meta.json", {}) or {}
        waiting_n = sorted(r_ for r_, why in (m.get("reasons") or {}).items() if why == "waiting-for-reply")
        wstems = {Path(x).stem for x in waiting_n}
        olds = {o for o, m_ in (_read_json(pdir / "repair_map.json", {}) or {}).items()
                if wstems & set((m_ or {}).get("targets") or [])}  # only an old note of a waiting target
        if rel in waiting_n or rel in olds or key in dk:
            dead_dirs.append((pdir, waiting_n, dk, rel in olds))
    # round 26 (c): a queue item that waits only for a dead request matches by its id or the key
    q_hit: list[str] = []
    qp = staging / "queue.json"
    if qp.is_file():
        with state_lock(staging):
            q = _read_json(qp, {}) or {}
            for it in q.get("items", []):
                wk = it.get("wait_keys") or []
                if it.get("stage") == "waiting-for-reply" and wk and all(request_dead(staging, k) for k in wk) \
                        and (note.strip() == str(it.get("id")) or key in wk):
                    it["stage"] = "failed"
                    it["error"] = f"request {', '.join(wk)} refused for good; operator-done: {reason[:120]}"
                    q_hit.append(str(it["id"]))
            if q_hit:
                _write_json(qp, q)
    dead_notes = {n_ for _d, ws, _k, _o in dead_dirs for n_ in ws}
    dead_keys = {k for _d, _w, ks, _o in dead_dirs for k in ks}
    keep = [x for x in keep if str(x.get("note")) not in dead_notes and str(x.get("request") or "") not in dead_keys]
    via_old = any(o for _d, _w, _k, o in dead_dirs)
    if len(keep) == len(reg) and not dead_dirs and not q_hit and not (st and rel in (st.get("notes") or {})):
        return {"note": rel, "entries_removed": 0, "plan_closed": False}  # nothing matches: no record
    for pdir, ws, dk, _o in dead_dirs:
        _write_json(pdir / OPERATOR_DEAD, {"notes": ws, "keys": dk, "reason": reason, "at": _now()})
    _write_json(staging / "needs_operator.json", keep)
    closed = False
    if st and rel in (st.get("notes") or {}) and not via_old:
        # round 26 (b): the old note of a dead folder stays in the plan: the quarantine at the
        # next pass reopens its bullets (`open: target rejected`), and its status follows
        e = st["notes"][rel]
        e["status_before_operator"] = e.get("status")
        e["status"] = "operator-closed"
        e["operator_reason"] = reason
        _write_json(sp, st)
        closed = True
    res = {"note": rel, "entries_removed": len(reg) - len(keep), "plan_closed": closed,
           "pending_quarantined_next_pass": [str(d.name) for d, _w, _k, _o in dead_dirs], "queue_failed": q_hit}
    _append(staging / "operator_decisions.jsonl", dict(res, reason=reason, at=_now()))
    provenance.record(staging, "operator-done", note=rel, reason=reason)
    return res


def refresh_needs_repair(staging: Path) -> list[dict[str, Any]]:
    """Pilot 8: the quarantined versions of the named targets of each `needs-repair` entry
    (the entry is written when the rejection is decided, before the quarantine copy exists)."""
    reg = _read_json(staging / "needs_operator.json", []) or []
    changed = False
    for it in reg:
        if it.get("state") == "needs-repair" and it.get("named_targets"):
            q = sorted(str(p.relative_to(staging)) for x in it["named_targets"]
                       for p in staging.glob(f"quarantine/**/{PERMANENT_DIR}/{x}.md"))
            if q != it.get("quarantined_targets"):
                it["quarantined_targets"], changed = q, True
    if changed:
        _write_json(staging / "needs_operator.json", reg)
    return [it for it in reg if it.get("state") == "needs-repair"]


def list_beside(staging: Path, zk_dir: Path, unit_dir: Path | None = None) -> list[dict[str, Any]]:
    """Round 22 (X1): `heal_beside` is removed. An old note left beside a published
    replacement (a kill between the two writes, a refused stub, a user edit) is never written
    here: it is listed in needs_operator.json, and `validate.py --staging` reports it as an
    ERROR. The next normal finalize writes the proper stub through the publisher (guard and
    stub check) when the guard allows."""
    out = replacement_beside_old(staging, zk_dir, unit_dir)
    needs_operator(staging, out, "old note beside its published replacement")
    return out


def replacement_beside_old(staging: Path, zk_dir: Path, unit_dir: Path | None = None) -> list[dict[str, Any]]:
    """Round 11 (D) invariant: an old note with a published replacement is a stub. Sources:
    the unit's repair_map.json and every `note-repair` provenance record."""
    pairs: dict[str, set[str]] = {}
    if unit_dir is not None:
        for rel, m in (_read_json(unit_dir / "repair_map.json", {}) or {}).items():
            pairs.setdefault(rel, set()).update(m.get("targets") or [])
    for r in provenance.read_records(staging):
        if r.get("event") == "note-repair" and r.get("new"):
            pairs.setdefault(str(r.get("note")), set()).update(r["new"])
    # W14 (round 21): a target that a fix call renamed is followed to its final name
    ren = _rename_map(staging)
    for rel, targets in pairs.items():
        for t0 in list(targets):
            x, seen = t0, set()
            while x in ren and x not in seen:
                seen.add(x)
                x = ren[x]
            targets.add(x)
    out = []
    for rel, targets in pairs.items():
        p = zk_dir / rel
        if not p.is_file():
            continue
        fm, _, _ = verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        if verify_claims.is_stub(fm):
            continue
        pubd = [t for t in targets if t != Path(rel).stem and _published_contract(zk_dir, f"{PERMANENT_DIR}/{t}.md")]
        if pubd:
            out.append({"note": rel, "published_replacements": sorted(pubd)})
    return out


def update_partial_stubs(staging: Path, zk_dir: Path, pub: Any) -> list[dict[str, Any]]:
    """Round 11 (D): a stub with open parts names each target as soon as it is published;
    the entry ends when nothing is open."""
    reg = _read_json(staging / PARTIAL, {}) or {}
    done = []
    for rel, e in list(reg.items()):
        now = [t for t in e.get("pending_targets", []) if _published_contract(zk_dir, f"{PERMANENT_DIR}/{t}.md")]
        if not now and not e.get("closed"):
            continue
        e["published"] = sorted(set(e.get("published", [])) | set(now))
        e["pending_targets"] = [t for t in e.get("pending_targets", []) if t not in now]
        if e.get("closed"):
            e["open_bullets"] = []
        pending = [f"note: {t}" for t in e["pending_targets"]] + [f"bullet: {b}" for b in e.get("open_bullets", [])]
        pub.write(rel, repair_stub(Path(rel).stem, e["published"], pending), why="partial stub updated")
        done.append({"note": rel, "published": e["published"], "pending": pending})
        if not pending:
            del reg[rel]
        else:
            reg[rel] = e
    _write_json(staging / PARTIAL, reg)
    return done



def validate_linked_existing(staging: Path, unit_dir: Path, zk_dir: Path) -> list[dict[str, Any]]:
    """Under the vault lock, recheck every external target before its old-note stub is written."""
    path = unit_dir / "repair_map.json"
    repair_map = _read_json(path, {}) or {}
    rows = [(rel, m, item) for rel, m in repair_map.items()
            for item in (m.get("linked_existing") or [])]
    if not rows:
        return []
    context = _read_json(unit_dir / "repair_context.json", {}) or {}
    vid = str(context.get("video") or "")
    allow = context.get("video_notes") or {}
    state_path = staging / "repair_state.json"
    state = _read_json(state_path, {}) or {}
    invalid: list[dict[str, Any]] = []
    for rel, entry, item in rows:
        title = str(item.get("title") or "")
        target_rel = f"{PERMANENT_DIR}/{title}.md"
        target = zk_dir / target_rel
        rec = last_published(staging, target_rel)
        owners = allow.get(title) or []
        valid = (bool(vid) and item.get("video") == vid and title in allow
                 and owners and owners == item.get("owners")
                 and item.get("sha256") and target.is_file()
                 and _published_contract(zk_dir, target_rel)
                 and _sha256(target) == item.get("sha256")
                 and rec is not None and rec.get("verification") == "source-checked"
                 and vid in (rec.get("sources") or []) and rec.get("sha256") == item.get("sha256")
                 and any(isinstance(c, list) and c and c[0] == vid for c in (item.get("cited") or [])))
        valid = valid and any(isinstance(cl, list) and len(cl) > 1 and cl[0] == title
                              and str(cl[1]).strip() for cl in (item.get("claim_lines") or []))
        if not valid:
            invalid.append(item)
    if not invalid:
        return []
    invalid_bundles = {(str(i.get("old")), str(i.get("bullet")), str(i.get("video") or vid))
                       for i in invalid}
    # One stale target invalidates every external target named by that bullet in this unit.
    invalid = [item for rel, entry, item in rows
               if (str(item.get("old")), str(item.get("bullet")),
                   str(item.get("video") or vid)) in invalid_bundles]
    invalid_keys = {(str(i.get("old")), str(i.get("bullet")), str(i.get("title"))) for i in invalid}
    invalid_titles_by_bundle: dict[tuple[str, str, str], set[str]] = {}
    for item in invalid:
        key = (str(item.get("old")), str(item.get("bullet")), str(item.get("video") or vid))
        invalid_titles_by_bundle.setdefault(key, set()).add(str(item.get("title") or ""))

    changed_rels: set[str] = set()
    for rel, entry in repair_map.items():
        linked = entry.get("linked_existing") or []
        entry["linked_existing"] = [
            x for x in linked
            if (str(x.get("old")), str(x.get("bullet")), str(x.get("title"))) not in invalid_keys
        ]
        ordinary = set(entry.get("ordinary_targets") or [])
        valid_titles = {str(x.get("title")) for x in entry["linked_existing"] if x.get("title")}
        entry["targets"] = sorted(ordinary | valid_titles)
        st_entry = (state.get("notes") or {}).get(rel)
        if not st_entry:
            continue
        for bundle, invalid_titles in invalid_titles_by_bundle.items():
            old, bid, bundle_vid = bundle
            if old != rel:
                continue
            answer = (((st_entry.get("bullets") or {}).get(bid) or {}).get("answers") or {}).get(bundle_vid)
            if not answer or not (set(answer.get("linked_existing") or []) & invalid_titles):
                continue
            before = dict(answer)
            answer.update(decision="unverified", was=before,
                          why="a required existing target changed before the locked stub commit")
            answer.pop("linked_existing", None)
            if "titles" in answer:
                answer["titles"] = [t for t in answer.get("titles") or [] if t not in invalid_titles]
            if answer.get("title") in invalid_titles:
                answer.pop("title", None)
            changed_rels.add(rel)
        linked_now = {
            title
            for bullet in (st_entry.get("bullets") or {}).values()
            for answer in (bullet.get("answers") or {}).values()
            for title in (answer.get("linked_existing") or [])
        }
        st_entry["linked_titles"] = sorted(linked_now)
        if rel in changed_rels:
            classes = classify_bullets(st_entry)
            st_entry["unverified_bullets"] = classes["unverified"]
            st_entry["rejected_bullets"] = classes["rejected"]
            st_entry["damaged_bullets"] = classes["damaged"]
            st_entry["open_bullets"] = classes["open"] + classes["unverified"] + classes["damaged"] + classes["rejected"]
            st_entry["unsupported_bullets"] = classes["unsupported"]
            if not st_entry.get("titles") and not linked_now:
                st_entry["status"] = "needs-repair"
                needs_operator(staging, [needs_repair_item(staging, rel, st_entry, st_entry["open_bullets"])],
                               "needs-repair: linked target changed before stub commit")
    for rel, entry in repair_map.items():
        if entry.get("linked_existing") == []:
            entry.pop("linked_existing", None)
    _write_json(path, repair_map)
    _write_json(state_path, state)
    claims_path = unit_dir / "claims.json"
    claims = _read_json(claims_path, {}) or {}
    claims.setdefault("problems", []).append({"type": "linked-existing-stale",
                                               "targets": sorted({x["title"] for x in invalid})})
    _write_json(claims_path, claims)
    return invalid

def materialize_repair_stubs(unit_dir: Path, zk_dir: Path) -> list[dict[str, Any]]:
    """Round 10 (P2): the stub of a repaired old note is written at finalize from the FINAL
    state of the unit: a fix call that renamed a new note moves the stub target with it; a
    dropped new note leaves the stub; no stub when no target is left (the old note stays as
    it is) or when bullets of the note still go on to a later source video. A target may also
    be a note that an earlier repair unit published (it is in the vault)."""
    rmap = _read_json(unit_dir / "repair_map.json", {}) or {}
    marked = _read_json(unit_dir / "marked.json", {}) or {}
    renamed, deleted = marked.get("renamed") or {}, set(marked.get("deleted") or [])
    out = []
    for rel, m in sorted(rmap.items()):
        if safe_note_rel(zk_dir, rel) is None:
            out.append({"note": rel, "stub": False, "why": "unsafe note path (absolute or outside the notes folder)"})
            continue
        final: list[str] = []
        vault_old = zk_dir / rel
        earlier: list[str] = []
        if vault_old.is_file():  # R7: a newer stub's published targets stay
            vfm, _, _ = verify_claims.split_frontmatter(vault_old.read_text(encoding="utf-8", errors="replace"))
            if verify_claims.is_stub(vfm):
                earlier = [Path(x).stem for x in _stub_targets(vfm) if _published_contract(zk_dir, x)]
        for t0 in list(m.get("targets") or []) + earlier:
            t1 = t0
            for _ in range(10):  # a chain of renames
                if t1 not in renamed:
                    break
                t1 = renamed[t1]
            if t1 in deleted:
                continue
            if (unit_dir / "out" / PERMANENT_DIR / f"{t1}.md").is_file() or (zk_dir / PERMANENT_DIR / f"{t1}.md").is_file():
                if t1 not in final:
                    final.append(t1)
        old = Path(rel).stem
        p = unit_dir / "out" / rel
        final = [x for x in final if x != old]  # X2 (round 22): no in-place repair (refused upstream)
        if not final:
            out.append({"note": rel, "stub": False, "why": "no target note is left"})
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        pending = [] if m.get("final", True) else ["bullets with a later source video"]
        p.write_text(repair_stub(old, final, pending), encoding="utf-8")
        out.append({"note": rel, "stub": True, "targets": final, "pending": pending, "by": "harness"})
    if rmap:
        _write_json(unit_dir / "repair_stubs.json", out)
    return out


def finalize(staging: Path, run_dir: Path, unit_dir: Path, ids: list[str], agent_rc: int,
             zk_dir: Path, review_note: str | None = None, commit_message: str | None = None,
             recovered: bool = False) -> dict[str, Any]:
    with _archive_candidate_scope(staging), _rename_index_scope(staging):
        return _finalize_impl(staging, run_dir, unit_dir, ids, agent_rc, zk_dir,
                              review_note, commit_message, recovered)


def _finalize_impl(staging: Path, run_dir: Path, unit_dir: Path, ids: list[str], agent_rc: int,
             zk_dir: Path, review_note: str | None = None, commit_message: str | None = None,
             recovered: bool = False) -> dict[str, Any]:
    t_finalize = _dt.datetime.now().timestamp()
    meta = _read_json(run_dir / "run.json", {"run_id": run_dir.name, "settings": {}})
    run_id = meta.get("run_id", run_dir.name)
    info = _read_json(unit_dir / "unit.json", {})
    kind = str(info.get("kind") or ("review" if review_note else "synth"))
    is_pending = bool(info.get("pending")) or kind == "pending"
    ids = ids or list(info.get("ids") or [])
    review_note = review_note or info.get("review_note")
    end, detail, transient = classify_end(unit_dir, agent_rc)
    agent_res = _read_json(unit_dir / "agent_result.json", {})
    settings = dict(meta.get("settings") or {})
    settings["models_served"] = agent_res.get("models_served", []) or _unit_models(unit_dir)
    if (unit_dir / "prompt.txt").exists():
        settings["prompt_text_sha256"] = _sha256(unit_dir / "prompt.txt")

    notes: list[dict[str, Any]] = []
    removals_done: list[dict[str, Any]] = []
    links_done: list[dict[str, Any]] = []
    review_info: dict[str, Any] | None = None
    pending_dir: str | None = None
    commit: dict[str, Any] = {}
    with vault_lock(staging, zk_dir):
        if (unit_dir / "finalized.json").exists():
            raise HarnessError(f"unit {unit_dir.name} is already finalized")
        pub = _Publisher(staging, run_id, unit_dir, zk_dir, settings)
        if recovered:
            notes, removals_done = _finalize_recovered(pub, unit_dir)
            # W16 (round 21): the recovered path follows renames too (scoped to this unit)
            rename_in_state(staging, (_read_json(unit_dir / "marked.json", {}) or {}).get("renamed") or {}, unit_dir)
            # V27: a note that the crashed finalize published keeps no link to a sibling of
            # the unit that never reached the vault (plain text, listed in unlinked.json)
            outs = {p.stem for p in (unit_dir / "out").rglob("*.md")} if (unit_dir / "out").is_dir() else set()
            gone_r = {x for x in outs if not any(zk_dir.rglob(f"{x}.md"))}
            if gone_r:
                pubd = [n["note"] for n in notes if n.get("action") == "published" and (zk_dir / n["note"]).is_file()]
                fake = [{"rel": rel_, "data": (zk_dir / rel_).read_bytes(), "entry": {}, "existed_before": True,
                         "stub": False} for rel_ in pubd]
                sib = [{"rel": f"{PERMANENT_DIR}/{x}.md", "data": b"", "entry": {}, "existed_before": False,
                        "stub": False} for x in gone_r]
                dec = {r_["rel"]: "publish" for r_ in fake} | {r_["rel"]: "quarantine" for r_ in sib}
                unlink_waiting_siblings(staging, fake + sib, dec, zk_dir)
                for r_ in fake:
                    if r_["data"] != (zk_dir / r_["rel"]).read_bytes():
                        pub.write(r_["rel"], r_["data"].decode("utf-8", errors="replace"),
                                  why="recovery: link to an unpublished sibling made plain text")
            # R6: a crashed unit's quarantined targets reopen their bullets too
            q_titles = {Path(n["note"]).stem for n in notes if n.get("action") == "quarantined"}
            if q_titles and (unit_dir / "repair_map.json").is_file():
                reopen_rejected_bullets(staging, unit_dir, q_titles)
        else:
            if kind == "repair":
                validate_linked_existing(staging, unit_dir, zk_dir)
            materialize_repair_stubs(unit_dir, zk_dir)
            ck = _Checker(staging, unit_dir, zk_dir, kind, _queue_kinds(staging, ids))
            results = ck.check_all()
            decisions: dict[str, str] = {}
            pending_cycles = int(info.get("pending_cycles") or 0)
            drafts = _drafts(unit_dir, zk_dir, results)
            review_repaired = set((_read_json(unit_dir / "review-repair.json", {}) or {}).get("notes") or {})
            # P7: notes whose fix call the budget stopped wait (pending, never quarantined).
            fp_ = _read_json(unit_dir / "fix-pending.json", {}) or {}
            budget_wait = set(fp_.get("notes") or {})
            wait_reason = str(fp_.get("reason") or "budget-stop")
            repair_ok_targets = _repair_written_targets(staging, zk_dir) if kind == "repair" else set()
            plan_notes = set(((_read_json_snapshot(staging / "repair_state.json", {}) or {}).get("notes") or {})) \
                if kind == "repair" else set()
            od_ = _read_json(unit_dir / OPERATOR_DEAD, {}) or {}
            dead_notes = {str(n_): f"review request {', '.join(od_.get('keys') or [])} refused for good; "
                                   f"operator-done: {str(od_.get('reason') or '')[:120]}" for n_ in (od_.get("notes") or [])}
            for r in results:
                rel = r["rel"]
                if kind == "repair" and not r["stub"] and (zk_dir / rel).is_file() \
                        and (rel in plan_notes or (r["existed_before"] and rel not in repair_ok_targets)):
                    # Z1 (round 23), Z-A1 (round 25): in EVERY mode, repair never writes a
                    # non-stub over a repair-plan note, or over a vault note that existed before
                    # this unit (also one that an earlier synthesis run published). The only
                    # exception: a target that an earlier unit of THIS repair wrote (R5 extend).
                    r["ok"] = False
                    r["failures"] = r["failures"] + [{"type": "title-equals-old", "where": rel,
                                                      "detail": "a repair note may not replace an old note in place"}]
                entry = {"note": rel, "existed_before": r["existed_before"], "status": r["status"],
                         "failure_types": sorted({f["type"] for f in r["failures"]})}
                if r["report"].get("details") is not None:
                    entry["bullets"] = [{"n": d.get("n"), "verdict": d["verdict"],
                                         "failures": [f["type"] for f in d["failures"]]}
                                        for d in r["report"]["details"]]
                r["entry"] = entry
                if rel in dead_notes:
                    # round 26 (b): the operator closed a review request refused for good:
                    # the note is quarantined `review-invalid`; its old bullets open again
                    decisions[rel] = "quarantine"
                    r["failures"] = r["failures"] + [{"type": "review-invalid", "where": "source-check",
                                                      "detail": dead_notes[rel]}]
                    entry["failure_types"] = sorted({f["type"] for f in r["failures"]})
                    entry["for_operator"] = True
                elif rel in drafts:
                    decisions[rel] = "draft"  # left behind by the agent: reported, not judged
                elif not r["ok"] and {f["type"] for f in r["failures"]} <= WAIT_TYPES:
                    decisions[rel] = "pending"
                    entry["pending_reason"] = "link-waiting"
                elif not r["ok"] and rel in budget_wait:
                    decisions[rel] = "pending"
                    entry["pending_reason"] = wait_reason
                elif not r["ok"]:
                    decisions[rel] = "quarantine"
                elif r["stub"]:
                    decisions[rel] = "stub"
                elif ck.needs_review(rel, r["fm"]):
                    v = verdict_for(unit_dir, rel, r["data"])
                    entry["review"] = v
                    if v is not None and v.get("unusable") and review_invalid_count(staging, rel, r["data"]) >= REVIEW_INVALID_MAX:
                        # Pilot 9 (round 22): the second unusable review reply of the same text:
                        # quarantined (`review-invalid`); its old notes follow the normal rule
                        decisions[rel] = "quarantine"
                        r["failures"] = r["failures"] + [{"type": "review-invalid", "where": "source-check",
                                                          "detail": str(v.get("unusable"))[:300]}]
                        entry["for_operator"] = True
                    elif v is None or v.get("unusable"):
                        # No usable review (timeout, invalid reply, no call): the note waits.
                        if v is not None:
                            entry["review_unusable"] = v["unusable"]
                        entry["pending_reason"] = (v or {}).get("code") or "review-missing"
                        decisions[rel] = "pending"
                    elif v["bad"] and rel in budget_wait:
                        decisions[rel] = "pending"
                        entry["pending_reason"] = wait_reason
                    elif v["bad"]:
                        decisions[rel] = "quarantine"
                        r["failures"] = r["failures"] + [
                            {"type": f"review:{b}", "where": "source-check", "detail": v["path"]} for b in v["bad"]]
                        if rel in review_repaired:
                            # Rejected again after its targeted repair turn: quarantined for good.
                            r["failures"].append({"type": "review-rejected-twice", "where": "source-check",
                                                  "detail": "rejected by the review, rewritten in a repair turn, "
                                                            "and rejected again"})
                            entry["for_operator"] = True
                        entry["failure_types"] = sorted({f["type"] for f in r["failures"]})
                    else:
                        decisions[rel] = "publish-reviewed"
                else:
                    decisions[rel] = "publish"
            for r in results:  # V7: a rewrite of a published repair note keeps its bullets and Evidence
                if decisions.get(r["rel"]) in ("publish", "publish-reviewed", "pending") and not r["stub"]:
                    lost = extension_loss(staging, r["rel"], r["data"].decode("utf-8", errors="replace"))
                    if lost:
                        decisions[r["rel"]] = "quarantine"
                        r["failures"] = r["failures"] + [lost]
                        r["entry"]["failure_types"] = sorted({f["type"] for f in r["failures"]})
            rename_in_state(staging, (_read_json(unit_dir / "marked.json", {}) or {}).get("renamed") or {}, unit_dir)
            rmap_all = _read_json(unit_dir / "repair_map.json", {}) or {}
            mk_ = _read_json(unit_dir / "marked.json", {}) or {}
            rejected_t = {Path(r["rel"]).stem for r in results if decisions.get(r["rel"]) == "quarantine"} \
                | set(mk_.get("deleted") or [])  # R6: a target that a fix call dropped
            for old_t, new_t in (mk_.get("renamed") or {}).items():  # R6: renamed, then rejected
                if new_t in rejected_t:
                    rejected_t.add(old_t)
            reopened = reopen_rejected_bullets(staging, unit_dir, rejected_t) if rmap_all and rejected_t else {}
            uncited = reopen_uncited_bullets(staging, zk_dir, {
                Path(r["rel"]).stem: r["data"].decode("utf-8", errors="replace") for r in results
                if decisions.get(r["rel"]) in ("publish", "publish-reviewed", "pending") and not r["stub"]})
            for rel_u, ids_u in uncited.items():
                reopened.setdefault(rel_u, [])
                reopened[rel_u] = sorted(set(reopened[rel_u]) | set(ids_u))
            partial_new: dict[str, Any] = {}
            full_stub: set[str] = set()
            okd = ("publish", "publish-reviewed")
            if kind == "repair" and (info.get("options") or {}).get("synth_mode") == "single":
                # R10: in a single-mode repair unit only the harness writes stubs.
                harness_stubs = {x["note"] for x in (_read_json(unit_dir / "repair_stubs.json", []) or [])
                                 if x.get("stub")}
                for r in results:
                    if decisions[r["rel"]] == "stub" and r["rel"] not in harness_stubs:
                        decisions[r["rel"]] = "quarantine"
                        r["failures"] = r["failures"] + [{"type": "model-written-stub", "where": r["rel"],
                                                          "detail": "a stub that the harness did not write"}]
                        r["entry"]["failure_types"] = sorted({f["type"] for f in r["failures"]})
            # Stubs follow their target (a transaction per rename group).
            for r in results:
                if decisions[r["rel"]] != "stub":
                    continue
                tgts = _stub_targets(r["fm"]) or [""]
                # A target that an earlier unit published is in the vault (round 10, P2).
                tdecs = [decisions.get(t) or ("publish" if t not in decisions and _published_contract(zk_dir, t)
                                              else None) for t in tgts]
                partial_open = list((r["fm"].get("superseded_pending") or []))
                if all(t in okd for t in tdecs) and not partial_open:
                    decisions[r["rel"]] = "publish-stub"  # ALL targets publish in this finalize
                    full_stub.add(r["rel"])
                elif any(t in okd for t in tdecs) and r["rel"] in rmap_all:
                    # Round 11 (D): an old note never stays beside a published replacement.
                    pub_t = [Path(t).stem for t, d in zip(tgts, tdecs) if d in okd]
                    open_t = [Path(t).stem for t, d in zip(tgts, tdecs) if d == "pending"]  # R14: waiting targets only
                    pending_l = [f"note: {t}" for t in open_t] + [f"bullet: {b}" for b in reopened.get(r["rel"], [])] \
                        + [x for x in partial_open if not str(x).startswith(("note:", "bullet:"))]
                    r["data"] = repair_stub(Path(r["rel"]).stem, pub_t, pending_l).encode("utf-8")
                    decisions[r["rel"]] = "publish-stub"
                    r["entry"]["partial"] = {"published": pub_t, "pending": pending_l}
                    partial_new[r["rel"]] = {"published": pub_t, "pending_targets": open_t,
                                             "open_bullets": [x for x in pending_l if not x.startswith("note:")],
                                             "unit": unit_dir.name}
                elif all(t in ("publish", "publish-reviewed", "pending") for t in tdecs):
                    decisions[r["rel"]] = "pending"
                else:
                    bad = [t for t, d in zip(tgts, tdecs) if d not in ("publish", "publish-reviewed")]
                    decisions[r["rel"]] = "quarantine"
                    r["failures"] = r["failures"] + [{"type": "stub-target-not-published", "where": r["rel"],
                                                      "detail": f"superseded_by target {bad} is not published in this unit"}]
                    r["entry"]["failure_types"] = sorted({f["type"] for f in r["failures"]})
            # X2 (round 22): no in-place repair any more, so no R2 (in place and split) handling.
            merge_duplicates(staging, zk_dir, unit_dir, results, decisions, kind)  # round 18: narrowed
            unlink_waiting_siblings(staging, results, decisions, zk_dir)  # round 18: no restore, unlinked.json
            pending: list[dict[str, Any]] = []
            for r in sorted(results, key=lambda x: decisions[x["rel"]] == "publish-stub"):
                rel, d, entry = r["rel"], decisions[r["rel"]], r["entry"]
                text = r["data"].decode("utf-8", errors="replace")
                if r.get("drop_links") and d in ("publish", "publish-reviewed"):
                    text = _ensure_a_link(_drop_link_lines(text, r["drop_links"]), staging, zk_dir)
                    entry["dead_links_dropped"] = r["drop_links"]
                    entry["links_dropped"] = r["drop_links"]
                if d in ("publish", "publish-reviewed", "publish-stub"):
                    mark = "source-checked" if d == "publish-reviewed" else (
                        "quote-checked" if r["status"] == "quote-checked" and not ck.video else None)
                    if mark:
                        text = verify_claims.set_verification_text(text, mark)
                    if not pub.write(rel, text, why=f"publish ({d})"):
                        # X15 (round 22): a refused write is never recorded as published
                        entry["action"] = "refused"
                        entry["refused"] = (pub.refused[-1] if pub.refused else {}).get("why", "refused")
                        notes.append(entry)
                        continue
                    provenance.record(staging, "note-published", run_id=run_id, note=rel,
                                      sources=verify_claims._source_ids(r["fm"]), unit_ids=ids,
                                      unit=unit_dir.name, verification=mark or r["status"],
                                      created=not r["existed_before"], unchanged=pub.unchanged,
                                      sha256=_sha256(zk_dir / rel), agent_end=end, settings=settings,
                                      **_note_models(unit_dir, rel), **_bullet_record(staging, rel, text))
                    entry["action"] = "published"
                    entry["verification"] = mark or r["status"]
                elif d == "pending":
                    pending.append(r)
                    entry["action"] = "pending-review"
                    entry.setdefault("pending_reason", "stub-target-pending" if r["stub"] else "review-missing")
                elif d == "merged":
                    entry["action"] = "merged"
                    entry["merged_into"] = r.get("merged_into")
                    dst = staging / "merged" / run_id / unit_dir.name / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(r["data"])
                    entry["merged_path"] = str(dst)
                elif d == "draft":
                    entry["action"] = "discarded-draft"
                    dst = staging / "discarded" / run_id / unit_dir.name / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(r["data"])
                    entry["discarded_path"] = str(dst)
                else:
                    entry["action"] = "quarantined"
                    entry["quarantine_path"] = pub.quarantine(rel, r["data"], r["failures"], "not-published")
                    provenance.record(staging, "note-quarantined", run_id=run_id, note=rel, unit_ids=ids,
                                      unit=unit_dir.name, failure_types=entry["failure_types"],
                                      agent_end=end, settings=settings)
                notes.append(entry)
            for d in (_read_json(unit_dir / "discarded.json", {}) or {}).get("notes") or []:
                notes.append({"note": d["note"], "existed_before": False, "status": "draft",
                              "failure_types": [], "action": "discarded-draft", "discarded_path": d["path"],
                              "discarded_before": "fix turn"})
            removals_done = _apply_removals(pub, unit_dir, ck)
            if pending:
                waiting_only = all((r.get("entry") or {}).get("pending_reason") == "waiting-for-reply" for r in pending)
                step = 0 if waiting_only else 1  # S4: waiting for a worker reply is not a cycle
                files_be = os.environ.get("ZR_LLM_BACKEND", "openrouter").strip() == "files"
                # Z-C3 (round 25): a review reply that this unit consumed and refused (invalid
                # or unusable) is a model attempt too: it counts a cycle, so a reviewer that
                # keeps answering badly reaches the cycle limit (exit 6)
                bad_reply = files_be and any(next(d.glob("reply-*.json"), None) for d in unit_dir.glob("review-*")) and not any(
                    (_read_json(d / "agent_result.json", {}) or {}).get("end") == "finished" for d in unit_dir.glob("review-*"))
                if step and files_be:
                    # pilot 8 (round 21): with the files backend a cycle is a model attempt: it
                    # counts only when a review verdict was consumed in this unit (the fix
                    # attempts keep their own maximum, fix_calls.json)
                    consumed = any((_read_json(d / "agent_result.json", {}) or {}).get("end") == "finished"
                                   for d in unit_dir.glob("review-*"))
                    step = 1 if consumed else 0
                if bad_reply:
                    step = 1
                pending_dir = _store_pending(staging, unit_dir, run_id, ids, kind, pending, ck, pending_cycles + step,
                                             review_note=review_note)
                for n in notes:
                    if n.get("action") == "pending-review":
                        n["pending_cycles"] = pending_cycles + step
            if review_note and kind == "review":
                review_info = _review_assigned(pub, ck, staging, unit_dir, review_note)
            links_done = _apply_links(pub, ck, staging, unit_dir.name)
            if partial_new or full_stub:
                reg = _read_json(staging / PARTIAL, {}) or {}
                for rel_ in full_stub:
                    reg.pop(rel_, None)  # the complete stub replaced the partial one
                reg.update(partial_new)
                _write_json(staging / PARTIAL, reg)
        relinked: list[dict[str, Any]] = []  # round 18 (C): links are not restored automatically
        partial_done = update_partial_stubs(staging, zk_dir, pub)
        recomputed = recompute_repair_stubs(staging, zk_dir, pub)  # round 15 (item 2)
        healed = []  # round 18 (B): no heal in a normal finalize; an old note beside its replacement
        needs_operator(staging, replacement_beside_old(staging, zk_dir, unit_dir), "old note beside a published replacement")
        beside = replacement_beside_old(staging, zk_dir, unit_dir)
        if pub.commit_paths:
            commit = _git_commit(zk_dir, pub.commit_paths, commit_message or "", staging) if commit_message else \
                {"ok": None, "paths": pub.commit_paths}
        published = [n["note"] for n in notes if n.get("action") == "published"]
        quarantined = [n["note"] for n in notes if n.get("action") == "quarantined"]
        if end == "limit" and "max_tokens" in (agent_res.get("detail") or detail) and not any(
                n.get("action") in ("published", "pending-review") for n in notes) \
                and not any(p.suffix == ".md" for p in (unit_dir / "out").rglob("*")):
            end = "limit-no-output"
            detail = (f"the agent hit the token limit and wrote no note; raise DEEPSEEK_MAX_TOKENS "
                      f"(now {os.environ.get('DEEPSEEK_MAX_TOKENS', settings.get('max_tokens', '?'))}). {detail}")
        pending_notes = [n["note"] for n in notes if n.get("action") == "pending-review"]
        rolled = index_rollback(staging, zk_dir, [n["note"] for n in notes
                                                  if n.get("action") in ("quarantined", "discarded-draft")])
        if (info.get("options") or {}).get("synth_mode") == "single":
            # Single mode: no model runs index_add.py; the harness indexes what it published.
            index_publish(staging, zk_dir, [n["note"] for n in notes if n.get("action") == "published"])
        _write_json(unit_dir / "finalized.json", {"at": _now(), "end": end, "published": published,
                                                   "quarantined": quarantined, "pending": pending_notes})

    queue_changes = []
    missing = [] if recovered or is_pending else missing_listed(unit_dir, zk_dir)
    if missing and end == "finished":
        # The agent listed a note that it never wrote: the unit is not complete.
        end, detail = "incomplete-output", f"queue_mark --notes lists notes that do not exist: {missing}"
    if kind in ("synth", "repair", "pending") and ids:
        reasons = sorted({t for n in notes if n.get("action") == "quarantined" for t in n.get("failure_types", [])})
        queue_changes = _update_queue(staging, ids, end, detail, transient, run_id, published, quarantined,
                                      pending_notes, reasons, "pending" if is_pending else kind)
    unit_ok = end == "finished" and not quarantined and not any(n.get("action") == "refused" for n in notes)  # X15
    no_change = (kind == "repair" and not is_pending and not recovered and not notes
                 and not any(r.get("applied") for r in removals_done))
    # A source-check review pass is done only with a recorded verdict for the note.
    no_verdict = bool(review_info) and (info.get("options") or {}).get("review_pass") == "source-check" \
        and review_info.get("contract", True) and not review_info.get("source_checked") \
        and not review_info.get("needs_repair") and not review_info.get("transcript_missing")
    if no_verdict:
        unit_ok = False
    if no_change:
        unit_ok = False  # a repair that changed nothing is not a success
        detail = (detail + " " if detail else "") + "repair unit wrote no note and removed none"
    if review_info and review_info.get("needs_repair"):
        unit_ok = False
    event = {
        "t": _now(), "event": "unit", "unit": unit_dir.name, "kind": kind, "ids": ids,
        "review_note": review_note, "end": end, "transient": transient, "agent_rc": agent_rc,
        "detail": detail[:500], "notes": notes, "removals": removals_done, "links": links_done,
        "partial_stubs_updated": partial_done, "replacement_beside_old_note": beside, "stubs_healed": healed,
        "stubs_recomputed": recomputed, "relinked": relinked,
        "queue": queue_changes, "unit_ok": unit_ok, "recovered": recovered, "pending_dir": pending_dir,
        "models_served": settings["models_served"], "review": review_info,
        "folds_refused": sorted({str(e.get("file") or "") for e in _read_jsonl(unit_dir / "events.jsonl")
                                 if e.get("event") == "fold-refused"}),
        "commit": commit, "pending_unit": is_pending, "no_change": no_change, "review_no_verdict": no_verdict,
        "index_rolled_back": rolled,
        "synth_mode": (info.get("options") or {}).get("synth_mode", "agent"),
        "synth_outcome": (_read_json(unit_dir / "claims.json", {}) or {}).get("outcome"),
        "synth_refused": (_read_json(unit_dir / "claims.json", {}) or {}).get("refused", []),
        "claims_uncovered": (_read_json(unit_dir / "claims.json", {}) or {}).get("claims_uncovered", []),
        # Round 10 (P3, M6): the bullet ledger and the reply problems of a repair unit.
        "bullets_dropped": (_read_json(unit_dir / "claims.json", {}) or {}).get("bullets_dropped", []),
        "speaker_check": (_read_json(unit_dir / "claims.json", {}) or {}).get("speaker_check"),
        "duplicates_merged": _read_json(unit_dir / "merged.json", []) or [],
        "repair_problems": [p for p in ((_read_json(unit_dir / "claims.json", {}) or {}).get("problems") or [])
                            if (_read_json(unit_dir / "claims.json", {}) or {}).get("mode") == "single-repair"],
        "repair_stubs": _read_json(unit_dir / "repair_stubs.json", []) or [],
        "repair_unclaimed": (_read_json(unit_dir / "claims.json", {}) or {}).get("repair_unclaimed", []),
        "repair_handled": [a["old"] for a in (_read_json(unit_dir / "claims.json", {}) or {}).get("applied", [])
                           if a.get("action") not in ("left as it is", "not answered")],
        "usage": _unit_usage(unit_dir),
        "times_s": dict(_unit_times(unit_dir), finalize=round(_dt.datetime.now().timestamp() - t_finalize, 1)),
        "base_urls": _unit_base_urls(unit_dir),
        "checker_changed": bool(meta.get("checker_sha256")) and meta.get("checker_sha256") != checker_hash(),
    }
    _append(run_dir / "events.jsonl", event)
    return event


def _finalize_recovered(pub: _Publisher, unit_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A dead unit: keep only what its finalize already published; quarantine the rest."""
    intents = {}
    for rec in _read_jsonl(unit_dir / "publish.jsonl"):
        if rec.get("phase") == "intent":
            intents[rec["rel"]] = rec["sha256"]
    published_by_records = {r.get("note") for r in provenance.read_records(pub.staging)
                            if r.get("event") == "note-published" and r.get("unit") == unit_dir.name}
    notes = []
    done_rels = set()
    for rel, sha in intents.items():
        if _sha256(pub.zk / rel) == sha:
            done_rels.add(rel)
            pub.published.append(rel)
            pub.commit_paths.append(rel)
            if rel not in published_by_records:
                # W34/W22 (round 21): the full record (bullets, Evidence tags, writers)
                txt = (pub.zk / rel).read_text(encoding="utf-8", errors="replace")
                provenance.record(pub.staging, "note-published", run_id=pub.run_id, note=rel, unit=unit_dir.name,
                                  verification="recovered", recovered=True, **_bullet_record(pub.staging, rel, txt),
                                  **_note_models(unit_dir, rel))
            notes.append({"note": rel, "action": "published", "recovered": True})
    shadow = unit_dir / "out"
    for p in sorted(shadow.rglob("*")) if shadow.is_dir() else []:
        if not p.is_file():
            continue
        rel = p.relative_to(shadow).as_posix()
        if rel in done_rels:
            continue
        fails = [{"type": "crashed-unit", "where": rel,
                  "detail": "the unit ended without a finalize record; its output is not trusted"}]
        notes.append({"note": rel, "action": "quarantined", "failure_types": ["crashed-unit"],
                      "quarantine_path": pub.quarantine(rel, p.read_bytes(), fails, "not-published")})
    return notes, []


def _store_pending(staging: Path, unit_dir: Path, run_id: str, ids: list[str], kind: str,
                   pending: list[dict[str, Any]], ck: _Checker, cycles: int = 1,
                   review_note: str | None = None) -> str:
    """Notes that passed the mechanical check but have no review verdict wait here."""
    # A newer version of the same note replaces an older pending copy.
    for old in (staging / "pending-review").glob("*--*/out"):
        for r in pending:
            if (old / r["rel"]).is_file():
                (old / r["rel"]).unlink()
        if old.is_dir() and not any(p.is_file() for p in old.rglob("*")):
            shutil.rmtree(old.parent)
    pdir = staging / "pending-review" / f"{run_id}--{unit_dir.name}"
    (pdir / "out").mkdir(parents=True, exist_ok=True)
    rels = []
    for r in pending:
        dst = pdir / "out" / r["rel"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(r["data"])
        rels.append(r["rel"])
    removals = {k: v for k, v in _read_json(unit_dir / "removals.json", {}).items()
                if (v or {}).get("replaced_by") in rels}
    _write_json(pdir / "removals.json", removals)
    _write_json(pdir / "bases.json", {k: v for k, v in ck.bases.items() if k in rels or k in removals})
    for name in PENDING_STATE_FILES:  # single mode: the state that fix calls and stubs need
        if (unit_dir / name).is_file():
            shutil.copy2(unit_dir / name, pdir / name)
    if (unit_dir / "calls").is_dir():  # S11/S1: the writer records (worker, model, key) travel with the note
        shutil.copytree(unit_dir / "calls", pdir / "calls", dirs_exist_ok=True)
    opts = _read_json(unit_dir / "unit.json", {}).get("options") or {}
    repair_video = _read_json(unit_dir / "unit.json", {}).get("repair_video")
    _write_json(pdir / "meta.json", {"ids": ids, "kind": kind, "from_unit": str(unit_dir), "notes": rels,
                                     "stored": _now(), "pending_cycles": cycles, "options": opts,
                                     "repair_video": repair_video,
                                     "review_note": review_note,
                                     # Z-C1 (round 25): the request keys this unit waits for
                                     "wait_keys": sorted(_unit_wait_keys(unit_dir)),
                                     "reasons": {r["rel"]: (r.get("entry") or {}).get("pending_reason", "review-missing")
                                                 for r in pending},
                                     "waiting_links": {r["rel"]: sorted({str(f.get("target")) for f in r["failures"]
                                                                         if f.get("type") == "link-waiting"})
                                                       for r in pending
                                                       if any(f.get("type") == "link-waiting" for f in r["failures"])}})
    provenance.record(staging, "note-pending-review", run_id=run_id, unit=unit_dir.name, notes=rels)
    return str(pdir)


OPERATOR_DEAD = "operator-dead.json"  # round 26 (b): operator-done on a folder that waits on a dead request
PENDING_STATE_FILES = ("candidates.json", "marked.json", "claims.json", "repair_map.json", "review-repair.json",
                       "fix_calls.json", "fix-pending.json", "repair_stubs.json", OPERATOR_DEAD)


def held_reply_keys(staging: Path) -> set[str]:
    """Pilot 9: the request keys that a WAITING unit holds (unit or review agent results that
    end `waiting-for-reply`, in runs or pending folders): their answered replies will be consumed."""
    keys: set[str] = set()
    for p in list(staging.glob("runs/*/units/**/agent_result.json")) + list(staging.glob("pending-review/*--*/**/agent_result.json")):
        unit = next((q for q in [p.parent, *p.parents] if q.parent.name in ("units",) or q.parent.name == "pending-review"), None)
        if unit is not None and unit.parent.name == "units" and (unit / "finalized.json").exists() \
                and not str(p).startswith(str(staging / "pending-review")):
            continue  # Z10: a finalized unit holds no key any more
        r = _read_json_snapshot(p, {}) or {}
        if r.get("end") == "waiting-for-reply":
            keys |= set(re.findall(r"request ([0-9a-f]{32})", str(r.get("detail") or "")))
    return keys


MAX_REFUSALS = int(os.environ.get("ZR_MAX_REFUSALS", "3"))


def request_dead(staging: Path, key: str) -> bool:
    """Z-C1 (round 25): the exchange request was refused MAX_REFUSALS times (obsolete for
    good: never open again, no worker answers it)."""
    x = staging / "exchange"
    return (x / "requests" / f"{key}.obsolete").is_file() \
        and len(list((x / "replies").glob(f"{key}.refused-*.json"))) >= MAX_REFUSALS


def folder_dead(staging: Path, pdir: Path) -> list[str]:
    """Round 26 (b): the dead request keys of a pending folder that waits ONLY for requests
    refused for good (and holds no answered reply); [] otherwise."""
    keys = folder_waits_keys(pdir)
    if not keys or folder_holds_reply(staging, pdir) or not all(request_dead(staging, k) for k in keys):
        return []
    return sorted(keys)


def waiting_titles(staging: Path) -> set[str]:
    """The titles of the notes that wait in pending-review. Round 26 (b): a folder that
    waits only for a dead request is not waiting (its notes show `target not published`)."""
    out: set[str] = set()
    for pdir in staging.glob("pending-review/*--*"):
        if not folder_dead(staging, pdir):
            out |= {q.stem for q in pdir.glob(f"out/{PERMANENT_DIR}/*.md")}
    return out


def _fix_wait_keys(path: Path) -> set[str]:
    """Exact files-backend request keys saved per note by fix_unit (round 27)."""
    pending = _read_json_snapshot(path, {}) or {}
    waiting_notes = pending.get("notes") or {}
    raw = pending.get("wait_keys") or {}
    if not isinstance(waiting_notes, dict) or not isinstance(raw, dict):
        return set()
    keys: set[str] = set()
    for rel, value in raw.items():
        if rel not in waiting_notes:
            continue  # only requests owned by notes that remain pending are live
        items = value if isinstance(value, (list, tuple, set)) else [value]
        keys |= {str(k) for k in items if re.fullmatch(r"[0-9a-f]{32}", str(k))}
    return keys


def _unit_wait_keys(unit_dir: Path) -> set[str]:
    keys: set[str] = _fix_wait_keys(unit_dir / "fix-pending.json")
    for p in unit_dir.rglob("agent_result.json"):
        r = _read_json_snapshot(p, {}) or {}
        if r.get("end") == "waiting-for-reply":
            keys |= set(re.findall(r"request ([0-9a-f]{32})", str(r.get("detail") or "")))
    return keys


def folder_waits_keys(pdir: Path) -> set[str]:
    """Keys held by a pending folder: explicit metadata, its fix-pending state, or
    waiting agent results. Legacy fix-pending files have no keys; the normal pending
    retry recomputes the same request and records its exact key before the next finalize."""
    keys: set[str] = {str(k) for k in ((_read_json_snapshot(pdir / "meta.json", {}) or {}).get("wait_keys") or [])}
    keys |= _fix_wait_keys(pdir / "fix-pending.json")
    for p in pdir.rglob("agent_result.json"):
        r = _read_json_snapshot(p, {}) or {}
        if r.get("end") == "waiting-for-reply":
            keys |= set(re.findall(r"request ([0-9a-f]{32})", str(r.get("detail") or "")))
    return keys


def folder_holds_reply(staging: Path, pdir: Path) -> bool:
    """A reply body exists for a key the folder waits for and can be revalidated by its caller."""
    x = staging / "exchange"
    for key in folder_waits_keys(pdir):
        if (x / "replies" / f"{key}.txt").is_file() and not request_dead(staging, key):
            return True
        reissue = _consumed_reissue_record(staging, key)
        active = (reissue or {}).get("reissued_key")
        if active and (x / "replies" / f"{active}.txt").is_file() and not request_dead(staging, active):
            return True
    return False


def _consumed_reissue_record(staging: Path, base_key: str) -> dict[str, Any] | None:
    """Read a structurally valid one-key consumed-reply reissue record, if present."""
    x = staging / "exchange"
    base_req = x / "requests" / f"{base_key}.json"
    sidecar = x / "reissues" / f"{base_key}.json"
    if not base_req.is_file() or not sidecar.is_file():
        return None
    data = _read_json_snapshot(sidecar, {}) or {}
    if (data.get("base_key") != base_key or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("reissued_key") or ""))
            or data.get("base_request_sha256") != hashlib.sha256(base_req.read_bytes()).hexdigest()
            or not isinstance(data.get("review_attempt"), dict)
            or data["review_attempt"].get("consumed_reissue_of") != base_key
            or data["review_attempt"].get("attempt") != 1
            or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("payload_sha256") or ""))):
        return None
    derived = x / "requests" / f"{data['reissued_key']}.json"
    if derived.is_file():
        req = _read_json_snapshot(derived, {}) or {}
        if (req.get("key") != data["reissued_key"] or req.get("reissue_of") != base_key
                or req.get("reissue_attempt") != 1 or req.get("reissue_payload_sha256") != data["payload_sha256"]):
            return None
    return data


def _folder_has_recoverable_consumed_wait(staging: Path, pdir: Path) -> bool:
    """Allow one bounded pass to pin an identical review under a fresh key.

    An existing alias may be replayed while the folder still lists its consumed base key;
    once the derived key is in the folder, ordinary reply/dead-request rules apply.
    """
    keys = folder_waits_keys(pdir)
    if len(keys) != 1:
        return False
    key = next(iter(keys))
    x = staging / "exchange"
    if (x / "requests" / f"{key}.obsolete").is_file() or request_dead(staging, key):
        return False
    if not (x / "consumed" / f"{key}.json").is_file() or (x / "replies" / f"{key}.txt").is_file():
        return False
    req = _read_json_snapshot(x / "requests" / f"{key}.json", {}) or {}
    if req.get("key") != key or req.get("kind") != "review":
        return False
    if req.get("reissue_of") or req.get("reissue_attempt") is not None:
        return False  # A consumed derived request is never eligible as a new alias base.
    record = _consumed_reissue_record(staging, key)
    if record is None:
        return not (x / "reissues" / f"{key}.json").exists()
    active = record["reissued_key"]
    if active in keys or request_dead(staging, active) or (x / "requests" / f"{active}.obsolete").is_file():
        return False
    # Admit only while repairing propagation from the base key. A reply, if present,
    # is independently recognized by folder_holds_reply above.
    return not (x / "consumed" / f"{active}.json").is_file()


def pending_units(staging: Path, run_dir: Path, owner_pid: int | None, reason: str | None = None) -> list[str]:
    """Turn each pending-review folder into a unit of this run (no agent output).
    A folder that waited MAX_PENDING_CYCLES (5) times stays for the operator (summary
    `pending_for_operator`); it is never deleted. With `reason`, only folders that hold
    a note pending for that reason (for example `link-waiting` at the end of a run)."""
    out = []
    for pdir in sorted((staging / "pending-review").glob("*--*")):
        if not (pdir / "meta.json").exists():
            continue
        meta = _read_json(pdir / "meta.json", {})
        if int(meta.get("pending_cycles") or 0) >= MAX_PENDING_CYCLES and not folder_holds_reply(staging, pdir) \
                and not _folder_has_recoverable_consumed_wait(staging, pdir) \
                and not (pdir / OPERATOR_DEAD).is_file():
            continue  # X5 (round 22): a folder at the limit that holds a reply is still taken
        if reason and reason not in (meta.get("reasons") or {}).values():
            continue
        # The unit keeps its original kind (repair rules stay repair rules) and options.
        kind = str(meta.get("kind") or "synth")
        if kind == "pending":
            kind = "synth"
        unit = unit_start(run_dir, kind, list(meta.get("ids") or []), None, owner_pid, "pending",
                          options=meta.get("options") or {})
        info = _read_json(unit / "unit.json", {})
        info["pending"] = True
        info["pending_cycles"] = int(meta.get("pending_cycles") or 0)
        info["waiting_links"] = meta.get("waiting_links") or {}
        if meta.get("repair_video"):
            info["repair_video"] = meta["repair_video"]
        _write_json(unit / "unit.json", info)
        shutil.rmtree(unit / "out")
        shutil.copytree(pdir / "out", unit / "out")
        if (pdir / "calls").is_dir():
            shutil.copytree(pdir / "calls", unit / "calls", dirs_exist_ok=True)
        for name in ("removals.json", "bases.json") + PENDING_STATE_FILES:
            if (pdir / name).exists():
                shutil.copy2(pdir / name, unit / name)
        _write_json(unit / "agent_result.json", {"rc": 0, "end": "finished", "detail": f"pending review from {pdir.name}"})
        shutil.rmtree(pdir)
        out.append(str(unit))
    return out


def _apply_removals(pub: _Publisher, unit_dir: Path, ck: _Checker) -> list[dict[str, Any]]:
    """A removal applies only when its replacement was published; the old file is
    archived and replaced by a stub, so links to it keep working."""
    out = []
    for rel, req in _read_json(unit_dir / "removals.json", {}).items():
        repl = (req or {}).get("replaced_by")
        cur = pub.zk / rel
        base = ck.bases.get(rel, "missing")
        if repl in pub.published and cur.is_file() and (base == "missing" or base == _sha256(cur)):
            pub.write(rel, _harness_stub(Path(rel).stem, Path(repl).stem), why=f"removed, replaced by {repl}")
            provenance.record(pub.staging, "note-removed", run_id=pub.run_id, note=rel, replaced_by=repl,
                              unit=unit_dir.name)
            out.append({"note": rel, "replaced_by": repl, "applied": True, "stub": True})
        else:
            out.append({"note": rel, "replaced_by": repl, "applied": False,
                        "reason": "replacement not published" if repl not in pub.published
                        else "vault file changed or missing"})
    return out


def _review_assigned(pub: _Publisher, ck: _Checker, staging: Path, unit_dir: Path, note_rel: str) -> dict[str, Any]:
    """A review run never removes a published note. A defect sets
    `verification: needs-repair` in place; repair_loop.sh takes these notes."""
    path = pub.zk / note_rel
    v = verdict_for(unit_dir, note_rel, path.read_bytes()) if path.is_file() else None
    info: dict[str, Any] = {"note": note_rel, "verdicts": v, "bad_verdicts": bool(v and v["bad"]),
                            "source_checked": bool(v and not v["bad"] and not v.get("unusable"))}
    if note_rel in pub.published:
        info["source_checked"] = True  # published in this unit: it passed its own review
        return info
    if not path.is_file():
        return info
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, _, _ = verify_claims.split_frontmatter(text)
    if not verify_claims.is_contract_note(fm):
        info["contract"] = False
        return info
    res = ck.check_text(note_rel, text, contract_required=True, existed=True)
    fails = res["failures"]
    info["failure_types"] = sorted({f["type"] for f in fails})
    if any(f["type"] == "transcript-missing" for f in fails):
        info["transcript_missing"] = True  # reported, never changed
        return info
    if v and v["bad"]:
        fails = fails + [{"type": f"review:{b}", "where": "source-check", "detail": v["path"]} for b in v["bad"]]
    if fails and str(fm.get("verification") or "") != "needs-repair":
        pub.write(note_rel, verify_claims.set_verification_text(text, "needs-repair"), why="review found a defect")
        info["needs_repair"] = True
        info["failure_types"] = sorted({f["type"] for f in fails})
        provenance.record(staging, "note-needs-repair", run_id=pub.run_id, note=note_rel, unit=unit_dir.name,
                          failure_types=info["failure_types"])
    elif fails:
        info["needs_repair"] = True
    return info


def _apply_links(pub: _Publisher, ck: _Checker, staging: Path, unit_name: str) -> list[dict[str, Any]]:
    out = []
    for rec in _read_jsonl(staging / "disagreements.jsonl"):
        if rec.get("unit") != unit_name:
            continue
        src, dst, kind = str(rec.get("from") or ""), str(rec.get("to") or ""), str(rec.get("kind") or "related")
        res: dict[str, Any] = {"from": src, "to": dst, "kind": kind}
        target = pub.zk / dst
        if not dst.startswith(PERMANENT_DIR + "/") or "/" in dst[len(PERMANENT_DIR) + 1:] or ".." in dst:
            res.update(applied=False, reason=f"links are added only to notes in '{PERMANENT_DIR}/'")
        elif src not in pub.published:
            res.update(applied=False, reason="the from-note was not published")
        elif not target.is_file():
            res.update(applied=False, reason="the to-note is not in the vault")
        else:
            text = target.read_text(encoding="utf-8")
            link = f"[[{Path(src).stem}]]"
            if link in text:
                res.update(applied=False, reason="link already present")
            else:
                line = "- " + LINK_LINE.get(kind, LINK_LINE["related"]).format(src=Path(src).stem)
                new = _add_connected_line(text, line)
                fm, _, _ = verify_claims.split_frontmatter(text)
                if verify_claims.is_stub(fm):  # X21: a stub is never a backlink target
                    res.update(applied=False, reason="the to-note is a stub")
                    out.append(res)
                    continue
                if kind == "supersedes-candidate" and verify_claims.is_contract_note(fm):
                    res.update(applied=False, reason="supersedes-candidate is only for an old note without sources")
                    out.append(res)
                    continue
                if verify_claims.is_contract_note(fm):
                    chk = ck.check_text(dst, new, contract_required=True, existed=True)
                    if not chk["ok"]:
                        res.update(applied=False, reason="the to-note does not pass after the edit: "
                                   + ",".join(sorted({f['type'] for f in chk['failures']})))
                        out.append(res)
                        continue
                if not pub.write(dst, new, why=f"backlink from {src}"):
                    res.update(applied=False, reason="write refused (see needs_operator.json)")
                    out.append(res)
                    continue
                res.update(applied=True)
                provenance.record(staging, "note-linked", run_id=pub.run_id, note=dst, linked_from=src, kind=kind)
        out.append(res)
    return out


def _add_connected_line(text: str, line: str) -> str:
    lines = text.rstrip("\n").split("\n")
    try:
        i = next(k for k, ln in enumerate(lines) if ln.strip() == "## Connected Ideas")
    except StopIteration:
        return text.rstrip("\n") + f"\n\n## Connected Ideas\n\n{line}\n"
    j = i + 1
    while j < len(lines) and not lines[j].startswith("## "):
        j += 1
    k = j
    while k > i + 1 and not lines[k - 1].strip():
        k -= 1
    lines.insert(k, line)
    return "\n".join(lines) + "\n"


def _git(zk_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(zk_dir), *args], capture_output=True, text=True)


def _git_commit(zk_dir: Path, paths: list[str], message: str, staging: Path) -> dict[str, Any]:
    """Commit exactly these paths. On failure, store them for a retry at the next start."""
    if _git(zk_dir, "rev-parse").returncode != 0:
        return {"ok": None, "reason": "not a git repo", "paths": []}
    uniq = sorted(set(paths))
    add = _git(zk_dir, "add", "-A", "--", *uniq)
    res = add
    if add.returncode == 0:
        res = _git(zk_dir, "commit", "-q", "-m", message, "--", *uniq)
        if res.returncode != 0 and "nothing to commit" in (res.stdout + res.stderr):
            return {"ok": True, "paths": uniq, "note": "nothing to commit"}
    if res.returncode == 0:
        return {"ok": True, "paths": uniq}
    pend = staging / "commit-pending.json"
    with state_lock(staging):
        data = _read_json(pend, [])
        data.append({"vault": str(zk_dir), "paths": uniq, "message": message, "at": _now(),
                     "error": (res.stderr or res.stdout)[-500:]})
        _write_json(pend, data)
    return {"ok": False, "paths": uniq, "error": (res.stderr or res.stdout)[-500:]}


def retry_pending_commits(staging: Path) -> list[dict[str, Any]]:
    pend = staging / "commit-pending.json"
    if not pend.exists():
        return []
    with state_lock(staging):
        data = _read_json(pend, [])
        _write_json(pend, [])
    out = []
    for c in data:
        r = _git_commit(Path(c["vault"]), c["paths"], c["message"] + " (retried)", staging)
        out.append({"paths": c["paths"], "ok": r["ok"]})
    return out


def _item_pending(it: dict[str, Any], published: list[str], quarantined: list[str]) -> bool:
    """Notes of the item still wait in pending-review after this unit."""
    return bool(set(it.get("pending_notes", [])) - set(published) - set(quarantined))


def _update_queue(staging: Path, ids: list[str], end: str, detail: str, transient: bool, run_id: str,
                  published: list[str], quarantined: list[str], pending: list[str], reasons: list[str],
                  kind: str) -> list[dict[str, Any]]:
    q_path = staging / "queue.json"
    if not q_path.exists():
        raise HarnessError(f"no queue.json in {staging}")
    changes = []
    with state_lock(staging):
        q = json.loads(q_path.read_text(encoding="utf-8"))
        for it in q["items"]:
            if it["id"] not in ids:
                continue
            before = it.get("stage")
            it.pop("worker", None)
            cf = it.pop("claimed_from", None)
            pend = kind == "pending"
            if end == "waiting-for-reply" and not pend:
                # Round 13 (files backend): the unit waits for a reply; no attempt. Skipped in
                # this pass; the next run sets it back to its old stage.
                it["claimed_from"] = cf or (before if before not in ("claimed",) else "extracted")
                it["stage"] = "waiting-for-reply"
                # round 26 (c): the request keys it waits for (a dead key is operator work)
                it["wait_keys"] = sorted(set(re.findall(r"request ([0-9a-f]{32})", detail or "")))
                changes.append({"id": it["id"], "stage_before": before, "stage_after": it["stage"]})
                continue
            if end == "budget-stop":
                # Round 10: the budget stopped the unit before its first call: no attempt.
                it["stage"] = cf or (before if before not in ("claimed",) else "extracted")
                changes.append({"id": it["id"], "stage_before": before, "stage_after": it["stage"]})
                continue
            if end != "finished" and not pend:
                it["stage"] = "incomplete"
                it["incomplete_reason"] = f"{end}: {detail[:300]}".strip()
                if not transient:
                    it["synth_attempts"] = it.get("synth_attempts", 0) + 1
                    if it["synth_attempts"] >= MAX_ATTEMPTS:  # X14 (round 22): the limit holds
                        it["stage"] = "failed"
                        it["error"] = f"unit failed {it['synth_attempts']} times (ZR_MAX_UNIT_ATTEMPTS={MAX_ATTEMPTS})"
            elif before in ("claimed", "extracted") and not pend:
                it["stage"] = "incomplete"
                it["incomplete_reason"] = f"agent finished without queue_mark.py: {detail[:300]}".strip()
                if not transient:
                    it["synth_attempts"] = it.get("synth_attempts", 0) + 1
                    if it["synth_attempts"] >= MAX_ATTEMPTS:  # X14
                        it["stage"] = "failed"
                        it["error"] = f"unit failed {it['synth_attempts']} times (ZR_MAX_UNIT_ATTEMPTS={MAX_ATTEMPTS})"
            else:
                # Round 8: a finished unit is synthesized whatever the review decided; a
                # quarantined note is not a reason to synthesize the video again. A pending
                # unit (only a review) never counts as an attempt.
                if pending or (pend and before == "pending-review" and _item_pending(it, published, quarantined)):
                    it["stage"] = "pending-review"
                elif before in ("synthesized", "skipped", "needs-attention"):
                    it["stage"] = before
                else:
                    it["stage"] = "synthesized"
                it.pop("incomplete_reason", None)
            if published:
                it["published_notes"] = sorted(set(it.get("published_notes", [])) | set(published))
            if quarantined:
                it["quarantined_notes"] = sorted(set(it.get("quarantined_notes", [])) | set(quarantined))
            done = set(published) | set(quarantined)
            it["pending_notes"] = sorted((set(it.get("pending_notes", [])) - done) | set(pending))
            it["quarantined_notes"] = sorted(set(it.get("quarantined_notes", [])) - set(published))
            it["notes_published"] = len(it.get("published_notes", []))
            it["notes_quarantined"] = len(it.get("quarantined_notes", []))
            it["notes_pending"] = len(it["pending_notes"])
            total = it["notes_published"] + it["notes_quarantined"] + it["notes_pending"]
            if it["stage"] == "synthesized" and total and it["notes_quarantined"] * 2 > total:
                it["stage"] = "needs-attention"  # more than half of its notes were quarantined
            elif it["stage"] == "needs-attention" and total and it["notes_quarantined"] * 2 <= total:
                it["stage"] = "synthesized"  # V18: with no note at all it stays needs-attention
            hist = it.setdefault("run_history", [])
            hist.append({"run_id": run_id, "end": end, "transient": transient, "published": len(published),
                         "quarantined": len(quarantined), "pending": len(pending)})
            del hist[:-RUN_HISTORY_CAP]
            changes.append({"id": it["id"], "stage_before": before, "stage_after": it["stage"]})
        _save_queue(q_path, q)
    return changes


# --------------------------------------------------------------------------- #
# Repair work lists
# --------------------------------------------------------------------------- #


def _repair_state(zk: Path, rel: str) -> str:
    """old-format | needs-repair | stub | done (contract note, source-checked or unsupported) | contract | missing."""
    p = zk / rel
    if not p.is_file():
        return "missing"
    fm, _, _ = verify_claims.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
    ver = str(fm.get("verification") or "")
    if verify_claims.is_stub(fm):
        return "stub"
    if ver == "needs-repair" and str(fm.get("repair_note") or "").strip():
        return "operator"  # repair found no supporting passage: the operator decides
    if ver == "needs-repair":
        return "needs-repair"
    if ver == "unsupported" and fm.get("unsupported_marked"):
        return "marked-unsupported"  # round 24: the harness mark; removable with --force
    if ver == "unsupported" or (verify_claims.is_contract_note(fm) and ver == "source-checked"):
        return "done"
    if not verify_claims.is_contract_note(fm):
        return "old-format"
    return "contract"


def _legacy_notes_by_video(staging: Path) -> dict[str, list[str]]:
    """video id -> note paths recorded in the legacy provenance.json of this staging."""
    out: dict[str, list[str]] = {}
    data = _read_json(staging / "provenance.json", {}) or {}
    for note, vids in (data.items() if isinstance(data, dict) else []):
        if not str(note).startswith(PERMANENT_DIR + "/"):
            continue
        for v in vids if isinstance(vids, list) else []:
            out.setdefault(str(v), []).append(str(note))
    return out


def repair_candidates(staging: Path, zk_dir: Path | None = None) -> list[dict[str, Any]]:
    """Notes that need repair, each note once: old-format notes of queue items whose
    transcript was replaced after synthesis (`transcript_replaced_at`), and vault notes
    marked `verification: needs-repair`. Stubs, source-checked contract notes and
    `unsupported` notes are not listed (`excluded` names them)."""
    zk = zk_dir or (Path(os.environ["ZK_DIR"]) if os.environ.get("ZK_DIR") else None)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in _queue_items(staging):
        if not it.get("transcript_replaced_at"):
            continue
        notes = set(it.get("published_notes", [])) | {f"{PERMANENT_DIR}/{t}.md" for t in it.get("notes_emitted", [])}
        notes |= {r.get("note") for r in provenance.read_records(staging)
                  if r.get("event") in ("note-published", "note-verified") and it["id"] in (r.get("sources") or [])}
        legacy = _legacy_notes_by_video(staging)  # round 10 (pilot 4): the legacy provenance.json too
        notes |= {n if n.endswith(".md") else f"{PERMANENT_DIR}/{n}.md" for n in legacy.get(it["id"], [])}
        on_disk = it.get("notes_on_disk")
        if isinstance(on_disk, list):  # written by ingest --vault (package A)
            notes |= {d["note"] for d in on_disk if isinstance(d, dict) and d.get("note")}
        keep, missing, excluded = [], [], []
        for n in sorted(x for x in notes if x):
            st = _repair_state(zk, n) if zk is not None else "old-format"
            if st == "missing":
                missing.append(n)
            elif st in ("old-format", "needs-repair") and n not in seen:
                keep.append(n)
                seen.add(n)
            elif st not in ("old-format", "needs-repair"):
                excluded.append({"note": n, "state": st})
        out.append({"id": it["id"], "reason": "transcript replaced", "transcript_replaced_at": it["transcript_replaced_at"],
                    "notes": keep, "notes_not_in_vault": missing, "excluded": excluded})
    if zk is not None and (zk / PERMANENT_DIR).is_dir():
        nr = [p.relative_to(zk).as_posix() for p in sorted((zk / PERMANENT_DIR).glob("*.md"))
              if "verification: needs-repair" in p.read_text(encoding="utf-8", errors="replace")[:3000]]
        nr = [n for n in nr if n not in seen and _repair_state(zk, n) == "needs-repair"]
        if nr:
            out.append({"id": None, "reason": "verification: needs-repair", "notes": nr})
        op = operator_notes(zk)
        if op:
            # Never "done": listed for the operator, not for another repair run.
            out.append({"id": None, "reason": "repair found no supporting passage (operator)", "notes": [],
                        "operator_notes": op})
    return out


def operator_notes(zk: Path) -> list[dict[str, str]]:
    out = []
    for p in sorted((zk / PERMANENT_DIR).glob("*.md")) if (zk / PERMANENT_DIR).is_dir() else []:
        head = p.read_text(encoding="utf-8", errors="replace")[:3000]
        if "verification: needs-repair" in head and "repair_note:" in head:
            fm, _, _ = verify_claims.split_frontmatter(head if head.count("\n---") else p.read_text(encoding="utf-8"))
            out.append({"note": p.relative_to(zk).as_posix(), "repair_note": str(fm.get("repair_note") or "")})
    return out


KNOWN_SOURCES = "known_sources.json"
_GIT_ID = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")


def _known_ids(staging: Path) -> set[str]:
    ids = {str(it.get("id")) for it in _queue_items(staging)}
    reg = _read_json(staging / "transcripts.json", {}) or {}
    ids |= set((reg.get("items") or {}).keys()) if isinstance(reg.get("items"), dict) else set()
    for d in verify_claims.lit_dirs(staging):
        ids |= {p.stem for p in d.glob("*.md")}
    return {i for i in ids if i}


def known_sources_build(staging: Path, zk_dir: Path) -> dict[str, Any]:
    """Map each note to the videos named in the message of the commit that ADDED it
    (`git log --diff-filter=A --name-only`). Writes <staging>/known_sources.json."""
    top = subprocess.run(["git", "-C", str(zk_dir), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        raise HarnessError(f"{zk_dir} is not inside a git repository")
    root = Path(top.stdout.strip()).resolve()
    prefix = zk_dir.resolve().relative_to(root).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    log = subprocess.run(["git", "-C", str(root), "log", "--diff-filter=A", "--name-only", "--format=@@%H %s"],
                         capture_output=True, text=True)
    if log.returncode != 0:
        raise HarnessError(f"git log failed: {log.stderr[-300:]}")
    ids = _known_ids(staging)
    vid_ids = {i[len("vid-"):]: i for i in ids if i.startswith("vid-")}
    notes: dict[str, Any] = {}
    commit, subject = "", ""
    for line in log.stdout.splitlines():
        if line.startswith("@@"):
            commit, _, subject = line[2:].partition(" ")
            continue
        if not line.strip() or not line.startswith(prefix):
            continue
        rel = line[len(prefix):]
        if not rel.startswith(PERMANENT_DIR + "/") or not rel.endswith(".md"):
            continue
        found = []
        # A `vid-...` id in the message counts whether or not a staging has it: a known
        # source without any transcript is exactly the `mark_unsupported` case (round 7).
        for m in re.finditer(r"(?<![A-Za-z0-9_-])(vid-[A-Za-z0-9_-]{6,})", subject):
            if m.group(1) not in found:
                found.append(m.group(1))
        # A bare 11-character token counts only when a staging knows it as a video id
        # (an 11-letter word such as "progression" is not an id).
        for m in _GIT_ID.finditer(subject):
            tok = m.group(1)
            vid = tok if tok in ids else vid_ids.get(tok)
            if vid and vid not in found:
                found.append(vid)
        # git log lists newest first: the oldest add of a path wins.
        notes[rel] = {"sources": found, "commit": commit, "subject": subject[:200]}
    out = {"vault": str(zk_dir), "built": _now(), "notes": notes}
    _write_json(staging / KNOWN_SOURCES, out)
    return out


def repair_sources(staging: Path, note_rel: str, zk_dir: Path | None = None) -> list[str]:
    """Known source videos of an old note: legacy provenance and queues, then
    known_sources.json (vault git history). The frontmatter `sources` come first."""
    stagings = [staging] + [d.parent for d in verify_claims.lit_dirs(staging) if d.parent != staging]
    prov, queues = [], []
    for st in stagings:
        prov += [st / "provenance.json", st / "provenance.jsonl"]
        queues += [st / "queue.json"]
    legacy = verify_claims.load_legacy_sources(prov, queues)
    out: list[str] = []
    if zk_dir is not None and (zk_dir / note_rel).is_file():
        fm, _, _ = verify_claims.split_frontmatter((zk_dir / note_rel).read_text(encoding="utf-8", errors="replace"))
        out += list(verify_claims._source_ids(fm))
    out += legacy.get(note_rel, []) or legacy.get(Path(note_rel).stem, [])
    for st in stagings:
        ks = (_read_json(st / KNOWN_SOURCES, {}) or {}).get("notes") or {}
        out += list((ks.get(note_rel) or {}).get("sources") or [])
    return list(dict.fromkeys(x for x in out if x))


def repair_plan(staging: Path, zk_dir: Path, note_rel: str, allow_other: bool = False) -> dict[str, Any]:
    """What the repair of one note may do: its known sources, which have a transcript,
    and `mark_unsupported` when no known source has one (and no override is given)."""
    lit = verify_claims.LitIndex(verify_claims.lit_dirs(staging))
    srcs = repair_sources(staging, note_rel, zk_dir)
    with_tr = [x for x in srcs if lit.get(x) is not None]
    origin = origin_sources(staging, note_rel)
    origin_missing = bool(origin) and not any(lit.get(x) is not None for x in origin)
    # The video that the note was CREATED from (vault git history) decides first: when its
    # transcript is missing, Evidence from videos that were folded in later cannot
    # support the original claims (round 7, F3).
    missing = (bool(srcs) and not with_tr) or origin_missing
    return {"note": note_rel, "state": _repair_state(zk_dir, note_rel),
            "sources": srcs, "origin_sources": origin, "with_transcript": with_tr,
            "mark_unsupported": missing and not allow_other,
            "reason": "source transcript missing" if missing else ""}


def origin_sources(staging: Path, note_rel: str) -> list[str]:
    """The videos named in the commit that added the note (known_sources.json)."""
    stagings = [staging] + [d.parent for d in verify_claims.lit_dirs(staging) if d.parent != staging]
    out: list[str] = []
    for st in stagings:
        ks = (_read_json(st / KNOWN_SOURCES, {}) or {}).get("notes") or {}
        out += list((ks.get(note_rel) or {}).get("sources") or [])
    return list(dict.fromkeys(out))


def mark_unsupported(unit_dir: Path, zk_dir: Path, note_rel: str, reason: str) -> Path:
    """Write the shadow copy of an old note with `verification: unsupported` and
    `unsupported_reason` (repair default when the source transcript is missing)."""
    if safe_note_rel(zk_dir, note_rel) is None:
        raise HarnessError(f"refused note path {note_rel!r}: give a path relative to the zettelkasten, inside "
                           f"{PERMANENT_DIR}")
    src = zk_dir / note_rel
    if not src.is_file():
        raise HarnessError(f"no note {note_rel} in {zk_dir}")
    text = src.read_text(encoding="utf-8", errors="replace")
    text = verify_claims.set_verification_text(text, "unsupported")
    fm_end = text.find("\n---", 4)
    if not text.startswith("---") or fm_end == -1:
        raise HarnessError(f"{note_rel} has no frontmatter")
    head, rest = text[:fm_end], text[fm_end:]
    head = re.sub(r"^unsupported_reason:.*\n?", "", head + "\n", flags=re.M).rstrip("\n")
    text = head + f"\nunsupported_reason: {json.dumps(reason)}" + rest
    dst = unit_dir / "out" / note_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    _write_json(unit_dir / "bases.json", {**_read_json(unit_dir / "bases.json", {}), note_rel: _sha256(src)})
    return dst


# --------------------------------------------------------------------------- #
# Review prompt passages (90 s of context around each quote)
# --------------------------------------------------------------------------- #


def passages(staging: Path, zk_dir: Path, note_rel: str, context_s: int = REVIEW_CONTEXT_S,
             max_chars: int = 30000, note_file: Path | None = None, with_ts: bool = False) -> str:
    """The cited passages of a note. With `with_ts` (fix calls, round 11) each block adds
    the same span with its `[MM:SS]` markers and the lit file path."""
    path = note_file or (zk_dir / note_rel)
    if not path.is_file():
        return f"(note not found: {note_rel})"
    fm, body, _ = verify_claims.split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    sections = verify_claims.parse_body_full(body).sections
    lit = verify_claims.LitIndex(verify_claims.lit_dirs(staging))
    out: list[str] = []
    for sec_name in ("Evidence", "Disagreement"):
        sec = sections.get(sec_name)
        for n_item, raw in enumerate(sec.bullets() if sec else [], 1):
            label = f"{sec_name.upper()} {n_item}" + (f" (skeleton item evidence-{n_item})" if sec_name == "Evidence" else "")
            m = verify_claims.EVIDENCE_ITEM.match("- " + raw)
            tag = verify_claims.SRC_TAG.search(raw)
            vid = m.group(1) if m else (tag.group(1) if tag else None)
            quote = verify_claims._parse_quote(m.group(4) if m else raw)
            if not vid or quote is None:
                out.append(f"- {sec_name} item without a source tag or quote: {raw[:200]}")
                continue
            tr = lit.get(vid)
            if tr is None:
                out.append(f"- {vid}: transcript-missing (no lit file in any registered lit folder)")
                continue
            parts = verify_claims.strict_quote_parts(quote)
            pos = tr.strict.find(parts[0]) if parts else -1
            cited = verify_claims.ts_to_seconds(m.group(2) if m else (tag.group(2) if tag else None))
            if pos != -1 and cited is not None and tr.has_ts:
                # The occurrence nearest to the cited timestamp, not the first one.
                best, bd, q = pos, None, pos
                while q != -1:
                    mi = tr.marker_index_at(q)
                    d = abs((tr.marker_time[mi] if mi >= 0 else 0) - cited)
                    if bd is None or d < bd:
                        best, bd = q, d
                    q = tr.strict.find(parts[0], q + 1)
                pos = best
            if pos == -1:
                out.append(f"- {vid}: quote NOT FOUND in the transcript: \"{quote[:200]}\"")
                continue
            end = pos + len(parts[-1]) if len(parts) == 1 else tr.strict.find(parts[-1], pos) + len(parts[-1])
            if tr.has_ts:
                mi = tr.marker_index_at(pos)
                t = tr.marker_time[mi] if mi >= 0 else 0
                lo_i = tr.marker_index_for_time(max(0, t - context_s))
                hi_i = tr.marker_index_for_time(t + context_s) + 1
                a = tr.marker_pos[lo_i] if lo_i >= 0 else 0
                b = tr.marker_pos[hi_i] if hi_i < len(tr.marker_pos) else len(tr.strict)
                stamp = f" @ {verify_claims._fmt_ts(t)} (context {verify_claims._fmt_ts(max(0, t - context_s))}"
                stamp += f" to {verify_claims._fmt_ts(t + context_s)})"
            else:
                span = 1500  # about 90 s of speech without timestamps
                a, b, stamp = max(0, pos - span), min(len(tr.strict), end + span), " (no timestamps; about 90 s of context)"
            s0, s1 = verify_claims.quote_sentences(tr.strict, pos, end)
            # Round 9: the sentence directly before and after the quoted one, when it belongs
            # to the same statement (not a question, which is usually the other voice).
            p0, p1 = verify_claims.adjacent_sentences(tr.strict, s0, s1)
            a, b = min(a, p0), max(b, p1)
            lines = [f"[{label}] {vid}{stamp} (speakers {tr.speakers}, asr_quality {tr.asr_quality})",
                     f"CONTEXT BEFORE: ...{tr.strict[a:p0].strip()}"]
            if p0 < s0:
                lines.append(f">> {tr.strict[p0:s0].strip()} <<")
            lines.append(f">>> {tr.strict[s0:s1].strip()} <<<")
            if p1 > s1:
                lines.append(f">> {tr.strict[s1:p1].strip()} <<")
            lines.append(f"CONTEXT AFTER: {tr.strict[p1:b].strip()}...")
            if with_ts:
                lines.append(f"LIT FILE: {tr.path}")
                if tr.has_ts:
                    lines.append("WITH TIMESTAMPS: " + _with_markers(tr, a, b))
            out.append("\n".join(lines))
    if not out:
        srcs = verify_claims._source_ids(fm) or provenance.merged_map(staging).get(note_rel, [])
        for sid in srcs:
            p = lit.paths.get(sid)
            out.append(f"- {sid}: no Evidence section; read the transcript at {p}" if p
                       else f"- {sid}: transcript-missing (no lit file)")
    if not out:
        out.append("- no source is recorded for this note: give every item the verdict transcript-missing")
    text = "\n\n".join(out)
    if len(text) <= max_chars:
        return text
    # Pilot 9 (round 22): never cut a marked passage. Shrink the context around every item
    # first (a cut passage made the reviewer answer transcript-missing for ever).
    if context_s > 10:
        return passages(staging, zk_dir, note_rel, max(10, context_s // 2), max_chars, note_file, with_ts)
    # Even 10 s of context is too long: drop only the CONTEXT lines, keep every marked passage.
    blocks = []
    for blk in out:
        blocks.append("\n".join(ln for ln in blk.split("\n") if not ln.startswith(("CONTEXT BEFORE:", "CONTEXT AFTER:",
                                                                                  "WITH TIMESTAMPS:"))))
    return "\n\n".join(blocks)


def _with_markers(tr: Any, a: int, b: int) -> str:
    """tr.strict[a:b] with a `[MM:SS]` marker at each transcript segment start."""
    parts, last = [], a
    for pos, t in zip(tr.marker_pos, tr.marker_time):
        if a <= pos < b:
            parts.append(tr.strict[last:pos])
            parts.append(f" [{verify_claims._fmt_ts(t)}] ")
            last = pos
    parts.append(tr.strict[last:b])
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def item_passages(note_text: str, items: list[dict[str, Any]]) -> dict[str, list[int]]:
    """For each skeleton item, the numbers of the EVIDENCE blocks whose marked sentences
    it relies on: evidence-N -> N; an item with source tags -> the Evidence items of the
    same video (within 30 s when both have a time); title, scope and an untagged item -> all."""
    _, body, _ = verify_claims.split_frontmatter(note_text)
    ev = verify_claims.parse_body_full(body).sections.get("Evidence")
    evs = []
    for raw in ev.bullets() if ev else []:
        m = verify_claims.EVIDENCE_ITEM.match("- " + raw)
        evs.append((m.group(1), verify_claims.ts_to_seconds(m.group(2))) if m else (None, None))
    every = list(range(1, len(evs) + 1))
    out: dict[str, list[int]] = {}
    for it in items:
        name = it["item"]
        if name.startswith("evidence-"):
            out[name] = [int(name.split("-")[1])]
            continue
        tags = [(m.group(1), verify_claims.ts_to_seconds(m.group(2))) for m in verify_claims.SRC_TAG.finditer(it["text"])]
        if not tags or name in ("title", "scope", "lead"):
            # The lead (like the title) may combine several Evidence items of the note.
            out[name] = every
            continue
        # m7: an Evidence item with the SAME video and time as a tag wins; else within 30 s.
        hit = [k for k, (v, t) in enumerate(evs, 1) for tv, tt in tags if v == tv and t is not None and t == tt]
        hit = hit or [k for k, (v, t) in enumerate(evs, 1) for tv, tt in tags
                      if v == tv and (t is None or tt is None or abs(t - tt) <= 30)]
        out[name] = sorted(set(hit)) or [k for k, (v, _t) in enumerate(evs, 1) if v in {x for x, _ in tags}] or every
    return out


def review_targets(staging: Path, unit_dir: Path, zk_dir: Path) -> list[dict[str, Any]]:
    """Shadow notes that passed the mechanical check and need the source-check review."""
    check = _read_json(unit_dir / "check.json", None)
    if check is None:
        check = check_unit(staging, unit_dir, zk_dir)
    out = []
    for rel, c in sorted(check.items()):
        if c.get("needs_review"):
            out.append({"rel": rel, "shadow": str(unit_dir / "out" / rel), "hints": c.get("warnings", [])})
    info = _read_json(unit_dir / "unit.json", {})
    rn = info.get("review_note")
    if rn and rn not in check and (zk_dir / rn).is_file() and str(info.get("kind")) == "review" \
            and (info.get("options") or {}).get("review_pass") == "source-check":
        # Review mode: the agent did not change the assigned note; review the vault text.
        fm, _, _ = verify_claims.split_frontmatter((zk_dir / rn).read_text(encoding="utf-8", errors="replace"))
        if verify_claims.is_contract_note(fm) and not verify_claims.is_stub(fm):
            out.append({"rel": rn, "shadow": str(zk_dir / rn), "hints": []})
    return out


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #


def _sum_times(units: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    out: Counter = Counter()
    for e in units:
        for k, v in (e.get("times_s") or {}).items():
            out[k] += float(v or 0)
    res = {k: round(v, 1) for k, v in out.items()}
    try:
        started = _dt.datetime.fromisoformat(str(meta.get("started")))
        res["run"] = round((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds(), 1)
    except (TypeError, ValueError):
        pass
    return res


def stuck_notes(staging: Path, at_limit: bool = True) -> dict[str, list[dict[str, Any]]]:
    """Pilot 9 (round 22): `stuck_notes` = notes in pending-review folders AT the cycle limit
    (an operator re-arms with `pending --rearm`); with at_limit=False the other pending
    notes (`pending_notes`). Grouped by reason, with cycles."""
    out: dict[str, list[dict[str, Any]]] = {}
    for pdir in sorted((staging / "pending-review").glob("*--*")):
        meta = _read_json(pdir / "meta.json", {}) or {}
        if (int(meta.get("pending_cycles") or 0) >= MAX_PENDING_CYCLES) != at_limit:
            continue
        for note, why in (meta.get("reasons") or {}).items():
            out.setdefault(str(why), []).append({"note": note, "folder": pdir.name,
                                                 "pending_cycles": meta.get("pending_cycles")})
    return out


def _operator_pending(staging: Path) -> list[dict[str, Any]]:
    """Pending folders that waited MAX_PENDING_CYCLES times: the pending step leaves them
    for the operator (never deleted)."""
    out = []
    for pdir in sorted((staging / "pending-review").glob("*--*")):
        meta = _read_json(pdir / "meta.json", {}) or {}
        if int(meta.get("pending_cycles") or 0) >= MAX_PENDING_CYCLES:
            out.append({"folder": str(pdir), "notes": meta.get("notes", []), "reasons": meta.get("reasons", {}),
                        "pending_cycles": meta.get("pending_cycles")})
    return out


def pending_rearm(staging: Path, note: str) -> list[str]:
    """Operator: give a pending note a new set of cycles (pending_cycles back to 0). The
    note is a vault path ("01 Permanent Notes/X.md") or a title."""
    rel = note if note.endswith(".md") else f"{PERMANENT_DIR}/{note}.md"
    out = []
    with state_lock(staging):
        for pdir in sorted((staging / "pending-review").glob("*--*")):
            meta = _read_json(pdir / "meta.json", {}) or {}
            if rel in (meta.get("notes") or []):
                meta["pending_cycles"] = 0
                meta.setdefault("rearmed", []).append(_now())
                _write_json(pdir / "meta.json", meta)
                out.append(str(pdir))
    if not out:
        raise HarnessError(f"no pending-review folder holds {rel}")
    return out


def _sum_usage(units: list[dict[str, Any]]) -> dict[str, Any]:
    total: Counter = Counter()
    for e in units:
        for k, v in (e.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                total[k] += v
    out = dict(total)
    if "cost" in out:
        out["cost"] = round(out["cost"], 6)
    return out


def _preflight_forced_for(staging: Path | None, run_id: str) -> list[str]:
    """Z28 (round 25): the findings of a forced preflight (`ZR_PREFLIGHT_FORCE=1`) that ran
    just before this run started (the `preflight-forced` record between the previous
    `run-started` record and this run's one)."""
    if staging is None:
        return []
    last: list[str] = []
    for r in provenance.read_records(staging):
        if r.get("event") == "preflight-forced":
            last = [str(x) for x in (r.get("findings") or [])]
        elif r.get("event") == "run-started":
            if r.get("run_id") == run_id:
                return last
            last = []
    return []


def summary(run_dir: Path, staging: Path | None = None) -> dict[str, Any]:
    meta = _read_json(run_dir / "run.json", {"run_id": run_dir.name, "settings": {}})
    events = _read_jsonl(run_dir / "events.jsonl")
    units = [e for e in events if e.get("event") == "unit"]
    ends = Counter(e["end"] for e in units)
    failures: Counter = Counter()
    review_counts: Counter = Counter()
    created = merged = 0
    quarantined, conflicts, pending, needs_repair, commit_failed = [], [], [], [], []
    for e in units:
        v = (e.get("review") or {}).get("verdicts") or {}
        review_counts.update(v.get("counts") or {})
        for n in e.get("notes", []):
            rv = n.get("review") or {}
            review_counts.update(rv.get("counts") or {})
            if n.get("action") == "published":
                if n.get("existed_before"):
                    merged += 1
                else:
                    created += 1
            elif n.get("action") == "quarantined":
                quarantined.append({"note": n["note"], "unit": e["unit"], "ids": e["ids"],
                                    "failure_types": n.get("failure_types", []),
                                    "quarantine_path": n.get("quarantine_path")})
                failures.update(n.get("failure_types", []))
                if "conflict-stale-base" in n.get("failure_types", []):
                    conflicts.append(n["note"])
            elif n.get("action") == "pending-review":
                pending.append({"note": n["note"], "unit": e["unit"], "pending_dir": e.get("pending_dir")})
        if (e.get("review") or {}).get("needs_repair"):
            needs_repair.append({"note": e["review"]["note"], "failure_types": e["review"].get("failure_types", [])})
        if (e.get("commit") or {}).get("ok") is False:
            commit_failed.append({"unit": e["unit"], "paths": e["commit"].get("paths"), "error": e["commit"].get("error")})
    staging = staging or (Path(meta["staging"]) if meta.get("staging") else run_dir.parent.parent)
    items = _queue_items(staging)
    zk = Path(meta["vault"]) if meta.get("vault") else None
    out = {
        "run_id": meta.get("run_id", run_dir.name),
        "kind": meta.get("kind"),
        "started": meta.get("started"),
        "summarized": _now(),
        # P9: in single mode the synthesis model is the model that made the notes.
        "model": (Counter(m for e in units if e.get("synth_mode") == "single" for m in e.get("models_served", []))
                  .most_common(1) or [((meta.get("settings") or {}).get("model"),)])[0][0],
        "agent_model": (meta.get("settings") or {}).get("model"),
        "provider": (meta.get("settings") or {}).get("provider"),
        "models_served": sorted({m for e in units for m in e.get("models_served", [])}),
        "lit_dirs": meta.get("lit_dirs", []),
        "recovered_units": meta.get("recovered_units", []),
        "commits_retried": meta.get("commits_retried", []),
        "vault_audit": meta.get("vault_audit", {}),
        "units_run": len(units),
        "videos_processed": len({i for e in units for i in e.get("ids", [])}),
        "held_degraded": sorted({i for e in events if e.get("event") == "held-degraded" for i in e["ids"]}),
        "held_videos": [{"id": it["id"], "hold_reasons": it.get("hold_reasons") or [it.get("error")],
                         "asr_issues": it.get("asr_issues", [])}
                        for it in items if it.get("stage") == "held"],
        "notes_need_reverification": repair_candidates(staging, zk),
        "notes_created": created,
        "notes_merged": merged,
        "notes_quarantined": len(quarantined),
        "quarantined": quarantined,
        "notes_pending_review": pending,
        "notes_needs_repair": needs_repair,
        "publish_conflicts": conflicts,
        "commit_failed": commit_failed,
        "removals": [r for e in units for r in e.get("removals", [])],
        "links": [r for e in units for r in e.get("links", [])],
        "folds_refused": sorted({f for e in units for f in e.get("folds_refused", [])}),
        "verification_failures_by_type": dict(failures),
        "discarded_drafts": [{"note": n["note"], "unit": e["unit"], "path": n.get("discarded_path")}
                             for e in units for n in e.get("notes", []) if n.get("action") == "discarded-draft"],
        "review_unusable": [{"note": n["note"], "unit": e["unit"], "reason": n.get("review_unusable")}
                            for e in units for n in e.get("notes", []) if n.get("review_unusable")],
        "usage": _sum_usage(units),
        "review_calls_cost_unknown": sum(
            1 for p in run_dir.glob("units/*/review-*/agent_result.json")
            if (_read_json(p, {}) or {}).get("cost_unknown") and not list(p.parent.glob("late-reply-*.json"))),
        "budget": {"limit_usd": float(os.environ.get("ZR_MAX_COST_USD", "5")),
                   "cost_usd": run_cost(run_dir),
                   "stopped": (run_dir / "budget-stop.json").exists(),
                   "stop": _read_json(run_dir / "budget-stop.json", None)},
        "wall_time_s": _sum_times(units, meta),
        "base_urls_served": sorted({u for e in units for u in e.get("base_urls", [])}),
        "pending_reasons": dict(Counter(n.get("pending_reason") for e in units for n in e.get("notes", [])
                                        if n.get("action") == "pending-review")),
        "pending_for_operator": _operator_pending(staging),
        "notes_for_operator": operator_notes(zk) if zk is not None else [],
        "notes_rejected_twice": [{"note": n["note"], "unit": e["unit"], "quarantine_path": n.get("quarantine_path")}
                                 for e in units for n in e.get("notes", []) if n.get("for_operator")],
        "items_need_attention": sorted(it["id"] for it in items if it.get("stage") == "needs-attention"),
        "claims_uncovered": {e["unit"]: e["claims_uncovered"] for e in units if e.get("claims_uncovered")},
        "bullets_dropped": [dict(b, unit=e["unit"]) for e in units for b in e.get("bullets_dropped") or []],
        "calls_by_kind": _calls_by_kind(run_dir),
        # round 17: every merge for the operator; the full text of the dropped note is kept
        # under staging/merged/<run>/<unit>/
        "duplicates_merged": [dict(m, unit=e["unit"]) for e in units for m in e.get("duplicates_merged") or []],
        "possible_duplicates": possible_duplicates(staging, zk),
        "needs_operator": _read_json(staging / "needs_operator.json", []) or [],
        "unlinked": _read_json(staging / UNLINKED, []) or [],
        "exchange_unconsumed": _exchange_unconsumed(staging),
        # Round 15: videos whose voices the lit data does not name; add speakers.yaml, then respeak
        "speaker_unresolved_videos": [{"ids": e["ids"], "unit": e["unit"], "reasons": e["speaker_check"].get("reasons"),
                                       "found": e["speaker_check"].get("found")}
                                      for e in units if (e.get("speaker_check") or {}).get("status") == "unresolved"],
        "stuck_notes": stuck_notes(staging),
        "pending_notes": stuck_notes(staging, at_limit=False),
        "bullet_invariant_violations": bullet_invariant(staging, zk) if zk else [],
        # Round 11 (D): old notes replaced in part (open targets or bullets), and any old note
        # left unchanged beside a published replacement (must stay empty).
        "old_notes_partially_replaced": _read_json(staging / PARTIAL, {}) or {},
        "repair_bullets_open": repair_open_bullets(staging),
        "notes_marked_unsupported": marked_unsupported_notes(zk, staging) if zk else [],  # round 24
        "preflight_forced": _preflight_forced_for(staging, str(meta.get("run_id") or run_dir.name)),  # Z28 (round 25)
        "repair_bullets": repair_bullet_counts(staging, zk) if zk else {},
        # Z8 (round 23): every `dropped: contradicts` bullet, for operator reading
        "repair_bullets_contradicts": (repair_bullet_counts(staging, zk).get("bullets_dropped") or {}).get(
            "contradicts the transcript", []) if zk else [],
        "operator_work": operator_work(staging, zk) if zk else [],
        "repair_notes_needs_repair": refresh_needs_repair(staging),
        "replacement_beside_old_note": replacement_beside_old(staging, zk) if zk else [],
        "repair_problems": {e["unit"]: e["repair_problems"] for e in units if e.get("repair_problems")},
        # Round 10: every video that ended without a note, with the reason.
        "videos_without_notes": [{"ids": e["ids"], "unit": e["unit"], "outcome": e.get("synth_outcome"),
                                  "detail": (e.get("detail") or "")[:200], "refused": e.get("synth_refused", [])}
                                 for e in units if e.get("synth_outcome") in ("no-output", "all-skipped",
                                                                              "all-refused", "budget-stop")],
        # Old notes of unknown source that no video's repair call took: for the operator.
        "repair_unclaimed": sorted({r for e in units for r in e.get("repair_unclaimed", [])}
                                   - {r for e in units for r in e.get("repair_handled", [])}),
        "checker_sha256": meta.get("checker_sha256"),
        "checker_changed_in_units": [e["unit"] for e in units if e.get("checker_changed")],
        "hints": sorted({e["detail"].split(". ")[0] for e in units if e.get("end") == "limit-no-output"}),
        "runs_ended": dict(ends),
        "runs_ended_by_limit": ends.get("limit", 0) + ends.get("limit-no-output", 0),
        "runs_ended_by_limit_no_output": ends.get("limit-no-output", 0),
        "runs_ended_by_timeout": ends.get("timeout", 0),
        "runs_ended_by_kill_or_crash": ends.get("killed", 0) + ends.get("crashed", 0),
        "runs_ended_by_error": ends.get("error", 0),
        "review_verdicts": dict(review_counts),
        "transcript_missing_reported": sorted({
            e["review"]["note"] for e in units if (e.get("review") or {}).get("transcript_missing")}),
        "incomplete_items": sorted({c["id"] for e in units for c in e.get("queue", []) if c["stage_after"] == "incomplete"}),
        "failed_items": sorted({c["id"] for e in units for c in e.get("queue", []) if c["stage_after"] == "failed"}),
        "queue_stages": dict(Counter(it.get("stage") for it in items)),
    }
    _write_json(run_dir / "summary.json", out)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _ids(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", allow_abbrev=False)
    s.add_argument("--staging", type=Path, required=True)
    ow = sub.add_parser("operator-work", help="round 22: print the count and list of operator work (exit code 7)")
    ow.add_argument("--staging", type=Path, required=True)
    ow.add_argument("--vault", type=Path)
    s.add_argument("--kind", default="synth")
    s.add_argument("--vault", type=Path, help="ZK_DIR: registry check, crash recovery, vault audit")
    u = sub.add_parser("unit-start", allow_abbrev=False)
    u.add_argument("--run-dir", type=Path, required=True)
    u.add_argument("--kind", default="synth", choices=["synth", "review", "clustering", "repair", "pending"])
    u.add_argument("--ids", default="")
    u.add_argument("--review-note")
    u.add_argument("--owner-pid", type=int, required=True, help="pid of the long-lived driver or worker process")
    u.add_argument("--label", default="")
    u.add_argument("--review-pass", help="review units: the pass (source-check, atomicity, linking)")
    u.add_argument("--allow-other-sources", action="store_true",
                   help="repair: accept Evidence from videos that are not known sources of the note")
    p = sub.add_parser("precheck", allow_abbrev=False)
    p.add_argument("--staging", type=Path, required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--ids", required=True)
    c = sub.add_parser("check", allow_abbrev=False)
    c.add_argument("--staging", type=Path, required=True)
    c.add_argument("--unit-dir", type=Path, required=True)
    c.add_argument("--vault", type=Path, required=True)
    t = sub.add_parser("review-targets", allow_abbrev=False)
    t.add_argument("--staging", type=Path, required=True)
    t.add_argument("--unit-dir", type=Path, required=True)
    t.add_argument("--vault", type=Path, required=True)
    rb = sub.add_parser("review-begin", allow_abbrev=False,
                        help="create the folder of one review agent for one note; prints the folder")
    rb.add_argument("--unit-dir", type=Path, required=True)
    rb.add_argument("--note", required=True, help="the note path relative to the zettelkasten")
    rb.add_argument("--file", type=Path, required=True, help="the text under review (shadow or vault file)")
    re_ = sub.add_parser("review-end", allow_abbrev=False, help="record how one review agent ended")
    re_.add_argument("--review-dir", type=Path, required=True)
    re_.add_argument("--agent-rc", type=int, required=True)
    ks = sub.add_parser("known-sources", allow_abbrev=False,
                        help="build <staging>/known_sources.json from the vault git history")
    ks.add_argument("--staging", type=Path, required=True)
    ks.add_argument("--vault", type=Path, required=True)
    rp = sub.add_parser("repair-plan", allow_abbrev=False)
    rp.add_argument("--staging", type=Path, required=True)
    rp.add_argument("--vault", type=Path, required=True)
    rp.add_argument("--note", required=True)
    rp.add_argument("--allow-other-sources", action="store_true")
    nu = sub.add_parser("note-unsupported", help="round 23 (Z11): list an old note without a source transcript")
    nu.add_argument("--staging", type=Path, required=True)
    nu.add_argument("--vault", type=Path, required=True)
    nu.add_argument("--note", required=True)
    nu.add_argument("--reason", default="source transcript missing", choices=MARK_REASONS)
    mf = sub.add_parser("mark-finished", help="round 25 (Z-A2): mark or list each finished all-dropped plan note")
    mf.add_argument("--staging", type=Path, required=True)
    mf.add_argument("--vault", type=Path, required=True)
    no_ = sub.add_parser("note-operator", help="round 23 (Z6): list the plan's operator notes in needs_operator.json")
    no_.add_argument("--staging", type=Path, required=True)
    no_.add_argument("--plan", type=Path, required=True)
    od = sub.add_parser("operator-done", help="round 23 (Z15): record an operator decision and clear the note's entries")
    od.add_argument("--staging", type=Path, required=True)
    od.add_argument("--note", required=True)
    od.add_argument("--reason", required=True)
    mu = sub.add_parser("mark-unsupported", allow_abbrev=False)
    mu.add_argument("--unit-dir", type=Path, required=True)
    mu.add_argument("--vault", type=Path, required=True)
    mu.add_argument("--note", required=True)
    mu.add_argument("--reason", default="source transcript missing")
    f = sub.add_parser("finalize", allow_abbrev=False)
    f.add_argument("--staging", type=Path, required=True)
    f.add_argument("--run-dir", type=Path, required=True)
    f.add_argument("--unit-dir", type=Path, required=True)
    f.add_argument("--ids", default="")
    f.add_argument("--agent-rc", type=int, required=True)
    f.add_argument("--vault", type=Path, required=True, help="the zettelkasten folder (ZK_DIR)")
    f.add_argument("--review-note", help="review mode: the assigned note (relative to --vault)")
    f.add_argument("--commit", help="git commit message; commits only the published paths")
    pu = sub.add_parser("pending-units", allow_abbrev=False)
    pu.add_argument("--staging", type=Path, required=True)
    pu.add_argument("--run-dir", type=Path, required=True)
    pu.add_argument("--owner-pid", type=int, required=True)
    pu.add_argument("--reason", help="only folders with a note pending for this reason (link-waiting)")
    for name in ("discard-drafts", "fix-report", "review-repair-message"):
        x = sub.add_parser(name, allow_abbrev=False)
        x.add_argument("--staging", type=Path, required=True)
        x.add_argument("--unit-dir", type=Path, required=True)
        x.add_argument("--vault", type=Path, required=True)
    rr = sub.add_parser("rereview", allow_abbrev=False, help="review a published or quarantined note again")
    rr.add_argument("--staging", type=Path, required=True)
    rr.add_argument("--vault", type=Path, required=True)
    rr.add_argument("--note", required=True)
    rr.add_argument("--model")
    pe = sub.add_parser("pending", allow_abbrev=False, help="list pending-review folders, or re-arm one note")
    pe.add_argument("--staging", type=Path, required=True)
    pe.add_argument("--rearm", help="a pending note (vault path or title): give it new pending cycles")
    bu = sub.add_parser("budget", allow_abbrev=False, help="exit 4 when the run cost passed ZR_MAX_COST_USD")
    bu.add_argument("--run-dir", type=Path, required=True)
    g = sub.add_parser("passages", allow_abbrev=False)
    g.add_argument("--staging", type=Path, required=True)
    g.add_argument("--vault", type=Path, required=True)
    g.add_argument("--note", required=True)
    g.add_argument("--note-file", type=Path, help="read this file (a shadow note) instead of the vault note")
    rc = sub.add_parser("repair-candidates", allow_abbrev=False)
    rc.add_argument("--staging", type=Path, required=True)
    rc.add_argument("--vault", type=Path)
    rs = sub.add_parser("repair-sources", allow_abbrev=False)
    rs.add_argument("--staging", type=Path, required=True)
    rs.add_argument("--note", required=True)
    rs.add_argument("--vault", type=Path)
    rg = sub.add_parser("registry", allow_abbrev=False)
    rg.add_argument("--vault", type=Path, required=True)
    rg.add_argument("--add", type=Path, required=True, help="a staging folder that feeds this vault")
    m = sub.add_parser("summary", allow_abbrev=False)
    m.add_argument("--run-dir", type=Path, required=True)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "operator-work":
            lines = operator_work(a.staging.resolve(), a.vault.resolve() if a.vault else None)
            print(len(lines))
            for ln in lines:
                print(ln)
            return 0
        if a.cmd == "start":
            print(start(a.staging.resolve(), a.kind, a.vault.resolve() if a.vault else None))
            return 0
        if a.cmd == "unit-start":
            opts: dict[str, Any] = {"allow_other_sources": True} if a.allow_other_sources else {}
            if a.review_pass:
                opts["review_pass"] = a.review_pass
            print(unit_start(a.run_dir, a.kind, _ids(a.ids), a.review_note, a.owner_pid, a.label, options=opts))
            return 0
        if a.cmd == "review-begin":
            print(review_begin(a.unit_dir, a.note, a.file.read_bytes()))
            return 0
        if a.cmd == "review-end":
            print(json.dumps(review_end(a.review_dir, a.agent_rc)))
            return 0
        if a.cmd == "known-sources":
            res = known_sources_build(a.staging.resolve(), a.vault.resolve())
            n = res["notes"]
            print(json.dumps({"file": str(a.staging / KNOWN_SOURCES), "notes": len(n),
                              "with_sources": sum(1 for v in n.values() if v["sources"])}))
            return 0
        if a.cmd == "repair-plan":
            print(json.dumps(repair_plan(a.staging.resolve(), a.vault.resolve(), a.note, a.allow_other_sources)))
            return 0
        if a.cmd == "note-unsupported":
            # round 24: the owner's mark (frontmatter keys only); a refusal goes to needs_operator
            rel = safe_note_rel(a.vault.resolve(), a.note) or a.note
            with vault_lock(a.staging.resolve(), a.vault.resolve()):
                ok = mark_unsupported_note(a.staging.resolve(), a.vault.resolve(), rel, a.reason)
            print(json.dumps({"note": rel, "marked": ok}))
            return 0
        if a.cmd == "mark-finished":
            with vault_lock(a.staging.resolve(), a.vault.resolve()):
                print(json.dumps(mark_finished(a.staging.resolve(), a.vault.resolve())))
            return 0
        if a.cmd == "note-operator":
            plan = _read_json(a.plan, {}) or {}
            for o in plan.get("operator") or []:
                needs_operator(a.staging.resolve(), [{"note": o["note"], "state": "operator"}],
                               f"operator: {o.get('reason', 'no known source')} (old note unchanged)")
            return 0
        if a.cmd == "operator-done":
            od_ = operator_done(a.staging.resolve(), a.note, a.reason)
            print(json.dumps(od_))
            if not od_["entries_removed"] and not od_["plan_closed"] and not od_.get("pending_quarantined_next_pass") \
                    and not od_.get("queue_failed"):
                print(f"operator-done: nothing matches {a.note!r} (no needs_operator entry, no plan note)", file=sys.stderr)
                return 1  # Z-C2 (round 25)
            return 0
        if a.cmd == "mark-unsupported":
            print(mark_unsupported(a.unit_dir, a.vault.resolve(), a.note, a.reason))
            return 0
        if a.cmd == "precheck":
            keep = precheck(a.staging.resolve(), a.run_dir, _ids(a.ids))
            print(",".join(keep))
            return 0 if keep else UNIT_NOT_OK
        if a.cmd == "check":
            res = check_unit(a.staging.resolve(), a.unit_dir, a.vault.resolve())
            # link-waiting is not for the agent to fix: the note waits for its sibling.
            bad = {rel: [f"{x['type']} at {x['where']}: {x['detail'][:300]}" for x in c["failures"]
                         if x["type"] not in WAIT_TYPES]
                   for rel, c in res.items() if not c["ok"]}
            bad = {k: v for k, v in bad.items() if v}
            print(json.dumps(bad, indent=1, ensure_ascii=False))
            return UNIT_NOT_OK if bad else 0
        if a.cmd == "review-targets":
            for tg in review_targets(a.staging.resolve(), a.unit_dir, a.vault.resolve()):
                hints = "; ".join(h["detail"] for h in tg["hints"])[:500]
                print(f"{tg['rel']}\t{tg['shadow']}\t{hints}")
            return 0
        if a.cmd == "finalize":
            ev = finalize(a.staging.resolve(), a.run_dir, a.unit_dir, _ids(a.ids), a.agent_rc,
                          a.vault.resolve(), review_note=a.review_note, commit_message=a.commit)
            short = {k: ev[k] for k in ("unit", "ids", "end", "transient", "unit_ok")}
            short["notes"] = [{k: n.get(k) for k in ("note", "action", "failure_types")} for n in ev["notes"]]
            print(json.dumps(short, ensure_ascii=False))
            if ev["end"] != "finished" or ev.get("no_change") or ev.get("review_no_verdict"):
                return UNIT_INCOMPLETE
            return 0 if ev["unit_ok"] else UNIT_NOT_OK
        if a.cmd == "pending-units":
            for unit in pending_units(a.staging.resolve(), a.run_dir, a.owner_pid, a.reason):
                print(unit)
            return 0
        if a.cmd == "passages":
            print(passages(a.staging.resolve(), a.vault, a.note, note_file=a.note_file))
            return 0
        if a.cmd == "repair-candidates":
            print(json.dumps(repair_candidates(a.staging, a.vault), indent=2, ensure_ascii=False))
            return 0
        if a.cmd == "repair-sources":
            print(",".join(repair_sources(a.staging, a.note, a.vault.resolve() if a.vault else None)))
            return 0
        if a.cmd == "registry":
            print(json.dumps(registry_add(a.vault, a.add), indent=2))
            return 0
        if a.cmd == "discard-drafts":
            print(json.dumps(discard_drafts(a.staging.resolve(), a.unit_dir, a.vault.resolve())))
            return 0
        if a.cmd == "fix-report":
            print(fix_report(a.staging.resolve(), a.unit_dir, a.vault.resolve()))
            return 0
        if a.cmd == "review-repair-message":
            print(review_repair_message(a.staging.resolve(), a.unit_dir, a.vault.resolve()))
            return 0
        if a.cmd == "rereview":
            print(json.dumps(rereview(a.staging.resolve(), a.vault.resolve(), a.note, a.model), indent=1))
            return 0
        if a.cmd == "pending":
            if a.rearm:
                print(json.dumps({"rearmed": pending_rearm(a.staging.resolve(), a.rearm)}))
                return 0
            rows = [{"folder": str(d), **{k: (_read_json(d / "meta.json", {}) or {}).get(k)
                                          for k in ("notes", "reasons", "pending_cycles")}}
                    for d in sorted((a.staging / "pending-review").glob("*--*"))]
            print(json.dumps(rows, indent=1, ensure_ascii=False))
            return 0
        if a.cmd == "budget":
            st = budget_status(a.run_dir)
            print(json.dumps(st))
            return 4 if st["over"] else 0
        if a.cmd == "summary":
            print(json.dumps(summary(a.run_dir), indent=2, ensure_ascii=False))
            return 0
    except HarnessError as exc:
        print(f"run_integrity: {exc}", file=sys.stderr)
        return UNIT_ALREADY_FINAL if "already finalized" in str(exc) else 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
