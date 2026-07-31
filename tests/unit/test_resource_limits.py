"""Per-user and global heavy-resource limit tests."""

from __future__ import annotations

from twitter_articlenator.resource_limits import ResourceLimiter


def test_per_user_limit_blocks_second_lease_until_release():
    limiter = ResourceLimiter({"playwright": (1, 3)})

    first = limiter.try_acquire("playwright", "alice")

    assert first is not None
    assert limiter.try_acquire("playwright", "alice") is None
    first.release()
    assert limiter.try_acquire("playwright", "alice") is not None


def test_global_limit_applies_across_users():
    limiter = ResourceLimiter({"transcription": (1, 2)})

    alice = limiter.try_acquire("transcription", "alice")
    bob = limiter.try_acquire("transcription", "bob")

    assert alice is not None
    assert bob is not None
    assert limiter.try_acquire("transcription", "carol") is None
    alice.release()
    assert limiter.try_acquire("transcription", "carol") is not None


def test_release_is_idempotent():
    limiter = ResourceLimiter({"playwright": (1, 1)})
    lease = limiter.try_acquire("playwright", "alice")
    assert lease is not None

    lease.release()
    lease.release()

    assert limiter.active("playwright") == {"global": 0, "users": {}}
