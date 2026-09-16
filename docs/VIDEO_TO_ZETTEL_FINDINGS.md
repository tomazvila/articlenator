# Findings: single video → calisthenics zettelkasten (2026-09-15)

Reader: a fresh agent that runs the next video-to-zettelkasten job. This file lists
what worked, what broke, and the exact fixes. Do not relearn these lessons.

## The working recipe (end to end)

1. Download audio (inside `nix develop`, repo root has `cookies.txt`):

   ```
   yt-dlp --cookies cookies.txt --js-runtimes node --no-update \
     -x --audio-format wav --no-keep-video \
     --output "$JOB/videos/<video_id>/audio.%(ext)s" "<url>"
   ```

2. Transcribe:

   ```
   whisper-cli -m /nix/store/27cj9jgv586w5ix4kgf8yhb41jd4vpf4-ggml-large-v3.bin \
     -f "$JOB/videos/<video_id>/audio.wav" -otxt -of "$JOB/videos/<video_id>/transcript" \
     -np -t 8 -et 2.8 --suppress-nst --language en
   ```

   The nix store hash changes on a whisper/model rebuild. Resolve the model path
   with `ls /nix/store/*ggml-large-v3.bin` before use.

3. Write the job manifest at `$JOB/manifest.json`. `videos` MUST be a **list** of
   dicts, not a dict (`ingest_transcripts.py` iterates it directly; a dict yields
   `'str' object has no attribute 'get'`). Fields per video: `video_id`, `title`,
   `url`, `status: "complete"`, `error: null`, `upload_date` (YYYYMMDD or null).
   Top level: `job_id`, `url`, `channel_title`, `channel_handle`.

4. Phase A ingest into the EXISTING staging so new notes link to old concepts:

   ```
   export ZR_STAGING="$PWD/zettel_ralph/staging_transcripts"
   python zettel_ralph/ingest_transcripts.py --channels-dir /home/deploy/Downloads/twitter-articles/channels
   ```

5. Phase B synthesis (one unit, ~15 min for a 53-min video):

   ```
   VAULT="/srv/obsidian/vaults/Themis 2.0"
   ZK_FOLDER="Video Transcripts Zettelkasten"
   ZR_STAGING="$PWD/zettel_ralph/staging_transcripts"
   AGENTS_FILE="$PWD/zettel_ralph/AGENTS_transcript.md"
   DEEPSEEK_API_KEY="$(cat /home/deploy/dotfiles/open-router-deepkseek-api-key.txt)"
   DEEPSEEK_BASE_URL="https://openrouter.ai/api/v1"
   DEEPSEEK_MODEL="deepseek/deepseek-v4-flash"
   bash zettel_ralph/loop.sh
   ```

6. Phase C review. Do NOT use the serial `review_loop.sh` for a big vault. Use
   `review_parallel.sh` (see below).

## What broke, and the fix

### 1. Whisper hallucination loop (cost: ~30 wasted minutes)

Symptom: one line repeated hundreds of times in `whisper.log` (735 copies here),
and the leading timestamp in the log stops advancing. `-bs 3` alone did NOT stop
it (the earlier session used that; it failed here at minute 15 of the audio).

Fix: restart with `-et 2.8 --suppress-nst`. The raised entropy threshold makes the
decoder fall back to higher temperature instead of looping; non-speech suppression
handles the music segments that trigger loops. Detection one-liner:

```
grep -oP '^\[\d+:\d+:\d+' whisper.log | tail -1   # does it advance between checks?
grep -c "<some repeated line>" whisper.log         # hundreds = loop
```

### 2. Process management after a restart (killed the wrong PID once)

`pkill -f whisper-cli` from outside `nix develop` left an old process alive. When
restarting, list PIDs first, kill by exact PID, then verify with `ps` again. Do
not sort by CPU time to pick a victim — sort by start time or PID order.

Same for review runs: after `pkill`, a `timeout 1800` wrapper can survive and keep
its child agent alive. Always verify zero processes with `ps aux | grep` before
relaunching, else two agents race on the same queue claim.

### 3. OpenRouter drops SSL connections mid-run (fixed in code)

