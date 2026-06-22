# NOTE_TEMPLATE.md — exact output format (single source of truth)

`validate.py`, `prompts/synthesis.md`, and `prompts/review.md` all defer to this file.
Formats mirror the proven `Andrew Torba AI Programming Zettelkasten`.

## Folder layout (inside `"$ZK_DIR"`)
```
Home.md                     type: index
00 Maps/MOC <Topic>.md      type: map note
01 Permanent Notes/<Claim Title>.md   type: permanent note
02 Examples/Grounded Examples.md      type: example index
03 Reviews/<Pass> Summary.md          type: review
```

## Permanent note (the unit of work)
```markdown
---
type: permanent note
created: 2026-06-22
status: expanded
verification: unverified   # OPTIONAL: unverified (1 source/contested) | corroborated (>=2 sources)
tags:
  - <broad-domain>        # exactly one: ai | markets | geopolitics | language-learning | ...
  - <fine-topic>          # >=1 fine topic tag (MOCs form around these): e.g. agent-harnesses
  - zettelkasten
  - permanent-note
---

# <A Declarative Claim, Title Case, Unique, == filename>

<One sentence stating the claim in your own words.>

## Why This Matters
<1–2 sentences on the real consequence/stakes. Never restate the title or write
"`X` matters because…".>

## Details
- <Supporting point, own words.>
- <Supporting point.>
- <Nuance, condition, or counter-case.>

## Grounded Example
<A concrete, specific instance that makes the claim tangible. No quotes.>

## Connected Ideas
- [[Another Permanent Note]] — <why it connects: the link context sentence.>
- [[A Contrasting Note]] — <how it tensions or qualifies this one.>
- [[MOC <Topic>]]
```

### Rules for permanent notes
- **One claim.** If you wrote two claims, make two notes. Atomic notes stay short
  (~3–6 Details bullets, body well under ~220 words); `validate.py` warns on fat notes.
- **Grounded Example is mandatory** (a hard vault standard). If the source has no concrete
  instance, construct a plausible specific scenario — never empty, never a restatement.
- **Links:** ≥1 resolvable wikilink; prefer a `— why it connects` clause (an intentional
  improvement over the source vault's bare links). A bare link is allowed; link spam is not.
- **Own words** — never paste or lightly reword source text.
- **No provenance** in the body or frontmatter. (Source IDs go to `provenance.json`.)
- `status`: `expanded` when first written; review passes may promote to `reviewed`.
- Link to at least one `[[MOC <Topic>]]` once that MOC exists.

## Tension note (when two sources contradict)
Use when a claim contradicts an existing note. Never fold a contradiction into an asserting
note. The title names the disagreement; the body holds both sides fairly.
```markdown
---
type: permanent note
created: 2026-06-22
status: expanded
verification: unverified
tags:
  - <domain-tag>
  - zettelkasten
  - permanent-note
  - tension
---

# <Claim A And Claim Not-A Are Both Argued; The Disagreement Turns On X>

Two defensible positions exist on <topic>; which holds depends on <the axis X>.

## Why This Matters
<Why the disagreement matters / what it changes downstream.>

## Details
- The case for A: <own words.>
- The case for not-A: <own words.>
- The crux: <the assumption or evidence that decides between them.>

## Grounded Example
<A concrete situation where the axis X tips the answer one way.>

## Connected Ideas
- [[Note Asserting A]] — <its position and its strongest support.>
- [[Note Asserting Not-A]] — <its position and where it conflicts.>
- [[MOC <Topic>]]
```

## MOC / map note (built at the ~5-note squeeze point)
```markdown
---
type: map note
created: 2026-06-22
status: expanded
tags:
  - <domain-tag>
  - zettelkasten
  - map-note
---

# MOC <Topic>

Entry point for notes in this topic.

## <Sub-theme>
- [[Note A]] — <one-line context.>
- [[Note B]] — <one-line context.>
```

## Home.md (two-level domain index — list MOCs grouped by domain, NOT every note)
A multi-domain corpus would make a flat "list every note" Home unreadable. Home lists
domains → their MOCs only; never individual permanent notes.
```markdown
---
type: index
created: 2026-06-22
status: reviewed
tags: [zettelkasten, index]
---

# Twitter Bookmarks Knowledge Base

Reusable Zettelkasten concepts distilled from bookmarked threads and articles,
written source-free with no raw provenance backlinks.

## AI & Agents
- [[MOC Agent Harnesses]]
- [[MOC Context Engineering]]

## Markets
- [[MOC Semiconductor Supply Chain]]

## Geopolitics
- [[MOC <Topic>]]
```

## Folding a claim into an existing note (dedup path)
When `index_query.py` returns a matching concept:
- Add the new nuance as a bullet under `## Details`, or a new `## Connected Ideas` link
  with context — do **not** create a near-duplicate note.
- Record the source with `index_add.py --title "<that note>" --file "<its path>"
  --source <id> --claim-inc`; once `claim_count >= 2` promote `verification: corroborated`.
- If the existing note now carries two distinct claims, **split it** (atomicity wins).
