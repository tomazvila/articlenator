"""Base classes for content sources."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass
class Article:
    """Represents a fetched article."""

    title: str
    author: str
    content: str  # HTML content
    published_at: datetime | None
    source_url: str
    source_type: str  # "twitter", "substack", etc.


@runtime_checkable
class ContentSource(Protocol):
    """Protocol for content sources.

    Using Protocol instead of ABC provides structural subtyping (duck typing),
    which is more flexible and Pythonic. Classes don't need to explicitly
    inherit from ContentSource - they just need to implement the methods.

    The @runtime_checkable decorator allows isinstance() checks.
    """

    def can_handle(self, url: str) -> bool:
        """Check if this source can handle the given URL.

        Args:
            url: The URL to check.

        Returns:
            True if this source can handle the URL, False otherwise.
        """
        ...

    async def fetch(self, url: str) -> Article:
        """Fetch an article from the given URL.

        Args:
            url: The URL to fetch.

        Returns:
            An Article containing the fetched content.

        Raises:
            ValueError: If the URL is invalid or content cannot be fetched.
        """
        ...