`deepseek_agent.py` died with `SSL: UNEXPECTED_EOF_WHILE_READING` on ~1 in 10
requests. One dropped request killed the whole agent run (up to 80 tool rounds of
lost work). Fixed in commit `4328e7d` (v0.5.9): `chat_completion` retries transient
`URLError`/`OSError` with exponential backoff (`TRANSIENT_ATTEMPTS=4`, cap 30 s).
HTTP errors still fail fast. Do not re-fix this. If a run still dies, just rerun
`loop.sh` — the queue is resumable and claims only `extracted` items.

### 4. The review loop is vault-wide and serial by default (cost: days)

`review_queue.py build` walks all notes in `01 Permanent Notes/`, not just new
ones. The serial `review_loop.sh` processes them one at a time: days of wall time.
Previous corpus runs never finished (0/404 notes had all passes before this run).

Use the parallel path instead:

```
export NREVIEW_WORKERS=16
bash zettel_ralph/review_parallel.sh
```

with the same env as Phase B. 16 workers finished the whole 412-note vault
(3 passes + clustering) in ~3.5 hours. Caveats:

- `review_parallel.sh` defaults `DEEPSEEK_MODEL` to a bare `deepseek-v4-flash`;
  export the full `deepseek/deepseek-v4-flash` or OpenRouter rejects it.
- It runs `review_queue.py build` first, which MERGES prior `passes_done` (safe to
  rerun; finished notes stay finished).

### 5. Parallel review leaves broken stubs (14 notes in one run)

The atomicity pass splits fat notes. Some splits wrote the new note with claim +
`## Why This Matters` + `## Connected Ideas` but NO `## Details` / `## Grounded
Example`. Strict validation then fails with `missing required section` errors.
Workers also truncated `## Why This Matters` in one case.

Handling that worked: after EACH pass finishes, run
`python zettel_ralph/validate.py --vault "$ZK_DIR"` and repair stubs before the
next pass starts. Repair with parallel subagents (4 agents x 3-4 notes, instructions:
add only the missing sections, keep section order Why This Matters / Details /
Grounded Example / Connected Ideas, 3-6 bullets, one concrete example paragraph,
no URLs, no "transcript"/"video" words). Four subagents fixed 14 notes in minutes.
Then commit in the vault repo (it has its own git).

### 6. Stale claims in review_queue.json

A killed serial loop leaves a claimed note under its worker name; parallel workers
only reclaim their own name. Before relaunching, release orphan claims:

```python
# for each item: delete claims[p] when p not in item["passes_done"]
```

Zero stale claims existed this time, but check.

### 7. Small traps

- `whisper-cli -otxt` writes the transcript file only at the END of the run. There
  is no streaming progress file; watch `whisper.log` timestamps instead.
- A `claimed` item with `attempts: 0` in queue.json (e.g. vid-OMDVJb88LGY) is
  harmless: `queue_claim.py` only claims `extracted` items. Leave it alone.
- Run every command inside `nix develop`; tools (`whisper-cli`, `ruff`, `pytest`)
  do not exist on the host.
- The vault folder has its own git repo. Commit after each repair so a bad edit
  stays revertible.

## Measured timings (this box: 16 cores, 62 GB RAM)

| Step | Time |
| --- | --- |
| yt-dlp audio (53-min video) | ~10 s |
| whisper large-v3, -t 8, whole file | ~90 min (~0.55x realtime) |
| Phase A ingest | seconds |
| Phase B synthesis, one video unit | ~15 min |
| Phase C parallel review, 412-note vault, 16 workers | ~3.5 h |

## Rule of thumb for the next run

1. Download + transcribe in the background; poll `whisper.log` timestamps every
   ~10 min and check for repeated-line loops.
2. Ingest, run `loop.sh` once for the new unit, watch for `ALL EXTRACTED ITEMS
   PROCESSED`.
3. Run `review_parallel.sh` with 16 workers. After each pass, validate, repair
   stubs with subagents, commit the vault.
4. End state to demand: `validate.py --strict` reports 0 errors, and the vault git
   log shows the review commits.
