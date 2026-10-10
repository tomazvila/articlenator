# zettel_ralph — a Ralph loop that turns Twitter bookmarks into an Obsidian zettelkasten

A degradation-resistant autonomous loop that converts the captured Twitter/X bookmark
corpus into atomic, densely-linked Obsidian permanent notes in the **Themis 2.0** vault,
following the conventions already proven by the existing
`Andrew Torba AI Programming Zettelkasten`.

> Status: **engineered, not run.** Phase B requires a DeepSeek API key; Phase A requires
> live X session cookies. Nothing here fetches or writes until you invoke the drivers.

---

## 1. Why a Ralph loop (not one big agent run)

The corpus is **1,576 bookmarks** (753 articles + 496 tweets + 345 videos). Each article
is thousands of words. A single agent context cannot hold the corpus *and* a growing
zettelkasten of ~1,000+ notes without hitting the failure modes documented in the
context-engineering sources:

- **context rot** — quality degrades non-uniformly as input grows, long before the limit
  (`Context-Engineering/lessons/0001`).
- **distraction / poisoning / confusion / clash** — the four ways long contexts fail
  (`Context-Engineering/lessons/0002`).
- **continuity loss** — long-running tasks lose state across the window boundary
  (walkinglabs `learn-harness-engineering` L05).

A Ralph loop is the structural fix: an **outer driver** invokes a **fresh-context agent**
each iteration; the agent does **one bounded unit of work**, persists everything to disk,
and exits. State lives in files, never in chat — *"information that doesn't exist in the
repo doesn't exist for the agent"* (L03). The four context levers (LangChain / Lance
Martin, via `lessons/0003`) are applied deliberately:

