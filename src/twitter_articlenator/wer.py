"""Word Error Rate utilities for evaluating transcription quality.

Both the reference (ground-truth) transcript and the hypothesis (whisper
output) are normalized before scoring so that punctuation, casing, speaker
labels (``JUSTICE KAGAN:``) and stage directions (``[crosstalk]``,
``(Laughter)``) do not count as errors. Normalization is intentionally
deterministic so a video's WER is comparable across runs (baseline + margin).
"""

from __future__ import annotations

import re

# "JUSTICE KAGAN:", "MR. CLEMENT:", "PRESIDENT DONALD J. TRUMP:" at line start.
_SPEAKER_LABEL = re.compile(r"^\s*[A-Z][A-Z0-9.'\- ]{1,48}:\s*", re.MULTILINE)
# Bracketed / parenthesised stage directions: [crosstalk], (Laughter), (Applause).
_STAGE_DIRECTION = re.compile(r"[\[(][^\])]*[\])]")
# Collapse anything that is not a word character or apostrophe.
_NON_WORD = re.compile(r"[^a-z0-9'\s]")
_WHITESPACE = re.compile(r"\s+")

_CONTRACTIONS = {
    "won't": "will not",
    "can't": "cannot",
    "n't": " not",
    "'re": " are",
    "'ve": " have",
    "'ll": " will",
    "'d": " would",
    "'m": " am",
    "let's": "let us",
    "'s": " s",
}


def normalize(text: str) -> str:
    """Normalize a transcript for WER comparison.

    Lowercases, strips speaker labels and stage directions, expands a small set
    of contractions, removes punctuation, and collapses whitespace.
    """
    text = _SPEAKER_LABEL.sub(" ", text)
    text = _STAGE_DIRECTION.sub(" ", text)
    text = text.lower()
    for src, dst in _CONTRACTIONS.items():
        text = text.replace(src, dst)
    text = _NON_WORD.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Return the WER of ``hypothesis`` against ``reference`` (both normalized).

    Raises ValueError when the reference is empty after normalization.
    """
    ref = normalize(reference)
    hyp = normalize(hypothesis)
    if not ref:
        raise ValueError("reference is empty after normalization")

    # Prefer jiwer when available; fall back to a word-level edit distance so
    # the function is usable (and unit-testable) without the dependency.
    try:
        from jiwer import wer as _jiwer_wer
    except ImportError:
        return _edit_distance_wer(ref.split(), hyp.split())
    return float(_jiwer_wer(ref, hyp))


def _edit_distance_wer(ref_words: list[str], hyp_words: list[str]) -> float:
    """Levenshtein word error rate (substitutions + insertions + deletions)."""
    n, m = len(ref_words), len(hyp_words)
    if n == 0:
        raise ValueError("reference is empty")
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        curr = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if ref_words[i - 1] == hyp_words[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[m] / n
