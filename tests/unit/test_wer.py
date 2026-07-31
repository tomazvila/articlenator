"""Unit tests for WER normalization and scoring."""

from __future__ import annotations

import pytest

from twitter_articlenator.wer import normalize, word_error_rate


def test_normalize_strips_speaker_labels():
    assert normalize("JUSTICE KAGAN: Hello, there.") == "hello there"
    assert normalize("MR. CLEMENT: We submit, Your Honor.") == "we submit your honor"


def test_normalize_strips_stage_directions():
    assert normalize("we go [crosstalk 00:11:26] now") == "we go now"
    assert normalize("thank you (Applause) everyone") == "thank you everyone"


def test_normalize_lowercases_and_removes_punctuation():
    assert normalize("The Cat, sat!") == "the cat sat"


def test_normalize_expands_common_contractions():
    assert normalize("we won't go") == "we will not go"
    assert normalize("they can't stay") == "they cannot stay"


def test_wer_identical_is_zero():
    assert word_error_rate("the cat sat on the mat", "the cat sat on the mat") == 0.0


def test_wer_single_substitution():
    assert word_error_rate("the cat sat", "the dog sat") == pytest.approx(1 / 3)


def test_wer_deletion():
    assert word_error_rate("a b c d", "a b c") == pytest.approx(1 / 4)


def test_wer_ignores_speaker_labels_in_reference():
    assert word_error_rate("MR. SMITH: hello world", "hello world") == 0.0


def test_wer_empty_reference_raises():
    with pytest.raises(ValueError):
        word_error_rate("[music only]", "anything at all")
