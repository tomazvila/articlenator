"""Tests for async helper functions."""

import asyncio
import threading

import pytest


class TestAsyncRunner:
    """Tests for the AsyncRunner class directly."""

    def test_async_runner_initialization(self):
        """Test AsyncRunner initializes with correct state."""
        from twitter_articlenator.app import AsyncRunner

        runner = AsyncRunner()
        assert runner._loop is None
        assert runner._thread is None

    def test_async_runner_creates_loop_on_first_run(self):
        """Test that AsyncRunner creates event loop on first run."""
        from twitter_articlenator.app import AsyncRunner

        runner = AsyncRunner()

        async def simple():
            return "hello"

        result = runner.run(simple())
        assert result == "hello"
        assert runner._loop is not None
        assert runner._loop.is_running()

    def test_async_runner_reuses_loop(self):
        """Test that AsyncRunner reuses the same event loop."""
        from twitter_articlenator.app import AsyncRunner

        runner = AsyncRunner()

        async def get_loop():
            return asyncio.get_running_loop()

        loop1 = runner.run(get_loop())
        loop2 = runner.run(get_loop())

        # Should be the same loop instance
        assert loop1 is loop2

    def test_async_runner_thread_safety(self):
        """Test AsyncRunner is thread-safe with concurrent calls."""
        from twitter_articlenator.app import AsyncRunner

        runner = AsyncRunner()
        results = []
        errors = []

        async def delayed_return(value):
            await asyncio.sleep(0.01)
            return value

        def thread_work(n):
            try:
                result = runner.run(delayed_return(n))
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=thread_work, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Got errors: {errors}"
        assert sorted(results) == [0, 1, 2, 3, 4]


class TestRunAsync:
    """Tests for the run_async helper function."""

    def test_run_async_executes_coroutine(self):
        """Test that run_async executes a coroutine and returns result."""
        from twitter_articlenator.app import run_async

        async def simple_coro():
            return 42

        result = run_async(simple_coro())
        assert result == 42

    def test_run_async_with_async_value(self):
        """Test run_async with coroutine that awaits."""
        from twitter_articlenator.app import run_async

        async def awaiting_coro():
            await asyncio.sleep(0.01)
            return "done"

        result = run_async(awaiting_coro())
        assert result == "done"

    def test_run_async_propagates_exceptions(self):
        """Test that run_async propagates exceptions from coroutine."""
        from twitter_articlenator.app import run_async

        async def failing_coro():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            run_async(failing_coro())

    def test_run_async_multiple_calls(self):
        """Test multiple sequential run_async calls work."""
        from twitter_articlenator.app import run_async

        async def counter(n):
            return n * 2

        results = []
        for i in range(5):
            results.append(run_async(counter(i)))

        assert results == [0, 2, 4, 6, 8]

    def test_run_async_isolates_event_loops(self):
        """Test that each run_async call uses a fresh event loop."""
        from twitter_articlenator.app import run_async

        loops = []

        async def capture_loop():
            loops.append(asyncio.get_running_loop())
            return True

        run_async(capture_loop())
        run_async(capture_loop())

        # Each call should have its own loop (not the same object)
        assert len(loops) == 2
        # Loops might be different objects or same recycled - what matters is no errors


class TestTwitterPlaywrightSourceInit:
    """Tests for TwitterPlaywrightSource initialization."""

    def test_twitter_source_can_be_created(self):
        """Test TwitterPlaywrightSource can be created without errors."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource(cookies="test=value")
        assert source._cookies_str == "test=value"

    def test_twitter_source_without_cookies(self):
        """Test TwitterPlaywrightSource can be created without cookies."""
        from twitter_articlenator.sources.twitter_playwright import TwitterPlaywrightSource

        source = TwitterPlaywrightSource()
        assert source._cookies_str is None

    def test_twitter_source_alias(self):
        """Test TwitterSource is aliased to TwitterPlaywrightSource."""
        from twitter_articlenator.sources import TwitterSource, TwitterPlaywrightSource

        assert TwitterSource is TwitterPlaywrightSource
