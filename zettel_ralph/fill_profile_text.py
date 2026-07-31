#!/usr/bin/env python3
"""Fill missing profile-crawl full_text fields from known X status URLs.

Search pagination can discover old status URLs without retaining the parser's
newer ``full_text`` field. This repair pass navigates directly to those known
URLs in one browser session and reuses the existing GraphQL tweet parser.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from twitter_articlenator.sources.bookmarks import BookmarkEntry, BookmarkScraper
from twitter_articlenator.sources.browser_pool import get_browser_pool

from profile_crawl import ProfileTimelineScraper, _merge_entries


def _atomic_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def _merge_entry_fields(item: dict, entry: BookmarkEntry) -> dict:
    merged = dict(item)
    parsed = asdict(entry)
    for key, value in parsed.items():
        if value not in (None, "", []):
            merged[key] = value
    merged["created_at"] = merged.get("created_at") or entry.bookmarked_at
    merged["url"] = merged.get("url") or entry.tweet_url
    return merged


async def _first_dom_text(page) -> str:
    selectors = [
        '[data-testid="longformRichTextComponent"]',
        'article [data-testid="tweetText"]',
    ]
    for selector in selectors:
        try:
            values = await page.eval_on_selector_all(
                selector,
                """els => els.map(e => e.innerText || e.textContent || "")
                    .map(t => t.trim()).filter(Boolean)""",
            )
        except Exception:  # noqa: BLE001
            values = []
        if values:
            return values[0]
    return ""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="profile_crawl.py JSON to update in place")
    ap.add_argument("--handle", default="thdxr")
    ap.add_argument("--limit", type=int, default=0, help="max missing items to try; 0 = all")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--per-item-wait", type=float, default=8.0)
    ap.add_argument("--save-every", type=int, default=10)
    ap.add_argument("--only-truncated", action="store_true", help="only try missing previews ending in ...")
    ap.add_argument("--strict-auth", action="store_true", help="fail if the home feed auth check is slow")
    ap.add_argument("--reset-after-failures", type=int, default=5)
    args = ap.parse_args()

    cookies = os.environ.get("X_COOKIES")
    if not cookies:
        raise SystemExit("ERROR: export X_COOKIES before running fill_profile_text.py.")

    path = Path(args.input)
    items = json.loads(path.read_text())
    if not isinstance(items, list):
        raise SystemExit("input JSON must be a list")

    missing = [item for item in items if not item.get("full_text")]
    if args.only_truncated:
        missing = [
            item
            for item in missing
            if str(item.get("text_preview") or "").endswith("...")
        ]
    if args.limit:
        missing = missing[: args.limit]
    by_id = {str(item.get("tweet_id")): item for item in items if item.get("tweet_id")}

    parser = ProfileTimelineScraper(
        cookies=cookies,
        handle=args.handle,
        since=datetime.min.replace(tzinfo=timezone.utc),
        include_reposts=True,
    )
    cookie_params = BookmarkScraper(cookies)._parse_cookies()
    seen: dict[str, BookmarkEntry] = {}

    pool = get_browser_pool()
    saved_since = 0
    ok = 0
    failed = 0

    async with pool.get_context(cookies=cookie_params) as context:
        page = await context.new_page()

        async def on_response(response):
            try:
                if "/graphql/" not in response.url or response.status != 200:
                    return
                for entry in parser._parse_graphql_response(await response.json()):
                    if entry.full_text:
                        seen[entry.tweet_id] = entry
            except Exception:  # noqa: BLE001
                return

        page.on("response", on_response)

        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
        except Exception as exc:  # noqa: BLE001
            if args.strict_auth:
                raise ValueError("Could not verify Twitter/X authentication from cookies.") from exc
            print("WARN home feed auth check timed out; continuing with direct status URLs", flush=True)

        consecutive_failures = 0
        for index, item in enumerate(missing, start=1):
            tweet_id = str(item.get("tweet_id") or "")
            url = item.get("url") or item.get("tweet_url")
            if not tweet_id or not url:
                failed += 1
                print(f"FAIL {index:04d} missing id/url", flush=True)
                continue

            entry = seen.get(tweet_id)
            if not entry:
                for candidate_url in (url, f"https://x.com/i/status/{tweet_id}"):
                    try:
                        await page.goto(
                            candidate_url,
                            wait_until="domcontentloaded",
                            timeout=45000,
                            referer="https://x.com/home",
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"WARN {index:04d} {tweet_id} navigation {str(exc)[:120]}",
                            flush=True,
                        )
                        continue

                    deadline = asyncio.get_event_loop().time() + args.per_item_wait
                    while asyncio.get_event_loop().time() < deadline:
                        entry = seen.get(tweet_id)
                        if entry and entry.full_text:
                            break
                        await asyncio.sleep(0.25)
                    if entry and entry.full_text:
                        break

            if entry and entry.full_text:
                by_id[tweet_id].update(_merge_entry_fields(by_id[tweet_id], entry))
                ok += 1
                saved_since += 1
                consecutive_failures = 0
                print(f"OK   {index:04d} {tweet_id} graphql", flush=True)
            else:
                dom_text = await _first_dom_text(page)
                if dom_text:
                    by_id[tweet_id]["full_text"] = dom_text
                    ok += 1
                    saved_since += 1
                    consecutive_failures = 0
                    print(f"OK   {index:04d} {tweet_id} dom", flush=True)
                else:
                    failed += 1
                    consecutive_failures += 1
                    print(f"FAIL {index:04d} {tweet_id} no text", flush=True)

            if saved_since >= args.save_every:
                _atomic_json(path, _merge_entries([], list(by_id.values())))
                saved_since = 0
            if args.reset_after_failures and consecutive_failures >= args.reset_after_failures:
                print("WARN resetting through home after failure streak", flush=True)
                try:
                    await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(3)
                except Exception:  # noqa: BLE001
                    pass
                consecutive_failures = 0
            await asyncio.sleep(args.delay)

    _atomic_json(path, _merge_entries([], list(by_id.values())))
    print(f"DONE ok={ok} failed={failed} remaining_missing={sum(not x.get('full_text') for x in by_id.values())}")


if __name__ == "__main__":
    asyncio.run(main())
