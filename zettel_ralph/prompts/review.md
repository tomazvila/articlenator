# prompts/review.md — review-pass protocol (Phase C)

You are one fresh-context iteration running a SINGLE named pass. Worker ≠ checker: you are
auditing notes a different iteration wrote. Read `NOTE_TEMPLATE.md` (the standard) first.
Use a method different from how the note was written (read for defects, not to confirm).
For any dedup check use `python index_query.py "<claim>"` — never open the index file.

The per-note passes (`atomicity`, `linking`, `source-free`) each operate on the **ONE note
whose path is in your invocation prompt** — not the whole vault. Fix that note only.

## pass: atomicity (single note)
- If this note carries more than one claim (multiple theses, a `## Details` that is really
  two ideas, a title you cannot state in one sentence), split it into separate notes; link
  the halves with contextual links; run `index_add.py` for the new note(s).
- Tighten a weak title into a sharp declarative claim; rename file + H1 together.

## pass: linking (single note)
- If this note is an orphan or dead-end, add ≥1 resolvable link (use `index_query.py` to
  find the best targets) with a short `— why it connects` clause.
- Add a reciprocal link where a relationship should be mutual but isn't.
- Remove link spam (links that assert relatedness without explaining it).
- Do NOT mass-convert every existing bare link to a clause — add context only where it is
  missing and genuinely helps; bare links are acceptable.

## pass: source-free (single note)
- Remove any raw provenance backlink (`x.com`/`twitter.com`/`t.co` URL, `@handle`,
  "in this thread", "the author says"). Rephrase into a standalone own-words claim.
- Confirm the note reads as reusable knowledge, not a tweet summary. (Provenance stays only
  in `staging/provenance.json`.)

## pass: clustering (vault-level, deterministic trigger)
- Your authoritative input is `$STAGING/squeeze.json` (topic → count, has_moc, at_squeeze),
  computed from disk. For every topic where `at_squeeze` is true, create/refresh
  `00 Maps/MOC <Topic>.md`: group its notes under sub-themes with one-line context, link
  related MOCs. Do not create MOCs below squeeze.
- Then rebuild `Home.md` as a TWO-LEVEL domain index: domains → their MOCs only (never list
  individual permanent notes), so it stays readable across a multi-domain vault.

## pass: mechanical (vault-level)
- Run/emulate `validate.py`. Fix every ERROR (missing frontmatter/sections, broken
  wikilinks, orphan permanent notes, wrong `type`, H1 ≠ filename, near-duplicate titles).
- Promote fully-checked notes `status: expanded → reviewed`.
- Append a short `03 Reviews/<Pass> Summary.md` (type: review) noting what changed.

## Clock out (every pass)
Unless the invocation prompt forbids it, rewrite `$STAGING/STATE.md` with what changed and
what remains. The driver marks the note done for this pass and commits; if nothing needed
fixing, say so and stop.