| Lever | How this harness uses it |
|---|---|
| **WRITE** (offload) | Whole corpus + all state on disk; the agent never holds the corpus. |
| **SELECT** (just-in-time) | The agent never loads the growing index. It reads dedup candidates via `index_query.py` (top-K for one claim) and gets MOC triggers via `validate.py --squeeze` — bounded retrieval, not "load everything". |
| **COMPRESS** | `STATE.md` is **rewritten compactly each iteration** (consolidate, don't accumulate — MEM1); durable rulings go to `DECISIONS.md` so continuity survives without bloat. |
| **ISOLATE** | Each iteration is a fresh window; that *is* the loop. |

---

## 2. Architecture: two phases, three loops

```
                 PHASE A  (deterministic, no LLM)            PHASE B  (the Ralph loop, LLM)
  bookmarks.json ─► ingest.py ─► staging/lit/*.md  ───────► loop.sh ──► fresh agent per iter
                       │            (clean markdown,          │          (synthesis.md prompt)
                       │             one per bookmark)        │              │
                       └─► queue.json (source-of-truth) ◄─────┴──────────────┘ writes notes +
                                                                              updates state
                                                                                   │
                                                            PHASE C  (review Ralph loop)
                                                            review_loop.sh ─► fresh agent per pass
                                                                              (review.md prompt)
                                                                                   │
                                                                              validate.py gate
```

- **Phase A — Ingestion (`ingest.py`).** Re-extracts every bookmark URL to a clean
  markdown *literature note* in `staging/lit/`, and builds `queue.json`. Idempotent and
  resumable (mirrors the proven bulk-download run: chunked, progress-tracked,
  rate-limit-aware, atomic writes). **This is the "better fidelity" path** chosen over
  parsing the lossy batched PDFs. A failed live fetch (dead link / 429 / deleted tweet)
  is marked `failed` *with its reason and kept in the queue* — never silently dropped;
  recovering those from the batched PDFs is a documented reconciliation step (§6 open
  scope), not auto-run.

- **Phase A2 — Clustering (`cluster.py`).** Deterministically groups the 496 tweets into
  ≤8-item themed clusters (cosine over tokens) and writes a `cluster` id into each queue
  item, so the synthesis agent picks a coherent unit instead of eyeballing the corpus.

- **Phase B — Synthesis Ralph loop (`loop.sh` + `prompts/synthesis.md`).** Each iteration
  takes ONE work unit (one article/video, or one precomputed tweet cluster), decomposes it
  into atomic claims, dedups each via `index_query.py`, and either creates a new permanent
  note, folds detail into an existing one, or — when claims **contradict** — writes a
  tension note. All links are **contextual**. MOCs are built when `validate.py --squeeze`
  (on-disk counts) flags a theme at the ~5-note squeeze point.

- **Phase C — Review Ralph loop (`review_loop.sh` + `prompts/review.md`).** Item-batched:
  the per-note passes (atomicity, linking, source-free) fix ONE note per fresh agent
  (tracked in `review_queue.json`); clustering is one squeeze-driven pass; a final strict
  `validate.py` closes the run. Mirrors the proven `03 Reviews` pipeline without ever
  asking one agent to hold the whole vault.

All three loops gate on **`validate.py`** (the deterministic verification step) and
checkpoint with **git** after every iteration.

### Articlenator handoff and future PDF ingestion
The June 2026 workflow used Articlenator to capture a large Twitter corpus and generate
batched PDFs for reading/export. The committed Zettel-Ralph harness deliberately did not
parse those PDFs: `ingest.py` re-extracts the original bookmark URLs into clean literature
notes because PDF text can lose article boundaries, URLs, and document structure.

Both paths belong to the same product workflow, but meet at a source-adapter boundary:
```
Articlenator bookmarks/articles ──► direct URL adapter (`ingest.py`) ──┐
                              └──► generated PDF ─► future PDF adapter ├─► staging/lit/*.md
YouTube channel jobs ─────────────► transcript adapter                 ┘   + queue.json
                                                                         │
                                                      synthesis + review Ralph loops
                                                                         │
                                                               Obsidian zettelkasten
```

Any future PDF adapter should emit the existing Phase-A contract rather than coupling PDF
parsing to agent reasoning:
- one bounded Markdown literature note under `staging/lit/<source-id>.md` per recovered
  source unit;
- one matching `queue.json` item with `id`, `kind`, `stage: "extracted"`, and `lit_note`;
- enough source metadata to record provenance and reconcile duplicate material;
- explicit failed items when a PDF page range cannot be assigned to a source.

This keeps later application integration source-format agnostic: Articlenator supplies
captured text or PDFs, while the agentic stages consume the same resumable staging format.

---

## 3. On-disk state (the contract between iterations)

Everything an iteration needs is reconstructable from these files. Nothing is carried in
the agent's head.

### `queue.json` — source-of-truth scope (the `feature_list.json` analog)
```json
{
  "version": 1,
  "items": [
    {
      "id": "tw-2068672726546485547",
      "url": "https://x.com/i/status/2068672726546485547",
      "kind": "article | tweet | video",
      "stage": "pending | extracted | synthesized | reviewed | failed | skipped",
      "lit_note": "staging/lit/tw-2068672726546485547.md",
      "cluster": null,
      "notes_emitted": ["Title A", "Title B"],
      "attempts": 0,
      "error": null
    }
  ]
}
```
`stage` only advances to `synthesized` after the item's notes are written **and**
`validate.py` passes for them — the irreversible-completion primitive from L08.

### `concept-index.json` — dedup index (read via `index_query.py`, written via `index_add.py`)
```json
{
  "version": 1,
  "concepts": [
    {
      "title": "An Atomic Note Separates One Concern So It Can Be Reused",
      "file": "01 Permanent Notes/An Atomic Note Separates One Concern So It Can Be Reused.md",
      "aliases": ["Atomicity", "One idea per note"],
      "gist": "one-line summary used for dedup",
      "tags": ["note-design"],
      "mocs": ["MOC Note Design"],
      "claim_count": 3
    }
  ],
  "mocs": [{"title": "MOC Note Design", "file": "00 Maps/MOC Note Design.md", "note_count": 7}]
}
```
The agent **never opens this file** (it grows toward 1,000+ entries — loading it whole is
the context-rot trap the loop exists to avoid). It queries the top-K closest concepts for a
single claim via `index_query.py`, and upserts entries via `index_add.py`. Lexical ranking
is default; a local embedding model can be dropped in for paraphrase recall (same
interface) — see §6.

### `provenance.json` — source map (keyed by note file, written by `index_add.py`)
```json
{ "01 Permanent Notes/An Atomic Note Separates One Concern So It Can Be Reused.md": ["tw-2068...", "tw-1234..."] }
```
Keeps the vault source-free while making **every note traceable to its origins** — this is
how "no provenance backlinks" and "don't lose information" coexist (§6).

### `review_queue.json` — Phase-C per-note progress (so review is item-batched, not whole-vault)
`{ "items": [ { "file": "01 Permanent Notes/X.md", "passes_done": ["atomicity"] } ] }`

### `STATE.md` — compact running state, **rewritten every iteration**
Counts, current phase, the 3 most recent decisions, themes approaching a squeeze point,
next steps, blockers. Append-only logs are the degradation path; this file is consolidated,
not grown (`template` in `state/STATE.template.md`).

---

## 4. How to run (when you choose to)

```bash
cd /Users/lilvilla/Programming/articlenator
nix develop

# Phase A — ingestion (needs live X cookies; export your session string)
export X_COOKIES='auth_token=...; ct0=...'
python zettel_ralph/ingest.py --bookmarks zettel_ralph/data/bookmarks.json

# Phase A2 — precompute themed tweet clusters (deterministic; no network/LLM)
python zettel_ralph/cluster.py

# Phase B — synthesis Ralph loop (needs `deepseek.txt` or DEEPSEEK_API_KEY)
bash zettel_ralph/loop.sh

# Phase C — review Ralph loop
bash zettel_ralph/review_loop.sh

# Verify the vault at any time
python zettel_ralph/validate.py --vault "$HOME/Documents/Themis 2.0/Twitter Bookmarks Zettelkasten"
```

Config is via env vars at the top of each script (vault path, output folder name, batch
size, MAX_ITERS, model). The DeepSeek model defaults to `deepseek-v4-flash`; override it
with `DEEPSEEK_MODEL`. The API key is read from `DEEPSEEK_API_KEY` or `deepseek.txt`.
Defaults target a **new** vault folder
`Twitter Bookmarks Zettelkasten/` mirroring the Andrew Torba structure, so the run cannot
touch existing notes.

### Maintenance and recovery
`maintenance.py` provides the reusable diagnostics and repair operations discovered during
the first large run. It replaces hard-coded, note-specific scratch scripts with a stable
JSON CLI suitable for later orchestration:
```bash
# Trace a permanent note back to its staged source IDs.
python zettel_ralph/maintenance.py provenance \
  --staging zettel_ralph/staging --note "01 Permanent Notes/A Claim.md"

# Inspect incoming, outgoing, unresolved, and reciprocal links.
python zettel_ralph/maintenance.py links --vault "$ZK_DIR" --note "A Claim"

# Filter the deterministic squeeze report to topics awaiting a MOC.
python zettel_ralph/maintenance.py squeeze --report zettel_ralph/staging/squeeze.json

# Preview retiring an obsolete note and its index/provenance records.
python zettel_ralph/maintenance.py retire-note --vault "$ZK_DIR" \
  --staging zettel_ralph/staging --note "Obsolete Claim"

# Apply only after reviewing the JSON plan.
python zettel_ralph/maintenance.py retire-note --vault "$ZK_DIR" \
  --staging zettel_ralph/staging --note "Obsolete Claim" --apply

# Inspect or deliberately repair one review-queue status.
python zettel_ralph/maintenance.py review-status \
  --staging zettel_ralph/staging --note "01 Permanent Notes/A Claim.md"
python zettel_ralph/maintenance.py review-mark \
  --staging zettel_ralph/staging --note "01 Permanent Notes/A Claim.md" \
  --pass linking --apply
```

Mutating commands are dry-run unless `--apply` is supplied, serialize updates with the
same staging lock as parallel workers, write JSON atomically, and reject paths outside the
configured vault. Retired notes are archived below `staging/retired/` with a `.disabled`
suffix instead of being irreversibly deleted. Synthesis agents are not allowed to invoke
these repair commands; they are an operator/orchestrator surface for recovery and future
application integration.

---

## 5. Guard rails (why it can't run away or rot)

- **Bounded:** `MAX_ITERS` hard cap per loop.
- **No-progress bail:** if an iteration produces no git commit and advances no queue item,
  the driver stops for human review (L11 stuck-detection).
- **WIP = 1:** one work unit per iteration; no "also refactor the vault while you're here"
  (L07).
- **Verification gate:** `validate.py` must exit 0 before an item is marked `synthesized`;
  the worker and the checker are separate processes (L09 worker≠checker).
- **Fresh window each iteration:** the agent is re-spawned per unit; context cannot
  accumulate across units.
- **Index never loaded into context:** dedup is `index_query.py` (top-K) + `index_add.py`
  (upsert); the growing index stays out of the agent's window, so late iterations cost the
  same as early ones.
- **Deterministic triggers:** clustering (`cluster.py`) and MOC squeeze points
  (`validate.py --squeeze`) are computed from disk, not eyeballed by the agent.
- **Idempotent + resumable:** re-running any loop continues from `queue.json` /
  `review_queue.json`; done items are skipped, writes are atomic, so a crash never redoes
  work or duplicates notes.

---

## 6. Decisions baked in (flagged for your sign-off)

1. **Provenance vs. "don't lose information."** Vault convention is *no raw provenance
   backlinks*, but you also said *don't lose information*. Reconciled by keeping every
   note's source IDs in **`provenance.json`** (keyed by note file, outside the vault) plus
   each staging literature note — permanent notes stay clean, but the note→source mapping
   is never lost and is queryable. *Alternative:* a non-link `sources:` frontmatter key.

2. **Output location.** Defaults to a dedicated `Twitter Bookmarks Zettelkasten/` folder
   (mirrors Andrew Torba; isolates the run from your 1,896-file vault root). *Alternative:*
   merge into the main vault for cross-domain linking.

3. **Contextual links — intentional divergence.** Measured: the Andrew Torba vault uses
   **100% bare `[[links]]`**. The harness *prefers* a `— why it connects` clause (better
   zettelkasten per the research) but keeps bare links acceptable and won't churn-rewrite
   them. *Alternative:* match the source vault exactly (bare only).

4. **`verification` frontmatter — intentional addition.** The source vault has none; this
   corpus is contested/multi-source, so single-source claims are tagged `unverified` until
   corroborated. *Alternative:* drop it to match the source vault exactly.

5. **Multi-domain structure.** The corpus spans ~6 domains (vs. the single-domain source
   vault). Home.md is a **two-level domain index** (domains → MOCs), MOCs form around
   **fine** topic tags (not broad domains), and every note carries one domain + ≥1 fine tag.

Open scope item: **videos (327).** Excluded by default (notes need text). They can be fed
through the app's existing Whisper transcription as an optional Phase A2 before synthesis.

See `REVIEW.md` for the 3-pass review that hardened this design.

---

## Transcription and ingest

This section describes how a video becomes a transcript file in `lit/vid-<id>.md`.
The note rules that use these files are in `NOTE_CONTRACT.md`.

### Two transcription paths

| Path | Used by | Output per video |
|---|---|---|
| `batch_channel.py` (whole file, one whisper run) | `run_channel_sthenics.sh`, SasaVenos runs | `videos/<id>/transcript.txt`, `transcript.json`, `transcript.vtt`, `whisper.json`, `whisper.log`, `asr.json`, `meta.json` |
| App channel job (`src/.../channel_transcription_service.py`, 600 s chunks) | `run_radoslav_radev.sh` | `videos/<id>/transcript.txt`, `transcript.json`, `transcript.vtt`, `manifest.json`, `asr.json` |

Both paths keep timed segments next to the text. Both write `asr.json`: the model
that really ran, the prompt, the language, and the quality check result.

### Whisper model

- The model is an explicit setting. `batch_channel.py`: `--whisper-model`, else
  `$WHISPER_MODEL`, else `$TWITTER_ARTICLENATOR_WHISPER_MODEL` (set by `nix develop`
  to `ggml-large-v3.bin`). App path and `run_radoslav_radev.sh`: `WHISPER_MODEL`, else
  `TWITTER_ARTICLENATOR_WHISPER_MODEL`.
- If the model file does not exist, the run stops with an error before any download.
  There is no fallback to a smaller model.
- The recorded model name is the file name without `.bin` (for example
  `ggml-large-v3`). It comes from `params.model` in whisper's own JSON output, or
  from the configured file. It never comes from a constant.
- Old data: the 41 Radoslav Radev transcripts ran with `ggml-small.en.bin`, but each
  per-video manifest says `large-v3` (a constant in the old code). Ingest writes
  `asr_model: "unknown"` and `asr_model_claimed: "large-v3"` for these files. Old
  `batch_channel.py` transcripts have `asr_model: "unknown"`.

### Vocabulary prompt

- whisper gets an initial prompt with domain words, so it writes "planche" and not
  "plunge". The default is the built-in calisthenics list in
  `src/twitter_articlenator/sources/asr_tools.py` (`DEFAULT_CALISTHENICS_VOCABULARY`).
- `batch_channel.py`: `--vocabulary "planche, front lever"`, `--vocabulary-file FILE`
  (one word or phrase per line), `--prompt "raw text"`. The value `none` turns the
  prompt off. The prompt is saved as `asr_prompt` in the channel `manifest.json` and
  the next run of the same channel uses it again.
- App path: `TWITTER_ARTICLENATOR_WHISPER_PROMPT` (`calisthenics` gives the built-in
  list). `run_radoslav_radev.sh` sets it to `calisthenics` unless `WHISPER_PROMPT` is set.
- The pipeline never changes transcript words after whisper.

### Quality check

`asr_tools.assess_transcript` runs after each transcription and again at ingest. It
is deterministic and needs no model.

It marks a unit (a timed segment, or a line of the text) as damaged if:

1. the unit is identical to, or inside, the nearest longer unit before or after it, or
   80 % or more of its word trigrams occur there (repeat loops and rolling captions
   such as "A B", "B", "B C"); or
2. its words are inside a phrase of up to 25 words that repeats 4 or more times back
   to back and covers 20 or more words (loops inside one long line).

Damaged units with at most 2 clean units between them form one span. A span with 40
or more damaged words and 3 or more units is listed. Smaller spans are minor.

| `asr_quality` | Rule | Ingest stage |
|---|---|---|
| `ok` | All checks ran and found no listed span. | `extracted` |
| `partial` | Listed spans exist, but they hold less than 50 % of the words and of the audio time. | `extracted` |
| `degraded` | Empty; listed spans hold 50 % or more; fewer than 60 distinct words per minute; more than 300 words per minute (hidden duplication); or timed segments stop more than 120 s and 15 % before the end of the audio. | `held` |
| `unknown` | No damage found, but the audio duration is not known, so the density, rate, and end-of-audio checks did not run. | `extracted` |

`asr_issues` entries:

- `loop: [MM:SS]-[MM:SS] (<words> words, <kind>: "<example>")` for each listed span
  (`loop: lines A-B (...)` when the file has no timestamps). Do not quote from these
  spans; use the rest of the file.
- `minor: loop: ...` for a small span, `minor: no speech for N s after MM:SS`.
- `not checked (audio duration unknown): speech density, speech rate, end of audio`.
- For `degraded`, the first entries give the reason (for example
  `damaged spans hold 92% of the transcript`).

A phrase loop also covers near copies next to it (the same word in 70 % of the
positions, for example "have to" before "need to"). Words at both ends that do not
fit the phrase stay outside the loop.

#### Span fields in the lit file

- **Lit body line.** Lit body line 1 is the first line after the closing `---` of the
  frontmatter. Blank lines and the `# <title>` line count. Ingest writes a blank line
  (body line 1), `# <title>` (body line 2), a blank line (body line 3), and the
  transcript from body line 4. Transcript line N is lit body line N + 3
  (`asr_tools.LIT_BODY_LINE_OFFSET`).
- `asr_span_unit`: `timestamp` if the body has `[MM:SS]` lines, else `lit-body-line`.
- `asr_damaged_spans`: `[["MM:SS", "MM:SS"], ...]` for `timestamp`; `["lines A-B", ...]`
  in lit body line numbers for `lit-body-line`. The `loop: lines A-B` issues use the
  same numbers.
- `asr_damaged_spans_detail`: one entry per span, always in lit body line numbers:
  `start_line`, `start_char`, `end_line`, `end_char`, `start`, `end` (`MM:SS` or
  null), `words`, `kind`. `start_char` is the 0-based offset of the first damaged
  character in `start_line`; `end_char` is the offset after the last damaged
  character in `end_line`. Offsets count every character of the line as written,
  also leading spaces and the `[MM:SS] ` prefix. Text before `start_char` on the
  first line and after `end_char` on the last line is clean and can be quoted.

Old `transcript.txt` files that contain whisper console lines
(`[00:15:05.160 --> 00:15:14.680]  text`) are read as timed segments.

The limits are in `QualityThresholds`.

### Ingest (`ingest_transcripts.py`)

- Stages a video only if the channel manifest says `complete`, `transcript.txt`
  exists, and a per-video job manifest (app path) is also `complete`. Each skipped
  video is printed with its reason and listed in `<staging>/ingest_report.json`.
- A queue item gets stage `held` (no worker claims it) for a `degraded` transcript or
  an untrusted title. The reasons are in `hold_reasons` and `error`. Overrides:
  `--allow-degraded` (`ALLOW_DEGRADED=1`) and `--allow-untrusted-titles`
  (`ALLOW_UNTRUSTED_TITLES=1`). The lit file still records the problem.
- Items at `extracted` or `held` are refreshed on each run (lit file and stage).
  Items that a worker claimed or finished keep their stage and lit file; ingest only
  updates `asr_quality`, `asr_issues`, `asr_damaged_spans`, `asr_model`,
  `title_trusted` on the queue item.
- Ingest does not create `provenance.json`; the harness owns provenance.
- Lit file frontmatter: `id`, `kind`, `video_id`, `source_url`, `title`,
  `title_source`, `title_trusted`, `channel`, `author` (same as `channel`), `speakers`,
  `speakers_source`, `speakers_found_by`, `multi_speaker`, `published_at`,
  `duration_seconds`, `asr_model`, `asr_quality`, `asr_issues`, `asr_damaged_spans`,
  `timestamps`, `extracted_at`, `asr_source` (folder of the transcript files),
  `canonical`, and when present `asr_model_claimed`, `asr_prompt`, `title_manifest`,
  `title_trust_reason`, `title_run_log`, `supersedes`.
- Body: if timed segments exist, one line per segment with `[MM:SS]` (or `[H:MM:SS]`
  after one hour) at the segment start. Notes cite `vid-<id> @ MM:SS`.

### Canonical transcript (`transcripts.json`)

Each ingest run writes `<staging>/transcripts.json`. `items[<id>].lit_note` is the
canonical lit file of that video (relative to the staging dir). Quotes, verification,
and repairs use only that file. `items[<id>].superseded` lists older transcripts of the
same video; they are history. The canonical lit file also has `canonical: true`; a
superseded one has `canonical: false` and `superseded_by`.

### New transcripts for processed videos (`--replace-transcripts`)

`--asr-dir DIR` reads `DIR/<video_id>/audio.txt` plus `audio.json` / `audio.vtt` (a
plain `whisper-cli -otxt -ovtt -oj -of audio` run) when `audio.json` is not empty.
Title, URL, date, and speakers still come from the channel manifests.

Without `--replace-transcripts`, only items at `extracted`/`held` use the new files;
the report lists the others in `replace_available`. With it:

- every video with a new transcript gets a new canonical `lit/vid-<id>.md`;
- the old file moves to `lit/_superseded/vid-<id>.<old asr_model>.md`;
- with `--vault <zettelkasten folder>`, the queue item also gets `notes_on_disk`
  (`[{"note": "01 Permanent Notes/<title>.md", "exists": true|false}]`) for its
  `notes_emitted` and `published_notes`, so a repair list can skip notes that no
  longer exist (renamed, split, removed);
- the queue item keeps its stage and gets `transcript_replaced_at` and
  `transcript_replaced` (`old_model`, `new_model`, `old_lit`, `old_quality`,
  `new_quality`, `stage_at_replace`);
- an item that is `skipped` or `held` only because of degraded ASR (error starts with
  `asr_quality degraded` or `title not verified`) returns to `extracted` if the new
  transcript is not `degraded`;
- a second run with the same files changes nothing.

```bash
python3 zettel_ralph/ingest_transcripts.py \
  --channels-dir zettel_ralph/channel_jobs_radoslav_radev \
  --asr-dir zettel_ralph/retranscribe_large_v3 --replace-transcripts \
  --staging zettel_ralph/staging_radoslav_radev
python3 zettel_ralph/ingest_transcripts.py \
  --channels-dir "$HOME/Downloads/twitter-articles/channels" \
  --asr-dir zettel_ralph/retranscribe_large_v3 --replace-transcripts \
  --staging zettel_ralph/staging_transcripts
```

### Old lit files without the new fields (`--upgrade-frontmatter`)

`--upgrade-frontmatter` rewrites the frontmatter of the lit files of processed items
in place. The body (the text that existing notes quote) and the stage do not change.
`timestamps` follows the kept body. `extracted_at` is kept.
`frontmatter_upgraded_at` records the change. It can run together with
`--replace-transcripts`; replaced items get a full new file instead.

### Speakers

- Main speaker: `channel_owner` in `speakers.yaml`, else `channel_owner` in the channel
  manifest, else the channel title before a separator ("Radoslav Radev ⎮ Calisthenics
  Mastery" gives "Radoslav Radev").
- Guests from the title, as written in the title. `speakers_found_by` says how each
  name was found:
  - `title-mention`: "w. @name", "with @name";
  - `title-ft`: "ft. name", "feat. name", "featuring name", "w/ name";
  - `title-with`: "with Firstname Lastname", "Q&A with Firstname Lastname";
  - `title-interview`: "The David Packer Interview", "Interview: Jane Doe".
  A name that matches another channel ("@saša venos") maps to that channel owner
  ("SasaVenos").
- `speakers_source` is the summary: `manual` (override), `title-mention` (a guest from
  the title), or `channel-owner`.
- `multi_speaker`: `true` with two or more names; `"unknown"` if the title suggests a
  conversation (interview, podcast, Q&A, coaching call, ft.) but gives no name; else
  `false`.
- `channel_kind: podcast` or `interview` (in `speakers.yaml` or the channel manifest)
  makes `multi_speaker` `"unknown"` for every video of the channel that has one name
  only, because the guest is often named only in the audio. Only an override line
  makes it certain.
- Overrides, strongest first: `speakers.yaml` next to the channel `manifest.json`, then
  `"speakers"` on the video entry in the manifest. Titles do not always name the guest,
  so use the file for those videos:

```yaml
# <channels-dir>/<job>/speakers.yaml   (speakers.json with the same keys also works)
channel_owner: SasaVenos            # main speaker of this channel
channel_kind: solo                  # podcast | interview | solo
aliases:                            # other spellings -> display name
  saša venos: SasaVenos
videos:                             # per video: main speaker first
  lkcLJc2c0NQ: [SasaVenos, David Packer]
  2o5uPfmsO48:
    - sthenics_
    - SasaVenos
```

- The file is read strictly. A malformed line (unclosed `[` or quote, tab indent, a
  list as `channel_owner`, an unknown key, a duplicate key, a key without value) stops
  ingest with exit code 2 and `<file>:<line>: <reason>`. It is never read in part.
- Template: `python3 zettel_ralph/ingest_transcripts.py --channels-dir DIR
  --speakers-template OUT_DIR` writes `OUT_DIR/<job>/speakers.yaml` with every video id,
  its title, and the detected speakers. Entries with `multi_speaker: unknown` are
  written commented out (`# <id>: [...]  # UNKNOWN`); they take effect only after the
  owner edits them. Copy the edited file next to the channel `manifest.json`.

### Titles

- `batch_channel.py` writes `videos/<id>/meta.json` from the yt-dlp entry of that same
  id. The manifest and ingest take the title from there.
- Ingest sets `title_trusted: true` for a `meta.json` title, a manifest entry that
  `batch_channel.py` wrote (`title_source`), an app channel job manifest, or a manifest
  title that matches the id-keyed run files of `batch_channel.py` (`failed.txt`,
  `pipeline.log`). Any other title is `title_trusted: false`: the item is `held` with
  "title not verified", a warning is printed, and `title_trust_reason` says why. If the
  run log has another title for that id, `title_run_log` holds it and ingest uses it
  to find guests.
- Cause of the sthenics_ defect (4 of 10 wrong titles): the old inline manifest step in
  `run_channel_sthenics.sh` called yt-dlp again with `--cookies /dev/null`. That call
  returned no videos, so the manifest had 0 entries. An operator agent then wrote the
  manifest by hand with four invented titles. The inline step is gone; the script now
  runs `batch_channel.py --manifest-only` (disk only, no network).
- Repair (network; writes `meta.json` for every video, then rebuilds the manifest):

```bash
python3 zettel_ralph/batch_channel.py --channel @sthenics_ --manifest-only --refresh-metadata
python3 zettel_ralph/batch_channel.py --channel @SasaVenos --manifest-only --refresh-metadata
```

### Failures in the manifest

- `batch_channel.py` writes the manifest in every mode, also with `--transcribe-only`.
- Each video has `status` `complete`, `error` (with the message, also for a timeout),
  or `pending` (no attempt yet). The job `status` is `complete` only if every video is
  complete, else `partial`. The counts are `done_videos`, `failed_videos`,
  `pending_videos`.
- A whisper exit code other than 0 is an error. Partial whisper files are removed, and
  `transcript.txt` is written last, so its presence means the video finished.
- A failed channel listing stops the run. It never looks like an empty channel.
- `--kill-stale-whisper` keeps the old behavior (kill every `whisper-cli` on the host).
  It is now off by default because it also stopped other jobs.

### Book ingest (`ingest_books.py`)

`ingest_books.py` turns PDF and EPUB books into the Phase-A contract. It writes one
`lit/<id>.md` note and one `queue.json` item (`kind: article`, `stage: extracted`) for each
unit. The synthesis loop uses `--kind article` for these items.

```sh
nix develop --command python3 zettel_ralph/ingest_books.py --staging DIR BOOK... \
  [--max-words 7000] [--min-words 800] [--meta meta.json]
```

- **EPUB split.** The OPF spine gives the order. The nav document or NCX gives the titles.
  A spine file without a nav title joins the unit before it.
- **PDF split.** The outline gives the chapters. The adapter picks the shallowest outline
  depth where at most 20 percent of the words sit in sections above 1.5 times
  `--max-words`. A PDF without an outline splits into page windows. The note then has
  `split_method: page-window`.
- **Size.** A chapter above `--max-words` splits into parts at heading or paragraph
  boundaries (`part`, `part_count`). A section below `--min-words` joins its neighbor.
- **Skipped sections.** Copyright, contents, index, acknowledgments, about the author,
  bibliography and similar sections are skipped. The JSON summary lists them.
- **Ids.** `book-<slug of author and title, 40 characters>-c<NN>[-p<M>]`. The ids are
  stable between runs.
- **Metadata.** The order is `--meta` (file path to `{"title","author"}`), then the EPUB or
  PDF metadata, then the file name pattern `<Author> - <Title>.<ext>`.
- **Failures.** A book that does not parse (corrupt file, scanned PDF with no text layer)
  gets a `failed` queue item with the reason. The adapter does not run OCR.
- **Rerun.** A book with items in `queue.json` is not parsed again. The stage of each item
  stays as it is. A failed book is tried again on the next run.
- **Known limits.** The text comes from `pypdf`. Figure text, side bars and decorative
  headings can stay in the body. A hyphen at a line end is always removed, so a real
  compound word can lose its hyphen.

### Drivers

`run_radoslav_radev.sh` and `run_channel_sthenics.sh` run synthesis with
`loop_parallel.sh` and review with `parallel_review.sh` (package C). After each phase
they print the newest `<staging>/runs/<run_id>/summary.json`. If a phase exits with a
code other than 0, or ingest fails, the driver stops; it does not continue into the
next phase.

---

## Verification and run integrity

This section describes how a note gets from the agent into the vault, the checks on
the way, and the files that record each run. The note rules are in `NOTE_CONTRACT.md`.

### Flow of one unit

1. `run_integrity.py start` checks the transcript registry (below), retries failed git
   commits, creates a minimal `Home.md` when the zettelkasten has none (the fallback
   link target of `## Connected Ideas`), and recovers dead units: a unit without `finalized.json` whose owner
   process (pid plus start time from `/proc/<pid>/stat`) is gone. A dead unit's output
   is not trusted: its notes go to quarantine and its queue item becomes `incomplete`.
   Only notes that its finalize already renamed into the vault (intent record in
   `publish.jsonl` and the same sha in the vault) stay, and they are committed.
2. The agent writes into the unit's shadow folder `units/<unit>/out/` (a write to a
   vault path goes there). It never writes into the vault.
3. `run_integrity.py check` runs the mechanical check (`verify_claims` + form check)
   on every shadow file. A failing note gives the agent up to `ZR_FIX_TURNS` (2) more
   turns with the checker's report.
4. Each video note that passes gets the `source-check` review: ONE chat-completion call
   per note (`review_call.py`, round 6). There is no review agent and no tool.
   - `review-begin` (the harness) writes `units/<unit>/review-N/request.json` (note,
     sha256 of the reviewed text, expected items) and `skeleton.json`: one item for the
     title, the scope, the lead, each Details bullet, the example, each reason and each
     Evidence item, with the keys `item`, `text`, `tag`, `verdict`, `also`,
     `transcript_text`, `problem`, plus `scope_complete`.
   - Request: system = `prompts/source_check.md`; user = the note text, the cited
     passages with +-90 s of context (about 1,500 characters without timestamps), the
     checker warnings as hints (`low-quote-support`, `added-qualifier`,
     `dropped-hedge`), and the skeleton. `response_format: json_object` (when the
     provider refuses it, the first JSON object of the reply is used), `max_tokens`
     6000 (`ZR_REVIEW_MAX_TOKENS`), temperature 0, a hard limit of 180 s per call
     (`ZR_REVIEW_TIMEOUT`). Model: `ZR_REVIEW_MODEL` (default: the synthesis model).
   - An invalid or incomplete reply gets at most 2 more calls (`ZR_REVIEW_RETRIES`)
     with the validation error.
   - The harness writes `messages.json`, `reply-K.json`, `verdict.json`,
     `agent_result.json` (usage, cost, time, model, base URL) and `review.json`
     (`by: harness`, end `finished`, `review-timeout`, `review-invalid`,
     `review-error` or `budget-stop`).
   Text units (tweets, articles) and clustering units have no review.
5. `run_integrity.py finalize` (one lock per vault, keyed on the resolved vault path, in
   `$ZR_STATE_DIR`, default `~/.local/state/zettel_ralph/locks/`):
   - a verdict counts only from a `review.json` of this unit and this note with a normal
     end (rc 0, `finished`), the same sha256 as the shadow text that is published, and
     the same verdict file. No match by title or by a `note` field across units;
   - coverage: every expected item is an object with a known verdict; the note is
     published only when every verdict is `supported`, every `also` is empty and
     `scope_complete` is true;
   - a note with a mechanical failure or a verdict other than `supported` (or
     `scope_complete: false`) goes to quarantine; its queue item becomes `incomplete`
     with the reasons and is tried again (at most `ZR_MAX_UNIT_ATTEMPTS`, 3);
   - a note without a usable verdict (no recorded review, a timeout, an invalid reply
     after the retries, the budget) goes to `<staging>/pending-review/` (outside the
     vault) with `pending_reason`. It is never quarantined for this, and its queue item
     is not sent back to synthesis. `pending-units` turns it into a unit of the next run
     with its original kind and options (a repair stays a repair). Every driver runs
     this pending step at start. After `ZR_MAX_PENDING_CYCLES` (5) cycles the folder
     stays for the operator (`pending_for_operator` in the summary); it is never
     deleted;
   - links: `[[Home]]` and a note written in the same unit count. A self-link fails
     (`self-link`). A link to a note that another unit of the same run has in its
     shadow folder, or that waits in pending-review, is `link-waiting`: not a failure
     for a fix turn, the note waits in pending-review, and the pending step at the end
     of the run (`zr_run_summary`) checks it again;
   - a note that passes both is published as `verification: source-checked`;
     `quote-checked` (mechanical only) is never a published state for video notes;
   - before a vault file is overwritten or removed, it is archived to
     `<staging>/retired/<run_id>/<unit>/` and a `note-archived` record is written;
   - a stub (`superseded_by: "[[A]]"`, or `"[[A]]; [[B]]"` for a split) is published
     only when ALL its targets are published in the same finalize; a removal or rename
     leaves a stub, so links keep working;
     `move_file` in repair mode always writes a stub; a rename chain A->B->C ends
     with C and a stub at A;
   - names of NEW notes: ASCII letters, digits and ` ,.'()&+!%=-`, no leading or
     trailing space or dot, at most 180 bytes (an existing name stays usable); a new name that equals an existing note after NFKC,
     case folding and space collapse is a `duplicate-name`;
   - a shadow file that holds a secret (the value of any `*_KEY`, `*API_KEY*`,
     `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*PASSWD*`, `*COOKIE*`, `*CREDENTIAL*`
     variable, a key-shaped string, or a word plus a value such as `password: ...`,
     `api key = ...`, `bearer ...`) is quarantined with the secret removed;
   - a repair drops a `## Connected Ideas` line whose link target does not
     exist (`dead_links_dropped` in the report); a repair unit that writes nothing is
     not a success (`no_change`, exit 11);
   - a unit whose `queue_mark.py --notes` list names a note that it never wrote ends
     as `incomplete-output`, and its item becomes `incomplete`;
   - with `--commit`, git adds and commits exactly the published paths and checks the
     return codes; a failed commit is listed as `commit_failed` and retried at the
     next `start`.
6. A review of a vault note never removes it. A defect sets
   `verification: needs-repair` in place. The `source-check` review pass runs no
   editing agent; the pass is done only with a recorded verdict (else finalize exits
   11 and the note is released for a new attempt).
7. Repair. `repair-candidates` lists each note once: old-format notes of items whose
   transcript was replaced (`transcript_replaced_at`, `notes_on_disk`) and notes with
   `verification: needs-repair`. Stubs, `unsupported` notes and source-checked
   contract notes are not listed (`excluded`). The known sources of a note are its
   `sources`, the legacy provenance and `<staging>/known_sources.json`, which
   `run_integrity.py known-sources --staging S --vault ZK` builds from the vault git
   history (the commit that added the note, `git log --diff-filter=A --name-only`, and
   the video ids in its message). When no known source has a transcript,
   `repair_loop.sh` sets `verification: unsupported` with `unsupported_reason: "source
   transcript missing"` without an agent run, and finalize refuses Evidence from other
   videos (`other-source-evidence`); `repair_loop.sh --allow-other-sources` turns both
   off.

Exit codes of `finalize`: 0 = all notes published, 10 = a note was quarantined,
11 = the run ended by a limit, a timeout, a kill or an error (or a repair changed
nothing, or a source-check pass has no verdict), 12 = already finalized.

Cost: every agent turn writes `agent_result.turn-N.json`, and `agent_result.json` sums
all turns; each review call writes its own `review-N/agent_result.json`. Unit and run
costs are the sum of these. `DEEPSEEK_BASE_URL` defaults to OpenRouter in `run_lib.sh`
before `start`, so `run.json` records the endpoint that the run uses;
`base_urls_served` in the summary lists the endpoints that the calls used. Budget:
`ZR_MAX_COST_USD` (default 5) per run; when the run cost passes it, the driver starts
no new unit or review, exits 4, and the summary has `budget.stopped: true`.
`summary.json` `wall_time_s` has the time of the synthesis turns, fix turns, review
calls, finalize and the whole run.

One driver per staging: `zr_run_start` takes `flock` on `<staging>/.driver.lock`. A
second driver on the same staging exits with code 75 and a message. Workers that a
driver starts share its run and its lock. `queue_next.py` claims a unit under the
staging state lock (`stage: claimed`, `worker: serial`); `queue_next.py --release`
returns a claim that no finalize took to its old stage.

Tests: `tests/unit/conftest.py` sets `ZR_STATE_DIR` to a temp folder and
`ZR_TEST_RUN=1` (then `start` refuses a staging inside `zettel_ralph/`), and fails the
session when a test changed a file in `zettel_ralph/`.

Entry points (every agent run goes through `run_lib.sh`): synthesis `loop.sh`,
`loop_parallel.sh` (workers `parallel_worker.sh`), `parallel_synth.sh`; review
`parallel_review.sh <N>` and `review_loop.sh`; repair `repair_loop.sh`. The run
wrappers (`run.sh`, `run_thdxr_6mo.sh`, `run_transcripts*.sh`) stop before the review
when synthesis fails.

### Transcript registry

All lit folders that feed one vault: `<staging>/lit`, `ZR_LIT_DIRS` (`:`-separated) and
`<staging parent>/lit_dirs.txt`. `run_integrity.py registry --vault <ZK> --add <staging>`
records a staging in `$ZR_STATE_DIR/vault_registry.json` (keyed on the resolved vault
path) and writes `lit_dirs.txt`;
`start` fails when more than one registered staging feeds the vault and one of their lit
folders is missing. `<staging>/transcripts.json` names the canonical file of a video
(`lit/_superseded/` is never read); without it, the newest `extracted_at` (then the
file time) wins and `canonical: false` loses.

### Agent tools and helpers

- Read roots: in `$ZK_DIR` only `00 Maps/`, `01 Permanent Notes/` and `Home.md`; the
  unit's shadow folder; the registered lit folders; `STATE.md`, `DECISIONS.md` and
  `repair/` in the staging; the prompt files (`AGENTS*.md`, `NOTE_CONTRACT.md`,
  `NOTE_TEMPLATE.md`, `prompts/*.md`, `state/*.md`). Not the vault root, not another
  zettelkasten, not the queue or provenance. Paths are resolved first, so `..` and
  symlinks that lead outside are refused; `list_dir` and `find_files` show only
  readable entries. Writes go to the shadow folder (zettelkasten paths) or to
  `STATE.md`, `DECISIONS.md` and `repair/*.json`.
- No variable expansion in agent paths or helper arguments, except `$ZK_DIR` and
  `$ZR_STAGING`, which the harness replaces.
- Helpers run without any secret variable in their environment; tool results and
  errors have secret values removed.
- Each helper accepts only its listed flags (exact match). Paths that a helper reads
  must be inside the read roots. `index_query.py` refuses `--index` and `--vault`.
  `verify_claims.py` is report-only; under the agent a vault path (absolute or
  `01 Permanent Notes/<name>.md`) means the unit's shadow version.
- Self-check: under the agent, `verify_claims.py "01 Permanent Notes/<Title>.md" --lit
  "$ZR_STAGING/lit"` (the same command line as before) also runs every check of
  finalize on the shadow note: form and required sections, orphan and
  `## Connected Ideas`, dead links, names, secrets, folds. The extra failures are in
  `finalize_check`; the exit code is 1 when any check fails.
- `queue_mark.py --notes` adds to the unit's note list (a union over calls; an existing
  note that the unit folded into is a valid entry); `--notes-replace` sets the list.
  An edit of an existing vault note is never a discarded draft.
