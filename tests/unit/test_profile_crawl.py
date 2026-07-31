"""Tests for zettel_ralph/profile_crawl.py timeline parsing."""

from datetime import datetime, timezone

from zettel_ralph.profile_crawl import ProfileTimelineScraper, _merge_entries


def _tweet_result(rest_id: str, author: str, created_at: str, text: str = "hello"):
    return {
        "__typename": "Tweet",
        "rest_id": rest_id,
        "core": {
            "user_results": {
                "result": {
                    "__typename": "User",
                    "core": {"screen_name": author, "name": author},
                    "legacy": {},
                }
            }
        },
        "legacy": {
            "full_text": text,
            "created_at": created_at,
            "entities": {},
        },
    }


def _timeline_item(tweet):
    return {
        "entryId": f"tweet-{tweet['rest_id']}",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {
                "itemType": "TimelineTweet",
                "tweet_results": {"result": tweet},
            },
        },
    }


def test_profile_parser_expands_multi_item_timeline_modules():
    scraper = ProfileTimelineScraper(
        cookies="auth_token=x; ct0=y",
        handle="thdxr",
        since=datetime(2025, 12, 26, tzinfo=timezone.utc),
    )
    data = {
        "data": {
            "user": {
                "result": {
                    "timeline_v2": {
                        "timeline": {
                            "instructions": [
                                {
                                    "type": "TimelineAddEntries",
                                    "entries": [
                                        {
                                            "content": {
                                                "entryType": "TimelineTimelineModule",
                                                "items": [
                                                    {
                                                        "item": {
                                                            "itemContent": _timeline_item(
                                                                _tweet_result(
                                                                    "1",
                                                                    "thdxr",
                                                                    "Fri Jun 26 10:00:00 +0000 2026",
                                                                )
                                                            )["content"]["itemContent"]
                                                        }
                                                    },
                                                    {
                                                        "item": {
                                                            "itemContent": _timeline_item(
                                                                _tweet_result(
                                                                    "2",
                                                                    "thdxr",
                                                                    "Thu Jun 25 10:00:00 +0000 2026",
                                                                )
                                                            )["content"]["itemContent"]
                                                        }
                                                    },
                                                ],
                                            }
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        }
    }

    entries = scraper._parse_graphql_response(data)

    assert [entry.tweet_id for entry in entries] == ["1", "2"]


def test_profile_scope_filters_other_authors_and_old_posts():
    scraper = ProfileTimelineScraper(
        cookies="auth_token=x; ct0=y",
        handle="thdxr",
        since=datetime(2025, 12, 26, tzinfo=timezone.utc),
    )
    own_recent = scraper._parser._parse_tweet_result(
        _tweet_result("1", "thdxr", "Fri Jun 26 10:00:00 +0000 2026")
    )
    other_recent = scraper._parser._parse_tweet_result(
        _tweet_result("2", "someone_else", "Fri Jun 26 10:00:00 +0000 2026")
    )
    own_old = scraper._parser._parse_tweet_result(
        _tweet_result("3", "thdxr", "Thu Dec 25 10:00:00 +0000 2025")
    )

    assert scraper._in_scope(own_recent) == (True, False)
    assert scraper._in_scope(other_recent) == (False, False)
    assert scraper._in_scope(own_old) == (False, True)


def test_merge_entries_dedupes_and_sorts_by_created_at():
    merged = _merge_entries(
        [
            {"tweet_id": "1", "created_at": "2026-01-01T00:00:00+00:00"},
            {"tweet_id": "2", "created_at": "2026-01-03T00:00:00+00:00"},
        ],
        [
            {"tweet_id": "1", "created_at": "2026-01-02T00:00:00+00:00"},
            {"tweet_id": "3", "created_at": "2026-01-04T00:00:00+00:00"},
        ],
    )

    assert [item["tweet_id"] for item in merged] == ["3", "2", "1"]
    assert merged[-1]["created_at"] == "2026-01-02T00:00:00+00:00"
