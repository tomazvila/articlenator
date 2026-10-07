# DECISIONS — durable conventions (read every iteration; append only lasting rulings)

Long-horizon continuity across memoryless iterations. STATE.md is episodic and rewritten;
this file holds stable policy so iteration N+200 doesn't re-litigate what N settled. Keep it
short — only rulings that should bind future iterations. One line each.

## Canonical titles (recurring concepts → the agreed note title)
- <e.g. "context window scaling" -> [[Context Rot Degrades Quality Before The Window Fills]]>

## Fold-vs-split rulings
- <e.g. valuation calls per ticker are folded into one note per company, not per tweet>

## Cross-cutting patterns
- Video notes: `NOTE_CONTRACT.md` is the rule. It overrides any line in this file.
  Disagreements go into the NEW note's `## Disagreement` plus `note_link.py`.
- Tweet/article notes: opposing claims use the tension-note pattern (NOTE_TEMPLATE.md),
  never silent folding.
- Every note is written with `verification: unverified`. Only the harness changes it.

## Intentional divergences from the source (Andrew Torba) vault — decided, do not "fix"
- Links MAY carry a `— why it connects` clause (the source vault uses bare links). Bare
  links remain acceptable; do not mass-rewrite.
- A `verification` frontmatter key is used (the source vault has none) because this corpus
  is contested/multi-source.

## Tag vocabulary — two levels (keep consistent)
- Broad domains (exactly one per note): ai, agents, markets, finance, semiconductors,
  hardware, software, geopolitics, language-learning, health, personal, crypto, science,
  calisthenics.
- Fine topics (>=1 per note; MOCs form around these): e.g. agent-harnesses,
  context-engineering, valuation, export-controls, spaced-repetition, isometric-volume,
  training-frequency, training-to-failure, planche-progression, front-lever, ...
