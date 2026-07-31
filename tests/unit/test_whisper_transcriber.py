"""Tests for whisper-cli output parsing."""

from __future__ import annotations

from twitter_articlenator.sources.whisper_transcriber import parse_whisper_json


def test_parse_whisper_json_basic(tmp_path):
    p = tmp_path / "chunk.json"
    p.write_text(
        '{"transcription":[{"offsets":{"from":0,"to":1500},"text":" hello world "},'
        '{"offsets":{"from":1500,"to":3000},"text":"  "}]}',
        encoding="utf-8",
    )
    segments = parse_whisper_json(p)
    assert len(segments) == 1  # blank segment dropped
    assert segments[0].text == "hello world"
    assert segments[0].start == 0.0
    assert segments[0].end == 1.5


def test_parse_whisper_json_tolerates_invalid_utf8(tmp_path):
    # whisper-cli can emit invalid UTF-8 bytes for music / no-speech audio.
    p = tmp_path / "chunk.json"
    p.write_bytes(
        b'{"transcription":[{"offsets":{"from":0,"to":1000},"text":"noi\xffse"}]}'
    )
    segments = parse_whisper_json(p)  # must not raise UnicodeDecodeError
    assert len(segments) == 1
    assert segments[0].text.startswith("noi")  # invalid byte replaced, not fatal


def test_parse_whisper_json_empty_transcription(tmp_path):
    p = tmp_path / "chunk.json"
    p.write_text('{"transcription":[]}', encoding="utf-8")
    assert parse_whisper_json(p) == []
