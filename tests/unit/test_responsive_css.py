"""Regression guards for the mobile/responsive CSS invariants.

These pin down the iPhone bugs reported against the live UI:

* iOS Safari auto-zooms the page whenever a *focused* form control has a
  ``font-size`` below 16px. Every free-text field in this app is a
  ``<textarea>``, so any textarea below 16px triggers the zoom.
* The status row (spinner + status text) is a flex row. Without shrink/wrap
  safety a long "link + name" string can neither wrap nor shrink, so it
  overflows the panel and squashes the spinner.

The tests parse ``style.css`` directly (no browser needed) so they stay fast
and deterministic, and they fail loudly if anyone reintroduces a sub-16px form
control or removes the flex-overflow protections.
"""

import re
from pathlib import Path

import pytest

STYLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "twitter_articlenator"
    / "static"
    / "style.css"
)

# Input types that are NOT free-text and therefore never trigger the iOS zoom.
_NON_TEXT_INPUT = (
    "checkbox|radio|file|button|submit|reset|hidden|range|color|image"
)


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _iter_rules(css: str):
    """Yield ``(selector, body)`` for every innermost rule.

    The regex matches innermost ``{ ... }`` blocks (bodies contain no braces),
    which naturally flattens rules nested inside ``@media`` / ``@supports``.
    """
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _strip_comments(css)):
        yield match.group(1).strip(), match.group(2).strip()


def _font_size_px(body: str):
    match = re.search(r"font-size:\s*([\d.]+)px", body)
    return float(match.group(1)) if match else None


def _targets_text_control(selector: str) -> bool:
    """True if the selector targets a free-text form control (textarea / text
    input / select) as an element (not a class that merely contains the word)."""
    if re.search(r"(?:^|[\s,>+~])textarea(?:\b|:|\[|,|$)", selector):
        return True
    if re.search(r"(?:^|[\s,>+~])select(?:\b|:|,|$)", selector):
        return True
    if re.search(
        r'input\[type="(?:text|url|search|email|number|tel|password|date|datetime-local)"\]',
        selector,
    ):
        return True
    # A bare ``input`` element rule that is not narrowed to a non-text type.
    if re.search(rf'(?:^|[\s,>+~])input(?!\[type="(?:{_NON_TEXT_INPUT})"\])(?:\b|:|\[|,|$)', selector):
        return True
    return False


@pytest.fixture(scope="module")
def css() -> str:
    return STYLE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules(css):
    return list(_iter_rules(css))


def test_textarea_font_size_prevents_ios_zoom(rules):
    """Positive space: every textarea rule that sets a font-size uses >= 16px."""
    sizes = [
        _font_size_px(body)
        for selector, body in rules
        if re.search(r"(?:^|[\s,>+~])textarea(?:\b|:|\[|,|$)", selector)
    ]
    sizes = [s for s in sizes if s is not None]
    assert sizes, "expected at least one textarea rule to declare a font-size"
    too_small = [s for s in sizes if s < 16]
    assert not too_small, (
        f"textarea font-size below 16px triggers iOS Safari auto-zoom: {too_small}"
    )


def test_no_text_form_control_below_16px(rules):
    """Negative space / forward guard: no text input, select, or textarea rule
    may set a font-size under 16px (the iOS zoom threshold)."""
    offenders = []
    for selector, body in rules:
        size = _font_size_px(body)
        if size is None or size >= 16:
            continue
        if _targets_text_control(selector):
            offenders.append((selector, size))
    assert not offenders, (
        f"form controls with font-size < 16px will auto-zoom on iOS: {offenders}"
    )


def test_spinner_cannot_be_squished_by_long_text(rules):
    """The spinner must not shrink when the status text next to it is long."""
    body = next((b for s, b in rules if s == ".spinner"), None)
    assert body is not None, "expected a .spinner rule"
    assert re.search(r"flex-shrink:\s*0", body) or re.search(r"flex:\s*0\s+0", body), (
        ".spinner needs flex-shrink:0 so a long status string can't squash it"
    )


def test_status_text_wraps_long_links(css):
    """Edge case: a long 'link + name' status string must shrink and wrap
    inside the flex status row instead of overflowing the panel."""
    stripped = _strip_comments(css)
    block = re.search(r"#status-text[^{]*\{([^}]*)\}", stripped)
    assert block, "expected a rule targeting #status-text"
    body = block.group(1)
    assert re.search(r"min-width:\s*0", body), (
        "#status-text needs min-width:0 to be allowed to shrink/wrap in a flex row"
    )
    assert re.search(r"(?:overflow-wrap|word-break):", body), (
        "#status-text needs overflow-wrap/word-break so long URLs break instead of overflowing"
    )