- `queue_mark.py` accepts only the unit's ids; `note_link.py` links only notes in
  `01 Permanent Notes/`.
- `agent_result.json` sums usage, steps and time over the fix turns of a unit
  (`turns` lists the earlier turns); `rc`, `end` and `detail` are those of the last turn.
- `agent.log` lines (assistant text and finish summaries too) have secrets removed.

### `verify_claims.py` rules (changes in round 3)

- Limit words: a qualifier counts when it stands right before the number, small
  words between are ignored ("a minimum of 10" = "minimum 10"; "only for four weeks" =
  "only four weeks"). Only a CHANGED or DROPPED qualifier of the quote fails; a limit
  word that only the claim adds is left to the review.
- "one two times" (a spoken range) = one to two. "in one workout" / "each workout" =
  per session. A number in a video or episode name ("Ep.7") is not a quantity.
- The lead sentence may take its numbers from any quote of its tagged videos.
- `low-quote-support` is a warning (a hint for the review), never a failure.
- Damaged spans (package A): `asr_span_unit: timestamp | lit-body-line`; lit body line
  1 is the first line after the closing `---`; `asr_damaged_spans_detail` character
  offsets keep the clean part of a boundary line quotable. Without `asr_span_unit`,
  the spans are not used and the note gets the warning `span-unit-unknown` (re-ingest).
