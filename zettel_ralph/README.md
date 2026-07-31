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
