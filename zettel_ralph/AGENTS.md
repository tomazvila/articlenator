# AGENTS.md — synthesis iteration entry point (tweets and articles)

You are one **fresh-context iteration** of a Ralph loop that converts staged Twitter/X
literature notes into an Obsidian zettelkasten. You do **ONE work unit**, persist state to
disk, and exit. The next iteration starts with no memory of you — only what you write down
survives. **What is not in these files does not exist.**

Video transcript units (`kind: video`) do not use this file. They use
`AGENTS_transcript.md` and `NOTE_CONTRACT.md`.

## Startup (clock in) — do this before anything else
1. `pwd`; confirm you are in the `zettel_ralph/` harness.
2. Read `prompts/synthesis.md` (the per-unit process) and `NOTE_TEMPLATE.md` (format of
   tweet and article notes, MOCs, Home).
3. Resolve the staging directory from `$STAGING` (or `$ZR_STAGING`). Read
   `$STAGING/STATE.md` (compact running state) and `$STAGING/DECISIONS.md` (canonical
   titles, fold rulings, the tension-note pattern). If a line in them disagrees with
   `prompts/synthesis.md` or `NOTE_TEMPLATE.md`, those two files are correct.
4. **Do NOT read `staging/concept-index.json` directly** — it is large and grows every
   iteration. Dedup ONLY via `python3 index_query.py "<claim>" --kind tweet` (or
   `--kind article`), which returns the closest existing concepts.
5. Run `python3 validate.py --vault "$ZK_DIR" --squeeze` for on-disk topic counts.

## Your work unit is GIVEN to you
The invocation prompt contains your pre-selected unit as JSON: `{cluster, kind, ids,
lit_notes}` (WIP = 1). **Do not open `$STAGING/queue.json`** — it has 1,500+ items. Read only
the exact `lit_notes` paths listed for your unit. Do not substitute `staging/lit/` when the
listed path points at an isolated staging folder.

## Do the work
Follow `prompts/synthesis.md`: decompose into claims with their conditions → for each,
`index_query.py --kind tweet` to dedup → CREATE a new note, FOLD into an existing note when
it is the SAME claim, or make a TENSION note when it contradicts one → add contextual
wikilinks → if `--squeeze` flags a theme, build or refresh its MOC.

## Finish (clock out) — required before you stop, in this order
1. Write/update notes under the vault folder (path is in the invocation prompt) in the
   `NOTE_TEMPLATE.md` format. Re-read each before continuing.
2. For every concept you touched, run
   `python3 index_add.py --title "<H1>" --file "01 Permanent Notes/<H1>.md" --gist "<gist>" --tags "<tags>" --source <id>`
   (add `--claim-inc` for a fold). This updates the index and records the
   source id; you never open the index yourself.
3. Mark the unit done:
   `python3 queue_mark.py --ids <id1,id2> --stage synthesized --notes "<Title A>;<Title B>"`
   (or `python3 queue_mark.py --ids <id1,id2> --stage skipped --reason "no reusable idea: <claim>"`).
4. If you made a lasting structural call (a canonical title, a fold ruling), append one
   line to `$STAGING/DECISIONS.md`.
5. Unless the invocation prompt forbids it, **rewrite** `$STAGING/STATE.md` compactly.
6. Stop. Do not start a second unit.

## Hard constraints (non-negotiable)
1. **WIP = 1.** One work unit per iteration. Never "also tidy up" other notes.
2. **One claim per note.** If a source has many points, each point becomes its own note
   or is folded into an existing note that holds the SAME claim.
3. **Faithful to the source.** Keep every number with its unit and period, and every
   condition that limits the claim. Never convert, round or combine a number. No
   mechanism, explanation or example that the source does not contain.
4. **No orphans.** Every permanent note has ≥1 resolvable wikilink, with a short context
   clause (`- [[Note]] — why it connects`) where it helps.
5. **Sources of tweet notes.** Do not put `x.com`/`twitter.com`/`t.co` URLs or
   "via @author" in the note body. `index_add.py --source <id>` records the source.
6. **Claim titles with their conditions.** Titles state a claim, are unique, and are the
   filename. A general title only when the source states a general rule.
7. **Dedup before create.** Always run `index_query.py --kind tweet` first. Fold only the
   SAME claim (not only shared words). Reuse canonical titles from `DECISIONS.md`.
8. **Contradictions become tension notes.** Never fold a contradiction in or overwrite —
   make a tension note that holds both sides with the source of each side. Never average.
9. **Verification field.** Always write `verification: unverified`.
10. **Two-level tags.** One broad domain tag (e.g. `ai`, `markets`, `geopolitics`) AND ≥1
    fine topic tag. MOCs form around the fine tags.
11. **Never run scripts or spawn processes.** Your ONLY shell commands are the named
    `python3` helpers (`index_query.py`, `index_add.py`, `queue_mark.py`, `validate.py`).
12. **Match the template** (`NOTE_TEMPLATE.md`) — frontmatter, sections, folders.
13. **Idempotent.** If the unit is already `synthesized`, pick another. Never duplicate a
    note that already exists.

## Where things are
- Staged input: exact `lit_notes` paths from the invocation, usually `$STAGING/lit/<id>.md`
- Running state: `$STAGING/STATE.md`  ·  Durable conventions: `$STAGING/DECISIONS.md`
- Dedup: `index_query.py "<claim>" --kind tweet`; record: `index_add.py ...`
- Sources: `index_add.py` writes the legacy `provenance.json`; the harness keeps the
  append-only `provenance.jsonl`. Never edit either file.
- Output vault folder: the **absolute path is given to you in the invocation prompt**
  ("Write all notes under this folder: ..."). It also lives in `$ZK_DIR`.
  Subfolders: `00 Maps/`, `01 Permanent Notes/`, `03 Reviews/`.
- Format: `NOTE_TEMPLATE.md`  ·  Process: `prompts/synthesis.md`
