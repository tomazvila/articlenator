"""Content sources registry."""

from .base import Article, ContentSource
from .twitter_playwright import TwitterPlaywrightSource
from .web import WebArticleSource

# Keep old import for backwards compatibility but prefer Playwright version
TwitterSource = TwitterPlaywrightSource

__all__ = [
    "Article",
    "ContentSource",
    "TwitterSource",
    "TwitterPlaywrightSource",
    "WebArticleSource",
    "get_source_for_url",
]

# Registered sources in priority order
_SOURCES: list[type[ContentSource]] = [
    TwitterPlaywrightSource,  # Playwright-based, more reliable
    WebArticleSource,  # Fallback for any HTTP URL
]


def get_source_for_url(url: str, **kwargs) -> ContentSource | None:
    """Get the appropriate source for a URL.

    Args:
        url: URL to find a source for.
        **kwargs: Additional arguments passed to source constructor.

    Returns:
        ContentSource instance if a handler is found, None otherwise.
    """
    for source_cls in _SOURCES:
        # Create instance to check if it can handle the URL
        source = source_cls(
            **{k: v for k, v in kwargs.items() if k in _get_init_params(source_cls)}
        )
        if source.can_handle(url):
            return source
    return None


def _get_init_params(cls: type) -> set[str]:
    """Get parameter names for a class's __init__ method."""
    import inspect

    sig = inspect.signature(cls.__init__)
    return {p.name for p in sig.parameters.values() if p.name != "self"}
