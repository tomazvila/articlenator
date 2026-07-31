#!/usr/bin/env python3
"""Collect a Twitter/X profile timeline into the Ralph bookmarks JSON shape.

The Ralph ingestion pipeline already knows how to turn a list of tweet/article URLs into
staged literature notes. This script only discovers profile-owned status/article URLs for
a date window by listening to the same GraphQL timeline responses the browser receives.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import structlog

from twitter_articlenator.sources.bookmarks import BookmarkEntry, BookmarkScraper
from twitter_articlenator.sources.browser_pool import get_browser_pool

log = structlog.get_logger()

HERE = Path(__file__).resolve().parent


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _subtract_months(day: date, months: int) -> date:
    month = day.month - months
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    month_lengths = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]
    return date(year, month, min(day.day, month_lengths[month - 1]))


def _start_of_day_utc(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _end_of_day_exclusive_utc(day: date) -> datetime:
    return _start_of_day_utc(day) + timedelta(days=1)


def _entry_datetime(entry: BookmarkEntry) -> datetime | None:
    if not entry.bookmarked_at:
        return None
    try:
        return datetime.fromisoformat(entry.bookmarked_at)
    except ValueError:
        return None


def _timeline_dicts(value: Any):
    if isinstance(value, dict):
        instructions = value.get("instructions")
        if isinstance(instructions, list):
            yield value
        for child in value.values():
            yield from _timeline_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _timeline_dicts(child)


class ProfileTimelineScraper:
    """Scrape profile tweets/articles by intercepting profile GraphQL responses."""

    def __init__(
        self,
        cookies: str,
        handle: str,
        since: datetime,
        until: datetime | None = None,
        include_reposts: bool = False,
        with_replies: bool = False,
        target_url: str | None = None,
    ) -> None:
        self._cookies_str = cookies
        self.handle = handle.lstrip("@")
        self.handle_lower = self.handle.lower()
        self.since = since
        self.until = until
        self.include_reposts = include_reposts
        self.with_replies = with_replies
        self.target_url = target_url
        self._parser = BookmarkScraper(cookies)

    def _parse_cookies(self):
        return self._parser._parse_cookies()

    def _parse_timeline_entry(self, entry: dict) -> list[BookmarkEntry]:
        """Parse all tweet items from one timeline entry.

        BookmarkScraper returns the first item from a module because bookmarks are
        singletons. Profile modules can hold several tweets, so this expands them all.
        """
        found: list[BookmarkEntry] = []
        content = entry.get("content", {})
        entry_type = content.get("entryType", "") or content.get("__typename", "")

        if entry_type == "TimelineTimelineItem":
            item = self._parser._parse_item_content(content.get("itemContent", {}))
            if item:
                found.append(item)
        elif entry_type == "TimelineTimelineModule":
            for module_item in content.get("items", []):
                item_content = module_item.get("item", {}).get("itemContent", {})
                item = self._parser._parse_item_content(item_content)
                if item:
                    found.append(item)
        return found

    def _parse_graphql_response(self, data: dict) -> list[BookmarkEntry]:
        entries: list[BookmarkEntry] = []
        for timeline in _timeline_dicts(data):
            for instruction in timeline.get("instructions", []):
                inst_type = instruction.get("type", "")
                if inst_type in ("TimelineAddEntries", "TimelineReplaceEntry"):
                    for entry in instruction.get("entries", []):
                        entries.extend(self._parse_timeline_entry(entry))
                elif inst_type == "TimelineAddToModule":
                    for module_item in instruction.get("moduleItems", []):
                        item_content = module_item.get("item", {}).get("itemContent", {})
                        item = self._parser._parse_item_content(item_content)
                        if item:
                            entries.append(item)
                elif inst_type == "TimelinePinEntry":
                    entry = instruction.get("entry")
                    if isinstance(entry, dict):
                        entries.extend(self._parse_timeline_entry(entry))
        return entries

    def _in_scope(self, entry: BookmarkEntry) -> tuple[bool, bool]:
        """Return (include, older_than_window)."""
        if not self.include_reposts and entry.author.lower() != self.handle_lower:
            return False, False
        created = _entry_datetime(entry)
        if created is None:
            return True, False
        if self.until and created >= self.until:
            return False, False
        if created < self.since:
            return False, True
        return True, False

    async def scrape(
        self,
        *,
        max_scrolls: int,
        max_empty_scrolls: int,
        old_scrolls_to_stop: int,
        scroll_delay: float,
    ) -> list[BookmarkEntry]:
        pool = get_browser_pool()
        cookies = self._parse_cookies()
        intercepted: list[BookmarkEntry] = []
        collecting = False

        async with pool.get_context(cookies=cookies) as context:
            page = await context.new_page()

            async def on_response(response):
                if not collecting:
                    return
                try:
                    if "/graphql/" not in response.url or response.status != 200:
                        return
                    entries = self._parse_graphql_response(await response.json())
                    if entries:
                        intercepted.extend(entries)
                        log.info(
                            "profile_api_intercepted",
                            count=len(entries),
                            total=len(intercepted),
                        )
                except Exception as exc:  # noqa: BLE001
                    log.debug("profile_response_parse_failed", error=str(exc))

            page.on("response", on_response)

            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            try:
                await page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
            except Exception as exc:  # noqa: BLE001
                raise ValueError("Could not verify Twitter/X authentication from cookies.") from exc

            path = f"{self.handle}/with_replies" if self.with_replies else self.handle
            target_url = self.target_url or f"https://x.com/{path}"
            collecting = True
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)

            seen_ids: set[str] = set()
            collected: list[BookmarkEntry] = []
            last_intercepted_len = 0
            empty_scrolls = 0
            old_scrolls = 0

            for scroll_num in range(1, max_scrolls + 1):
                current_len = len(intercepted)
                new_entries = intercepted[last_intercepted_len:current_len]
                last_intercepted_len = current_len

                new_in_window = 0
                saw_older = False
                for entry in new_entries:
                    if entry.tweet_id in seen_ids:
                        continue
                    seen_ids.add(entry.tweet_id)
                    include, older = self._in_scope(entry)
                    saw_older = saw_older or older
                    if include:
                        collected.append(entry)
                        new_in_window += 1
                        created = _entry_datetime(entry)
                        created_label = created.date().isoformat() if created else "unknown-date"
                        print(f"FOUND {len(collected):04d} {created_label} {entry.tweet_url}", flush=True)

                if new_in_window:
                    empty_scrolls = 0
                    old_scrolls = 0
                else:
                    empty_scrolls += 1
                    if saw_older and collected:
                        old_scrolls += 1

                if old_scrolls >= old_scrolls_to_stop:
                    print(
                        f"STOP reached {old_scrolls} scrolls beyond {self.since.date().isoformat()}",
                        flush=True,
                    )
                    break
                if empty_scrolls >= max_empty_scrolls:
                    print(f"STOP no new in-window items after {empty_scrolls} scrolls", flush=True)
                    break

                await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                await asyncio.sleep(scroll_delay)

        collected.sort(key=lambda entry: entry.bookmarked_at or "", reverse=True)
        return collected


def _entry_to_ralph_dict(entry: BookmarkEntry) -> dict:
    data = asdict(entry)
    data["created_at"] = entry.bookmarked_at
    data["url"] = entry.tweet_url
    return data


def _merge_entries(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for item in existing + new_entries:
        tweet_id = str(item.get("tweet_id") or "")
        if tweet_id:
            by_id[tweet_id] = item
    return sorted(
        by_id.values(),
        key=lambda item: str(item.get("created_at") or item.get("bookmarked_at") or ""),
        reverse=True,
    )


async def main() -> None:
    today = date.today()
    default_since = _subtract_months(today, 6)

    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", default="thdxr")
    ap.add_argument("--since", default=default_since.isoformat())
    ap.add_argument("--until", default=today.isoformat())
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-scrolls", type=int, default=400)
    ap.add_argument("--max-empty-scrolls", type=int, default=10)
    ap.add_argument("--old-scrolls-to-stop", type=int, default=4)
    ap.add_argument("--scroll-delay", type=float, default=2.5)
    ap.add_argument("--include-reposts", action="store_true")
    ap.add_argument("--with-replies", action="store_true")
    ap.add_argument("--search", action="store_true", help="crawl X search for from:<handle> in the date window")
    ap.add_argument("--merge-existing", action="store_true", help="dedupe/merge into --out instead of replacing it")
    args = ap.parse_args()

    cookies = os.environ.get("X_COOKIES")
    if not cookies:
        raise SystemExit("ERROR: export X_COOKIES before running profile_crawl.py.")

    since = _start_of_day_utc(_parse_day(args.since))
    until = _end_of_day_exclusive_utc(_parse_day(args.until)) if args.until else None
    target_url = None
    if args.search:
        search_until = until.date().isoformat() if until else ""
        query = f"from:{args.handle} since:{args.since}"
        if search_until:
            query = f"{query} until:{search_until}"
        target_url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"

    scraper = ProfileTimelineScraper(
        cookies=cookies,
        handle=args.handle,
        since=since,
        until=until,
        include_reposts=args.include_reposts,
        with_replies=args.with_replies,
        target_url=target_url,
    )
    entries = await scraper.scrape(
        max_scrolls=args.max_scrolls,
        max_empty_scrolls=args.max_empty_scrolls,
        old_scrolls_to_stop=args.old_scrolls_to_stop,
        scroll_delay=args.scroll_delay,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    new_items = [_entry_to_ralph_dict(entry) for entry in entries]
    if args.merge_existing and out.exists():
        try:
            new_items = _merge_entries(json.loads(out.read_text()), new_items)
        except (OSError, ValueError, TypeError):
            pass
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(new_items, indent=2, ensure_ascii=False))
    tmp.replace(out)
    print(f"WROTE {len(new_items)} entries -> {out}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
