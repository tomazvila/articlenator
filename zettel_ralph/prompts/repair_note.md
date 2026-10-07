# prompts/repair_note.md — repair ONE old note into contract form

You repair ONE note into the form of `NOTE_CONTRACT.md`: an old note (no `sources:`), or a
published note with `verification: needs-repair`. Read `NOTE_CONTRACT.md` first
(sections 1 to 8, 10 to 13; section 13 is the repair rule). Its examples are fictional.

## How your work reaches the vault
- Your writes go to a shadow work folder; nothing reaches the vault until the checks
  pass. You never rewrite the old note in place: you `move_file` it to a new title,
  and `move_file` leaves a stub at the old path.
- After you finish, the harness runs the mechanical check. If a note fails, you get the
  checker's JSON report and at most TWO fix turns: fix exactly the named items, or
  remove the item.
- Then the `source-check` review reads each note against the cited passages. A note is
  published only when every verdict is `supported` (`verification: source-checked`).
  You always write `verification: unverified`, except `needs-repair` (step 3). You
  NEVER write `unsupported`: the harness marks a note unsupported without an agent run
  when its source video has no transcript (`unsupported-not-allowed` fails).
- The harness rewrites and publishes the stub: it keeps every old bullet with a status
  line and may list parts that are still open (`superseded_pending`).

## Inputs (in the invocation prompt)
- The note path.
- The recorded sources of the note (`repair-sources`; the video that the note was
  created from decides). Quote ONLY from these videos.
  Never search other transcripts for a source that the prompt does not list.
- Canonical transcripts: `<staging>/transcripts.json` → `items[<id>].lit_note`. Never
  quote from `lit/_superseded/`.

## Procedure
1. List the old note's items: title, lead sentence, each Details bullet, the old
   `## Grounded Example`, "Why This Matters".
2. For each item, find the supporting passage in the listed transcripts. Copy the quote
   from the file.
3. If no item of the old note is supported by its transcript: keep the old text
   exactly. Change only the frontmatter (`replace_in_file`): set `verification:
   needs-repair` (replace the old value or add the line) and add
   `repair_note: "no supporting passage found"` (any other
   change fails: `needs-repair-body-changed`). Write the output record and stop; the
   operator takes the note. Never attach Evidence from an unlisted video.
4. Decide the new form: `sources`, `## Evidence` and source tags FIRST; `scope` (with
   `modality`) from words said in the passage; the speaker per passage (contract 3);
   numbers, units, periods, alternatives, limit words and hedges as in the quotes
   (contract 5.2, 5.3). Remove each item without a passage.
5. Old `## Grounded Example`: if a quote supports it, rename it
   `## Example From The Source` and tag it; else delete it.
6. Write the files with EXACTLY one of these tool sequences:
   The title is ALWAYS new and starts with the speaker; never the old title or file
   name, also when the claim did not change (`title-equals-old`). Never
   `write_file` at the old path.
   - **One new note:** (a) `move_file` from the old path to
     `01 Permanent Notes/<New Title>.md` (this leaves a stub at the old path);
     (b) `write_file` the full new note at the new path. Never write the new note
     before the `move_file`: `move_file` copies the old text over its target.
   - **Split into two notes (one per speaker or scope):** (a) `move_file` from the old
     path to `01 Permanent Notes/<New Title A>.md`; (b) `write_file` note A at that
     path; (c) `write_file` note B at `01 Permanent Notes/<New Title B>.md`; (d)
     `replace_in_file` on the stub at the old path: `superseded_by: "[[New Title A]]"`
     → `superseded_by: "[[New Title A]]; [[New Title B]]"`, and the line "This note
     was replaced by [[New Title A]]." → "This note was replaced by [[New Title A]]
     and [[New Title B]]."
   - A different value between the two halves: add `## Disagreement` to note B and run
     `python3 note_link.py --from "01 Permanent Notes/<New Title B>.md" --to "01 Permanent Notes/<New Title A>.md" --kind disagreement`.
7. Check each note you wrote and fix until exit code 0, or remove the failing item:

   ```
   python3 verify_claims.py "01 Permanent Notes/<Title>.md" --lit "$ZR_STAGING/lit" --repair
   ```

8. Record each note you wrote or changed:

   ```
   python3 index_add.py --title "<H1>" --file "01 Permanent Notes/<H1>.md" --repair
   ```

9. Write the output record (below).

## Output record
Write `$ZR_STAGING/repair/<old note file stem>.json`:

```json
{
  "old_note": "01 Permanent Notes/<Old Title>.md",
  "result": "renamed | split | needs-repair",
  "new_notes": ["01 Permanent Notes/<New Title>.md"],
  "items": [
    {"item": "details-3", "old_text": "<old bullet>", "verdict": "corrected",
     "quote": "<quote used>", "tag": "vid-<id> @ MM:SS"},
    {"item": "details-5", "old_text": "<old bullet>", "verdict": "removed",
     "reason": "no supporting passage"}
  ]
}
```

Item verdicts: `kept`, `corrected`, `moved-to:<new note file>`, `removed`.
