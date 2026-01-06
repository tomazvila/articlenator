"""Shared pytest fixtures."""

import pytest


@pytest.fixture
def sample_article():
    """Create a sample Article for testing."""
    from datetime import datetime

    from twitter_articlenator.sources.base import Article

    return Article(
        title="Test Article",
        author="testuser",
        content="<p>This is test content.</p>",
        published_at=datetime(2025, 12, 29, 10, 30, 0),
        source_url="https://x.com/testuser/status/123456789",
        source_type="twitter",
    )


@pytest.fixture
def app():
    """Create Flask test application."""
    from twitter_articlenator.app import create_app

    app = create_app(test_config={"TESTING": True})
    yield app


@pytest.fixture
def client(app):
    """Create Flask test client."""
    return app.test_client()


@pytest.fixture
def tmp_output_dir(tmp_path):
    """Create a temporary output directory."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return output_dir
