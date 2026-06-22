#!/usr/bin/env python3
"""Render the overall-pipeline progress bar from live queue + vault state.

Overall % credits ingestion as half-done and synthesis as fully-done per text item
(articles + tweets; videos are out of the note pipeline). 40-cell bar.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
Q = HERE / "staging" / "queue.json"
VAULT = Path.home() / "Documents" / "Themis 2.0" / "Twitter Bookmarks Zettelkasten"
CELLS = 40


def main() -> None:
    items = json.loads(Q.read_text())["items"]
    text = [it for it in items if it["kind"] != "video"]
    total = len(text) or 1
    synth = sum(1 for it in text if it["stage"] == "synthesized")
    extr = sum(1 for it in text if it["stage"] == "extracted")
    failed = sum(1 for it in text if it["stage"] == "failed")
    pend = sum(1 for it in text if it["stage"] == "pending")
    notes = len(list((VAULT / "01 Permanent Notes").glob("*.md"))) if VAULT.exists() else 0
    mocs = len(list((VAULT / "00 Maps").glob("*.md"))) if VAULT.exists() else 0

    frac = (synth + 0.5 * extr) / total
    fill = round(frac * CELLS)
    bar = "▰" * fill + "▱" * (CELLS - fill)
    print(f"Overall pipeline  {bar} {frac * 100:.0f}%")
    print(f"  text items {total} (videos excluded) · synthesized {synth} · ingested-queued {extr} "
          f"· pending {pend} · failed {failed}")
    print(f"  vault: {notes} permanent notes · {mocs} MOCs")


if __name__ == "__main__":
    main()
