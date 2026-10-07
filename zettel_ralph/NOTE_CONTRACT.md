# Note contract: transcript → permanent note

This file is the ONE normative definition of a permanent note made from a video
transcript (`kind: video`). Other files point here and do not repeat the format.
If a prompt, `NOTE_TEMPLATE.md`, `DECISIONS.md` or `STATE.md` disagrees with this
file, this file is correct.

Tweet and article notes (`kind: tweet | article`) do not use this contract. They
keep the old format and the old fold rule in `NOTE_TEMPLATE.md` and
`prompts/synthesis.md`.

**All examples in this file are FICTIONAL** (invented channels, people, video ids
`vid-EXAMPLE0001` to `vid-EXAMPLE0006`, and climbing or rowing topics). They show the
form only. Never copy their titles, numbers or link names into a real note.

Words in this file:

- **Transcript**: the literature file `lit/vid-<id>.md`.
- **Note**: a permanent note in `01 Permanent Notes/`.
- **Contract note**: a note with a `sources:` list in its frontmatter.
- **Old note**: a note without `sources:` (written before this contract).
- **Passage**: a continuous span of words in one transcript.
- **Source tag**: the text `[src: vid-<id> @ MM:SS]`.
- **Quantity**: a number together with its unit and its period.

## 1. Transcript input
<!-- anchor: transcript-input -->

Ingest writes this frontmatter (more keys are allowed):

```yaml
id: vid-<id>
source_url: https://www.youtube.com/watch?v=<id>
title: "<video title>"
channel: "<channel name>"
speakers: ["<speaker 1>", "<speaker 2>"]
speakers_source: channel-owner | title-mention | manual
asr_model: "<whisper model file name>"
asr_quality: ok | partial | degraded | unknown
asr_issues: ["minor: ...", ...]
asr_span_unit: timestamp | lit-body-line          # lit body line 1 = first line after the closing ---
asr_damaged_spans: [["MM:SS", "MM:SS"], ...]   # or ["lines A-B"] (lit body lines) without timestamps
asr_damaged_spans_detail: [{start_line, start_char, end_line, end_char, ...}]   # exact damaged text
timestamps: true | false
```

If `timestamps` is true, the body has `[MM:SS]` markers (`[H:MM:SS]` after one
hour). A passage has the time of the nearest marker before it.

Rules:

- Only `asr_quality` decides if a file is usable. If it is `degraded`, write no
  note. (The harness normally skips these files before any model call.) Do not
  decide on your own that a file is degraded.
- If `asr_issues` has an entry that starts with `minor:` (for example a short
  repetition loop), never quote from the looped span (the check fails such a
  quote). Use the rest of the file.
- If `asr_quality` is `partial`, the file is usable except the spans in
  `asr_damaged_spans`. Never quote from a damaged span: the verification step
  fails such a quote (`quote-in-damaged-span`).
<!-- agent-only -->
- Canonical transcript: `<staging>/transcripts.json` names the file to quote
  (`items[<id>].lit_note`). Files in `lit/_superseded/` (`canonical: false`) are
  history; never quote from them.
<!-- /agent-only -->
- `speakers` is a HINT, not proof: decide the speaker per passage.
- A channel with `channel_kind: podcast | interview` gives `multi_speaker: "unknown"` for a
  video whose title names one person: decide the speaker per passage.

## 2. Note frontmatter
<!-- anchor: frontmatter -->

```yaml
---
type: permanent note
created: YYYY-MM-DD
status: expanded
tags:
  - calisthenics          # one broad domain tag
  - <fine-topic>          # one or more fine topic tags
  - zettelkasten
  - permanent-note
sources:
  - id: vid-<id>
    url: https://www.youtube.com/watch?v=<id>
    speaker: "<speaker>"
    channel: "<channel>"
scope:
  skill: "<words of the speaker, or not stated>"
  level: "<words of the speaker, or not stated>"
  equipment: "<words of the speaker, or not stated>"
  basis: "speaker's own practice"
  modality: "recommendation"
  quantities:
    - value: "<number and unit as spoken>"
      period: "per week"
verification: unverified
---
```

Optional keys: `speaker_evidence`, `speaker_label` and `needs_review` (see Speaker).
`speaker_status` is written only by the harness (see Speaker).
<!-- agent-only -->
Repair keys: `superseded_by`, `repair_note` and (harness only) `unsupported_reason`
(see Repairing an old note).
<!-- /agent-only -->

| Field | Rule |
|---|---|
| `sources` | One entry per video that supports the note. Keys: `id` (`vid-<id>`), `url`, `speaker`, `channel`. Copy `url` and `channel` from the transcript. `speaker` follows the rules under Speaker. |
| `scope.skill`, `scope.level`, `scope.equipment` | Words that the speaker SAID in the cited passages (the skill, who it applies to, the equipment). Name the step or skill whenever the passage names it (for example the progression step the speaker talks about); `not stated` only when the passage names none. Not from the video title, not from other passages, not from your knowledge. |
| `scope.basis` | By speech act: `speaker's own practice` (what the speaker does or did, or an opinion marked "I think", "in my opinion", "my experience"); `general rule` (advice or a rule to the listener with no such mark); `reported third party` (the rule or practice of another person or group, named in the note); `single example` ("let's say", one case). |
| `scope.modality` | One of: `requirement`, `recommendation`, `option`, `prediction`, `observation`, `not stated` (see Speech act). |
| `scope.quantities` | One item per quantity in the note (never empty when the note has a number). `value`: number and unit as spoken. `period`: one of `per set`, `per session`, `per meal`, `per day`, `per week` (including the explicit phrase "one day of the week"), `per month`, `per cycle`, `every N weeks` (N as spoken, for example `every 2 weeks`), one-off windows `this week`, `this day`, `this month`, `for the week`, `for the month`, or `these N weeks/days/months` (for example `these two weeks`), and `over N weeks/days/months`, or finite year spans as spoken (`N years`, `N years and a half`, with optional `for`/`over`/`in`/`last`), for a total explicitly tied to that span (for example, `four weeks ... that means 16 workouts` → `over four weeks`), and `not stated`. When one spoken quantity has the explicit bounded-window alternative `for the week or for the month`, retain both alternatives in one period value joined by `or`. Recognize `for the week` and `for the month` only as periods on `workout(s)` quantities. These windows preserve source wording; do not assign them to other units or convert them to recurring rates such as `per week` or `per month`. A one-off window records when a single target applies; it is not a recurring rate. Normalize an explicit repeated meal or day limit to `per meal` or `per day` only when the surrounding conditional diet practice establishes that rate and the amount is bound to that meal or day. No quantity: `quantities: []`. |
| `verification` | Always write `unverified`. The harness sets every other value: `source-checked` when the mechanical check and the review pass, `failed`, `needs-repair`, `unsupported`, and the value of a stub. |
| `status` | Write `expanded`. The harness sets other values. |