- Speakers match on the name before a channel separator ("Radoslav Radev ⎮ Calisthenics
  Mastery" = "Radoslav Radev"). A speaker outside the list needs `speaker_evidence`
  found in the transcript.
- Round 17: a repair never keeps the old file name. Every repaired claim is a new note
  with a new title, and the old note becomes a stub (`title-equals-old` refuses a note
  with the old title).

### Round 4 (real-model pilot)

- Driver defaults (all drivers, through `run_lib.sh`): `DEEPSEEK_MAX_TOKENS=32000`,
  `AGENT_TIMEOUT=1800`, model `deepseek/deepseek-v4-flash`; env overrides them. A unit that
  hits the token limit and writes no note ends as `limit-no-output`; `summary.json`
  `hints` names `DEEPSEEK_MAX_TOKENS`.
- Drafts: `queue_mark.py --notes` stores the unit's final note list in
  `units/<unit>/marked.json`. When every listed note exists, a shadow note that is not
  listed, not a stub and not part of a rename is a `discarded-draft`: copied to
  `<staging>/discarded/<run>/<unit>/`, listed in the summary, not quarantined, no
  attempt counted. Without a list, such notes are judged as before. The agent tool
  `delete_draft` removes a shadow-only note.
- Spoken numbers: a list with one trailing unit ("6, 7, 8, 9 or 10 seconds") equals
  the spoken "6, 7, 8, 9 seconds, 10 seconds". A unit that a quote number only
  inherits from its neighbor is not "dropped". Round 5 (contract 4): a claim "A to B"
  needs a quote range "A to B" or a quote that lists every value from A to B; "A or B"
  needs the two spoken values ("50 60" gives "50 or 60", not "50 to 60"); a list that
  skips values stays a list. A widened or narrowed range fails (`range-mismatch`).
- Round 6 (pilot 2): `scope-not-in-quote` (a `skill`, `level` or `equipment` value
  other than `not stated` that shares no content word with the Evidence quotes);
  `speaker-evidence-missing` (a transcript with `multi_speaker: true` or "unknown",
  and a note that names a speaker without `speaker_evidence` within +-90 s of a cited
  passage); a quote in a `minor:` loop span of `asr_issues` fails like a damaged span;
  warning `dropped-hedge` (the quote that carries the claim has "maybe", "probably",
  "usually", "for most", "I think", "let's say", "for example", "around" or another
  hedge, and neither the item nor the title has a hedge word).
- Round 5: "limit" and "cap" are limit words only as a verb that binds the number
  ("limit sets to 3", "capped at 3"), not as a noun or adjective ("your limit",
  "limit sessions"). A year ("2024") never joins a range or list.
  `quantities-missing` ignores labels ("step 3", "rule number one"), ordinals ("3rd"),
  years and an angle used as a name ("the 90 degree position"). A frontmatter key that
  occurs twice (for example `unsupported_reason`) fails. `passages` shows the
  occurrence of a quote that is nearest to the cited timestamp.
- New failures: `quantities-missing` (empty `scope.quantities` while Details hold
  numbers) and `dead-link` (a Connected Ideas or Disagreement link to a note that is
  neither in the vault nor written in the unit). New warning, passed to the reviewer:
  `added-qualifier` (a limit word next to a number that the quote does not have).
- `note_link.py` refuses `--kind supersedes-candidate` when the target has `sources:`,
  and `--kind disagreement` when the from-note has no `## Disagreement`. Backlink lines
  are neutral: "Related newer note: [[X]]" or "Other side of a disagreement: [[X]]".
- Observability: `agent_result.json` has `usage` (calls, prompt, completion, reasoning
  and cached tokens, cost; OpenRouter is asked for usage); `summary.json` sums it per
  run. `tools.jsonl` has each tool result (2,000 characters, secrets removed).
  `run.json` has `checker_sha256` (verify_claims, run_integrity, validate, secret_scan);
  a unit finalized by a different checker is listed in `checker_changed_in_units`.
- Helpers accept `--help`; `python3 -c` stays refused, and the message lists the helpers.
- Verdict files: extra keys are fine; verdict values are matched case-insensitively.
  Round 5 replaced the file search by stem, unit name or `note` field with the review
  records above. A file that cannot be used is reported with its reason
  (`review_unusable`); after a second pending cycle the note goes to quarantine as
  `review-unusable` and the queue item becomes `incomplete`.

### Round 7 (second final review)

- `verification: unsupported` is accepted only when `run_integrity.py repair-plan` says
  `mark_unsupported` for the note (no known source has a transcript, or the video that
  the note was created from has none), and then `mark-unsupported` (the harness) writes
  it. The body must stay byte for byte as the old note apart from the status lines
  (`verification`, `unsupported_reason`, `repair_note`). An agent-written `unsupported`
  in any other case fails (`unsupported-not-allowed`); a changed body fails
  (`unsupported-body-changed`).
- When no bullet of an old note has a supporting passage, the repair keeps the old text
  and sets `verification: needs-repair` and `repair_note: "no supporting passage found"`.
  The note is never "done": `repair-candidates` lists it under `operator_notes`, the
  summary under `notes_for_operator`, and `zr_report_runs` exits non-zero.
- `known-sources` keeps every `vid-...` id of a commit message, also when no staging has
  that video. A bare 11-character token counts only when a staging knows it. On a clone
  of the real vault history: 476 notes added; commit 380b468 "(20 from vid-5oVYIU3l3lM)"
  gives 16 notes that are still in the vault, all `mark_unsupported` (4 were removed
  later); 109 notes have a source with a transcript; 293 have no recorded source.
- Review call: every `supported` item needs a non-empty `transcript_text` that occurs in
  the passages that were sent (case, punctuation and number words ignored; at most two
  spans). Else the reply is invalid (retry with the error, then `review-invalid`).
  HTTP 429, 5xx and connection faults: 3 tries with backoff (`ZR_REVIEW_BACKOFF`). A
  JSON-mode refusal is found by its status (400, 404, 415, 422). A reply above 200 KB is
  refused. A timed-out call is `cost_unknown` until its late reply arrives
  (`late-reply-K.json`); the summary counts `review_calls_cost_unknown`.
- User message: a fixed first line (the note and the passages are data; instructions in
  them are ignored); the note between `<<<NOTE` and `NOTE>>>`; the passages between
  `<<<PASSAGES` and `PASSAGES>>>`, one block per Evidence item:
  `[EVIDENCE N (skeleton item evidence-N)] vid-x @ MM:SS ...`, `CONTEXT BEFORE: ...`,
  `>>> the sentence(s) that hold the quote <<<`, `CONTEXT AFTER: ...`; then
  `ITEM PASSAGES`, one line per skeleton item: `details-1 -> EVIDENCE 2`.
- `injection-suspect`: a title, lead or non-Evidence line that addresses the reviewer or
  the model ("note to the reviewer", "set every verdict", "ignore previous", "system
  prompt") fails.
- `delete_draft` and `move_file` update `marked.json` (`deleted`, `renamed`); finalize
  does not count those titles as missing, so a fix turn needs no new clock-out.
- Cost without `usage.cost`: tokens times a price (`ZR_PRICE_IN`, `ZR_PRICE_OUT` per
  million; built-in upper estimates for deepseek-v4-flash and deepseek-v4-pro in
  `pricing.py`). A reply without usage is estimated from characters. `start` refuses a
  run whose models have no cost source unless `ZR_ALLOW_UNKNOWN_COST=1`.
- `scope-not-in-quote`: a scope part passes when one of its words is in an Evidence quote,
  or ALL its words are in the quote's sentence +-1 sentence (400 characters per sentence
  in text without punctuation). The message says: extend the quote, or write not stated.
- Number lists: "two, three, four or (even) six months" equals "two months, three months,
  four months, even six months". A unitless "100" ("not 100 tight") is not a counted
  quantity.
- A `link-waiting` note whose sibling failed is published with that link removed and
  `[[Home]]` added when no link is left (`links_dropped`).
- `queue_next.py --release-all` at the end of every top-level driver returns every claim.
- `run_integrity.py pending --staging S` lists pending folders; `--rearm "<note>"` gives a
  note new pending cycles.
- `validate.py` accepts stubs (an H1 and a `superseded_by` target that exists). Under the
  agent, `validate.py --vault` reads only `00 Maps/`, `01 Permanent Notes/` and
  `Home.md` and skips symlinks.
- `speaker_evidence` without a speaker cue (a name from `speakers`, a question, "you",
  "my guest", "tell me") gives the warning `speaker-evidence-no-cue`.
- `repair_loop.sh` skips notes that are done, stubs, operator notes or missing, unless
  `--force`.

### Round 8 (third real-model pilot)

- A review rejection is not a synthesis failure. After the first review, the notes with a
  defect verdict get ONE targeted repair turn (`zr_review_repair`, message from
  `run_integrity.py review-repair-message`): per rejected note the transcript file, the
  rejected items with `problem`, item text and `transcript_text`, and the marked
  passages. Then the mechanical check and ONE new review run for the rewritten notes only
  (`review_call.py` skips a note whose current text has a usable verdict). A note
  rejected again is quarantined with `review-rejected-twice` and listed in
  `notes_rejected_twice`. `ZR_REVIEW_REPAIR=0` turns the repair turn off.
- Queue: a finished unit leaves its item `synthesized` (or `skipped`), with
  `notes_published`, `notes_quarantined`, `notes_pending`; quarantined notes never send the
  video back to synthesis. `needs-attention` when more than half of its notes were
  quarantined. A pending unit never counts as a synthesis attempt, also when its item was
  left `claimed`.
- Quarantined and discarded notes leave `concept-index.json` and `provenance.json` at
  finalize (`index_rolled_back`); a failed fold gets its entry back from the vault file.
- Pending step: a dead `## Connected Ideas` link is dropped, `[[Home]]` added when no link
  is left (`links_dropped`).
- `run_integrity.py rereview --staging S --vault ZK --note "<note>" [--model M]` reviews a
  published or quarantined note again in a new run folder; the vault does not change.
- Fix turns: drafts outside the final note list go to `discarded/` first
  (`discard-drafts`); the message lists only listed notes that fail, with the transcript
  file path(s) and the marked passages (`fix-report`). Fix and repair turns have their own
  output limit `ZR_FIX_MAX_TOKENS` (default 20000); a turn that hits it ends cleanly
  (`fix-turn-limit.json`, the unit stays finished).
- Review call: `ZR_REVIEW_MAX_TOKENS` default 12000. A reply that ends by length without
  JSON gets a retry with "answer with the JSON object only" and, when
  `ZR_REVIEW_RETRY_REASONING` is set (a JSON object such as `{"effort": "low"}`; OpenRouter's
  `reasoning` parameter, not verified offline), that `reasoning` object; a provider that
  refuses it (HTTP 400/422) gets the retry without it. The `scope` item may list one span
  per scope value (`;`, `|`, `,` or `...`) and may have an empty `transcript_text`.
- Checker: `ellipsis-hides-attribution` (a `...` in a quote skips another speaker's name,
  "he says", "according to", "X told me", "his idea" or a negation);
  `speaker-evidence-is-claim` (the evidence lies inside an Evidence quote and has no host
  cue, or repeats the lead); warning `title-drops-frame` (the lead or a quote has a frame
  quantity or limit word that the title drops; a review hint); no `dropped-hedge` hint for
  "let's say" / "for example" when the note has `modality: observation` or an Example
  section.
- Review input: `title`, `scope` and `lead` map to ALL Evidence blocks in `ITEM PASSAGES`;
  hints are only `low-quote-support`, `added-qualifier`, `dropped-hedge`,
  `title-drops-frame` (`REVIEW_HINT_TYPES`).
- `helper_results.jsonl` (per unit) keeps the full output of each `verify_claims.py` and
  `validate.py` run of the agent, secrets removed.

### Round 9: single-call synthesis (default for video units)

`ZR_SYNTH_MODE=single` (default; `agent` keeps the agentic loop). Code: `synth_call.py`.
A tweet or article unit always runs in agent mode.

- Synthesis: ONE call per video (per part of a long transcript). System
  `prompts/synth_single.md`; user = the canonical lit file between `<<<TRANSCRIPT` and
  `TRANSCRIPT>>>`, the block `EXISTING NOTES YOU MAY LINK TO` (up to 30 vault notes by
  keyword overlap: contract notes as `title | speaker | skill`, old notes marked "(older note
  without sources)"), and a reply-format reminder. Model `ZR_SYNTH_MODEL`
  (`anthropic/claude-sonnet-5.5`), `ZR_SYNTH_MAX_TOKENS` 24000, `ZR_SYNTH_REASONING`
  (OpenRouter `reasoning`, default `{"max_tokens": 4000}`, sent to `anthropic/` and
  `deepseek/` models; dropped once when the provider refuses it), timeout 600 s, no tools.
  A lit file over `ZR_SYNTH_MAX_INPUT_CHARS` (120000) is split at transcript lines into
  parts that overlap by 60 s (or 1,500 characters); one call per part; claim ids get a
  `P<n>-` prefix; a note that two parts write is kept once (normalized title).
- Reply grammar: `=====CLAIMS=====` (`C<n> | <MM:SS or line> | <paraphrase>`), one
  `=====NOTE: <Title> | claims: C1,C3=====` block per note, `=====SKIPPED=====`
  (`C<n> | <reason>`). The splitter drops code-fence lines and CR, ignores (and records)
  text before the first marker, takes the H1 when the marker title differs (recorded),
  accepts the older separator form `=====NOTE=====`, and marks the last note `truncated`
  when the reply ends by length or the note lacks `## Evidence` / `## Connected Ideas`.
- A cut reply gets at most 2 continuation calls (the open claim ids are listed; finished
  notes are named, not repeated). Claims with neither a note nor a skip get ONE coverage
  call; claims still open are `claims_uncovered` (unit event and summary). A skip "already
  covered by [[X]]" counts only when X is a candidate. `claims.json` per unit holds the
  claims, the coverage and the skip reasons.
- The harness writes each note into the shadow folder (no fold: a title that exists in the
  vault is not written), records the note list (`marked.json`, `by: harness`) and the
  queue claim, and indexes published notes at finalize (`by: harness`). Links: `[[Home]]`,
  a note of the same reply, or a candidate title; anything else is `dead-link`.
- Fix calls (`prompts/fix_single.md`): one call per failing listed note, at most
  `ZR_FIX_TURNS` (2) per note for mechanical failures and one per note for a review
  rejection. User = the note between `<<<NOTE` / `NOTE>>>`, `CHECKER REPORT` or
  `REJECTED REVIEW ITEMS`, the transcript path and the marked passages. Reply
  `=====NOTE: <Title>=====` + the corrected note (a new title renames it) or
  `=====DROP=====` + a reason (the claim becomes a skip).
- Repair (`repair_loop.sh`, single mode): ONE call per video (`prompts/repair_single.md`)
  with the lit file and every old note of that video (`=====OLD NOTE: <file name>=====`).
  Reply: `=====NOTE: <New Title> | repairs: <old file name>=====` (several notes may repair
  one old note) or `=====KEEP-NEEDS-REPAIR: <old file name>=====` + a reason. New title(s):
  a stub with all targets (the old title is refused: `title-equals-old`); keep: the old
  note stays unchanged, and the operator list names it with the model's reason.
  Notes without a transcript: `unsupported` with no call. Notes without a known source:
  the top 3 transcripts by rare words and numbers get them, flagged "source unknown"; a
  note no video takes is listed in `repair_unclaimed`.
- Review: `ZR_REVIEW_MODEL` default `anthropic/claude-sonnet-5.5`. Passages mark the
  quoted sentence(s) `>>> … <<<` and the adjacent sentence of the same statement
  `>> … <<` (not a question). No "let's say" / "for example" hint when the hedge sits in
  another clause than the claim's words.
- Every call goes through the same cost recorder (`units/<unit>/calls/*.json`) and
  `ZR_MAX_COST_USD`. Price table (when the provider reports no cost): sonnet-5.5 $2 / $10,
  flash $0.30 / $1.70, pro $0.40 / $0.80 per million tokens (bake-off measurements; not
  queried online).

### Round 10 (fourth real-model pilot, review of frozen-v4)

- Start: `prompt_build.check_manifest()` runs first. A built prompt that differs from
  `prompts/build_manifest.json` stops the run before any call (M7).
- Reasoning: each call type has its own setting: `ZR_SYNTH_REASONING`,
  `ZR_FIX_REASONING`, `ZR_REPAIR_REASONING`, `ZR_REVIEW_REASONING`. The default is
  `{"effort": "low"}`. If a reply ends by length with less than 200 characters of text,
  the harness makes the call once more with `ZR_NO_REASONING` (`{"enabled": false}`).
  That second call is recorded as `<name>-noreason`. A reply with no text never counts.
- Budget: each call reserves its maximum cost under the run lock
  (`budget_reserve`/`budget_release`). If the budget stops a fix call, the note goes to
  `pending-review` with reason `budget-stop` (file `fix-pending.json`). The pending step of
  the next run makes the fix call. If the budget stops a synthesis before its first call,
  the item goes back to its old stage and no attempt is counted.
- Reply cache: `staging/synth-cache/<vid>--<key>.json` holds the synthesis replies of a
  video. If a single-mode unit dies after a complete reply and published nothing, the
  recovery releases its item (`recovered-released`). The next run takes the saved reply at
  no cost, and the check and the review run again.
- Repair (`zr_repair_single`): `repair-groups --state` writes the plan, then
  `next-batch` gives one call at a time.
  - Known notes go in batches per video: at most `ZR_REPAIR_MAX_NOTES` (12) old notes and
    `ZR_REPAIR_MAX_INPUT_CHARS` (160000) per call.
  - Each old note is read again before its call. If it changed after the plan, it is
    done (M4).
  - Each Details bullet gets an id `<old file>#bN`. The reply ends with
    `=====BULLETS=====`: one line per id, `kept in <Title>`, `corrected in <Title>`,
    `dropped: not in this transcript` or `dropped: contradicts the transcript`. Missing
    ids get one coverage call. `bullets_dropped` lists every dropped bullet.
  - A note with several source videos goes to its next video with the bullets that the
    last video reported "not in this transcript".
  - A note of unknown source goes to its best candidate video only. The next candidate
    gets it only after a `KEEP-NEEDS-REPAIR`.
  - Stubs are written at finalize from `repair_map.json` and the final titles (after fix
    calls). If no target is left, the old note stays as it is.
- Candidate search for a note of unknown source: BM25 over content words, title words,
  number+unit pairs and rare word pairs, divided by the best possible score. A speaker
  named in the note limits the candidates. `ZR_CANDIDATE_MIN_SCORE` (0.5).
- Mechanical check: "six weekly levels", "for six weeks" next to "per week",
  "rule number three", "the 90 degree handstand push-up", "finish a workout 70%",
  "twice" = "two times" and "one hour each" = "one hour each workout" are no longer
  failures. No note line starts with `=====` (`marker-line`); `## Disagreement` holds
  only `- This note:` and `- Other side:` lines.
- Review: `transcript_text` holds at least four consecutive words (or the whole marked
  text if it is shorter) from the passages of that item (`ITEM PASSAGES`), at most two
  spans. An Evidence item with the same video and time as a source tag wins over the
  30-second window.
- Fix calls: a note that lacks sections or keys gets the "Note skeleton" of
  `NOTE_CONTRACT.md` in its fix message. A rename onto an existing title is refused. The
  review-repair record follows a renamed note.
- Summary: `model` is the model that made the single-mode calls; `agent_model` is the
  agent model. Synthesis wall time includes `calls/*.json`. `videos_without_notes` makes
  `zr_report_runs` print ATTENTION.

### Round 11 (fifth real-model pilot)

- Reasoning: a model of `ZR_SYNTH_REASONING_MODELS` never gets a call without an
  explicit reasoning object. `off` or an invalid value is a start error. If the provider
  refuses the object (HTTP 400/422), the call stops with a clear error.
  - A length stop with 0 reasoning tokens is a truncation: the next call is a continuation.
  - A length stop with reasoning tokens and little text (or a reasoning share of 0.8 or
    more) gets ONE retry with the same setting and 1.5 x max_tokens (cap
    `ZR_MAX_TOKENS_CAP`, 48000), recorded as `<name>-more`.
  - `ZR_NO_REASONING` is an opt-in only. `synth_call.py probe-reasoning --model M` makes
    ONE small paid call and prints the reasoning tokens (never run by the tests).
- Budget: one rule (`_budget_decision`) for every call and for the driver. The first call
  of a run is checked too. A stop is sticky: the driver ends with exit 4.
- Two-stage synthesis (`ZR_SYNTH_STAGES=two`, default; `one` keeps the round-9 reply):
  - Stage 1: one inventory call per part (`prompts/inventory_single.md`), only
    `=====CLAIMS=====` with `C<n> | <MM:SS or line> | <paraphrase> | <type>`.
  - Claims typed `anecdote` or `other` are skipped without a call.
  - Stage 2: batches of at most `ZR_SYNTH_BATCH_CLAIMS` (5) claims (`prompts/synth_single.md`):
    the lit frontmatter, each claim line with its type and its excerpt (+-90 s, or
    +-1,500 characters), `NOTES ALREADY WRITTEN FOR THIS VIDEO` and the link candidates.
  - A skip `passage unreadable` / `damaged span` on a clean passage, or a bare `not a
    transferable training claim` for a typed claim, is refused; the claim is sent ONCE more
    in a later batch with a FLAG line.
  - `claims.json` `yield`: inventoried, by type, notes, skipped by reason, rejected skips.
- Duplicates: a note marker without `claims:` is matched to the nearest claim by its
  source-tag times. A note with the same normalized title, or the same (videos, tag times,
  lead), as a written note is the same note (`duplicate-note`).
- Repair state: `<staging>/repair_state.json` holds, per old note, the source videos in
  order, the answers per bullet and video, the titles written and the position; per batch,
  the status. A rerun resumes there. Every repair call is cached in
  `<staging>/repair-cache/` by its full request: a finished call is never paid twice.
- Repair requests: at most `ZR_REPAIR_MAX_NOTES` (4) old notes and `ZR_REPAIR_MAX_BULLETS`
  (25) bullets per call; each bullet has its top-3 candidate passages
  (`  candidates: [MM:SS] "..."`). A "not in this transcript" drop whose top passage shares a
  number+unit or 60 % of the bullet's words is asked once more
  (`prompts/repair_drop_check.md`).
- Unknown source: up to 10 videos by the word search (`ZR_CANDIDATE_ATTACH_SCORE`, 0.3),
  ordered by bullet passages; the next candidate (up to 3) gets the note in the same run
  when a video leaves it out.
- Stubs: when at least one target of an old note publishes, the old note becomes a stub of
  the published targets with `superseded_pending` (open targets, bullets with a later
  video), registered in `<staging>/repair_partial.json` and updated when the rest publishes.
  Summary: `old_notes_partially_replaced`, `replacement_beside_old_note` (must be empty);
  `validate.py --staging` reports an old note left beside a published replacement.
- Checker: step numbers ("step three", "step number four") are labels; a cited time up to
  30 s after a quote's segment is accepted; a scope period that is in the sentence before
  the quote is a warning; "twice" = "two times", "once" = "one time"; a host question in
  `speaker_evidence` is never the claim, and the report records the matched cue.
- Fix calls carry `[MM:SS]` markers and the lit file path. The pending step runs the
  review-fix call. A title with "_" (`sthenics_`) is made safe without a call.

### Running with sub-agent workers (round 13, files backend)

Set `ZR_LLM_BACKEND=files` (default `openrouter`). The harness then makes no network call.
Every model call (inventory, batch, synth, continuation, coverage, fix, title fix,
review, review fix, repair, ledger, drop check) becomes a request file in
`<staging>/exchange/` (`ZR_EXCHANGE_DIR` may name another folder under the staging):

- `requests/<key>.json`: key, kind, unit, model_hint, system, user, max_tokens,
  reply_path, meta_path, must_not_share_worker_with;
- `requests/<key>.system.md` and `requests/<key>.user.md`: the same text for a worker;
- the worker writes `replies/<key>.txt` (the raw reply) and `replies/<key>.meta.json`
  (`{"key": ..., "worker": ..., "model": ...}`).

`<key>` is the first 32 hex characters of the sha256 of the full request (messages,
model, max_tokens, reasoning), the same key as the repair request cache. A request goes through the secret check before it is written
(the same check runs before a network call).

The pass loop:

1. Run the driver (`loop.sh` for synthesis, `repair_loop.sh` for repair). A unit whose
   call has no reply yet stops in the state `waiting-for-reply`: no failure, no attempt,
   no quarantine, no budget use. The driver goes on to the next unit, so one pass writes
   every independent request. When requests wait, the driver prints their count and exits
   with code 5.
2. Workers answer the open requests: `python3 synth_call.py exchange-list --staging <staging>
   --open --json` lists them (key, kind, paths, word count, must_not_share_worker_with).
   The worker instruction is `prompts/worker_instruction.md`.
3. Run the driver again. Each waiting unit runs again from its reply files and caches
   (two-stage synthesis cache, `repair_state.json`), takes the replies and writes the next
   requests. Repeat until the driver exits with 0.

`python3 synth_call.py exchange-status --staging <staging>` shows the open requests by
kind, the answered, the consumed, the orphans (a reply without a request) and the refused
replies.

Reply rules: the reply file is the raw model text. The harness removes a UTF-8 BOM and ONE
code fence around the whole file, nothing else. A reply is used only for its own key: a
meta file that names another key makes the harness refuse the reply. The call record and
the provenance record of a published note keep `worker` and `model` (`synth_model`,
`synth_worker`, `review_model`, `review_worker`).

Review independence: a review request lists the request keys of the calls that wrote the
note (`must_not_share_worker_with`). The harness refuses a review reply whose meta
`worker` is the worker of one of those calls; the request stays open for another worker.

Budget: with the files backend the cost is 0 and `ZR_MAX_COST_USD` does not apply.
`ZR_MAX_CALLS` limits the requests that one run writes. The summary key `calls_by_kind`
lists the calls per kind and backend.

One synthesis pass (from `zettel_ralph/`):

```sh
ZR_LLM_BACKEND=files ZR_STAGING=<staging> VAULT=<vault> ZK_FOLDER=<zk folder> bash loop.sh; echo "exit $?"
python3 synth_call.py exchange-list --staging <staging> --open --json   # give these to the workers
```

One repair pass:

```sh
ZR_LLM_BACKEND=files ZR_STAGING=<staging> VAULT=<vault> ZK_FOLDER=<zk folder> bash repair_loop.sh <notes...>; echo "exit $?"
python3 synth_call.py exchange-list --staging <staging> --open --json
```

### Round 14 (review of frozen-v6)

- Note paths: a repair note path must be relative and resolve inside `01 Permanent Notes`
  (`safe_note_rel`); `repair-groups` skips other paths (`unsafe-path`), and no stub or
  `unsupported` copy is written for them.
- An old note beside a published replacement (a kill between the two writes, a refused
  stub, a user edit) is listed in `needs_operator.json` and reported by
  `validate.py --staging` as an ERROR (round 22: no automatic heal, no in-place repair). The
  next normal finalize writes the proper stub through the publisher when the write guard
  allows it. A stub that the model wrote in a repair unit is refused (`model-written-stub`);
  a newer stub's published targets stay in the stub.
- Repair ledger: "kept/corrected in X" settles a bullet only when X was written; the request
  shows the full current text of every already written note of the old note, and a note
  that drops one of its bullet ids is refused (`extend-lost-bullets`). Bullets of a target
  that is quarantined (also in a pending unit, in recovery, or dropped/renamed by a fix
  call) reopen; a line with two titles reopens only when both are rejected. A batch that
  fails twice before its answers are recorded ends `failed` with its bullets open. An empty
  reply is not cached. A partial stub closes when no bullet goes on.
- Files backend: a reply counts only with a valid meta file (an object with string `key`,
  `worker`, `model`); write the meta first and the reply last (temporary file, rename). Bad
  meta, a same-worker review and an invalid review reply are moved aside
  (`<key>.refused-<n>.*`) and the request opens again; `exchange-list` shows
  `writer_workers`, `refused_workers` and `refusals`. The writers of a note are found from
  the request's `must_not_share_worker_with` keys and from the writer call records, which
  travel into pending units. A request that the latest run no longer needs is marked
  `<key>.obsolete` and left out of `--open`. Waiting for a reply does not count a pending
  cycle. The exit trap keeps the driver's own exit code (2, 3, 4); a `ZR_MAX_CALLS` stop
  writes `budget-stop.json`.
- Two-stage synthesis: a claim time is read with one helper (`[01:47]`, `~01:40`, H:MM:SS);
  duplicate claims are dropped only across parts, with the same time, the same numbers and
  a near-equal paraphrase (each drop is recorded with both texts); `P<n>-C<n>` ids parse; an
  unknown type spelling is normalized or sent to a batch; `already covered by [[X]]` needs
  a written or candidate X; a note covers a claim only when it cites a time from 15 s
  before to 60 s after the claim (`claim-not-cited`); claims refused twice make the video
  `needs-attention` with their reasons (no attempt).
- One-stage mode and the coverage call use `prompts/synth_onecall.md` (B), or the built-in
  CLAIMS-first stub while that file does not exist.

### Round 15 (pilot 6, files backend)

- Waiting for a reply never uses a pending cycle (round 14). The driver exits 5 while
  requests are open or answered replies are still needed, and 6 (never 0) while pending
  folders sit at the cycle limit; the summary lists `stuck_notes` by reason. A re-armed
  note that the review rejects gets its review-fix call.
- Stubs: at every finalize the stub of every old note in `repair_state.json` is rebuilt from
  the state (round 21 format below). The summary key `bullet_invariant_violations` must stay
  empty. A second write of a note in one unit keeps the first archive copy
  (`<stem>.v<n>.md`).
- Speakers: the inventory may add `=====SPEAKERS=====` (`<label> | <name or unknown> |
  <host/guest/solo> | <evidence quote + time>`). When it finds several voices but the lit
  file names one speaker, or names a person the lit data does not list, the video is
  `speaker-unresolved`: its notes carry `speaker: "unresolved speaker, <channel> video"`, `speaker_status: unresolved`, and titles start with
  "Unresolved Speaker In <Channel> Video". The checker refuses a person's name in such a note
  (`speaker-unresolved-named`) and a vague "A Speaker In ..." title elsewhere
  (`vague-speaker`). The summary lists `speaker_unresolved_videos`; after the owner names
  the speaker, `synth_call.py respeak --staging S --vault V --video VID --name NAME
  [--note TITLE] --apply` renames the notes and rewrites their links (no LLM call).
- Every stage-2 batch of a video is emitted in one pass (no earlier batch's titles in the
  request); look-alike notes across batches stay two notes and are listed in
  `possible_duplicates` (round 18). Links to a note that
  the harness renames are rewritten in the sibling notes.
- Unknown source: the best video must pass `ZR_CANDIDATE_ATTACH_SCORE` (0.3); the next
  candidates need `ZR_CANDIDATE_NEXT_SCORE` (0.15); up to 3 are tried.

### Round 16 (duplicates, links, review carry-over, speaker labels)

- Review carry-over: a supported review stays valid when the reviewed content (title claim,
  lead, sections except Connected Ideas, Evidence quotes and tags, scope, speakers) is
  unchanged; a rejection never carries over.
- `respeak --map S1=Name,S2=Name` names notes by their `speaker_label`.
- Merge and relink of round 16 were narrowed or removed in round 18 (below).

### Round 19 (review of frozen-v7, pilot 7)

- A repair-published note's provenance records the bullets it settles and its Evidence tags;
  a later version that loses one of those tags is refused (`extend-lost-evidence`), and the
  published version stays. Renames by fix calls and respeak update `repair_state.json`;
  stubs resolve stub chains to the final published notes. `bullet_invariant_violations`
  reports: bullets listed open but held by a published note, bullets settled in a title
  that exists nowhere, stub targets that are missing or stubs, published replacements that
  the stub does not link, and published notes that no longer record a bullet or its tags.
- A dead link in the lead or Details is refused before publish; recovery after a crash
  turns links to unpublished siblings into plain text.
- Ledger reason `dropped: damaged transcript`: the bullet stays open (never unsupported)
  and is listed for the operator (`damaged_bullets`).
- Title prefix of unresolved speakers: `Unresolved Speaker In <Channel> Video`.
- All title-fix requests of a unit go out in one pass.

### Round 18 (review of frozen-v7): fewer mechanisms

- Merge: only two NEW notes of one unit in one finalize (in practice: of one reply), both publishing, with the same
  title claim (speaker prefix removed), the same non-empty numbers, the same speaker and a
  symmetric lead overlap of 0.9 or more. Never in repair units (`ZR_MERGE_IN_REPAIR=1` to
  allow), never against a vault note, never settling repair bullets. Every other look-alike
  pair is only listed in `possible_duplicates.json` (summary `possible_duplicates`).
- Heal: only in crash recovery, and only when the old note's text is unchanged since the
  plan, no version waits for review, and the replacement was published by this pipeline.
  Else nothing is written and the note is listed in `needs_operator.json`.
- Links: an unlinked sibling stays plain text (listed in `unlinked.json`); no automatic
  restore. Unlink touches only body text (not code, Evidence, tables), never a link to an
  existing vault note, and a note left without a link gets `[[Home]]`. `validate.py`
  resolves links as Obsidian does (case, path, `.md`, embeds and files, code).
- Unresolved speakers: `speaker: "unresolved speaker, <channel> video"` (no parentheses);
  no listed name stays in the title or body. `respeak` changes only notes this pipeline
  published, refuses a target title that exists, fixes the Evidence speakers, and archives
  every note whose links it rewrites.
- A repair batch counts only calls that failed; a wait or a budget stop never counts. A
  waiting repair unit no longer makes `repair_loop.sh` exit 1 (it exits 5).
- A request is obsolete only by `exchange-status --mark-obsolete KEY` (operator).

### Round 21: stubs keep every old claim; operator tools

- Stubs: a stub keeps every old claim. Each claim shows where it went or why it is open.
  Read the bullets with `open:` or `(unverified)` and the entries in `needs_operator.json`.
  The pipeline does not overwrite a stub or a published note that you edited; it lists the
  note in `needs_operator.json`.
  - The harness writes every stub with `stub_check.render` and checks it with
    `stub_check.check` before the write: every old body line is carried unchanged, there is
    one claim entry per old Details bullet in the old order (`old_bullets`), and every `→`
    target is a published note that is not a stub. A failed check refuses the stub and lists
    the note in `needs_operator.json`.
  - Format (`NOTE_CONTRACT.md`, "Stub of a repaired old note"): frontmatter
    `status: superseded`, `superseded_by`, `superseded_pending`, `verification: superseded`,
    `old_bullets`, `repair_archive`; body: a header sentence, `## Old claims and where they
    went` (each old bullet verbatim with a status line: `→ [[T]]`, `→ [[T]] (unverified)`,
    `open: <reason>`, `dropped: <reason> (<video id>)` or `unsupported: no source video
    states this`), then `## Old text` as a blockquote.
  - A bullet is `unsupported` only when every planned source video answered with a drop.
    A stub's text is the old text and is not source-checked: a consumer (for example a
    program generator) uses only notes with `verification: source-checked`.
