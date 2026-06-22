#!/usr/bin/env python3
"""Phase A: re-extract every bookmark to a clean markdown literature note.

This is the "better fidelity" ingestion path: instead of parsing the lossy batched
PDFs, we re-run the articlenator source extraction per URL to recover the full title
+ body, convert it to markdown, and stage one literature note per bookmark. The
synthesis Ralph loop (Phase B) reads these staged notes, never the live network.

Design properties:
  * Idempotent + resumable  - re-running skips items already `extracted`; state lives
    in queue.json so a crash never redoes work.
  * Rate-limit aware        - chunked with delays; consecutive-failure circuit breaker,
    mirroring the proven bulk-download run.
  * Loses nothing silently  - a failed live fetch marks the item `failed` WITH the reason
    and keeps it in queue.json (never dropped). Failed items can be reconciled later from
    the batched PDFs (see README "open scope"); that fallback is a documented extension
    point, not auto-run here.

Run inside the project's nix env:
    nix develop
    export X_COOKIES='auth_token=...; ct0=...'
    python zettel_ralph/ingest.py --bookmarks zettel_ralph/data/bookmarks.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from twitter_articlenator.sources import get_source_for_url

HERE = Path(__file__).resolve().parent
STAGING = HERE / "staging"
LIT_DIR = STAGING / "lit"
QUEUE = STAGING / "queue.json"


# --------------------------------------------------------------------------- #
# Minimal, dependency-free HTML -> Markdown (no new flake deps needed).
# Good enough for an LLM to read; preserves headings, lists, links, emphasis,
# code and blockquotes. Not a general-purpose converter.
# --------------------------------------------------------------------------- #
class _MdParser(HTMLParser):
    _BLOCK = {"p", "div", "section", "article", "header", "footer", "ul", "ol", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._list_stack: list[str] = []
        self._skip = 0  # depth inside script/style

    def handle_starttag(self, tag, attrs):  # noqa: D102
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("p", "div", "section", "br"):
            self.out.append("\n\n" if tag != "br" else "  \n")
        elif tag == "ul":
            self._list_stack.append("ul")
            self.out.append("\n")
        elif tag == "ol":
            self._list_stack.append("ol")
            self.out.append("\n")
        elif tag == "li":
            bullet = "- " if (self._list_stack[-1:] or ["ul"])[-1] == "ul" else "1. "
            self.out.append("\n" + "  " * max(0, len(self._list_stack) - 1) + bullet)
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in ("code", "pre"):
            self.out.append("`")
        elif tag == "blockquote":
            self.out.append("\n\n> ")

    def handle_endtag(self, tag):  # noqa: D102
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in ("code", "pre"):
            self.out.append("`")
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self.out.append("\n")
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):  # noqa: D102
        if not self._skip:
            self.out.append(data)


def html_to_markdown(html: str) -> str:
    p = _MdParser()
    p.feed(html or "")
    text = "".join(p.out)
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse blank runs
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# Queue helpers
# --------------------------------------------------------------------------- #
def _url(b: dict) -> str:
    """Tolerate both the derived 'missing_canonical' shape (`url`) and the raw bookmark
    export shape (`tweet_url`). The canonical tweet URL is what the source layer expects
    for both native articles and tweets (proven by the bulk download run)."""
    return b.get("url") or b.get("tweet_url") or ""


def _slug_id(item: dict) -> str:
    prefix = {"article": "ar", "tweet": "tw", "video": "vid"}.get(_kind(item), "bm")
    return f"{prefix}-{item.get('tweet_id') or re.sub(r'[^0-9]', '', _url(item))[-18:]}"


def _kind(item: dict) -> str:
    if item.get("is_article"):
        return "article"
    if item.get("has_video"):
        return "video"
    return "tweet"


def build_queue(bookmarks: list[dict]) -> dict:
    items = []
    for b in bookmarks:
        items.append(
            {
                "id": _slug_id(b),
                "url": _url(b),
                "kind": _kind(b),
                "stage": "pending",
                "lit_note": None,
                "cluster": None,
                "notes_emitted": [],
                "attempts": 0,
                "error": None,
            }
        )
    return {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "items": items}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX - a crash mid-write can't corrupt the target


def load_queue() -> dict:
    return json.loads(QUEUE.read_text())


def save_queue(q: dict) -> None:
    _atomic_write(QUEUE, json.dumps(q, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
async def fetch_one(url: str, cookies: str, attempts: int = 2):
    last = None
    for _ in range(attempts):
        src = get_source_for_url(url, cookies=cookies)
        if src is None:
            return None, "no source for url"
        try:
            return await src.fetch(url), None
        except Exception as e:  # noqa: BLE001 - tolerate any per-item failure
            last = str(e)[:200]
            await asyncio.sleep(2)
    return None, last


def write_lit_note(item: dict, article) -> Path:
    LIT_DIR.mkdir(parents=True, exist_ok=True)
    body = html_to_markdown(article.content)
    fm = {
        "id": item["id"],
        "kind": item["kind"],
        "author": article.author or "",
        "title": article.title or "",
        "source_url": article.source_url or item["url"],
        "published_at": article.published_at.isoformat() if article.published_at else None,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    fm_yaml = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items())
    dest = LIT_DIR / f"{item['id']}.md"
    _atomic_write(dest, f"---\n{fm_yaml}\n---\n\n# {article.title}\n\n{body}\n")
    return dest


def is_rate_limit(err: str | None) -> bool:
    e = (err or "").lower()
    return any(s in e for s in ("authentication failed", "log in", "/login", "rate limit", "too many", "429"))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bookmarks", required=True, help="path to bookmarks json (missing_canonical format)")
    ap.add_argument("--chunk", type=int, default=int(os.environ.get("ING_CHUNK", "25")))
    ap.add_argument("--item-delay", type=float, default=float(os.environ.get("ING_ITEM_DELAY", "3")))
    ap.add_argument("--chunk-delay", type=float, default=float(os.environ.get("ING_CHUNK_DELAY", "8")))
    ap.add_argument("--max-consecutive-fail", type=int, default=8)
    ap.add_argument("--include-videos", action="store_true", help="also extract video items (else skipped)")
    ap.add_argument("--rebuild", action="store_true", help="rebuild queue.json from scratch")
    ap.add_argument("--limit", type=int, default=0, help="extract at most N pending items (0 = all; for pilots)")
    args = ap.parse_args()

    cookies = os.environ.get("X_COOKIES")
    if not cookies:
        raise SystemExit("ERROR: export X_COOKIES='auth_token=...; ct0=...' before running ingestion.")

    STAGING.mkdir(parents=True, exist_ok=True)
    if QUEUE.exists() and not args.rebuild:
        q = load_queue()
    else:
        q = build_queue(json.loads(Path(args.bookmarks).read_text()))
        save_queue(q)

    todo = [it for it in q["items"] if it["stage"] == "pending"]
    if args.limit:
        todo = todo[: args.limit]
    print(f"START items={len(q['items'])} pending={len(todo)}" + (" (limited)" if args.limit else ""), flush=True)

    consecutive_fail = 0
    processed = 0
    for it in todo:
        if it["kind"] == "video" and not args.include_videos:
            it["stage"] = "skipped"
            it["error"] = "video (transcription not enabled)"
            continue

        it["attempts"] += 1
        article, err = await fetch_one(it["url"], cookies)
        if article is not None:
            dest = write_lit_note(it, article)
            it["lit_note"] = str(dest.relative_to(HERE))
            it["stage"] = "extracted"
            it["error"] = None
            consecutive_fail = 0
            print(f"OK   {it['id']} <- {it['url']}", flush=True)
        else:
            it["stage"] = "failed"
            it["error"] = err
            consecutive_fail += 1
            print(f"FAIL {it['id']} :: {err}", flush=True)
            if is_rate_limit(err):
                print("  !! rate-limit/auth signal; consider refreshing cookies", flush=True)
            if consecutive_fail >= args.max_consecutive_fail:
                save_queue(q)
                raise SystemExit(f"STOP: {consecutive_fail} consecutive failures - refresh cookies and re-run.")

        processed += 1
        if processed % args.chunk == 0:
            save_queue(q)
            await asyncio.sleep(args.chunk_delay)
        else:
            await asyncio.sleep(args.item_delay)

    save_queue(q)

    # Seed the Phase-B state files so the synthesis loop can run standalone.
    cidx = STAGING / "concept-index.json"
    if not cidx.exists():
        _atomic_write(cidx, json.dumps({"version": 1, "concepts": [], "mocs": []}, indent=2))
    prov = STAGING / "provenance.json"
    if not prov.exists():
        _atomic_write(prov, "{}\n")
    for name in ("STATE.md", "DECISIONS.md"):
        dst = STAGING / name
        if not dst.exists():
            tmpl = HERE / "state" / f"{name.split('.')[0]}.template.md"
            _atomic_write(dst, tmpl.read_text() if tmpl.exists() else f"# {name}\n")

    done = sum(1 for it in q["items"] if it["stage"] == "extracted")
    failed = sum(1 for it in q["items"] if it["stage"] == "failed")
    skipped = sum(1 for it in q["items"] if it["stage"] == "skipped")
    print(f"DONE extracted={done} failed={failed} skipped={skipped}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
