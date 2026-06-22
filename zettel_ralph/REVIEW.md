# 3-Pass Review of the zettel_ralph Ralph Loop

Each pass was an independent adversarial review against a distinct rubric, run on the
written harness; findings above MINOR were applied before the next pass. Verification after
each pass: `ruff` clean, `bash -n` clean, `validate.py` exits 0 on the real 1,028-note gold
vault, and the new guards were positively tested (they fire on a deliberately bad note).

## Pass 1 — Correctness / will-it-run end-to-end
Lens: schema contracts, wiring, idempotency, loop control, Phase-A realism.

| ID | Finding | Fix applied |
|----|---------|-------------|
| C1 | `ingest.py` read `b["url"]`, but the raw bookmark export uses `tweet_url`/`article_urls` (only the derived file has `url`) → `KeyError` on first item | `_url()` tolerates both shapes; canonical tweet URL used (matches the proven bulk run) |
| C2 | `concept-index.json` read by the agent but created by nothing → iter-1 crash | seeded in both `loop.sh` and `ingest.py` |
| C3 | `STATE.md` never seeded from its template | seeded in `loop.sh` and `ingest.py` |
| C4 | block-style YAML lists didn't parse → `--strict` final gate failed on a *correct* vault | `parse_frontmatter` now handles inline + block sequences (`tags`, `aliases`) |
| M1 | no-progress bail keyed on `synthesized` count → false-trips on legitimately `skipped` items | bail now keyed on `pending+extracted` dropping (skips count as progress) |
| M2 | agent inherited caller cwd → relative paths in `AGENTS.md` broke | `cd "$HERE"` before the agent |
| M3 | `$ZK_DIR` reached the agent as a literal token, not a path | loop passes the resolved absolute path in the prompt |
| M5 | non-atomic `queue.json` writes (corruptible on Ctrl-C, which the circuit-breaker invites) | `_atomic_write` (tmp + `os.replace`) for queue and lit notes |
| M6 | README claimed a PDF fallback that wasn't implemented | claim corrected to "documented reconciliation step, not auto-run"; failed items kept in queue with reason |
| N1 | atomicity H1 count counted `#` inside code fences | fenced-code-aware H1 counting |

## Pass 2 — Context-engineering / degradation resistance
Lens: the four failure modes (poison/distract/confuse/clash), the four levers
(write/select/compress/isolate), context rot, MEM1, scale to hundreds of iterations.

| ID | Finding | Fix applied |
|----|---------|-------------|
| C1 | **the dedup index was loaded whole every iteration** and grows toward ~1000+ entries (~150–250k tokens) → context-rot bomb; the "scalability device" was the liability | `index_query.py` (top-K JIT retrieval) + `index_add.py` (upsert) — the agent **never opens the index**; provenance moved to `provenance.json` keyed by note file |
| C2 | no clustering computed (`cluster: None`); agent would eyeball ~496 tweets every iter | `cluster.py` precomputes ≤8-item themed clusters into the `cluster` field |
| C3 | Phase C asked ONE agent to fix the whole 1000-note vault per pass — re-introduced the monolithic-context failure | rewrote `review_loop.sh` + `review_queue.py`: per-note item-batched passes with gate/bail; clustering is squeeze-driven |
| M1 | "last 3 decisions" too lossy → policy drift across memoryless iterations | added durable `DECISIONS.md` (canonical titles, rulings, taxonomy), read every clock-in |
| M2 | MOC squeeze relied on agent eyeballing self-reported counts | `validate.py --squeeze` computes triggers from disk (ground truth) |
| M3 | title-based dedup misses semantic dups / title drift | `index_query.py` token-overlap ranking + near-duplicate-title warning in `validate.py` (embeddings noted as a same-interface upgrade) |
| M4 | poisoning unmitigated; source-free design worsened traceability | `verification: unverified→corroborated`; provenance keyed by note **file** (traceable) |
| M5 | clash unhandled — opposite finance/geopolitics claims would silently fold/overwrite | explicit tension-note branch in `synthesis.md` + tension template in `NOTE_TEMPLATE.md` |

## Pass 3 — Zettelkasten output quality / fidelity
Lens: claim-atomicity (the user's #1 ask), "don't lose information", own-words, multi-domain
fit, the gold vault's exact conventions (measured directly).

| ID | Finding | Resolution |
|----|---------|-----------|
| C1 | gold vault is **100% bare links**; harness mandated a `— why` clause and made bare links a defect → would churn-rewrite to a style you never used | **decision** (README §6.3): clause *preferred* as an intentional improvement, bare links acceptable, review won't mass-rewrite |
| C2 | `verification` key diverges from the gold vault (uses none) | **decision** (README §6.4): kept as an intentional addition for a contested corpus; made optional, not on every note |
| C3 | claim-atomicity (your #1 requirement) was unenforced — only an H1 count that can't fail | real `validate.py` guards: fat-`## Details` (>6 bullets), body >220 words, title joining claims; `synthesis.md` "N points → N notes; one fat note is a DEFECT" |
| C4 | failed/un-extracted tweets got no cluster id → silently lost (violates "don't lose info") | `cluster.py` marks them `unclustered` and prints a loud warning + count |
| M1 | Grounded Example is a hard gold standard but was only a WARN | promoted to **ERROR**; `synthesis.md` requires constructing a specific scenario if the source has none |
| M2 | "skip pure opinion" risked dropping information you wanted kept | biased to capture: opinions become `unverified` notes; a `skipped` reason must name the discarded idea |
| M3 | nothing detected quote-dumps / thin paraphrase (collector's fallacy) | `validate.py --staging` flags long verbatim n-gram overlap with the source lit note |
| M4 | flat domain tags → one giant `MOC AI`; gold MOCs are fine-grained | two-level tags (domain + fine topic); squeeze fires on **fine** tags; domains indexed on Home |
| M5 | Home.md doesn't scale for multi-domain | Home is a two-level domain index (domains → MOCs only); rebuilt in the clustering pass |
| m6 | lexical dedup could stitch cross-domain "Frankenstein" notes | fold only on same-claim/same-domain; cross-domain lexical hits treated as no-match |
| m8 | gold vault's weak "`X` matters because" filler would be inherited | `NOTE_TEMPLATE.md` rule forbids restating the title in Why-This-Matters |

## Net result
Engineering (fresh-context loop, JIT dedup, deterministic triggers, atomic writes, gates,
bounded loops) is sound. Output quality now enforces the user's core intent — claim-level
atomicity, don't-lose-information, own-words, multi-domain structure — with the gold-vault
divergences made explicit decisions rather than silent drift. Five decisions remain for
sign-off (README §6).
