#!/usr/bin/env python3
"""Render the overall-pipeline progress bar from live queue + vault state.

Overall % credits ingestion as half-done and synthesis as fully-done per text item
(articles + tweets; videos are out of the note pipeline). 40-cell bar.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))
Q = STAGING / "queue.json"
VAULT = Path(
    os.environ.get("ZK_DIR")
    or (Path.home() / "Documents" / "Themis 2.0" / "Twitter Bookmarks Zettelkasten")
)
CELLS = 40
SAMPLE = STAGING / ".progress_sample.json"
NWORKERS = int(os.environ.get("NWORKERS", "4"))
SYNTH_SEC = float(os.environ.get("SYNTH_SEC", "300"))  # est. per-item synthesis wall (1 worker)
WINDOW = 2700  # smooth rate over ~45 min


def _measure(synth: int, extr: int, failed: int, pend: int, total: int):
    """Phase-aware, smoothed ETA = (ingestion remaining) + (synthesis remaining).

    Rates are measured over a ~45-min sample ring (not one noisy window). Ingestion remaining
    uses the measured rate of items leaving 'pending'. Synthesis remaining uses the measured
    synth rate once it has started, else a fixed per-item estimate / NWORKERS."""
    now = time.time()
    try:
        ring = json.loads(SAMPLE.read_text())
    except (FileNotFoundError, ValueError):
        ring = []
    if not isinstance(ring, list):
        ring = []
    out_of_pending = synth + extr + failed  # items that have left 'pending'
    if not ring or now - ring[-1]["t"] >= 120:
        ring.append({"t": now, "c": out_of_pending, "s": synth})
        ring = ring[-15:]
        try:
            tmp = SAMPLE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(ring))
            tmp.replace(SAMPLE)
        except OSError:
            pass

    old = next((x for x in ring if now - x["t"] >= WINDOW), ring[0] if ring else None)
    ingest_rate = None
    if old and now - old["t"] > 30 and out_of_pending > old["c"]:
        ingest_rate = (out_of_pending - old["c"]) / (now - old["t"])

    # Synthesis is bursty (gated by session-limit resets), so a measured rate is meaningless
    # — it balloons during a stall. Use a stable per-item ACTIVE-WORK estimate instead; the
    # wall-clock is longer because of quota resets (labelled as such in the output).
    ing_rem = (pend / ingest_rate) if (pend > 0 and ingest_rate) else 0.0
    unsynth = max(0, total - synth)
    syn_rem = unsynth * SYNTH_SEC / max(1, NWORKERS)
    return now, ing_rem + syn_rem


def main() -> None:
    items = json.loads(Q.read_text())["items"]
    # The transcript pipeline has only videos; count them when no text item exists.
    text = [it for it in items if it["kind"] != "video"] or items
    total = len(text) or 1
    synth = sum(1 for it in text if it["stage"] == "synthesized")
    extr = sum(1 for it in text if it["stage"] == "extracted")
    failed = sum(1 for it in text if it["stage"] == "failed")
    pend = sum(1 for it in text if it["stage"] == "pending")
    incomplete = sum(1 for it in text if it["stage"] == "incomplete")
    held = sum(1 for it in text if it["stage"] == "held")
    pending_review = sum(1 for it in text if it["stage"] == "pending-review")
    quarantined = sum(len(it.get("quarantined_notes", [])) for it in text)
    notes = len(list((VAULT / "01 Permanent Notes").glob("*.md"))) if VAULT.exists() else 0
    mocs = len(list((VAULT / "00 Maps").glob("*.md"))) if VAULT.exists() else 0

    frac = (synth + 0.5 * extr) / total
    fill = round(frac * CELLS)
    bar = "▰" * fill + "▱" * (CELLS - fill)
    now, secs = _measure(synth, extr, failed, pend, total)
    h = int(secs // 3600)
    active = f"~{h}h" if h else f"~{int(secs // 60)}m"
    tag = "active work; wall-clock gated by session-limit resets" if pend == 0 else "to ingest+synthesize"
    print(f"Overall pipeline  {bar} {frac * 100:.0f}%")
    print(f"  text items {total} (videos excluded) · synthesized {synth} · ingested-queued {extr} "
          f"· pending {pend} · failed {failed} · incomplete {incomplete} · held {held} · pending review {pending_review} · quarantined notes {quarantined}")
    for it in text:
        if it["stage"] == "held":
            reasons = it.get("hold_reasons") or [it.get("error") or "no reason recorded"]
            print(f"  held {it['id']}: " + "; ".join(str(r) for r in reasons))
    replaced = [it["id"] for it in text if it.get("transcript_replaced_at")]
    if replaced:
        print(f"  transcript replaced after synthesis (notes need re-verification): {', '.join(replaced)}")
    runs = sorted((STAGING / "runs").glob("*/summary.json"))
    if runs:
        print(f"  last run summary: {runs[-1]}")
    print(f"  vault: {notes} permanent notes · {mocs} MOCs · ETA {active} ({tag})")


if __name__ == "__main__":
    main()
