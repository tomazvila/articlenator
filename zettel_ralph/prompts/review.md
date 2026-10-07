# prompts/review.md — review-pass protocol (Phase C)

You are one fresh-context iteration running a SINGLE named pass. Worker ≠ checker: you are
auditing notes a different iteration wrote. For video notes, `NOTE_CONTRACT.md` is the
standard (verdicts: section 9). For tweet/article notes and for MOCs, `NOTE_TEMPLATE.md`
is the standard. Read for defects, not to confirm. For a dedup check use
`python3 index_query.py "<claim>" --speaker ...` — never open the index file.

The per-note passes (`atomicity`, `linking`) each operate on the **ONE note whose path is
in your invocation prompt** — not the whole vault.

## Rules for every pass
- Never write a number, a unit or a period that you did not copy from a transcript quote.
- Never remove `sources`, `scope`, source tags, `## Evidence`, `## Disagreement`, speaker
  names or video ids. They are required content.
- Never change `verification:`. Only the harness sets it.
- Never add a mechanism, an explanation or an example that the transcript does not
  contain.
- Never edit `provenance*`, `queue*.json` or `concept-index.json`.
- Never merge two notes from different speakers. Sibling notes with different speakers
  (or a different scope) are allowed, also when their titles are near duplicates.
- An old note (no `sources:`) is not changed in a review pass. The repair prompt
  (`prompts/repair_note.md`) handles it.

## pass: atomicity (single note)
- Contract notes: split only when the note holds two claims with a different scope.
  Never split a claim away from its condition. Each new note gets its own `sources`
  (only the videos its bullets cite), its scope, its tags, and copies of the Evidence
  items its bullets cite. Do not change any quote or number when you move a bullet.
  Run `index_add.py` for each new or renamed note. Rename file and H1 together.
- Old notes: do not split (repair handles them).
- Tweet/article notes: split a note with two claims; link the halves.

## pass: linking (single note)
- If this note is an orphan or dead-end, add ≥1 resolvable link (use `index_query.py`)
  with a short `— why it connects` clause.
- Do not add a link to another note's file. Record a missing backlink with
  `python3 note_link.py --from "01 Permanent Notes/<this>.md" --to "01 Permanent Notes/<other>.md" --kind related`.
- Remove link spam. A link is not a source; copy no content from the linked note.

## pass: clustering (vault-level, deterministic trigger)
- Input: `$STAGING/squeeze.json` (topic → count, has_moc, at_squeeze). For every topic
  where `at_squeeze` is true, create/refresh `00 Maps/MOC <Topic>.md` in the format of
  `NOTE_TEMPLATE.md` and the rules of `NOTE_CONTRACT.md` section 8: one heading per
  speaker, disagreeing pairs under `## Disagreements`, no merged claims.
- Then rebuild `Home.md` as a two-level domain index (domains → MOCs only).

## pass: mechanical (vault-level)
- Run/emulate `validate.py`. Fix every ERROR about form (frontmatter keys, broken
  wikilinks, orphans, wrong `type`, H1 ≠ filename).
- A near-duplicate title warning between notes of different speakers or scopes is not a
  defect. Do not merge or rename them.
- Do not fix a content ERROR (quote mismatch, number not in quote, missing scope) with
  new content. List it in the summary for the repair step.
- A missing `## Example From The Source` is correct when the speaker gives no example.
- Set `status: reviewed` only when `validate.py` reports no ERROR for the note.
- Append a short `03 Reviews/<Pass> Summary.md` (type: review).

## Clock out
Unless the invocation prompt forbids it, rewrite `$STAGING/STATE.md` with what changed and
what remains. Then stop.
