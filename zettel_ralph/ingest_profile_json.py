#!/usr/bin/env python3
"""Stage profile-crawl JSON directly as Ralph literature notes.

This is for profile corpora collected from X GraphQL responses by profile_crawl.py.
It preserves the Ralph contract (queue.json + staging/lit/*.md) without re-opening
thousands of individual status URLs in the browser.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_staging_env = os.environ.get("ZR_STAGING")
STAGING = Path(_staging_env) if _staging_env else HERE / "staging"
if not STAGING.is_absolute():
    STAGING = (Path.cwd() / STAGING).resolve()
LIT_DIR = STAGING / "lit"
QUEUE = STAGING / "queue.json"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def _item_id(item: dict) -> str:
    prefix = "ar" if item.get("is_article") else "tw"
    tweet_id = str(item.get("tweet_id") or re.sub(r"[^0-9]", "", str(item.get("tweet_url") or ""))[-18:])
    return f"{prefix}-{tweet_id}"


def _title(item: dict) -> str:
    text = str(item.get("full_text") or item.get("text_preview") or "").strip()
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first:
        first = re.sub(r"\s+", " ", first)
        return first[:97] + "..." if len(first) > 100 else first
    created = str(item.get("created_at") or item.get("bookmarked_at") or "")[:10]
    author = item.get("author") or "unknown"
    return f"Post by @{author}" + (f" on {created}" if created else "")


def _markdown_body(item: dict) -> str:
    text = str(item.get("full_text") or item.get("text_preview") or "").strip()
    parts = []
    if text:
        parts.append(text)
    urls = [url for url in item.get("article_urls", []) if isinstance(url, str) and url]
    if urls:
        parts.append("## Links\n" + "\n".join(f"- {url}" for url in urls))
    if item.get("has_video"):
        parts.append("## Media\n- Contains video or animated media in the original X post.")
    return "\n\n".join(parts).strip() or "(No text content captured.)"


def _write_lit_note(item: dict) -> Path:
    LIT_DIR.mkdir(parents=True, exist_ok=True)
    item_id = _item_id(item)
    kind = "article" if item.get("is_article") else "tweet"
    source_url = item.get("url") or item.get("tweet_url") or ""
    published_at = item.get("created_at") or item.get("bookmarked_at")
    title = _title(item)
    fm = {
        "id": item_id,
        "kind": kind,
        "author": item.get("author") or "",
        "title": title,
        "source_url": source_url,
        "published_at": published_at,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "has_video": bool(item.get("has_video")),
    }
    fm_yaml = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items())
    dest = LIT_DIR / f"{item_id}.md"
    _atomic_write(dest, f"---\n{fm_yaml}\n---\n\n# {title}\n\n{_markdown_body(item)}\n")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="profile_crawl.py JSON output")
    ap.add_argument("--rebuild", action="store_true", help="replace queue.json")
    args = ap.parse_args()

    STAGING.mkdir(parents=True, exist_ok=True)
    items = json.loads(Path(args.input).read_text())
    if not isinstance(items, list):
        raise SystemExit("profile JSON must be a list")

    queue_items = []
    for item in items:
        item_id = _item_id(item)
        source_url = item.get("url") or item.get("tweet_url") or ""
        lit = _write_lit_note(item)
        queue_items.append(
            {
                "id": item_id,
                "url": source_url,
                "kind": "article" if item.get("is_article") else "tweet",
                "stage": "extracted",
                "lit_note": os.path.relpath(lit, HERE),
                "cluster": None,
                "notes_emitted": [],
                "attempts": 0,
                "error": None,
            }
        )

    if QUEUE.exists() and not args.rebuild:
        raise SystemExit(f"{QUEUE} already exists; pass --rebuild to replace it")
    q = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": queue_items,
    }
    _atomic_write(QUEUE, json.dumps(q, indent=2, ensure_ascii=False))

    for name, default in (
        ("concept-index.json", '{"version": 1, "concepts": [], "mocs": []}\n'),
        ("provenance.json", "{}\n"),
    ):
        dst = STAGING / name
        if not dst.exists():
            _atomic_write(dst, default)
    for name in ("STATE.md", "DECISIONS.md"):
        dst = STAGING / name
        if not dst.exists():
            tmpl = HERE / "state" / f"{name.split('.')[0]}.template.md"
            _atomic_write(dst, tmpl.read_text() if tmpl.exists() else f"# {name}\n")

    print(f"staged {len(queue_items)} profile items -> {STAGING}")


if __name__ == "__main__":
    main()