- `needs-repair`: an old note with open bullets and no published target stays unchanged in
  the vault; `needs_operator.json` lists its open bullets and the quarantined target paths
  (state `needs-repair`).
- Summary `repair_bullets`: every old bullet in exactly one class, as its stub line shows:
  `bullets_settled`, `bullets_unverified`, `bullets_open` (by reason), `bullets_dropped` (by
  reason), `bullets_unsupported`; the classes add up to `bullets_total`.
- `synth_call.py relink --staging S --vault V [--apply]`: offline maintenance. It restores
  the links listed in `unlinked.json` whose target is published now. Dry run by default;
  it never runs automatically.
- `review_loop.sh` refuses `ZR_LLM_BACKEND=files` (exit 2): with the files backend,
  `loop.sh` and `repair_loop.sh` do the source-check reviews.
- `respeak --map S1=Name,S2=Name` names notes by their `speaker_label`; `--name` sets one
  name. A name that is not in the lit speakers or the SPEAKERS record is refused unless
  `--force-name` is given. After `--apply`, `speakers_resolved.json` records the names and
  the mapping per video, and `validate.py` accepts those names.

### Driver exit codes (`loop.sh`, `repair_loop.sh`; rounds 22-23)

The driver prints the operator-work list after EVERY run, whatever the exit code. The exit
code follows this precedence: the driver's own code (1, 2, 3, 4, 75) first, then 5, then 6,
then 7, then 0.

