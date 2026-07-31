#!/usr/bin/env python3
"""Parse a Claude session-limit reset time from agent output; print the epoch of the next
reset (or 0 if none found). Lets workers sleep until quota returns instead of hammering.

Handles messages like: "You've hit your session limit · resets 11:20pm (Europe/Vilnius)".
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta

txt = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
m = re.search(r"reset[s]?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", txt, re.I)
if not m:
    print(0)
    sys.exit(0)

h = int(m.group(1))
mn = int(m.group(2) or 0)
ap = m.group(3).lower()
if ap == "pm" and h != 12:
    h += 12
if ap == "am" and h == 12:
    h = 0

now = datetime.fromtimestamp(time.time())
target = now.replace(hour=h, minute=mn, second=0, microsecond=0)
if target <= now:
    target += timedelta(days=1)
print(int(target.timestamp()))
