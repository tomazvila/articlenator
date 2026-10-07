# prompts/synthesis.md — per-unit synthesis protocol (tweets and articles)

This file is for work units with `kind: tweet` or `kind: article`. For `kind: video`, do
not use this file: follow `AGENTS_transcript.md`, `NOTE_CONTRACT.md` and
`prompts/synthesis_transcript.md`.

Tweet and article notes keep the old note format and the old fold rule
(`NOTE_TEMPLATE.md`). These rules are new for them too:
- Only the source. No mechanism, number, explanation or example that the source does
  not contain. The example section exists only when the source gives an example.
- Numbers exactly as written, with their unit and period. Never convert, round, combine
  or re-base.
- Keep the conditions that limit a claim (who, which tool, "in my case").
- `verification: unverified` always.

```
/zettel.synthesize{
  intent="Turn ONE staged literature note (or one tweet cluster) into atomic, linked
          Obsidian permanent notes that keep the source's conditions and numbers,
          deduped against what exists.",

  input={
    work_unit      = <given in the invocation prompt: {cluster, kind, ids, lit_notes}>,
    lit_notes      = <only the staging/lit/<id>.md paths listed for your unit>,
    concept_index  = <read only through index_query.py>,
    template       = <NOTE_TEMPLATE.md>,
    state          = <staging/STATE.md>
  },

  process=[
    /decompose{
      action="Read the literature note. List each distinct claim together with the
              conditions that limit it.",
      rules=[
        "One claim per note. Do not split a claim away from its condition.",
        "A stated opinion is a claim (verification: unverified).",
        "Skip filler (greetings, hype, emoji reactions). If you skip an item entirely,
         the queue `skipped` reason names the discarded idea."
      ]
    },
    /dedupe{
      action="For each claim run `python3 index_query.py \"<claim>\" --kind tweet` (or
              `--kind article`). Never open concept-index.json. Also query the claim's
              negation to find contradictions. Honour canonical titles in DECISIONS.md.",
      decide=[
        "SAME claim (not just shared words; same domain) -> FOLD: add the detail to that
          note's Details or a Connected-Ideas link; run index_add.py --claim-inc.",
        "different domain / only lexical overlap -> NO match.",
        "no close match -> CREATE a new permanent note.",
        "several near-matches -> CREATE a connective note linking them with context.",
        "CONTRADICTS an existing claim -> make a TENSION note (NOTE_TEMPLATE.md) that
          holds both claims with the source of each. Never average, never overwrite."
      ]
    },
    /write{
      action="Create/append notes per NOTE_TEMPLATE.md ('Permanent note from a tweet or
              an article').",
      rules=[
        "Write each claim in plain English; keep its numbers, units, periods and
         conditions as in the source.",
        "No x.com/twitter.com/t.co URLs or @handles in the note body. The source id goes
         to provenance through index_add.py.",
        "'## Example From The Source' only if the source gives an example.",
        "'## Why This Matters' only a consequence the source states; else omit it.",
        "No orphans: >=1 resolvable wikilink with a short context clause.",
        "Title Case, unique titles (== filename) that carry the claim's conditions."
      ]
    },
    /cluster{
      action="If a topic now has >=5 permanent notes and no MOC (or an MOC that omits
              the new notes), create/refresh 00 Maps/MOC <Topic>.md (NOTE_TEMPLATE.md).",
      rule="MOCs are emergent - only build one when the squeeze point is reached."
    },
    /record{
      action="For every note touched run
              `python3 index_add.py --title \"<H1>\" --file \"01 Permanent Notes/<H1>.md\" --gist \"<gist>\" --tags \"<tags>\" --source <id>`
              (add `--claim-inc` for a fold). Never open concept-index.json or
              provenance.json."
    },
    /verify{
      action="Re-read each note: frontmatter, sections, >=1 contextual link, numbers and
              conditions as in the source, no text beyond the source. Fix before
              clocking out."
    },
    /clock_out{
      action="`python3 queue_mark.py --ids <ids> --stage synthesized --notes \"<Title A>;<Title B>\"`;
              REWRITE staging/STATE.md compactly. Then STOP."
    }
  ],

  output={
    notes_written = <list of note files created/updated>,
    moc_touched   = <MOC file or none>,
    queue_delta   = <item ids -> synthesized or skipped>
  }
}
```
