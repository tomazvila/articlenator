# prompts/synthesis.md — per-unit synthesis protocol

The detailed process AGENTS.md routes you through, expressed as a protocol shell
(intent / input / process / output), adapted from
`Context-Engineering/20_templates/control_loop.py:ProtocolShell`.

```
/zettel.synthesize{
  intent="Turn ONE staged literature note (or one tweet cluster) into atomic, densely
          and contextually linked Obsidian permanent notes, deduped against what exists.",

  input={
    work_unit      = <given in the invocation prompt: {cluster, kind, ids, lit_notes}>,
    lit_notes      = <only the staging/lit/<id>.md paths listed for your unit>,
    concept_index  = <staging/concept-index.json: titles, aliases, gists, mocs>,
    template       = <NOTE_TEMPLATE.md>,
    state          = <staging/STATE.md>
  },

  process=[
    /decompose{
      action="Read the literature note. Extract EVERY distinct reusable idea as its own
              candidate claim, phrased as one declarative sentence in your own words.",
      rules=[
        "A claim is atomic if removing anything breaks it and nothing is missing.",
        "An article with N distinct points must yield N notes (new or folded) - emitting a
         single note for a multi-point source is a DEFECT (the user's #1 requirement). Err
         toward MORE atomic notes, never one fat note.",
        "Bias to capture (the user's goal is to NOT lose information): a stated opinion IS a
         reusable claim - keep it as a note (mark verification: unverified), don't discard.
         Skip ONLY pure filler with no idea ('great thread', emoji reactions, pure hype).",
        "If you skip an item entirely, the queue `skipped` reason must name the discarded
         idea, so nothing is lost without a trace."
      ]
    },
    /dedupe{
      action="For each candidate claim run `python index_query.py \"<claim>\"` (NEVER open
              concept-index.json yourself). It returns the closest existing concepts with a
              similarity score. Also query the claim's NEGATION to surface contradictions
              (opposing notes often share few words). Honour canonical titles in DECISIONS.md.",
      decide=[
        "SAME claim (not just shared words; same domain) -> FOLD: add nuance to that note's
                                      Details or a contextual Connected-Ideas link; run
                                      index_add.py --claim-inc. Do not duplicate.",
        "different domain / only lexical overlap -> treat as NO match (avoid Frankenstein
                                      notes stitched from unrelated sources).",
        "no close match           -> CREATE a new permanent note.",
        "several near-matches      -> CREATE a connective note linking them with context.",
        "CONTRADICTS an existing   -> make a TENSION note (NOTE_TEMPLATE.md): hold both
          claim                       claims, link both with context, name the axis of
                                      disagreement. Never fold a contradiction into an
                                      asserting note or overwrite it."
      ]
    },
    /write{
      action="Create/append notes per NOTE_TEMPLATE.md exactly.",
      rules=[
        "Own words only; never paste source text or lightly reword it (collector's
         fallacy). If a sentence could be found verbatim in the source, rewrite it.",
        "Every note MUST have a non-empty '## Grounded Example'. If the source gives no
         concrete instance, construct a plausible SPECIFIC scenario that instantiates the
         claim - never leave it empty, never just restate the claim.",
        "No orphans: >=1 resolvable wikilink. PREFER a short '- [[Note]] - why it connects'
         clause (an intentional improvement over the source vault's bare links); a bare
         link is acceptable, an unexplained pile of links is not.",
        "'## Why This Matters' states the real consequence; never restate the title or
         write '`X` matters because'.",
        "Declarative, unique, Title-Case claim titles (== filename).",
        "No x.com/twitter.com/t.co URLs or @handles anywhere in the note.",
        "Mark `verification: unverified` for a single-source or contested claim; promote to
         `corroborated` once >=2 independent sources fold in (claim_count >= 2)."
      ]
    },
    /cluster{
      action="If a topic now has >=5 permanent notes and no MOC (or an MOC that omits
              the new notes), create/refresh 00 Maps/MOC <Topic>.md and link the
              relevant notes to it. This is the 'mental squeeze point' trigger.",
      rule="MOCs are emergent - only build one when the squeeze point is reached."
    },
    /record{
      action="For every note touched run `python index_add.py --title ... --file ...
              --gist ... --tags ... [--aliases ...] [--moc ...] --source <id> [--claim-inc]`.
              This upserts the concept entry AND appends provenance keyed by note file. You
              never open concept-index.json or provenance.json.",
      rule="provenance.json (by note file) is the ONLY place sources are kept; vault stays
            source-free, but every note is traceable to its origins."
    },
    /verify{
      action="Re-read each note you wrote. Confirm: frontmatter present and typed;
              required sections present; >=1 contextual link; no provenance; title is a
              claim. Fix before clocking out. Use a DIFFERENT reading pass than writing.",
      ref="Context-Engineering/20_templates/PROMPTS/verification_loop.md"
    },
    /clock_out{
      action="`python queue_mark.py --ids <ids> --stage synthesized --notes '<titles>'`
              (never open queue.json); REWRITE staging/STATE.md compactly (counts, last 3
              decisions, themes near squeeze, next steps, blockers). Then STOP."
    }
  ],

  output={
    notes_written      = <list of permanent note files created/updated>,
    moc_touched        = <MOC file or none>,
    concept_index_delta= <concepts added/changed>,
    queue_delta        = <item ids -> synthesized>,
    state_rewritten    = true
  }
}
```

## Worked micro-example (shape only)
Source thread argues agent reliability comes from the harness, with three points.

- Decompose → 3 claims: (a) the harness, not the model, is the durable product; (b) the
  same model behaves differently under different scaffolds; (c) file-backed memory beats
  prompt wording for reliability.
- Dedupe → (a) matches existing `Harness Scaffolding Is the Agent Product` → **fold** a
  nuance bullet + append source id. (b),(c) are new → **create** two notes.
- Write → two new notes, each with thesis / Why / Details / Grounded Example / Connected
  Ideas (linking to (a) and to each other with context).
- Cluster → topic "agent-harnesses" now has 6 notes, no MOC → create
  `MOC Agent Harnesses`.
- Record → concept-index updated, all three source ids in `sources[]`.
- Clock out → queue item `synthesized`, STATE.md rewritten, stop.