| Code | Meaning | What to do |
|---|---|---|
| 0 | Nothing is left for workers, and the operator-work list is empty. | The run is complete. |
| 5 | Requests wait for worker replies (files backend), or a note waits for its next review request after one unusable reply. Only this driver's own kind of waits counts. | Let the workers answer, then run the driver again. A request that is refused 3 times (`ZR_MAX_REFUSALS`) becomes obsolete for good: it is not open again, no unit waits for it, and `needs_operator.json` names its note. If exit 5 comes 3 passes in a row with the same `--labels` numbers, stop: a worker does not answer. That is a matter for the dispatcher of the workers, not for the driver. |
| 6 | Pending folders are at the cycle limit, and nothing else waits. With the files backend, a cycle is a consumed review reply: a verdict, or an invalid or unusable reply that the unit refused. A folder at the limit that holds an answered reply is still taken by the next pass. | Read `stuck_notes` in the summary. Re-arm with `python3 run_integrity.py pending --staging S --rearm '<note>'`, or decide by hand. |
| 7 | The run finished, but operator work remains (`run_integrity.py operator-work`): `needs_operator.json` entries (no transcript, no known source, target rejected, damaged transcript, refused writes, old note beside its replacement, requests refused 3 times (named by their note), `source <id> has no transcript`, `mark refused: <shape>`), `needs-repair` notes, `done-unsupported` notes that are not marked yet, `UNLISTED` notes, missing plan notes, `dropped: contradicts` bullets, stub-check and two-state errors of `validate.py --staging`, pending folders that wait for no reply, each `pending target <title> waits on a dead review request <key>` and `waiting: <id> waits on a dead request <key>` (a request refused 3 times: nothing waits for it, and it is never shown as `waiting`), and `failed` queue items. | Act on each item. Then record the decision with `python3 run_integrity.py operator-done --staging S --note '<rel or title>' --reason '<text>'`: it removes the note's entries (an entry of a refused request also matches by its request key), closes a plan note (`operator-closed`, never planned again), and records the decision in `operator_decisions.jsonl`. For a pending folder that waits on a dead review request, `operator-done` on the waiting note, on the old note of that target, or on the request key marks the folder: the next pass quarantines its waiting notes as `review-invalid`, the old note's bullets become `open: target rejected`, and the old note stays in the plan (its status follows). For a queue item that waits on a dead request, `operator-done` on the item id or the key sets the item to `failed`. If nothing matches, it changes nothing and exits 1. |
| 4 | The budget (`ZR_MAX_COST_USD`) or the call limit (`ZR_MAX_CALLS`) stopped the run. | Raise the limit, or stop. |
| 2 / 3 | `loop.sh`: the iteration cap, or a stall (no progress). `repair_loop.sh`: 2 = agent mode refused (repair runs in single mode only; `ZR_REPAIR_AGENT_MODE_I_KNOW=1` overrides). | Read the log. |
| 1 | An error: a failed call, `run_integrity start` failed, a failed preflight, or a `ZR_LLM_BACKEND` value other than `openrouter` or `files`. | Read the message. |
| 75 | Another driver holds the staging lock (also when the preflight sees the lock). | Wait for it to end. |

