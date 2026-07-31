# prompts/synthesis_transcript.md — transcript-specific overrides

You are synthesizing a **video transcript** (Whisper ASR output), not a Twitter
article/tweet. Follow `prompts/synthesis.md` as the core protocol (decompose → dedupe →
write → cluster → record → verify → clock out) and `NOTE_TEMPLATE.md` for format, with the
adjustments below. Everything else (atomic notes, own words, dedup-before-create, tension
notes, contextual links, no provenance, index/queue helpers, WIP=1) is unchanged.

## Source shape
- Your work unit is **ONE video** (`kind: "video"`, a single id). There are **no clusters**
  for transcripts — synthesize the one `lit_notes` file you are given and stop.
- The note body is a long, **spoken** monologue/discussion: rambly, repetitive, and ASR-
  transcribed. Treat it as raw material, never as quotable prose.

## /decompose — what to keep vs. drop
- A long video yields **many** atomic claims. Walk the whole transcript; extract every
  distinct reusable idea/argument/prediction as its own declarative claim. Err strongly
  toward MORE notes — a single fat note for an hour-long talk is the #1 defect.
- **Drop pure non-content** (no reusable idea): sponsor reads / ads, "like and subscribe" /
  channel plugs, intros & sign-offs, greetings and banter, restated repetition, and pure
  hype. A skipped video must still name, in the `skipped` reason, what was discarded.
- **Keep opinions and predictions** — a stated view IS a reusable claim (`verification:
  unverified`). The goal is to not lose the thinking, only the filler.

## ASR caveat (important)
- Whisper mis-hears **proper nouns, product names, numbers, and jargon**. Do **not** treat
  any specific figure, name, or quote as authoritative. Capture the *idea*; if a claim hinges
  on an exact number/name that looks ASR-garbled, phrase it qualitatively (e.g. "a large
  majority" rather than a suspicious precise percent) or omit the shaky specific.
- Never paste transcript spans — spoken text reworded is still the collector's fallacy.
  Rewrite each claim as crisp declarative English.

## Everything else
Dedup via `index_query.py`, fold/create/tension exactly as `synthesis.md` says; one broad
domain tag + ≥1 fine topic tag; build an MOC at the ~5-note squeeze point; `index_add.py`
records provenance by note file; `queue_mark.py --stage synthesized`; rewrite `STATE.md`;
stop after the one video.
