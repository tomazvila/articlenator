# AGENTS_transcript.md — synthesis iteration entry point (video transcripts)

You are one **fresh-context iteration** of a Ralph loop that converts staged **video
transcripts** into an Obsidian zettelkasten. You do **ONE work unit** (one video), persist
state to disk, and exit. The next iteration starts with no memory of you — only what you
write down survives. **What is not in these files does not exist.**

## Startup (clock in) — do this before anything else
1. `pwd`; confirm you are in the `zettel_ralph/` harness.
2. Read `staging/STATE.md` (compact running state) and `staging/DECISIONS.md` (durable
   conventions: canonical titles for recurring concepts, fold-vs-split rulings, the
   tension-note pattern). `$ZR_STAGING` resolves the staging dir (`echo "$ZR_STAGING"`).
   Read DECISIONS.md every time — it is how you stay consistent with iterations you can
   never see.
3. Read `prompts/synthesis.md` (the core per-unit protocol), `prompts/synthesis_transcript.md`
   (transcript-specific overrides — spoken content, skip sponsor/filler, ASR caveats), and
   `NOTE_TEMPLATE.md` (exact format + standards).
4. **Do NOT read the concept-index json directly** — it is large and grows every iteration;
   loading it is the context-rot trap this loop exists to avoid. Dedup ONLY via
   `python index_query.py "<claim>"`, which returns the closest existing concepts.
5. Run `python validate.py --vault "$ZK_DIR" --squeeze` for authoritative, on-disk topic
   counts (which themes are at a MOC squeeze point). Act on these numbers; do not eyeball.

## Your work unit is GIVEN to you
The invocation prompt contains your pre-selected unit as JSON: `{cluster, kind, ids,
lit_notes}` — for transcripts this is **one video** (`kind: "video"`, a single id, no
cluster). **Do not open `staging/queue.json`** (200+ items). Read only the one `lit_notes`
path listed for your unit, from the staging `lit/` dir.

## Do the work
Follow `prompts/synthesis.md` with the `synthesis_transcript.md` overrides: walk the whole
transcript → extract EVERY distinct reusable claim in your own words (drop sponsor reads,
intros/outros, "like & subscribe", banter, repetition, hype) → for each, `index_query.py`
to dedup → CREATE a new note, FOLD into an existing one, or (if it contradicts an existing
claim) make a TENSION note → add contextual wikilinks → if `--squeeze` flags a theme, build
/refresh its MOC. Treat ASR specifics (names, numbers, jargon) as unreliable: capture the
idea, not a suspicious exact figure.

## Finish (clock out) — required before you stop, in this order
1. Write/update notes under the vault folder (path is in the invocation prompt) using the
   exact `NOTE_TEMPLATE.md` format. Re-read each (verify step) before continuing.
2. For every concept you touched, run `python index_add.py --title ... --file ... --gist
   ... --tags ... --source <id> [--claim-inc]`. This updates the index AND records
   provenance keyed by note file — you never open the index yourself.
3. Mark the unit done via `python queue_mark.py --ids <id> --stage synthesized
   --notes "Title A;Title B"` (or `--stage skipped --reason "no reusable idea: <what>"`).
   Never open `staging/queue.json` yourself.
4. If you made a lasting structural call (a canonical title for a recurring concept, a
   fold-vs-split ruling), append one line to `staging/DECISIONS.md`.
5. **Rewrite** `staging/STATE.md` compactly (consolidate, do not append).
6. Stop. Do not start a second unit.

## Hard constraints (non-negotiable)
1. **WIP = 1.** One video per iteration. Never "also tidy up" other notes.
2. **Atomic notes.** One reusable claim per permanent note. A long video yields MANY notes;
   one fat note for an hour-long talk is the #1 defect.
3. **Own words.** Rephrase every claim in plain declarative English. Never paste transcript
   spans or lightly reword them (the collector's fallacy).
4. **No orphans.** Every permanent note has ≥1 resolvable wikilink. Prefer a short context
   clause (`- [[Note]] — why it connects`); an unexplained pile of links is not acceptable.
5. **No raw provenance backlinks.** Never put youtube.com/youtu.be URLs, video ids, or
   "via <channel>" in a note. Provenance lives only in `staging/provenance.json` (keyed by
   note file, written via `index_add.py`).
6. **Declarative claim titles.** Titles state a claim, are unique, and are the filename.
7. **Dedup before create.** Always `index_query.py` first; prefer folding a detail into an
   existing note over a near-duplicate. Reuse canonical titles from `DECISIONS.md`.
8. **Match the template exactly** (`NOTE_TEMPLATE.md`) — frontmatter, sections, folders.
9. **Idempotent.** If the unit is already `synthesized`, pick another. Never duplicate a
   note that already exists.
10. **Verify before clock-out.** Re-read each note; ensure it would pass `validate.py`
    (frontmatter, sections, a link, no provenance).
11. **Contradictions become tension notes.** If a claim contradicts an existing note, never
    fold it in or overwrite — make a tension note holding both sides with context.
12. **Hedge unverified/ASR-shaky claims.** Single-source/contested claims are
    `verification: unverified`; promote to `corroborated` once ≥2 independent sources fold
    in. Treat ASR-garbled specifics qualitatively or omit them.
13. **Two-level tags.** One broad domain tag (`ai`, `markets`, …) AND ≥1 fine topic tag.
    MOCs form around the fine tags; domains are indexed on Home.md.
14. **Never run scripts or spawn processes.** Do NOT run any `.sh` file, `bash`, `sh`, or
    `claude`, and never start a loop/worker/sub-agent. Your ONLY shell commands are the
    named `python3` helpers (`index_query.py`, `index_add.py`, `queue_mark.py`,
    `validate.py`). You are one unit of work — synthesize it and stop.

## Where things are
- Staged input: `<staging>/lit/vid-<id>.md`  ·  Queue: `<staging>/queue.json` (don't open it)
- Running state: `<staging>/STATE.md`  ·  Durable conventions: `<staging>/DECISIONS.md`
  (staging dir = `$ZR_STAGING`)
- Dedup: read `index_query.py "<claim>"`, write `index_add.py ...` (never open the index)
- Provenance: `<staging>/provenance.json` (written by `index_add.py`, keyed by note file)
- Output vault folder: the **absolute path is given to you in the invocation prompt**
  ("Write all notes under this folder: ..."). It also lives in `$ZK_DIR`.
  Subfolders: `00 Maps/`, `01 Permanent Notes/`, `02 Examples/`, `03 Reviews/`.
- Format + standards: `NOTE_TEMPLATE.md`  ·  Process: `prompts/synthesis.md` +
  `prompts/synthesis_transcript.md`