`python3 synth_call.py exchange-status --staging S --count-open --labels [--kind repair]`
prints the driver's own numbers: `requests_open=N replies_to_consume=M folders_stuck=K
units_waiting=W`. A reply that no waiting unit will consume is not counted; the summary lists
it as `exchange_unconsumed`.

`python3 synth_call.py repair-status --staging S --vault V` (read-only) prints per old note
of the repair plan, and per listed note outside it: its state, the vault form (`stub`,
`stub (check failed)`, `unchanged`, `waiting`, `changed`, or `missing`), its open bullets,
its `contradicts` bullets, and its operator reasons. `UNLISTED` marks an unchanged, finished
note with open bullets that `needs_operator.json` does not name. Its last line gives the
operator-work count (the same list as exit 7).

`repair_loop.sh` first runs `repair-preflight` (stubs without `old_bullets`, unsafe names,
the backend value, missing paths, a git vault with no uncommitted change in `V`, stub-check
and two-state errors, an old note beside its replacement, another driver's lock). Then, when
the staging has no `known_sources.json`, it builds it (`run_integrity.py known-sources`,
offline, from the vault git history).

### Operator checklist for a repair run on a real vault

Names: `S` is the staging folder. `V` is the zettelkasten folder, `$VAULT/$ZK_FOLDER`
(the driver takes `VAULT` and `ZK_FOLDER`). The git root can be the whole Obsidian vault
above `V`; every git command below is limited to `V`. Run the commands from
`zettel_ralph/`.

An old note of the repair plan has exactly three allowed states (`NOTE_CONTRACT.md`,
"Stub of a repaired old note"): unchanged; a harness-written stub with all old body
text; or marked unsupported (body unchanged, frontmatter gains only `verification:
unsupported`, `unsupported_reason` and `unsupported_marked`). The harness marks a note only when no
source of the note has a transcript (`source transcript missing`), or when every
bullet ended `unsupported`, no target was published, and every recorded source of the
note was asked (`no source video states any claim of this note`). If a recorded source
has no transcript, the note stays unchanged and is the operator item `source <id> has
no transcript`. The harness marks only a note with LF line ends, no BOM, and a
frontmatter between two `---` lines with single-line mark keys that occur once; for any
other note the mark is refused, the note stays unchanged, and the operator item is
`mark refused: <shape>`. A marked note is planned again only with `--force`. Consumers
use only notes with `verification: source-checked`.

Conditions for the whole run:

- Use single mode only. Do not set `ZR_SYNTH_MODE`, and do not give
  `--allow-other-sources`. `repair_loop.sh` refuses agent mode with exit 2, because
  agent mode can write a new note over an old note.
- Set `ZR_LLM_BACKEND=files` exactly.
- Workers work only between passes. A worker writes `replies/<key>.meta.json` first,
  then writes the reply to a temporary file and renames it to `replies/<key>.txt`.
- Do not edit `V` until the run ends: not during a pass, and not between passes. Stop
  every writer into `V` for the whole run: pause Obsidian sync (and any other sync
  tool), and pause each cron job or script that commits or changes the vault.
- Never run `git reset --hard` on the vault. It acts on the whole repository and
  deletes user work outside `V`.
- Do not run `respeak` or `relink` until the run ends.
- Clear an operator item only with
  `python3 run_integrity.py operator-done --staging S --note '<rel or title>' --reason '<text>'`.
  `--note` matches a note path, a title, `exchange request <key>`, or a request key. If
  nothing matches, the command changes nothing and exits 1. Never edit
  `needs_operator.json` by hand.

Before the first pass:

1. `V` must be in a git repository, and `git -C V status --short -- .` shows nothing.
   The preflight checks only the zettelkasten folder; changes outside `V` do not stop it.
2. `repair_loop.sh` builds `S/known_sources.json` when it is missing
   (`run_integrity.py known-sources`). Build it first by hand and read it before pass 1:
   `python3 run_integrity.py known-sources --staging S --vault V`.
3. Run these three commands. Start only if all three are empty:
   - `python3 synth_call.py repair-preflight --staging S --vault V` reports
     `preflight: 0 finding(s)` (`repair_loop.sh` also runs it: a finding stops it with
     exit 1, another driver's lock with exit 75). If you force a start
     (`ZR_PREFLIGHT_FORCE=1`), the findings go to the summary key `preflight_forced`;
   - `python3 validate.py --staging S --vault V` reports no error;
   - `python3 run_integrity.py operator-work --staging S --vault V` lists nothing.
4. Copy `S` (for example `cp -a S S.before-run`).

Each pass:

5. Commit the vault and write down the commit id. The id of pass 1 is the pre-run
   commit:

   ```sh
   git -C V add -A -- . && git -C V commit --allow-empty -m "before repair pass N" -- . && git -C V rev-parse HEAD
   ```

6. Start the pass:

   ```sh
   ZR_LLM_BACKEND=files ZR_STAGING=S VAULT=<vault> ZK_FOLDER=<zk folder> bash repair_loop.sh <notes...>; echo "exit $?"
   python3 synth_call.py exchange-list --staging S --open --json   # give these to the workers
   ```

7. After EVERY pass, whatever the exit code, read:
   - the operator-work list that the driver prints at the end (it prints it for every
     exit code), and every `[repair] OPERATOR:` line. At the end of each pass the
     driver runs `mark-finished` itself: it marks each finished note that state 3
     allows, or lists it (`source <id> has no transcript`, `mark refused: <shape>`);
   - `python3 validate.py --staging S --vault V`. If it shows a stub-check error or a
     plan-note state error, stop the run;
   - `python3 synth_call.py repair-status --staging S --vault V`. If a row shows the
     vault form `missing`, stop the run;
   - `needs_operator.json`, compared with the copy of the previous pass. If an entry is
     a refused write or `old note beside its published replacement`, stop the run:
     treat a "beside" entry as a refused stub. Other expected entries: `needs-repair:
     no published target, open bullets (old note unchanged)` (a target was rejected,
     or the transcript is damaged); `operator: <the model's keep reason>`; `edited
     since the pipeline's last write (user edit?): not overwritten`; a review request
     that the workers refused 3 times (the note becomes an operator item);
   - `python3 synth_call.py exchange-status --staging S --count-open --labels --kind repair`.
     It prints `requests_open=N replies_to_consume=M folders_stuck=K units_waiting=W`:
     N requests wait for a worker reply, M replies wait for the next pass, K pending
     folders are at the cycle limit, and W units wait;
   - the summary keys `repair_bullets`, `repair_bullets_open`,
     `bullet_invariant_violations`, `stuck_notes` and `notes_marked_unsupported`, and
     `possible_duplicates.json` (each pair with `both_published` set to true).
