# NOTE_TEMPLATE.md — folders, tweet/article notes, MOCs, Home

**Video transcript notes are defined only in `NOTE_CONTRACT.md`** (format, rules, fold
rule, disagreement, repair). This file does not repeat that format. It gives the folder
layout, the tweet/article note format, the MOC format and the Home format.

## Folder layout (inside `"$ZK_DIR"`)
```
Home.md                               type: index
00 Maps/MOC <Topic>.md                type: map note
01 Permanent Notes/<Claim Title>.md   type: permanent note
03 Reviews/<Pass> Summary.md          type: review
```

(`02 Examples/` is an old folder. Do not write to it.)

## Permanent note from a tweet or an article
```markdown
---
type: permanent note
created: 2026-06-22
status: expanded
verification: unverified
tags:
  - <broad-domain>        # exactly one: ai | markets | geopolitics | calisthenics | ...
  - <fine-topic>          # >=1 fine topic tag (MOCs form around these)
  - zettelkasten
  - permanent-note
---

# <A Claim With Its Conditions, Title Case, Unique, == filename>

<One sentence stating the claim and its conditions.>

## Why This Matters
<Only a consequence that the source states. If the source states none, omit this section.>

## Details
- <Supporting point from the source.>
- <Condition or limit that the source states.>

## Example From The Source
<Only if the source gives a concrete example. If not, omit this section.>

## Connected Ideas
- [[Another Permanent Note]] — <why it connects.>
- [[MOC <Topic>]]
```

Rules for tweet and article notes:
- One claim with its conditions per note.
- Only the source: no mechanism, number, explanation or example that it does not contain.
- Numbers as in the source, with unit and period. Never convert, round or combine.
- ≥1 resolvable wikilink with a context clause. A linked note is not a source.
- Source ids go to the index through `index_add.py --source <id>`; no `x.com`,
  `twitter.com` or `t.co` URLs in the body.
- `verification: unverified` when written. `status: expanded`; the mechanical review
  pass sets `reviewed`.

### Fold (old rule, tweets and articles only)
When `index_query.py --kind tweet` returns a note with the SAME claim (not only shared
words, same domain): add the new detail as a bullet under `## Details` or as a Connected
Ideas link, and run `index_add.py --title "<that note>" --file "<its path>" --source <id>
--claim-inc`. If the note now holds two claims, split it. `claim_count` does not change
`verification`.

### Tension note (tweets and articles)
When a claim contradicts an existing note, never fold it in. Write a tension note:

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

# <Source A Says X; Source B Says Not-X For <Scope>>

Two sources disagree on <topic> for <scope>.

## Details
- Source A: <its claim, its conditions, its numbers as written.>
- Source B: <its claim, its conditions, its numbers as written.>

## Connected Ideas
- [[Note Asserting A]] — <its position.>
- [[Note Asserting Not-A]] — <its position.>
```

Never average two values. Never decide which side is correct.

## MOC / map note (built at the ~5-note squeeze point)

Rules (also `NOTE_CONTRACT.md` section 8):
- Notes from more than one speaker or author: one `##` heading per speaker.
- Notes that disagree: a pair under `## Disagreements`, with both speaker names.
- A MOC line repeats or shortens the note title. It never merges claims of two speakers
  into one sentence and never adds a claim.
- One speaker only: sub-theme headings are allowed; the first line names the speaker.

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

Entry point for notes in this topic. Notes are grouped by speaker.

## <Speaker A>
- [[Note A1]] — <one-line context.>

## <Speaker B>
- [[Note B1]] — <one-line context.>

## Disagreements
- [[Note A1]] (<Speaker A>) vs [[Note B1]] (<Speaker B>) — <the quantity or rule that differs.>
```

## Home.md (two-level domain index — MOCs grouped by domain, NOT every note)
```markdown
---
type: index
created: 2026-06-22
status: reviewed
tags: [zettelkasten, index]
---

# <Vault Name>

<One sentence on what the vault holds. For a video vault: notes from video transcripts,
each with its speaker, its video and verbatim evidence.>

## <Domain 1>
- [[MOC <Topic>]]

## <Domain 2>
- [[MOC <Topic>]]
```
