"""Tests for YouTube channel URL classification and enumeration parsing."""

from __future__ import annotations

from datetime import date

import pytest

from twitter_articlenator.sources.youtube_channel import (
    ChannelInfo,
    ChannelVideo,
    _parse_channel_json,
    _parse_channel_lines,
    filter_videos_by_title,
    is_youtube_channel_url,
    months_ago_yyyymmdd,
    parse_channel_url,
)

CANONICAL = "https://www.youtube.com/{}/videos"


@pytest.mark.parametrize(
    "url,expected_base",
    [
        ("https://www.youtube.com/@atmoio", "@atmoio"),
        ("https://www.youtube.com/@atmoio/videos", "@atmoio"),
        ("https://www.youtube.com/@atmoio/featured", "@atmoio"),
        ("https://www.youtube.com/@atmoio/streams", "@atmoio"),
        ("https://youtube.com/@atmoio", "@atmoio"),
        ("https://m.youtube.com/@atmoio", "@atmoio"),
        ("http://www.youtube.com/@atmoio", "@atmoio"),
        ("https://www.youtube.com/@atmoio/", "@atmoio"),
        ("https://www.youtube.com/channel/UCAwT-7B6StW_xzXPku6v7MQ", "channel/UCAwT-7B6StW_xzXPku6v7MQ"),
        ("https://www.youtube.com/channel/UCAwT-7B6StW_xzXPku6v7MQ/videos", "channel/UCAwT-7B6StW_xzXPku6v7MQ"),
        ("https://www.youtube.com/c/SomeName", "c/SomeName"),
        ("https://www.youtube.com/user/LegacyName", "user/LegacyName"),
    ],
)
def test_parse_channel_url_positive(url, expected_base):
    assert parse_channel_url(url) == CANONICAL.format(expected_base)
    assert is_youtube_channel_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=3eVl0aX-SW4",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=abc&list=PL123",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/live/abc123",
        "https://www.youtube.com/embed/abc123",
        "https://youtu.be/3eVl0aX-SW4",
        "https://www.youtube.com/",
        "https://www.youtube.com/feed/subscriptions",
        "https://www.youtube.com/results?search_query=x",
        "https://www.youtube-nocookie.com/@atmoio",
        "https://example.com/@atmoio",
        "ftp://www.youtube.com/@atmoio",
        "not a url",
        "",
    ],
)
def test_parse_channel_url_negative(url):
    assert parse_channel_url(url) is None
    assert is_youtube_channel_url(url) is False


def test_parse_channel_url_rejects_bare_channel_prefix():
    # "channel"/"c"/"user" without a following id/name are not channels.
    assert parse_channel_url("https://www.youtube.com/channel") is None
    assert parse_channel_url("https://www.youtube.com/c/") is None
    assert parse_channel_url("https://www.youtube.com/@") is None


def _sample_json():
    return {
        "channel": "Mo Bitar",
        "channel_id": "UCAwT-7B6StW_xzXPku6v7MQ",
        "uploader": "Mo Bitar",
        "uploader_id": "@atmoio",
        "title": "Mo Bitar - Videos",
        "entries": [
            {
                "id": "3eVl0aX-SW4",
                "title": "Elon's AI won.",
                "url": "https://www.youtube.com/watch?v=3eVl0aX-SW4",
                "duration": 834.0,
            },
            {
                "id": "plcuiOXn_Pk",
                "title": "Chinese OPEN SOURCE model good as FABLE",
                "duration": 600,
            },
        ],
    }


def test_parse_channel_json_builds_info():
    info = _parse_channel_json(_sample_json(), "https://www.youtube.com/@atmoio/videos")
    assert isinstance(info, ChannelInfo)
    assert info.channel_title == "Mo Bitar"
    assert info.channel_handle == "@atmoio"
    assert info.channel_id == "UCAwT-7B6StW_xzXPku6v7MQ"
    assert len(info.videos) == 2

    first = info.videos[0]
    assert first.video_id == "3eVl0aX-SW4"
    assert first.title == "Elon's AI won."
    assert first.url == "https://www.youtube.com/watch?v=3eVl0aX-SW4"
    assert first.duration == 834.0

    # url is synthesized from id when missing; duration coerced to float.
    second = info.videos[1]
    assert second.url == "https://www.youtube.com/watch?v=plcuiOXn_Pk"
    assert second.duration == 600.0