8. Act on the exit code. The code follows the precedence of "Driver exit codes": the
   driver's own code (1, 2, 3, 4, 75) first, then 5, then 6, then 7, then 0. A code
   can hide operator work, so always do step 7 first.
   - Exit 0: the operator-work list is empty. The run is complete only if
     `validate.py --staging` also shows no error.
   - Exit 5: give the open requests to the workers, then start the next pass. If exit 5
     comes 3 passes in a row with the same `--labels` numbers, stop: a worker does not
     answer. Make sure that the dispatcher of the workers runs. Then list the open
     requests (`exchange-list --open`), and repair each note whose review is refused or
     not answered by hand, or re-arm it.
   - Exit 6: read `stuck_notes` (`pending_notes` are normal and wait for the next pass).
     Re-arm each note once with
     `python3 run_integrity.py pending --staging S --rearm '<note>'`. If the note comes
     back as `review-invalid`, repair its old note by hand.
   - Exit 7: act on each item of the printed list. Every `dropped: contradicts` bullet
     is an item: read each one. A pending folder that waits on a dead review request
     (refused 3 times) is an item too: close it with `operator-done` on its
     old note. Then close each resolved note with `operator-done` (see above). It closes
     a plan note, so the note is not planned again.
   - Exit 1, 2, 4 or 75: read the message (see "Driver exit codes"), fix the cause,
     and start the pass again.
9. Set a maximum number of passes before the start. Stop at that number.

After the run:

10. Read in the stubs every `open:`, `(unverified)`, `dropped:` and `unsupported:` line.
    Compare each `dropped: contradicts the transcript` line with the other source videos
    of the note: later videos are not asked after such a drop. Read every `UNLISTED` row
    of `repair-status` and every `[repair] OPERATOR:` line. Read the notes marked
    unsupported (`notes_marked_unsupported` in the summary, and their `repair-status`
    rows); they are not operator work. Read 1 in 10 of the plain `→ [[T]]` lines, and do
    a check that the target note holds the old bullet.
11. Restore, if necessary:
    - One note (preferred: the archive copy). The stub frontmatter `repair_archive`
      gives a path relative to `S` (it starts with `retired/`):
      `cp "S/<repair_archive value>" "V/01 Permanent Notes/<Old Title>.md"`.
      The git form, `git -C V checkout <pre-run commit> -- "01 Permanent Notes/<Old
      Title>.md"`, loses an edit that was made between passes; the archive keeps it.
      Then remove the new notes of that repair and commit. If you keep the new notes,
      `validate.py --staging` reports the old note beside its replacement on every
      later run. After the restore, expect two kinds of report: `validate.py --staging`
      reports each link of the restored note to a removed note as a dead link, and the
      operator-work list shows the restored note as `unlisted` (its repair state is
      `done`, but the vault holds the old text). Remove or fix the dead links by hand,
      then close the note with `operator-done`.
    - The whole zettelkasten folder. This command changes only files below `V`, and
      keeps all files and commits outside `V`:

      ```sh
      git -C V restore --source <pre-run commit> --staged --worktree -- . && git -C V commit -m "restore before repair run" -- .
      ```

      Restore `S` from its copy too, because the staging state of the run no longer
      agrees with the vault.
12. Restore links: run `python3 synth_call.py relink --staging S --vault V` (dry run),
    read the plan, then run it again with `--apply`. `--apply` commits the changed notes
    itself.

What stays outside the mechanical check: a claim without a number that reverses or
widens its quote, modality and scope changes, a limit word that only the claim adds,
the speaker of each passage beyond `speaker_evidence`, and whether a bracket fixes a
real ASR error. The `source-check` review before publication judges these; a note is
published only when that review returns `supported` for every item.
