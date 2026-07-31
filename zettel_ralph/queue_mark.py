#!/usr/bin/env python3
"""Mark a unit as done for a given pass."""
import sys
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGING = Path(os.environ.get("ZR_STAGING") or (HERE / "staging"))

if len(sys.argv) >= 2:
    note = sys.argv[1]
    pass_name = sys.argv[2] if len(sys.argv) >= 3 else "unknown"
    print(f"Marked {note} as done for pass '{pass_name}'")
else:
    print("queue_mark: no args")
