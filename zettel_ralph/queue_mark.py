#!/usr/bin/env python3
"""Mark synthesis units in queue.json, or record a skipped passage.

Called by the agent. These forms work:

    python queue_mark.py --ids <id> --stage synthesized --notes "Title A;Title B"
    python queue_mark.py --ids <id> --stage synthesized --notes "Title A" --notes-replace

`--notes` adds to the unit's note list in $ZR_RUN_DIR/marked.json (union over calls);
`--notes-replace` sets the list to exactly these titles.
    python queue_mark.py <id> skipped --note "<reason>"
    python queue_mark.py --ids <id> --skip-passage "<first 8 words>" [--at MM:SS] --reason "<why>"

`--skip-passage` appends {id, passage, at, reason, run_id, ts} to $ZR_STAGING/skips.jsonl
under the state lock and does not change the stage (NOTE_CONTRACT.md section 10).

Under the run harness, ZR_UNIT_IDS holds the ids of the unit that this run owns; any
other id is refused. The agent's mark is a claim: run_integrity.py finalize sets the
final stage after it verifies the notes.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _lock import state_lock  # noqa: E402

STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
Q = STAGING / "queue.json"
# Stages an agent may set. `incomplete`, `held` and `claimed` belong to the harness.
AGENT_STAGES = ("synthesized", "skipped", "failed")


def _save(q: dict) -> None:
    tmp = Q.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(q, indent=2, ensure_ascii=False))
    tmp.replace(Q)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mark a queue unit or record a skipped passage", allow_abbrev=False)
    ap.add_argument("pos_ids", nargs="?", help="comma-separated unit id(s) (same as --ids)")
    ap.add_argument("pos_stage", nargs="?", help="target stage (same as --stage)")
    ap.add_argument("--ids", help="comma-separated unit id(s)")
    ap.add_argument("--stage", help="target stage: synthesized, skipped or failed")
    ap.add_argument("--notes", default="", help="semicolon-separated note titles emitted (added to the unit's list)")
    ap.add_argument("--notes-replace", action="store_true",
                    help="replace the unit's note list with --notes (default: union with earlier calls)")
    ap.add_argument("--reason", "--note", dest="reason", default="", help="failure/skip reason")
    ap.add_argument("--skip-passage", default=None, help="first words of a skipped passage")
    ap.add_argument("--at", default="", help="MM:SS of the skipped passage")
    a = ap.parse_args(argv)

    target_ids = {x.strip() for x in (a.ids or a.pos_ids or "").split(",") if x.strip()}
    if not target_ids:
        print("queue_mark: no valid ids provided", file=sys.stderr)
        return 2
    owned = os.environ.get("ZR_UNIT_IDS")
    if owned is not None:
        allowed = {x.strip() for x in owned.split(",") if x.strip()}
        foreign = sorted(target_ids - allowed)
        if foreign:
            print(f"queue_mark: ids {foreign} do not belong to this unit {sorted(allowed)}", file=sys.stderr)
            return 2

    if a.skip_passage is not None:
        if not a.reason:
            print("queue_mark: --skip-passage needs --reason", file=sys.stderr)
            return 2
        import provenance  # local: only this form needs it

        for vid in sorted(target_ids):
            provenance.append_jsonl(STAGING / "skips.jsonl", {
                "ts": _now(), "id": vid, "passage": a.skip_passage, "at": a.at, "reason": a.reason,
                "run_id": os.environ.get("ZR_RUN_ID", ""), "unit": os.environ.get("ZR_UNIT", ""),
            }, STAGING)
        print(f"queue_mark: recorded skipped passage for {sorted(target_ids)}")
        if not (a.stage or a.pos_stage):
            return 0

    stage = a.stage or a.pos_stage or "synthesized"
    if stage not in AGENT_STAGES:
        print(f"queue_mark: stage '{stage}' not in {AGENT_STAGES}", file=sys.stderr)
        return 2
    with state_lock():
        q = json.loads(Q.read_text())
        updated = 0
        for it in q["items"]:
            if it["id"] in target_ids:
                it["stage"] = stage
                it.pop("worker", None)
                it["marked_at"] = _now()
                if a.notes:
                    existing = it.get("notes_emitted", [])
                    for note in a.notes.split(";"):
                        n = note.strip()
                        if n and n not in existing:
                            existing.append(n)
                    it["notes_emitted"] = existing
                if a.reason:
                    it["error"] = a.reason
                updated += 1
        _save(q)
    run_dir = os.environ.get("ZR_RUN_DIR")
    if run_dir and a.notes:
        # The unit's final note list: finalize treats other shadow notes as discarded drafts.
        rec = Path(run_dir) / "marked.json"
        try:
            prev = json.loads(rec.read_text()) if rec.exists() else {}
        except ValueError:
            prev = {}
        titles = [t.strip().removesuffix(".md").split("/")[-1] for t in a.notes.split(";") if t.strip()]
        earlier = [str(t) for t in prev.get("notes", [])]
        # Cumulative: each call adds to the list (an existing note folded into counts too).
        notes = titles if a.notes_replace else list(dict.fromkeys(earlier + titles))
        # Keep the records of delete_draft and move_file (finalize does not count those titles).
        prev = {**{k: v for k, v in prev.items() if k in ("deleted", "renamed")},
                "stage": stage, "notes": notes, "at": _now(), "earlier": earlier, "replaced": a.notes_replace}
        rec.write_text(json.dumps(prev, indent=1, ensure_ascii=False))
        ri = sys.modules.get("run_integrity")
        if ri is not None:
            ri._invalidate_rename_index_for_marked(rec)
    print(f"queue_mark: marked {updated} item(s) as {stage}")
    return 0 if updated else 1


if __name__ == "__main__":
    sys.exit(main())
