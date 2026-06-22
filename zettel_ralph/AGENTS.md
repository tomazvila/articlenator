# AGENTS.md — synthesis iteration entry point

You are one **fresh-context iteration** of a Ralph loop that converts staged Twitter/X
literature notes into an Obsidian zettelkasten. You do **ONE work unit**, persist state to
disk, and exit. The next iteration starts with no memory of you — only what you write down
survives. **What is not in these files does not exist.**

## Startup (clock in) — do this before anything else
1. `pwd`; confirm you are in the `zettel_ralph/` harness.
2. Read `staging/STATE.md` (compact running state) and `staging/DECISIONS.md` (durable
   conventions: canonical titles for recurring concepts, fold-vs-split rulings, the
   tension-note pattern). DECISIONS.md is how you stay consistent with iterations you can
   never see — read it every time.
3. Read `prompts/synthesis.md` (the per-unit process) and `NOTE_TEMPLATE.md` (exact format
   + standards).
4. **Do NOT read `staging/concept-index.json` directly** — it is large and grows every
   iteration; loading it is the context-rot trap this loop exists to avoid. Dedup ONLY via
   `python index_query.py "<claim>"`, which returns the closest existing concepts.
5. Run `python validate.py --vault "$ZK_DIR" --squeeze` for authoritative, on-disk topic
   counts (which themes are at a MOC squeeze point). Act on these numbers; do not eyeball.

## Pick exactly ONE work unit
From `staging/queue.json`, pick the next unit whose `stage` is `extracted` (WIP = 1) by
its precomputed `cluster` id:
- an article/video is its own unit (`cluster` = `single-<id>`);
- tweets are already grouped into themed clusters (`cluster` = `cl-NNNN`) by `cluster.py`.

Read only that unit's literature note(s) from `staging/lit/`. **Never scan the corpus or
form clusters yourself** — clustering is precomputed.

## Do the work
Follow `prompts/synthesis.md`: decompose into atomic claims → for each, `index_query.py`
to dedup → CREATE a new note, FOLD into an existing one, or (if it contradicts an existing
claim) make a TENSION note → add contextual wikilinks → if `--squeeze` flags a theme, build
/refresh its MOC.

## Finish (clock out) — required before you stop, in this order
1. Write/update notes under the vault folder (path is in the invocation prompt) using the
   exact `NOTE_TEMPLATE.md` format. Re-read each (verify step) before continuing.
2. For every concept you touched, run `python index_add.py --title ... --file ... --gist
   ... --tags ... --source <id> [--claim-inc]`. This updates the index AND records
   provenance keyed by note file — you never open the index yourself.
3. In `staging/queue.json`, set the unit's item(s) `stage` to `synthesized` with
   `notes_emitted[]` (or `skipped` + reason if the source has no reusable idea).
4. If you made a lasting structural call (a canonical title for a recurring concept, a
   fold-vs-split ruling), append one line to `staging/DECISIONS.md`.
5. **Rewrite** `staging/STATE.md` compactly (consolidate, do not append).
6. Stop. Do not start a second unit.

## Hard constraints (non-negotiable)
1. **WIP = 1.** One work unit per iteration. Never "also tidy up" other notes.
2. **Atomic notes.** One reusable claim per permanent note. If a source has many points,
   each point becomes its own note or is folded into an existing thematic note.
3. **Own words.** Rephrase every claim in plain declarative English. Never paste tweet
   text or quotes as a note body (that is the collector's fallacy).
4. **No orphans.** Every permanent note has ≥1 resolvable wikilink. Prefer a short context
   clause (`- [[Note]] — why it connects`) — an intentional improvement over the source
   vault's bare links; a bare link is acceptable, an unexplained pile of links is not.
5. **No raw provenance backlinks.** Never put `x.com`/`twitter.com`/`t.co` URLs or
   "via @author" in a note. Provenance lives only in `staging/provenance.json` (keyed by
   note file, written via `index_add.py`).
6. **Declarative claim titles.** Titles state a claim (e.g. *"Harness Scaffolding Is the
   Agent Product"*), are unique, and are the filename.
7. **Dedup before create.** Always `index_query.py` first; prefer folding a detail into an
   existing note over making a near-duplicate. Reuse canonical titles from `DECISIONS.md`.
11. **Contradictions become tension notes.** If a claim contradicts an existing note,
    never fold it in or overwrite — make a tension note that holds both sides with context
    (see `NOTE_TEMPLATE.md`).
12. **Hedge unverified claims.** A single-source or contested claim is
    `verification: unverified`; promote to `corroborated` once ≥2 independent sources fold
    in. (Intentional addition over the source vault — useful for this contested corpus.)
13. **Two-level tags.** Every permanent note carries one broad domain tag (e.g. `ai`,
    `markets`, `geopolitics`) AND ≥1 fine topic tag. MOCs form around the fine tags; domains
    are indexed on Home.md.
8. **Match the template exactly** (`NOTE_TEMPLATE.md`) — frontmatter, sections, folders.
9. **Idempotent.** If the unit is already `synthesized`, pick another. Never duplicate a
   note that already exists.
10. **Verify before clock-out.** Re-read each note you wrote; ensure it would pass
    `validate.py` (frontmatter, sections, a link, no provenance).

## Where things are
- Staged input: `staging/lit/<id>.md`  ·  Queue: `staging/queue.json`
- Running state: `staging/STATE.md`  ·  Durable conventions: `staging/DECISIONS.md`
- Dedup: read `index_query.py "<claim>"`, write `index_add.py ...` (never open the index)
- Provenance: `staging/provenance.json` (written by `index_add.py`, keyed by note file)
- Output vault folder: the **absolute path is given to you in the invocation prompt**
  ("Write all notes under this folder: ..."). It also lives in `$ZK_DIR` (`echo "$ZK_DIR"`
  to resolve). Subfolders: `00 Maps/`, `01 Permanent Notes/`, `02 Examples/`, `03 Reviews/`.
- Format + standards: `NOTE_TEMPLATE.md`  ·  Process: `prompts/synthesis.md`
