# DECISIONS — durable conventions (read every iteration; append only lasting rulings)

Long-horizon continuity across memoryless iterations. STATE.md is episodic and rewritten;
this file holds stable policy so iteration N+200 doesn't re-litigate what N settled. Keep it
short — only rulings that should bind future iterations. One line each.

## Canonical titles (recurring concepts → the agreed note title)
- <e.g. "context window scaling" -> [[Context Rot Degrades Quality Before The Window Fills]]>

## Fold-vs-split rulings
- <e.g. valuation calls per ticker are folded into one note per company, not per tweet>

## Cross-cutting patterns
- Opposing claims use the tension-note pattern (NOTE_TEMPLATE.md), never silent folding.
- Single-source/contested claims are `verification: unverified` until a 2nd independent
  source folds in.

## Intentional divergences from the source (Andrew Torba) vault — decided, do not "fix"
- Links MAY carry a `— why it connects` clause (the source vault uses bare links). Bare
  links remain acceptable; do not mass-rewrite.
- A `verification` frontmatter key is used (the source vault has none) because this corpus
  is contested/multi-source.

## Tag vocabulary — two levels (keep consistent)
- Broad domains (exactly one per note): ai, agents, markets, finance, semiconductors,
  hardware, software, geopolitics, language-learning, health, personal, crypto, science.
- Fine topics (>=1 per note; MOCs form around these): e.g. agent-harnesses,
  context-engineering, valuation, export-controls, spaced-repetition, ...
