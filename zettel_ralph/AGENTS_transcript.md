# AGENTS_transcript.md — synthesis iteration entry point (video transcripts)

You are one **fresh-context iteration** of a Ralph loop that turns ONE video transcript
into Obsidian notes. You persist state to disk and exit. The notes are used to plan
training: each note shows who said it, in which video, under which conditions, and the
exact words behind each number.

## Startup, in this order
1. Read `NOTE_CONTRACT.md` sections 1 to 7, 10 to 12 and 14 (sections 8, 9 and 13 are for
   the clustering, review and repair passes). It is the only normative definition of a note. Its
   examples are fictional.
2. Read `prompts/synthesis_transcript.md` (hard rules, tools, steps).
3. Read `$ZR_STAGING/STATE.md` and `$ZR_STAGING/DECISIONS.md` (canonical titles, earlier
   rulings). **`NOTE_CONTRACT.md` overrides any older convention in them** (for example
   "source-free" notes, "replace the speaker name", "synthesize the content before a
   loop", "fold nuances into an existing note").
4. Do NOT read the concept-index json. Dedup only with `index_query.py`.

## Your work unit
The invocation prompt gives `{cluster, kind, ids, lit_notes}` with `kind: "video"` and one
id. Read that one `lit_notes` file; do not open `queue.json`. You are allowed to read
other notes in the vault to decide a fold or a disagreement.

## Finish, in this order
1. `verify_claims.py` passes on each of your notes.
2. `index_add.py` for each note; `note_link.py` for each link to another note.
3. Each skipped passage recorded with `queue_mark.py --skip-passage`.
4. `queue_mark.py --stage synthesized --notes` with your final titles and every note
   you folded into (or `--stage skipped`).
5. A lasting ruling (a canonical title) goes as one line into `DECISIONS.md`; it agrees
   with the contract. Rewrite `STATE.md` compactly. Stop; never start a second unit.

## Hard constraints
- One video per iteration. You write the notes of this unit. You change an existing
  note only to fold into it: a CONTRACT note of the same speaker and scope (contract
  7.1), through your shadow copy. Never edit an old note (no `sources:`) or a note of
  another speaker.
- Every note has ≥1 resolvable wikilink with a context clause, to a note that exists.
- No duplicate contract note: if a contract note of the same speaker and scope holds the
  claim, fold (when allowed) or skip. An old note (no `sources:`) is not a duplicate.
- Always `verification: unverified`. One broad domain tag (`calisthenics`) and ≥1 fine
  topic tag.
- Never edit `provenance*`, `queue*.json` or `concept-index.json` by hand.

Input: `$ZR_STAGING/lit/vid-<id>.md`. Notes go to `01 Permanent Notes/`. Do not build
MOCs; the clustering pass does that.