def test_parse_channel_json_drops_entries_without_id_and_dedupes():
    data = {
        "title": "X - Videos",
        "entries": [
            {"title": "no id here"},
            {"id": "keep1", "title": "A"},
            {"id": "keep1", "title": "duplicate"},
            None,
            {"id": "keep2", "title": "B"},
        ],
    }
    info = _parse_channel_json(data, "https://www.youtube.com/@x/videos")
    assert [v.video_id for v in info.videos] == ["keep1", "keep2"]
    # channel title falls back to the cleaned playlist title.
    assert info.channel_title == "X"


def test_parse_channel_json_flattens_nested_entries():
    data = {
        "channel": "Nested",
        "entries": [
            {"entries": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]},
            {"id": "c", "title": "C"},
        ],
    }
    info = _parse_channel_json(data, "https://www.youtube.com/@n/videos")
    assert [v.video_id for v in info.videos] == ["a", "b", "c"]


def test_parse_channel_json_empty():
    info = _parse_channel_json({"title": "Empty - Videos"}, "https://www.youtube.com/@e/videos")
    assert info.videos == []
    assert info.channel_title == "Empty"


@pytest.mark.parametrize(
    "today,months,expected",
    [
        (date(2026, 6, 21), 3, "20260321"),
        (date(2026, 1, 15), 3, "20251015"),  # crosses year boundary
        (date(2026, 5, 31), 3, "20260228"),  # day clamped to Feb (non-leap)
        (date(2024, 5, 31), 3, "20240229"),  # day clamped to Feb (leap)
        (date(2026, 6, 21), 12, "20250621"),
    ],
)
def test_months_ago_yyyymmdd(today, months, expected):
    assert months_ago_yyyymmdd(today, months) == expected


def test_months_ago_yyyymmdd_rejects_nonpositive():
    with pytest.raises(ValueError, match="months must be >= 1"):
        months_ago_yyyymmdd(date(2026, 6, 21), 0)


def test_parse_channel_lines():
    text = "\n".join(
        [
            "abc123\t20260620\t1401\tTheo\thttps://www.youtube.com/watch?v=abc123\tIt's time to go bigger",
            # title containing a tab must not corrupt earlier fields (maxsplit=5)
            "def456\t20260618\t1484\tTheo\thttps://www.youtube.com/watch?v=def456\tloops\tand\tmore",
            "def456\t20260618\t1484\tTheo\tdup\tduplicate id dropped",
            "\t\t\t\t\t",  # empty/garbage id -> skipped
            "too\tfew\tfields",  # malformed -> skipped
            "ghi789\tNA\tNA\tTheo\tNA\tno url or duration",
        ]
    )
    info = _parse_channel_lines(text, "https://www.youtube.com/@t3dotgg/videos", "@t3dotgg")
    assert [v.video_id for v in info.videos] == ["abc123", "def456", "ghi789"]
    assert info.channel_title == "Theo"
    assert info.channel_handle == "@t3dotgg"

    first = info.videos[0]
    assert first.upload_date == "20260620"
    assert first.duration == 1401.0
    assert first.url == "https://www.youtube.com/watch?v=abc123"

    second = info.videos[1]
    assert second.title == "loops\tand\tmore"  # tab-containing title preserved

    # NA url/duration -> synthesized url, no duration/date
    third = info.videos[2]
    assert third.url == "https://www.youtube.com/watch?v=ghi789"
    assert third.duration is None
    assert third.upload_date is None


def test_parse_channel_lines_handle_fallback_title():
    text = "abc123\t20260620\t10\tNA\thttps://y/abc123\tTitle"
    info = _parse_channel_lines(text, "https://www.youtube.com/@h/videos", "@h")
    # channel column is NA -> fall back to the handle for the title
    assert info.channel_title == "@h"


def test_filter_videos_by_title():
    videos = [
        ChannelVideo(video_id="1", title="Wading Through AI - Episode 1", url="u1"),
        ChannelVideo(video_id="2", title="A totally unrelated video", url="u2"),
        ChannelVideo(video_id="3", title="WADING THROUGH AI - Episode 2", url="u3"),
    ]
    out = filter_videos_by_title(videos, "wading through ai")
    assert [v.video_id for v in out] == ["1", "3"]  # case-insensitive
    assert filter_videos_by_title(videos, "no-such-thing") == []
    # surrounding whitespace in the query is ignored
    assert len(filter_videos_by_title(videos, "  episode  ")) == 2
