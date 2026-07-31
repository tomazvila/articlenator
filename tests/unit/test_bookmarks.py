"""Tests for sources/bookmarks.py - bookmark GraphQL parsing."""

from twitter_articlenator.sources.bookmarks import BookmarkScraper


def _tweet_result(
    *,
    rest_id="123456789",
    screen_name_core=None,
    name_core=None,
    screen_name_legacy=None,
    name_legacy=None,
    full_text="hello world",
):
    """Build a minimal tweet_results.result object.

    screen_name/name can be placed under result.core (current X schema) or
    result.legacy (older schema) independently to exercise both code paths.
    """
    user_core = {}
    if screen_name_core is not None:
        user_core["screen_name"] = screen_name_core
    if name_core is not None:
        user_core["name"] = name_core

    user_legacy = {}
    if screen_name_legacy is not None:
        user_legacy["screen_name"] = screen_name_legacy
    if name_legacy is not None:
        user_legacy["name"] = name_legacy

    return {
        "__typename": "Tweet",
        "rest_id": rest_id,
        "core": {
            "user_results": {
                "result": {
                    "__typename": "User",
                    "core": user_core,
                    "legacy": user_legacy,
                }
            }
        },
        "legacy": {"full_text": full_text, "entities": {}},
    }


class TestParseTweetResultAuthor:
    """Author/handle extraction across X schema variants."""

    def test_author_from_core_current_schema(self):
        """Current X schema: screen_name/name live under result.core."""
        scraper = BookmarkScraper("auth_token=x; ct0=y")
        entry = scraper._parse_tweet_result(
            _tweet_result(screen_name_core="SahilBloom", name_core="Sahil Bloom")
        )
        assert entry is not None
        assert entry.author == "SahilBloom"
        assert entry.display_name == "Sahil Bloom"
        assert entry.tweet_url == "https://x.com/SahilBloom/status/123456789"

    def test_author_from_legacy_old_schema(self):
        """Backward compatibility: older responses keep author under result.legacy."""
        scraper = BookmarkScraper("auth_token=x; ct0=y")
        entry = scraper._parse_tweet_result(
            _tweet_result(screen_name_legacy="oldhandle", name_legacy="Old Name")
        )
        assert entry is not None
        assert entry.author == "oldhandle"
        assert entry.display_name == "Old Name"

    def test_core_takes_precedence_over_legacy(self):
        """When both present, the current (core) values win."""
        scraper = BookmarkScraper("auth_token=x; ct0=y")
        entry = scraper._parse_tweet_result(
            _tweet_result(
                screen_name_core="newhandle",
                name_core="New Name",
                screen_name_legacy="oldhandle",
                name_legacy="Old Name",
            )
        )
        assert entry.author == "newhandle"
        assert entry.display_name == "New Name"

    def test_missing_author_yields_empty_but_keeps_id(self):
        """Edge: no author anywhere -> empty author, entry still parsed by id."""
        scraper = BookmarkScraper("auth_token=x; ct0=y")
        entry = scraper._parse_tweet_result(_tweet_result(rest_id="999"))
        assert entry is not None
        assert entry.author == ""
        assert entry.tweet_id == "999"

    def test_visibility_wrapper_unwrapped(self):
        """TweetWithVisibilityResults wrapper is unwrapped before author lookup."""
        scraper = BookmarkScraper("auth_token=x; ct0=y")
        inner = _tweet_result(screen_name_core="wrapped", name_core="Wrapped User")
        wrapped = {"__typename": "TweetWithVisibilityResults", "tweet": inner}
        entry = scraper._parse_tweet_result(wrapped)
        assert entry is not None
        assert entry.author == "wrapped"

    def test_tombstone_returns_none(self):
        """Tombstoned (deleted/withheld) tweets are skipped."""
        scraper = BookmarkScraper("auth_token=x; ct0=y")
        assert scraper._parse_tweet_result({"__typename": "TweetTombstone"}) is None

    def test_no_rest_id_returns_none(self):
        """A result without rest_id cannot be identified."""
        scraper = BookmarkScraper("auth_token=x; ct0=y")
        assert scraper._parse_tweet_result(_tweet_result(rest_id="")) is None
