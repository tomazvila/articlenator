"""Explicit local integration check for exchange_bridge liveness after malformed review."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from tests.unit.test_consumed_wait_recovery import ConsumedWaitRecovery
import run_integrity as ri


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", type=Path, required=True,
                        help="path to the exact exchange_bridge.py consumer to exercise")
    args = parser.parse_args()
    bridge_path = args.bridge.resolve()
    if not bridge_path.is_file():
        raise SystemExit(f"bridge integration requires an existing --bridge path: {bridge_path}")
    spec = importlib.util.spec_from_file_location("exchange_bridge_under_test", bridge_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load exchange bridge: {bridge_path}")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    case = ConsumedWaitRecovery("test_real_review_malformed_derived_reply_refuses_and_propagates_key")
    case.setUp()
    try:
        note = case._seed_review_note()
        base, derived = case._seed_consumed_base_through_review_caller(note)
        case._post_reply(derived)
        (case.xdir / "replies" / f"{derived}.txt").write_text("malformed JSON")
        result = case._call_review(note)
        if result.get("end") != "waiting-for-reply":
            raise AssertionError(f"malformed review did not wait: {result}")
        pending = case._finalize_waiting_review(note, derived)
        if derived not in ri.folder_waits_keys(pending):
            raise AssertionError("pending folder dropped the derived wait key")
        if (case.xdir / "consumed" / f"{derived}.json").exists():
            raise AssertionError("malformed reply was marked consumed")

        local_run = case.tmp / "bridge-integration-run"
        staging_link = local_run / "zr" / "staging_radoslav_radev"
        staging_link.parent.mkdir(parents=True, exist_ok=True)
        staging_link.symlink_to(case.staging, target_is_directory=True)
        code_link = local_run / "code" / "zettel_ralph"
        code_link.parent.mkdir(parents=True, exist_ok=True)
        code_link.symlink_to(Path(ri.__file__).resolve().parent, target_is_directory=True)
        script = bridge.LIVE_WAIT_CODE.replace(repr(bridge.RUN), repr(str(local_run)))
        namespace: dict = {}
        exec(compile(script, str(bridge_path), "exec"), namespace)
        sources = namespace["live_wait_sources"]()
        live = bool(sources.get(derived)) and not (case.xdir / "consumed" / f"{derived}.json").exists() \
            and not ri.request_dead(case.staging, derived)
        if not live:
            raise AssertionError(f"malformed reply correction key is not bridge-live: {sources}")
        print(json.dumps({"status": "PASS", "bridge": str(bridge_path), "base_key": base,
                          "derived_key": derived, "live_wait_sources": sources[derived],
                          "pending_folder": pending.name}))
        return 0
    finally:
        case.tearDown()


if __name__ == "__main__":
    raise SystemExit(main())