Unknown values are always the string `not stated`. Never guess a value.

## 3. Speaker
<!-- anchor: speaker -->

1. Start from `speakers` in the transcript. It is a hint.
2. The video is a conversation when the title says "interview", "podcast",
   "w.", "with", "ft." or "Q&A", OR the text shows a second voice: questions and
   answers, "my guest", a person spoken to by name, "same here", "agreed". Then
   decide the speaker PER PASSAGE from the text.
3. In a video with `multi_speaker: true` or `"unknown"`, a note that names a
   speaker MUST have `speaker_evidence: "<verbatim quote near the passage that
   shows who speaks>"`: the host's question or turn that the answer follows, a
   name, "my guest". It is a DIFFERENT sentence from the Evidence quotes, never
   the claim itself. Without such a quote, write `not stated` (single-call:
   `unknown` with `speaker_label`). The same applies to a speaker who is not in
   `speakers`.
   The host's question that the guest answers IS valid evidence for the guest:
   "so mara how long do you rest between hard sessions" → `speaker: "Mara Kell"`,
   `speaker_evidence` = that question. For a statement of the HOST, the evidence is
   the host's own turn ("welcome to erg talk ..." or the host's name).
3a. A passage where two voices mix (an exchange: "same here", "right right", "so
   your point is ...") gets `not stated` (single-call: `unknown`). A phrase that the host says is never the
   guest's claim.
<!-- agent-only -->
4. If you cannot tell who speaks a passage, write `speaker: "not stated"` and add
   `needs_review: speaker`. Its title has no speaker name (see Title). Such a note
   never folds.
<!-- /agent-only -->
<!-- single-only -->
4. One-voice video: the speaker is the person that the transcript frontmatter names
   (the channel owner). Several voices: the speaker is the voice that states the
   claim, from the `SPEAKERS` list of the inventory (the inventory marks each claim's
   voice; the harness sets `speaker_label` when all claims of a note share one). If that voice has no name,
   write `speaker: "unknown"` and `speaker_label: S<n>` (its label in the list), and
   start the title with `Unknown Speaker`.
<!-- /single-only -->
5. Never give a guest's own practice to the channel owner, and never the reverse.
6. A model-written title such as "A Speaker In The … Video …" is refused. The key
   `speaker_status: unresolved` and the speaker value `"unresolved speaker, <channel>
   video"` (in `sources` and in each Evidence item) are written only by the harness,
   never by a model: when the transcript data cannot confirm the voices, the harness
   replaces the speaker and starts the title with `Unresolved Speaker In <Channel>
   Video` (no brackets). That prefix is the harness's mark, not a vague title.

## 4. Title
<!-- anchor: title -->

- The title carries the scope: skill, equipment, level, and the frame
  ("From Her Own Test", "In His Own Training").
- The title names the speaker when the speaker is known. With `reported third
  party`, it also names the person whose rule it is ("Ivo Marsh Reports That His
  Old Coach Rowed ...").
- If the speaker is unknown, the title starts with `Unknown Speaker` (single-call)
  and names no person; never "A Speaker In The … Video".
- The title verb agrees with `scope.modality` (see Speech act). An option ("you can
  add a rest day if you like") never becomes "Recommends" or "Requires".
- A number for a NAMED step or transition goes into the title WITH that name:
  "Requires 10 Seconds On The 15 Millimeter Edge Before The 10 Millimeter Edge".
  Never "before the next step" or "for any progression". Two named transitions
  give two notes.
- A number in the title has its unit and period, as in the Evidence quote.
- Keep every hedge, condition and limit ("only", "maximum") and the frame quantity
  IN THE TITLE too. "from three sessions a week, only one maximum two hard" →
  wrong "Advises One Or Two Hard Sessions"; right "Advises Only One, Maximum Two
  Hard Sessions Of Three Per Week", period `per week`.
- "I think" / "in my opinion" is never dropped: `basis: speaker's own practice`, or
  the lead says "X thinks that ...". Wrong "Recommends Long Rests"; right "Thinks
  Long Rests Help" or a lead "Ivo Marsh thinks that ...".
- Spoken number runs (also in the lead and Details): "A to B" only when the quote
  lists every value from A to B ("4, 5, 6 laps, 7 laps" → "4 to 7 laps"); "A or B"
  for two spoken values ("one two" → "one or two", "50 60" → "50 or 60"); a list
  that skips values stays a list with one shared unit at the end ("three, five, or
  even eight weeks" → "three, five or eight weeks"). Consecutive repeated-unit
  quantities may form one spoken list with a shared unit. Keep every spoken value:
  missing or added alternatives change the claim. The checker accepts these forms.
  A range never widens or narrows the spoken values. Never copy the raw run into a title.

## 5. Body
<!-- anchor: body -->

```markdown
# <Title>

<Lead sentence: claim with scope and modality.> [src: vid-<id> @ MM:SS]

## Details

- <one fact from one passage> [src: vid-<id> @ MM:SS]

## Evidence

- vid-<id> @ MM:SS (<Speaker>): "<verbatim quote>"

## Example From The Source        <- only if the speaker gives one

<the speaker's example> [src: vid-<id> @ MM:SS]

## Why This Matters               <- only reasons the speaker gave; else omit

<reason> [src: vid-<id> @ MM:SS]

## Disagreement                   <- see Disagreement

## Connected Ideas

- [[Other Note Title]] — <one line: how it relates>
```

Without timestamps, write `[src: vid-<id>]` and `- vid-<id> (<Speaker>): "..."`.

Allowed lines (the check enforces them):

| Part | Only these lines |
|---|---|
| frontmatter | the keys under "Note frontmatter" |
| `# <Title>` | one H1, equal to the file name |
| lead | one paragraph that ends with a source tag |
| `## Details` | `- <fact> [src: ...]` bullets |
| `## Evidence` | `- vid-<id> @ MM:SS (<Speaker>): "<quote>"` items |
| `## Example From The Source`, `## Why This Matters` | one paragraph that ends with a source tag |
| `## Disagreement` | `- This note: ...` and `- Other side: ...` lines |
| `## Connected Ideas` | `- [[<Title>]] — <one line>` |

No other section. No line that addresses a reader, a reviewer, a checker or a model.
No line that starts with `=====`.

### 5.1 Details and Evidence

- Every Details bullet, the lead sentence, the example and each reason end with
  a source tag. A tag needs an Evidence item of the same video at that marker.
- A bullet says only what its passage says. No added mechanism, explanation,
  number or condition.
- Evidence quotes are COPIED from the file, never retyped: same case, same
  punctuation, same ASR errors. At most 60 words. `...` joins two parts of one
  passage; each part is an exact copy.
- Garbled ASR words stay in the Evidence quote only. Never copy them into the
  title, the lead or a Details bullet; if the claim depends on them, skip the
  passage: write no note from it.
- A note cites the claim's OWN time. Single-call mode: for each claim that a note
  lists in `claims:`, at least one Evidence item lies from 15 s before to 60 s after
  that claim's marker (without timestamps: inside that claim's excerpt). A note that
  lists a claim it does not cite there does not cover it.
- Evidence uses only the videos in `sources`. A video that appears only in
  `## Disagreement` is not added to `sources`.
<!-- agent-only -->
- Link rule: `## Connected Ideas` has at least one link, in this order of choice:
  (1) a note that you write in this unit and that is really related; (2) an
  existing vault note that `index_query.py` returned and that is really related;
  (3) `[[Home]]` only when neither exists: links that all point to Home help no
  reader. Never a self-link, never a title that you have not seen.
<!-- /agent-only -->
<!-- single-only -->
- Link rule: `## Connected Ideas` has at least one link, in this order of choice:
  (1) a note of this reply that is really related; (2) a title from `EXISTING NOTES
  YOU MAY LINK TO` that is really related; (3) `[[Home]]` only when neither exists:
  links that all point to Home help no reader. Never a self-link.
<!-- /single-only -->
- The claim of a bullet comes from its quoted sentence or from the sentence
  directly before or after it in the same speaker turn. Quote the sentence that
  holds the claim.

### 5.2 Numbers, units and periods

How the mechanical check reads a quantity: a number (digits or number
words), then the first unit word in the next 3 words, then the nearest period
phrase in the same clause. So:

- Keep the number, its unit and its period in the SAME clause:
  "12 sets of hangs per week", not "12 sets of hangs, and that is per week".
- If two quantities have different periods, write them in separate sentences.
- Keep the form of the number: digits stay digits, words stay words. "twice" and
  "two times", "once" and "one time" count as the same quantity.
- Never convert, round, add, multiply, combine or re-base. "per week" never
  becomes "per session" or "each".
- Keep a one-off time window distinct from a recurring rate: "10 reps in these two weeks" has period `these two weeks`, not `every two weeks` or `per week`. A finite total may use `over N weeks` only when the source explicitly ties that duration to the amount (for example, a stated year-and-a-half total → `year and a half`; “four weeks ... that means 16 workouts” → `over four weeks`); do not infer a window from an unrelated nearby duration or arithmetic alone. If no window is clear, use `not stated`.
- Keep the whole range and every alternative: "60 to 90 seconds", never "60
  seconds"; "five or seven seconds", never "seven seconds".
- If the period is not clear, write `period: not stated` and no period in the
  bullet.
- If the speaker's own numbers do not agree, write both with tags. Do not
  explain the difference.
- Write ordinals and labels as the speaker says them ("rule number three", "the
  90 degree hold"); never add a count that the quote does not contain.

### 5.3 Speech act (modality) and limit words

The verb of the note agrees with the speech act of the quote:

| `modality` | Signal words in the quote | Verbs in the note |
|---|---|---|
| `requirement` | "you need", "make sure", "minimum", "must", "you have to", "don't ... until" | requires, needs |
| `recommendation` | "I recommend", "my advice", "I'd say", "don't do more than" | recommends, advises |
| `option` | "you can", "if you want", "it's okay to" | allows, says ... is an option |
| `prediction` | "you will", "you'll be ready", "it's possible to ... in one week" | predicts, expects |
| `observation` | "I train", "I got from", "it worked for me" | reports, trains, did |

- A prediction is never written as a requirement: "you will hang around five or
  seven seconds on the 15 millimeter edge the first time" predicts the first hang
  on the NEXT edge. It is not the benchmark to leave the current edge.
- "let's say", "for example", "imagine" mark an EXAMPLE, not a rule, when they
  govern the clause that the claim comes from. Put it in `## Example From The
  Source`, or write it with `basis: single example` and `modality: observation`.
  Never turn it into a general rule or prediction.
- Another person's or group's practice is not an instruction to the reader:
  "most sprinters I know rest five minutes" → wrong "Rest Five Minutes"; right
  "Reports That Most Sprinters He Knows Rest Five Minutes", `basis: reported third
  party`, `modality: observation`. The same for the speaker's own past plan.
- Keep every limit word and hedge of the quote ("only", "maximum", "minimum", "at
  least", "around", "up to", "maybe", "I think", "usually", "not always"); never ADD
  one.
- `scope.modality` is the speech act of the title and the lead sentence. Each
  bullet agrees with its own quote.

### 5.4 Bracketed corrections

- A bracket fixes ONE word that is a known ASR spelling error of a term:
  `plunge [planche]`, `pools [pulls]`. The term must appear correctly elsewhere in
  the same transcript or in the vocabulary list (planche, front lever, back
  lever, maltese, iron cross, victorian, tuck, straddle, pike, handstand,
  muscle-up, pull-up, dip, ring, RPE, bodyweight, toe, toes).
- Never use a bracket to add a missing word, a number, a unit or a meaning.
- If the passage that carries the claim is not readable, write no note from it
  and record it as a skipped passage.

### 5.5 `## Example From The Source` and `## Why This Matters`

- Example: only when the speaker gives a concrete example. Never invented. If
  there is none, the section is absent.
- Why This Matters: only reasons the speaker gave, with a tag. Else absent.

## 6. What a note must not contain
<!-- anchor: must-not-contain -->

- Knowledge from outside the transcript (physiology, mechanisms, numbers from
  other notes or from the video title only).
- An invented example, an invented scope value, an added limit word.
- Details from a second speaker.
- A `verification:` value other than `unverified`.

## 7. Dedup, fold and links
<!-- anchor: dedup-fold-links -->

### 7.1 Fold rule

A new passage goes into an existing note (a "fold") only when ALL are true:

1. The existing note is a contract note (it has `sources:`).
2. All its `sources` have the same speaker as the new passage, and that speaker
   is not `not stated`.
3. `skill`, `level`, `equipment`, `basis` and `modality` are equal after
   normalization (lower case, one space). The value `not stated` never equals
   anything, not even `not stated`.
4. The new passage changes no quantity in the note.

`index_query.py` prints `fold_check.fold_allowed` with the first three rules;
the agent checks rule 4. `index_add.py` refuses a fold that breaks rules 1 to 3
(exit code 3). In doubt, do not fold: create a new note.

When you fold: add the bullet with its own tag, its Evidence item, and the
video in `sources`.

### 7.2 Create and link

No fold: create a new note and link both in `## Connected Ideas`. Copy nothing from
the other note except an Evidence quote for `## Disagreement`. Never create a
second contract note for a claim that one of the same speaker and scope holds; an
old note is never a duplicate (see Old notes).

`note_link.py` kinds (`--from` is always your new note):

| Other note | Kind |
|---|---|
| contract note, same question, different value or opposite rule | `disagreement` (your note MUST have `## Disagreement`) |
| contract note, related | `related` |
| old note (no `sources:`) | `supersedes-candidate` (ONLY for an old note) |

### 7.3 Disagreement
<!-- anchor: disagreement -->

When the new passage gives a different value or the opposite rule for the same
question as another note:
<!-- single-only -->

Single-call mode: only between two notes of the same reply (the existing-notes list
holds no quotes). The second of the two notes gets the section; it copies the other
note's Evidence quote. Against a listed existing note, write no section: link the
note under `## Connected Ideas` ("— other speaker, different value").
<!-- /single-only -->

- The NEW note gets `## Disagreement`:

  ```markdown
  ## Disagreement

  - This note: <Speaker A> [src: vid-<a> @ MM:SS]: "<quote from this note's Evidence>"
  - Other side: [[Other Note]] (<Speaker B>) [src: vid-<b> @ MM:SS]: "<quote copied from the other note's Evidence>"
  ```

- Never average. Never decide which side is correct.
<!-- agent-only -->
- If the other note has no Evidence (an old note), write
  `- Other side: [[Other Note]] (older note without sources): <one line, no numbers>`.
- Do NOT edit the other note. Record the pair:
  `python3 note_link.py --from "01 Permanent Notes/<new>.md" --to "01 Permanent Notes/<other>.md" --kind disagreement`.
  The harness adds the backlink to the other note.
<!-- /agent-only -->

### 7.4 Old notes

When `index_query.py` returns an old note (`old_format: true`):

- Never edit it and never fold into it.
- Create a new contract note. In `## Connected Ideas` write
  `- [[Old Note]] — older note without sources`.
- Record the pair:
  `python3 note_link.py --from "01 Permanent Notes/<new>.md" --to "01 Permanent Notes/<old>.md" --kind supersedes-candidate`.

## 8. Topic maps (MOCs) (for the clustering pass; synthesis agents skip this section)
<!-- anchor: topic-maps -->

The format is in `NOTE_TEMPLATE.md` ("MOC / map note"). Rules:

- Notes from more than one speaker: one `##` heading per speaker.
- Notes that disagree: a pair under `## Disagreements`:
  `- [[Note A]] (Speaker A) vs [[Note B]] (Speaker B) — <what differs>`.
- One speaker only: sub-theme headings are allowed; the first line names the
  speaker.
- A MOC line never merges two speakers into one sentence and adds no claim.

## 9. Review verdicts
<!-- anchor: review-verdicts -->

The `source-check` review is ONE model call per note (system prompt
`prompts/source_check.md`). The harness sends the note, the cited passages with
90 s of context, checker hints and a JSON skeleton with one item per title, scope,
lead, Details bullet, example, reason and Evidence item; the model returns the
filled skeleton and the harness writes the verdict file. An item without a verdict
blocks publication. Verdicts, in order of precedence (if several apply,
report the first; list the others in `also`):

| # | Verdict | Meaning |
|---|---|---|
| 1 | `transcript-missing` | The cited transcript is not readable. |
| 2 | `no-tag` | The item has no source tag. |
| 3 | `quote-mismatch` | The Evidence quote is not an exact copy. |
| 4 | `invented-correction` | A bracket adds content or fixes more than one ASR word. |
| 5 | `not-in-source` | The quote does not support the item at all. |
| 6 | `wrong-speaker` | The speaker is not the person who says the passage. |
| 7 | `changed-number` | A number differs from the quote, or an alternative is dropped. |
| 8 | `changed-unit` | The unit differs. |
| 9 | `changed-period` | The period differs or is added. |
| 10 | `changed-modality` | The speech act differs (option or prediction written as recommendation or requirement, an example written as a rule). |
| 11 | `dropped-qualifier` | A limit word or hedge is lost, or one is added. |
| 12 | `changed-scope` | A scope value is not in the passage, a named step became "the next step", or a personal practice became a general rule. |
| 13 | `supported` | None of the above. |

The JSON also has `scope_complete` (all scope fields present and not guessed).
`scope_complete: false` sends the note to quarantine like a failed verdict.

## 10. No note and skip records
<!-- anchor: no-note -->

Write no note from a passage when it is:

- an anecdote, greeting, product promotion or story with no claim a reader
  applies to training;
- not readable (ASR damage in the words that carry the claim);
- in a looped span (`minor:` issue) or a damaged span (`partial`);
- without a verbatim quote that supports the claim.

<!-- agent-only -->
Record each skipped passage (works in parallel runs):

```
python3 queue_mark.py --ids vid-<id> --skip-passage "<first 8 words of the passage>" --at MM:SS --reason "<why>"
```

(Leave out `--at MM:SS` when the transcript has no timestamps.) If the whole file
gives no note, mark the unit: `python3 queue_mark.py --ids vid-<id> --stage skipped --reason "<why>"`.
<!-- /agent-only -->
<!-- single-only -->
Single-call mode: a claim of the request that gets no note has a line
`C<n> | <reason>` in `=====SKIPPED=====`. The reason is one of these exact strings,
copied as written, and only under its condition:

| Reason | Only when |
|---|---|
| `not a transferable training claim: <one sentence why>` | the claim type is `anecdote` or `other`, or the sentence says why the statement cannot guide anyone's training |
| `not a transferable training claim: no excerpt holds this claim` | the claim line shows `(no excerpt found: the claim time is not in the transcript)` and no other excerpt of the request holds the statement |
| `passage unreadable` | the excerpt words that carry the claim are garbled (the harness checks this and sends the claim again if the excerpt is clean) |
| `damaged span` | the excerpt lies in a damaged or `minor:` loop span |
| `mixed voices and no way to tell the speaker` | two voices mix and no turn shows who speaks |
| `already covered by [[<Title>]]` | the title is a link candidate or a note of this reply |

A prediction, an option, the speaker's own practice, a benchmark for a named step
and a technique cue are ALL note-worthy; never skip them as not transferable.
<!-- /single-only -->

## 11. Quote match rule
<!-- anchor: quote-match -->

- Whitespace runs (spaces, tabs, line breaks) become one space; timestamp
  markers are removed from the transcript. Everything else must match: case,
  punctuation, spelling, ASR errors.
- Bracketed corrections are removed before the comparison.
- `...` splits the quote into parts; each part matches in order.
- With `@ MM:SS`, the first word of the quote is within one marker of the cited
  marker.

## 12. How a note reaches the vault, and the self-check
<!-- anchor: flow -->

Two modes. **Single-call mode (default for videos):** one model call per video
writes all notes as text (`prompts/synth_single.md`); the harness writes the files,
runs the mechanical check, makes one fix call per failing note
(`prompts/fix_single.md`), runs the `source-check` review (see Review verdicts), and publishes
notes whose verdicts are all `supported`. **Agent mode (option):** an agent with
tools, as in the steps below.

<!-- agent-only -->
1. Your writes go to a shadow work folder, not to the vault. Nothing reaches the
   vault until the checks below pass.
2. Write one note per tool call. To change a title, use `move_file` on the note;
   never write a second file under the new name. To remove a draft of this unit,
   use `delete_draft`. A shadow note that is not in your `--notes` list is
   discarded as a draft; it is never published.
3. Before clock-out, run for each note (report-only; a vault path means your
   shadow copy):

   ```
   python3 verify_claims.py "01 Permanent Notes/<Title>.md" --lit "$ZR_STAGING/lit"
   ```

   Exit code 0 = pass, 1 = fail. Fix what the report names, or delete the claim
   and record a skipped passage.
4. `queue_mark.py --notes` lists your final notes (the titles after any rename)
   AND every existing note that you folded into, separated by `;`. Within one unit
   the lists of several calls add up.
5. After you finish, the harness runs the same check. If a note fails, you get the
   checker's JSON report and at most TWO fix turns: fix exactly the items that the
   report names, or delete the claim. Change nothing else. In a fix turn do NOT run
   `queue_mark.py --notes` again, unless a title changed (the list is cumulative).
6. Then the harness runs a `source-check` review of each note (see Review verdicts). A note
   is published only when every verdict is `supported` (`verification:
   source-checked`). You always write `verification: unverified`.
<!-- /agent-only -->

### 12.1 Check results and what to do
<!-- anchor: fix-actions -->

Every result of the mechanical check and every review verdict, with the one action
it asks for. Change only the named item. Never invent text to pass a check.

| Result | Do this |
|---|---|
| `quote-mismatch` | Copy the quote again from the passage, character for character. |
| `quote-too-long` | Shorten the quote to at most 60 words, or join two exact parts with `...`. |
| `ellipsis-hides-attribution` | A `...` skips a name, "he says" or a negation: quote the full span. |
| `invented-correction` | Remove the bracket, or keep it to one ASR word of a known term. |
| `quote-in-damaged-span` | Also for `minor:` loop spans: quote another passage, or drop the claim. |
| `number-not-in-quote` | Use only numbers of the quote: remove the number, or quote the sentence that has it. |
| `changed-number`, `range-mismatch` | Write the values exactly as the quote has them (see Title, spoken number runs). |
| `changed-unit`, `dropped-unit` | Use the unit of the quote. |
| `changed-period`, `dropped-period` | Use the period of the quote, in the same clause as the number. |
| `quantities-missing` | Add one `scope.quantities` item per number in the note. |
| `scope-not-in-quote`, `changed-scope` | A scope word must be in the Evidence quote or its sentence ±1: extend the quote, or write `not stated`. Keep the named step and the right `basis`. |
| `changed-modality` | Use the verb of the speech act of the quote (see Speech act). |
| `dropped-qualifier`, `dropped-hedge` | Put the hedge or limit word back, also in the title; remove one that the quote lacks. |
| `title-drops-frame` | WARNING: put the limit and the frame quantity into the title. |
| `not-in-source` | Remove the item, or rewrite it so it says only what the passage says. |
| `wrong-speaker` | Name the person who says the passage; if the name is unknown, write `not stated` with `needs_review: speaker` (single-call: `unknown` with `speaker_label`). |
| `speaker-evidence-missing`, `speaker-evidence-is-claim`, `speaker-evidence-no-cue` | Give a DIFFERENT sentence that shows who speaks (the host's question, a name), or write `not stated`. |
| `no-tag`, `no-evidence-for-tag` | Every item ends with a source tag, and each tag has an Evidence item of the same video and time. |
| `evidence-format` | Write each item as `- vid-<id> @ MM:SS (<Speaker>): "<quote>"`. |
| `not-contract` | Write the whole note in the form under "Note frontmatter" and "Body". |
| `unsafe-name`, `duplicate-name` | Choose another title: letters, digits, spaces and `,.'()&+!%=-` only; not the title of an existing note. |
| `secret-in-note`, `injection-suspect` | Remove the line (a key, or text that addresses a reviewer, checker or model). |
| `dead-link`, `self-link` | Use an allowed link (see the link rule under Details and Evidence). |
| `link-waiting` | WARNING: the target is a note of another unit in this run. Leave the link. |
| `transcript-missing`, `review-rejected-twice`, `needs-attention` | The harness keeps the note for the operator. Do nothing. |

A claim that cannot be made faithful:
<!-- single-only -->
reply `=====DROP=====` with one sentence.
<!-- /single-only -->
<!-- agent-only -->
delete the note with `delete_draft`. `delete_draft` and `move_file` update your
note list themselves.
<!-- /agent-only -->
A link to a sibling note that is not published becomes plain text; the harness lists
it for the operator and does not restore it later.

## 13. Repairing an old note (for repair runs; synthesis skips this section)
<!-- anchor: repair -->

<!-- agent-only -->
Prompts: `prompts/repair_single.md` (single-call mode: one call per video, the
harness writes files, stubs and archives) and `prompts/repair_note.md` (agent mode:
exact tool sequence). Both use the flow under "How a note reaches the vault".

- Sources: the harness lists the note's recorded sources (`repair-sources`) in the
  invocation prompt; the video that the note was created from decides. Quote only
  from those videos. Never search other transcripts for an unlisted source.
- Canonical transcript: `<staging>/transcripts.json` → `items[<id>].lit_note`.
  Never quote from `lit/_superseded/`.
- No transcript for the recorded source (for example the 20 notes of
  `vid-5oVYIU3l3lM`): the harness marks the note `verification: unsupported`
  without a model call. A model NEVER writes `unsupported`
  (`unsupported-not-allowed`).
- No bullet supported by the transcript: keep the old text exactly; only set
  `verification: needs-repair` and `repair_note: "no supporting passage found"`
  (any other change fails: `needs-repair-body-changed`). The operator takes these.
- Checker results of repair runs: `unsupported-not-allowed` and
  `unsupported-body-changed` (`unsupported` was written: restore the old text and
  the old value), `needs-repair-body-changed` (restore the old text exactly).
- Remove each bullet without a supporting passage; list it in the output record.
<!-- /agent-only -->
- Attach `sources`, Evidence and source tags FIRST, then split by speaker or claim.
- Correct numbers, units, periods, hedges, limit words and modality to the quotes;
  restore `scope`, `basis` and the speaker per passage.
- Read the candidate passages of each bullet AND search the whole transcript before
  you drop it. A bullet with a wrong number, unit, period, step or speaker is
  CORRECTED to the quote, never dropped. Drop a bullet only when no candidate and
  nothing else in the transcript states it, or when the transcript contradicts it.
- A bullet that merges two statements of the transcript may be split across two new
  notes.
<!-- single-only -->
- Single-call mode: every numbered bullet (`<old file>#bN`) is accounted for exactly
  once in the `=====BULLETS=====` block. A note with several source videos is
  repaired video by video: the harness passes a bullet that is not in this
  transcript to the note's next source video, so `dropped: not in this transcript`
  loses nothing. `dropped: contradicts the transcript` is FINAL: later videos are not
  asked, and the stub shows `dropped: contradicts the transcript (<video id>)`. Use it
  only when a passage states the opposite.
- When the bullet's passage lies in a damaged span, or the transcript is degraded or
  partial and the passage cannot be read: `dropped: damaged transcript`, never `not
  in this transcript`. The bullet stays open for the note's other videos and the
  operator.
<!-- /single-only -->
- Old `## Grounded Example`: keep it as `## Example From The Source` only when a
  quote supports it; else delete it.
- Title: a repaired claim is ALWAYS a new note with a NEW title that follows the
  rules under "Title" (it starts with the speaker). NEVER reuse the old note's title
  or file name, also when the claim did not change. A note with such a title
  (`title-equals-old`) is not written, no call fixes it, and its bullets stay open.
  The old note stays unchanged until the harness replaces it with a stub that holds
  all of its old text, so links keep working.
<!-- agent-only -->
- Always `move_file` FIRST (old path → new title; this leaves a provisional stub at
  the old path), THEN write the new note at the new path. Never write the new note
  before the `move_file`: `move_file` copies the old text over its target. Never
  `write_file` at the old path. The harness replaces every stub at publish with the
  format in "Stub of a repaired old note".
<!-- /agent-only -->
- A later review that finds a defect in a published note sets
  `verification: needs-repair`; the repair run takes such notes as input.

## 14. Worked examples (FICTIONAL)
<!-- anchor: worked-examples -->

All channels, people, ids and passages below are invented.

### 14.1 Own practice: per week vs per session (`vid-EXAMPLE0001`)

Ivo Marsh, "How I Plan My Erg Weeks":

> [02:10] In my own training I row three steady pieces a week.
> [02:15] Each session is 45 to 60 minutes, and I keep it at a heart rate where I can still talk.
> [02:24] Sometimes it is only two in race season, when the hard work takes over.

Wrong: "Steady Erg Work Should Be 45 To 60 Minutes Three Times A Week" (own
practice made a rule; hedge dropped).

```markdown
---
type: permanent note
created: 2026-10-04
status: expanded
tags: [calisthenics, endurance-training, zettelkasten, permanent-note]
sources:
  - id: vid-EXAMPLE0001
    url: https://www.youtube.com/watch?v=EXAMPLE0001
    speaker: "Ivo Marsh"
    channel: "Marsh Rowing"
scope:
  skill: "steady pieces"
  level: "not stated"
  equipment: "not stated"
  basis: "speaker's own practice"
  modality: "observation"
  quantities:
    - value: "three steady pieces"
      period: "per week"
    - value: "45 to 60 minutes"
      period: "per session"
verification: unverified
---

# Ivo Marsh Rows Three Steady Pieces A Week, Sometimes Only Two In Race Season, In His Own Training

In his own training Ivo Marsh rows three steady pieces a week, sometimes only two in race season. [src: vid-EXAMPLE0001 @ 02:10]

## Details

- He rows three steady pieces a week. [src: vid-EXAMPLE0001 @ 02:10]
- Each session is 45 to 60 minutes, at a heart rate where he can still talk. [src: vid-EXAMPLE0001 @ 02:15]
- Sometimes it is only two in race season, "when the hard work takes over". [src: vid-EXAMPLE0001 @ 02:24]

## Evidence

- vid-EXAMPLE0001 @ 02:10 (Ivo Marsh): "In my own training I row three steady pieces a week."
- vid-EXAMPLE0001 @ 02:15 (Ivo Marsh): "Each session is 45 to 60 minutes, and I keep it at a heart rate where I can still talk."
- vid-EXAMPLE0001 @ 02:24 (Ivo Marsh): "Sometimes it is only two in race season, when the hard work takes over."

## Connected Ideas

- [[Home]] — first note of this topic; no topic map yet.
```

Points: two periods, two sentences. "Rows" = observation (own practice, no advice).
The hedge is in the title too.

### 14.2 Disagreement between two speakers (`vid-EXAMPLE0002` vs `vid-EXAMPLE0003`)

Tomas Brink, `vid-EXAMPLE0002` @ 01:40: "I tell my athletes to leave at least 48
hours between two hard finger sessions." An EXISTING contract note by Mara Kell
(`vid-EXAMPLE0003`, an option with a condition) quotes: "i'm fine with hard finger
sessions on back to back days if you keep them under 20 minutes".

```markdown
---
type: permanent note
created: 2026-10-04
status: expanded
tags: [calisthenics, finger-training, zettelkasten, permanent-note]
sources:
  - id: vid-EXAMPLE0002
    url: https://www.youtube.com/watch?v=EXAMPLE0002
    speaker: "Tomas Brink"
    channel: "Tomas Brink Climbing"
scope:
  skill: "hard finger sessions"
  level: "my athletes"
  equipment: "not stated"
  basis: "general rule"
  modality: "recommendation"
  quantities:
    - value: "at least 48 hours"
      period: "not stated"
verification: unverified
---

# Tomas Brink Advises His Athletes To Leave At Least 48 Hours Between Two Hard Finger Sessions

Tomas Brink tells his athletes to leave at least 48 hours between two hard finger sessions. [src: vid-EXAMPLE0002 @ 01:40]

## Details

- He tells his athletes to leave at least 48 hours between two hard finger sessions. [src: vid-EXAMPLE0002 @ 01:40]
- The days in between are for easy mileage on big holds. [src: vid-EXAMPLE0002 @ 01:47]

## Evidence

- vid-EXAMPLE0002 @ 01:40 (Tomas Brink): "I tell my athletes to leave at least 48 hours between two hard finger sessions."
- vid-EXAMPLE0002 @ 01:47 (Tomas Brink): "The days in between are for easy mileage on big holds."

## Disagreement

- This note: Tomas Brink [src: vid-EXAMPLE0002 @ 01:40]: "I tell my athletes to leave at least 48 hours between two hard finger sessions."
- Other side: [[Mara Kell Allows Hard Finger Sessions On Back To Back Days If They Stay Under 20 Minutes]] (Mara Kell) [src: vid-EXAMPLE0003]: "i'm fine with hard finger sessions on back to back days if you keep them under 20 minutes"

## Connected Ideas

- [[Mara Kell Allows Hard Finger Sessions On Back To Back Days If They Stay Under 20 Minutes]] — the other side of the disagreement.
```

<!-- agent-only -->
Then `note_link.py --kind disagreement` to the Mara Kell note, which is not edited.
<!-- /agent-only -->
<!-- single-only -->
Single-call mode: the list of existing notes has no quotes, so `## Disagreement`
is written only between two notes of the same reply. Against a listed note, link it
in `## Connected Ideas` ("— other speaker, different value") and write no
Disagreement section.
<!-- /single-only -->
`basis: general rule`: "I tell my athletes" is advice to others.
"at least" and her condition stay; neither side is judged.

### 14.3 Prediction, named step, option, "let's say" (`vid-EXAMPLE0002`)

> [03:10] After about six weeks on this plan, most people will add one grade to their hardest boulder.
> [03:40] Do not start one-arm hangs until you can hold 20 seconds two-armed on the 20 millimeter edge.
> [04:05] You can add a fourth session in the off-season if you want.
> [04:30] Let's say you skip the warm-up hangs, that's when a pulley goes.

| Passage | Wrong | Right |
|---|---|---|
| 03:10 | "Requires Six Weeks To Gain A Grade" | prediction: "Tomas Brink Predicts That Most People Add One Grade After About Six Weeks On His Plan" |
| 03:40 | "Requires A 20-Second Hang Before Harder Training" | named step: "Tomas Brink Requires 20 Seconds Two-Armed On The 20 Millimeter Edge Before One-Arm Hangs" |
| 04:05 | "Recommends Four Sessions A Week" | option: "Tomas Brink Says A Fourth Session In The Off-Season Is An Option" |
| 04:30 | "Predicts That Skipped Warm-Ups Cause Pulley Injuries" | example: `## Example From The Source` of a warm-up note, or `basis: single example`, `modality: observation` |

The 03:10 note in full:

```markdown
---
type: permanent note
created: 2026-10-04
status: expanded
tags: [calisthenics, finger-training, zettelkasten, permanent-note]
sources:
  - id: vid-EXAMPLE0002
    url: https://www.youtube.com/watch?v=EXAMPLE0002
    speaker: "Tomas Brink"
    channel: "Tomas Brink Climbing"
scope:
  skill: "hardest boulder"
  level: "most people"
  equipment: "not stated"
  basis: "general rule"
  modality: "prediction"
  quantities:
    - value: "about six weeks"
      period: "not stated"
verification: unverified
---

# Tomas Brink Predicts That Most People Add One Grade After About Six Weeks On His Plan

Tomas Brink predicts that after about six weeks on his plan, most people will add one grade to their hardest boulder. [src: vid-EXAMPLE0002 @ 03:10]

## Details

- After about six weeks on this plan, he says most people will add one grade to their hardest boulder. [src: vid-EXAMPLE0002 @ 03:10]

## Evidence

- vid-EXAMPLE0002 @ 03:10 (Tomas Brink): "After about six weeks on this plan, most people will add one grade to their hardest boulder."

## Connected Ideas

- [[Tomas Brink Advises His Athletes To Leave At Least 48 Hours Between Two Hard Finger Sessions]] — another note from the same video.
```

Points: "will" → "predicts"; hedges stay; no equipment is named.

### 14.4 Interview: speaker per passage (`vid-EXAMPLE0004`)

Transcript `speakers: ["Sam Rourke", "Lena Ortiz"]`, `multi_speaker: true`. The host
asks "so lena what is the one mistake you see in club rowers"; the next line is her
answer. The line after it ("right right and that's what i always told my crew too")
is the host again: never give it to her.

```markdown
---
type: permanent note
created: 2026-10-04
status: expanded
tags: [calisthenics, endurance-training, zettelkasten, permanent-note]
sources:
  - id: vid-EXAMPLE0004
    url: https://www.youtube.com/watch?v=EXAMPLE0004
    speaker: "Lena Ortiz"
    channel: "Erg Talk"
speaker_evidence: "so lena what is the one mistake you see in club rowers"
scope:
  skill: "weekly time"
  level: "not stated"
  equipment: "not stated"
  basis: "general rule"
  modality: "recommendation"
  quantities:
    - value: "about 80 percent of your weekly time"
      period: "per week"
verification: unverified
---

# Lena Ortiz Advises Keeping About 80 Percent Of Your Weekly Time Easy

Lena Ortiz advises, with "i'd say", to keep about 80 percent of your weekly time easy. [src: vid-EXAMPLE0004]

## Details

- Answering the host's question about club rowers, she says "they row every piece too hard". [src: vid-EXAMPLE0004]
- Her advice, with "i'd say": keep about 80 percent of your weekly time easy, and it pays off in the spring. [src: vid-EXAMPLE0004]

## Evidence

- vid-EXAMPLE0004 (Lena Ortiz): "they row every piece too hard i'd say keep about 80 percent of your weekly time easy and it pays off in the spring"

## Connected Ideas

- [[Home]] — first note of this topic; no topic map yet.
```

Points: `speaker_evidence` is the host's question right before the answer. `level` is
`not stated`: "club rowers" is only in the host's question.

### 14.5 No note

- Anecdote (`vid-EXAMPLE0001` @ 01:05): "My brother started rowing at forty and he
  loves it". No method, quantity or condition.
<!-- agent-only -->
  Record it:

  ```
  python3 queue_mark.py --ids vid-EXAMPLE0001 --skip-passage "My brother started rowing at forty and he" --at 01:05 --reason "anecdote, no transferable claim"
  ```
<!-- /agent-only -->
<!-- single-only -->
  Single-call mode: `C1 | 01:05 | brother started rowing at forty` in the claims
  list and `C1 | not a transferable training claim` in `=====SKIPPED=====`.
<!-- /single-only -->

- Degraded transcript (`vid-EXAMPLE0006`, `asr_quality: degraded`): no note. The
  harness skips it before any model call.

## 15. Note skeleton
<!-- anchor: note-skeleton -->

Every note has exactly this shape:

```markdown
---
type: permanent note
created: <YYYY-MM-DD>
status: expanded
tags: [calisthenics, <fine-topic>, zettelkasten, permanent-note]
sources:
  - id: vid-<id>
    url: https://www.youtube.com/watch?v=<id>
    speaker: "<speaker or not stated>"
    channel: "<channel>"
scope:
  skill: "<passage words or not stated>"
  level: "<passage words or not stated>"
  equipment: "<passage words or not stated>"
  basis: "<one basis value>"
  modality: "<one modality value>"
  quantities: []
verification: unverified
---

# <speaker + verb that matches the modality + the claim with its limit words and frame quantity>

<one lead sentence> [src: vid-<id> @ MM:SS]

## Details

- <one fact> [src: vid-<id> @ MM:SS]

## Evidence

- vid-<id> @ MM:SS (<Speaker>): "<copied quote>"

## Connected Ideas

- [[<Title>]] — <one line>
```

Optional, before `## Connected Ideas`: `## Example From The Source`, `## Why This
Matters`, `## Disagreement`; `speaker_evidence` after `sources`.

## 16. Stub of a repaired old note (harness-written)
<!-- anchor: stub -->

The published stub is always written by the harness (`stub_check.render`); a model
never writes stub text (an agent's `move_file` stub is provisional and is replaced).
A repair never rewrites an old note in place. After a repair run, an old note of the
repair plan is in exactly one of THREE states:

1. Unchanged: byte-identical to the planned version.
2. A stub: written by the harness, with all of its old body text (this section).
3. Marked unsupported: the body is byte-identical, and the frontmatter gains only
   `verification: unsupported`, `unsupported_reason: "<source transcript missing | no
   source video states any claim of this note>"` and `unsupported_marked: <date>`. Only
   the harness writes this mark, through the publisher, with an archive copy.

State 3 applies only in these two cases:

- No transcript for the note's sources: no known source of the note has a transcript, or
  the video that the note was created from has none. The harness marks the note
  (`source transcript missing`) without a model call.
- Every bullet ended `unsupported`: every planned source video answered
  `dropped: not in this transcript` for every bullet, no target was published, and every
  RECORDED source of the note was asked. If a recorded source has no transcript (the
  note has several sources, and the others were asked), the note is NOT marked: it stays
  unchanged, and the operator item is `source <id> has no transcript`, one per missing
  source. With every recorded source asked, the reason is `no source video states any
  claim of this note`.

A note with any open, unverified, settled or `dropped: contradicts the transcript`
bullet is never marked; nor is a note whose source is unknown. A note with no published
target gets no stub. A kept note (`KEEP-NEEDS-REPAIR`) stays unchanged (state 1), and
the operator list names it with the model's reason. A model never writes these lines.

Shape rule of the mark. The harness marks only a note with LF line ends, no byte order
mark, and a frontmatter between two `---` lines in which each mark key occurs at most
once, with a single-line value. For any other note the mark is refused: the note stays
unchanged, and the operator item is `mark refused: <shape>`. `unsupported_reason` takes
a reason from the list above, and `unsupported_marked` holds a date `YYYY-MM-DD`.

A marked note is planned again only with `--force` (`repair_loop.sh --force <note>`).

`validate.py --staging` enforces the three states. For a marked note it compares the
parsed frontmatter key sets: the old keys plus the three mark keys, with every old value
unchanged, and a byte-identical body. A stub without `old_bullets` is an error.
Consumers (for example a program generator) use only notes with
`verification: source-checked`.

The stub does NOT carry the old note's frontmatter. The full old file, with its
frontmatter, is in the archive copy that `repair_archive` names. A stub keeps
EVERY old Details bullet verbatim, in the old order, each with one status line, and
every other old body line under `## Old text`. The harness checks each stub with
`stub_check.check` before it writes it: an old line that is missing, a wrong bullet
count, or a `→` target that is not a published non-stub note refuses the stub.

A stub's text is the OLD text and is NOT source-checked. Consumers (for example a
program generator) use only notes with `verification: source-checked`.

Fictional example:

```markdown
---
type: permanent note
status: superseded
superseded_by: "[[Tomas Brink Advises His Athletes To Leave At Least 48 Hours Between Two Hard Finger Sessions]]; [[Tomas Brink Advises Warming Up Properly Before Hangs]]"
superseded_pending:
  - "note: Tomas Brink On Warm-Up Hang Length"
verification: superseded
old_bullets: 4
repair_archive: "repair-archive/Rest And Warm-Up Rules For Hangboard Training.v1.md"
---

# Rest And Warm-Up Rules For Hangboard Training

This note was replaced by [[Tomas Brink Advises His Athletes To Leave At Least 48 Hours Between Two Hard Finger Sessions]] and [[Tomas Brink Advises Warming Up Properly Before Hangs]]. The text below is the OLD text of this note, unchanged and not source-checked. Each old claim shows where it went. Archive copy: `repair-archive/Rest And Warm-Up Rules For Hangboard Training.v1.md`.

## Old claims and where they went

- Leave 24 hours between hard finger sessions.
  - → [[Tomas Brink Advises His Athletes To Leave At Least 48 Hours Between Two Hard Finger Sessions]]
- Warm up properly, because skipped warm-up hangs cost a pulley.
  - → [[Tomas Brink Advises Warming Up Properly Before Hangs]] (unverified)
- A warm-up hang lasts 30 seconds.
  - open: target waiting (Tomas Brink On Warm-Up Hang Length)
- Cold fingers lose strength.
  - unsupported: no source video states this

## Old text

> Rest and warm-up keep fingers healthy.
>
>
> ## Grounded Example
> A climber who rests two days gets stronger.
>
> ## Connected Ideas
> - [[Home]]
```

Frontmatter: `status: superseded`; `superseded_by` (the published targets, if any);
`superseded_pending` (strings `note: <Title>` for a target not yet published, or
`bullet: <bullet text>` for a bullet still with a later video, if any);
`verification: superseded`; `old_bullets` (the number of old Details bullets);
`repair_archive` (the archive copy of the old file).

Status lines:

| Status | Meaning |
|---|---|
| `→ [[T]]` | carried into the published note T and confirmed |
| `→ [[T]] (unverified)` | carried into T, but the check cannot confirm it there |
| `open: target rejected (<title>)` | the target note was rejected |
| `open: target waiting (<title>)` | the target note waits for its review |
| `open: target not published (<title>)` | the target note was not published |
| `open: evidence removed (<title>)` | a fix removed the bullet's evidence from the target |
| `open: damaged transcript` | the passage is in a damaged span |
| `open: not yet processed` | no repair call has handled the bullet yet |
| `open: not every source video answered` | some planned source videos have not answered |
| `open: no answer recorded` | no ledger answer exists for the bullet |
| `open: no repair record for this bullet` | the repair state holds no record of the bullet |
| `dropped: contradicts the transcript (<video id>)` | that video's transcript states the opposite; final, later videos are not asked |
| `unsupported: no source video states this` | every planned source video answered with a drop |

`unsupported` is written only when every planned source video answered with a drop.
An operator reads every `open:` and `(unverified)` line.
