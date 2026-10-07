"""Tests for the pure ASR helpers: model label, quality check, segments, speakers."""

from __future__ import annotations

import json

import pytest

from twitter_articlenator.sources import asr_tools as asr

# 40 different sentences of 10-14 words: "normal speech" without repeats.
_TOPICS = [
    "planche",
    "front lever",
    "handstand",
    "maltese",
    "iron cross",
    "muscle-up",
    "back lever",
    "human flag",
    "V-sit",
    "L-sit",
]
_ACTIONS = ["hold", "press", "lean", "negative", "push-up", "row", "raise", "pull"]


def _normal_sentences(count: int) -> list[str]:
    out = []
    for i in range(count):
        topic = _TOPICS[i % len(_TOPICS)]
        action = _ACTIONS[(i // len(_TOPICS)) % len(_ACTIONS)]
        out.append(
            f"In session {i} we work on the {topic} {action} for {i % 7 + 2} sets "
            f"with {i % 5 + 3} seconds rest number {i}."
        )
    return out


def _segments(sentences: list[str], seconds_each: float = 5.0) -> list[asr.Seg]:
    return [asr.Seg(i * seconds_each, (i + 1) * seconds_each, s) for i, s in enumerate(sentences)]


# --------------------------------------------------------------------------- #
# model identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path,label",
    [
        ("/home/x/ggml-small.en.bin", "ggml-small.en"),
        ("/nix/store/27cj9jgv586w5ix4kgf8yhb41jd4vpf4-ggml-large-v3.bin", "ggml-large-v3"),
        ("models/ggml-large-v3-turbo.bin", "ggml-large-v3-turbo"),
        ("custom-model.bin", "custom-model"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_model_label_comes_from_the_file_name(path, label):
    assert asr.model_label(path) == label


def test_english_only_model_detection():
    assert asr.is_english_only_model("/x/ggml-small.en.bin")
    assert not asr.is_english_only_model("/x/ggml-large-v3.bin")


def test_require_model_file_missing_gives_clear_error(tmp_path):
    missing = tmp_path / "ggml-large-v3.bin"
    with pytest.raises(asr.WhisperModelMissingError) as info:
        asr.require_model_file(missing, setting="WHISPER_MODEL")
    msg = str(info.value)
    assert str(missing) in msg
    assert "WHISPER_MODEL" in msg
    assert "does not fall back" in msg


def test_require_model_file_unset_gives_clear_error():
    with pytest.raises(asr.WhisperModelMissingError, match="No whisper model is configured"):
        asr.require_model_file(None, setting="WHISPER_MODEL")


def test_require_model_file_existing(tmp_path):
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"x")
    assert asr.require_model_file(model, setting="X") == model


# --------------------------------------------------------------------------- #
# quality check
# --------------------------------------------------------------------------- #


def test_normal_transcript_is_ok():
    sentences = _normal_sentences(120)  # 600 s of audio, about 150-170 wpm
    segs = _segments(sentences)
    report = asr.assess_transcript("\n".join(sentences), segments=segs, duration=600.0)
    assert report.quality == asr.QUALITY_OK
    assert report.issues == []
    assert report.spans == []


def test_looped_transcript_is_degraded():
    """Synthetic whisper loop: real speech, then one line repeated to the end."""
    sentences = _normal_sentences(40)
    loop = "You have to have a very high level of muscle strength."
    looped = sentences + [loop] * 400
    segs = _segments(looped, seconds_each=5.0)
    report = asr.assess_transcript("\n".join(looped), segments=segs, duration=len(looped) * 5.0)
    assert report.quality == asr.QUALITY_DEGRADED
    joined = " | ".join(report.issues)
    assert "damaged spans hold" in joined
    assert "very high level of muscle strength" in joined
    # The span starts at the first copy (segment 40 at 200 s) and ends at the end.
    assert report.span_markers() == [["03:20", "36:40"]]
    assert "low speech density" in joined


def test_looped_text_without_segments_is_degraded():
    """Text-only transcripts (whisper -otxt, one segment per line) are checked too."""
    lines = _normal_sentences(30) + ["I'm going to do dynamic exercises."] * 200
    report = asr.assess_transcript("\n".join(lines))
    assert report.quality == asr.QUALITY_DEGRADED
    assert any(i.startswith("loop: lines 31-230") for i in report.issues)
    assert any(i.startswith("not checked (audio duration unknown)") for i in report.issues)


def test_loop_inside_one_long_line_is_found():
    """The app pipeline writes transcript.txt as one long line; the phrase rule finds loops."""
    text = " ".join(_normal_sentences(20)) + " " + " ".join(["and then we go to the next set"] * 60)
    report = asr.assess_transcript(text)
    assert report.quality == asr.QUALITY_DEGRADED
    assert any("repeated phrase" in i for i in report.issues)


def test_local_loop_in_a_clean_file_is_partial_with_marker_range():
    """BBuB0XMVhDw case: one loop of ~300 words in an otherwise clean lecture."""
    sentences = _normal_sentences(200)
    sentences[100:100] = ["If you get your deep handstand push-ups from 3 to 7 reps, do this."] * 12
    segs = _segments(sentences)
    report = asr.assess_transcript(None, segments=segs, duration=len(sentences) * 5.0)
    assert report.quality == asr.QUALITY_PARTIAL
    assert report.span_markers() == [["08:20", "09:20"]]
    assert any(i.startswith("loop: [08:20]-[09:20]") for i in report.issues)


def test_short_local_loop_is_minor_only():
    sentences = _normal_sentences(300)
    sentences[150:150] = ["I'm going to show you how."] * 3
    report = asr.assess_transcript(None, segments=_segments(sentences), duration=1515.0)
    assert report.quality == asr.QUALITY_OK
    assert report.issues and all(i.startswith("minor: loop:") for i in report.issues)


def test_rolling_captions_are_found():
    """u_kMa8Ejv-M / yXYXqeTrXRU case: each phrase is written two or three times."""
    lines = []
    sentences = _normal_sentences(80)
    for a, b in zip(sentences, sentences[1:]):
        lines += [f"{a} {b}", b]  # "A B", "B", "B C", "C", ...
    report = asr.assess_transcript("\n".join(lines), duration=600.0)
    assert report.quality == asr.QUALITY_DEGRADED
    assert any("repeat of the next or previous segment" in i for i in report.issues)


def test_implausible_speech_rate_is_degraded():
    sentences = _normal_sentences(150)  # about 2,000 words
    report = asr.assess_transcript("\n".join(sentences), duration=300.0)
    assert report.quality == asr.QUALITY_DEGRADED
    assert any("implausible speech rate" in i for i in report.issues)


def test_no_duration_gives_unknown_not_ok():
    sentences = _normal_sentences(60)
    report = asr.assess_transcript("\n".join(sentences))
    assert report.quality == asr.QUALITY_UNKNOWN
    assert report.issues == [
        "not checked (audio duration unknown): speech density, speech rate, end of audio"
    ]


def test_whisper_console_lines_are_parsed_as_segments():
    text = "\n".join(
        f"[00:00:{i * 5:02d}.000 --> 00:00:{i * 5 + 4:02d}.500]   {s}"
        for i, s in enumerate(_normal_sentences(10))
    )
    segs = asr.segments_from_stdout_text(text)
    assert segs[1].start == 5.0 and segs[1].end == 9.5
    assert segs[0].text.startswith("In session 0")
    report = asr.assess_transcript(text, duration=50.0)
    # Timestamp digits are not words.
    assert report.metrics["words"] == len(asr._norm(" ".join(_normal_sentences(10))).split())


def test_low_speech_density_is_degraded():
    sentences = _normal_sentences(10)  # about 130 words in 20 minutes
    report = asr.assess_transcript("\n".join(sentences), duration=1200.0)
    assert report.quality == asr.QUALITY_DEGRADED
    assert any("low speech density" in i for i in report.issues)


def test_transcript_that_stops_early_is_degraded():
    sentences = _normal_sentences(120)  # segments cover 0-600 s
    report = asr.assess_transcript(None, segments=_segments(sentences), duration=1800.0)
    assert report.quality == asr.QUALITY_DEGRADED
    assert any("transcript stops at 10:00 of 30:00" in i for i in report.issues)


def test_empty_and_missing_transcripts():
    assert asr.assess_transcript("   \n").quality == asr.QUALITY_DEGRADED
    assert asr.assess_transcript("").issues == ["empty transcript"]
    assert asr.assess_transcript(None).quality == asr.QUALITY_UNKNOWN


def test_short_repeated_replies_are_not_a_loop():
    sentences = _normal_sentences(60)
    mixed = []
    for s in sentences:
        mixed += [s, "Yeah.", "Okay."]
    report = asr.assess_transcript("\n".join(mixed), duration=300.0)
    assert report.quality == asr.QUALITY_OK


# --------------------------------------------------------------------------- #
# segments and rendering
# --------------------------------------------------------------------------- #


def test_whisper_json_and_transcript_json_parsing(tmp_path):
    wj = {
        "transcription": [
            {"offsets": {"from": 0, "to": 1500}, "text": " hello "},
            {"offsets": {"from": 1500, "to": 3000}, "text": "  "},
        ]
    }
    assert asr.segments_from_whisper_json(wj) == [asr.Seg(0.0, 1.5, "hello")]
    tj = {"segments": [{"start": 1.0, "end": 2.0, "text": "a b"}]}
    assert asr.segments_from_transcript_json(tj) == [asr.Seg(1.0, 2.0, "a b")]

    (tmp_path / "whisper.json").write_text(json.dumps(wj), encoding="utf-8")
    assert asr.load_segments(tmp_path) == [asr.Seg(0.0, 1.5, "hello")]
    (tmp_path / "transcript.json").write_text(json.dumps(tj), encoding="utf-8")
    assert asr.load_segments(tmp_path) == [asr.Seg(1.0, 2.0, "a b")]


def test_retranscribe_layout_audio_json_and_model(tmp_path):
    """Plain `whisper-cli -oj -of <dir>/audio` output: segments and the real model path."""
    doc = {
        "params": {"model": "/nix/store/abc-ggml-large-v3.bin", "language": "en"},
        "transcription": [{"offsets": {"from": 160, "to": 5840}, "text": " i see this"}],
    }
    (tmp_path / "audio.json").write_text(json.dumps(doc), encoding="utf-8")
    assert asr.load_segments(tmp_path) == [asr.Seg(0.16, 5.84, "i see this")]
    assert asr.model_path_from_dir(tmp_path) == "/nix/store/abc-ggml-large-v3.bin"
    assert asr.model_label(asr.model_path_from_dir(tmp_path)) == "ggml-large-v3"


def test_load_segments_from_vtt_and_none(tmp_path):
    assert asr.load_segments(tmp_path) is None
    segs = [asr.Seg(0.0, 2.5, "first"), asr.Seg(3661.0, 3665.0, "late line")]
    (tmp_path / "transcript.vtt").write_text(asr.render_vtt(segs), encoding="utf-8")
    assert asr.load_segments(tmp_path) == segs


def test_render_timestamped_markers():
    segs = [
        asr.Seg(5.2, 7.0, "start"),
        asr.Seg(754.0, 760.0, "mid"),
        asr.Seg(3723.0, 3725.0, "end"),
    ]
    assert asr.render_timestamped(segs).splitlines() == [
        "[00:05] start",
        "[12:34] mid",
        "[1:02:03] end",
    ]


# --------------------------------------------------------------------------- #
# vocabulary prompt
# --------------------------------------------------------------------------- #


def test_default_vocabulary_has_core_skills():
    prompt = asr.vocabulary_prompt(asr.DEFAULT_CALISTHENICS_VOCABULARY)
    for word in ("planche", "front lever", "maltese", "iron cross", "hefesto", "handstand push-up"):
        assert word in prompt
    # whisper keeps about 224 prompt tokens; stay well under that.
    assert len(prompt.split()) < 120


def test_parse_vocabulary_and_empty_prompt():
    assert asr.parse_vocabulary("planche, front lever\n# comment\n\nmaltese") == [
        "planche",
        "front lever",
        "maltese",
    ]
    assert asr.vocabulary_prompt([]) is None
    assert asr.vocabulary_prompt(["  "]) is None


# --------------------------------------------------------------------------- #
# speakers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "title,guests",
    [
        ("Debunking Calisthenics Myths w. @saša venos Ep.1", ["saša venos"]),
        (
            "How to Periodize Skills 101. Finally learn to program the Planche Ep.4 w.@saša venos",
            ["saša venos"],
        ),
        (
            "Debunking Calisthenics Myths with @saša venos: Too tall for Calisthenics? Ep.10",
            ["saša venos"],
        ),
        (
            "How to structure a Calisthenics Program. Macrocycle planning Ep.8 with  @SasaVenos ​",
            ["SasaVenos"],
        ),
        ("Debunking Calisthenics Myths Ep.7", []),
        (None, []),
    ],
)
def test_guests_from_title(title, guests):
    assert asr.guests_from_title(title) == guests


def test_resolve_speakers_owner_guest_and_override():
    aliases = {"sasavenos": "SasaVenos"}
    speakers, source = asr.resolve_speakers(
        channel_owner="sthenics_",
        override=None,
        title="Debunking Calisthenics Myths w. @saša venos Ep.1",
        aliases=aliases,
    )
    assert speakers == ["sthenics_", "SasaVenos"]
    assert source == "title-mention"

    speakers, source = asr.resolve_speakers(
        channel_owner="sthenics_", override=None, title="Debunking Calisthenics Myths Ep.7"
    )
    assert (speakers, source) == (["sthenics_"], "channel-owner")

    speakers, source = asr.resolve_speakers(
        channel_owner="sthenics_",
        override=["Saša Venos", "Host Name"],
        title="Debunking Calisthenics Myths w. @saša venos Ep.1",
        aliases=aliases,
    )
    assert (speakers, source) == (["SasaVenos", "Host Name"], "manifest")


def test_speaker_key_ignores_accents_case_and_spaces():
    assert asr.speaker_key("Saša Venos") == asr.speaker_key("sasavenos") == "sasavenos"


@pytest.mark.parametrize(
    "title,speakers,found_by,multi",
    [
        (
            "The David Packer Interview (Calisthenics Skills)",
            ["Owner", "David Packer"],
            ["channel-owner", "title-interview"],
            "true",
        ),
        (
            "Training for Neural Adaptations, Hypertrophy / ft. Rob Mauceri",
            ["Owner", "Rob Mauceri"],
            ["channel-owner", "title-ft"],
            "true",
        ),
        (
            "Bad Form Training? Role of Genetics? / ft. Sthenics (Denis Piccolo)",
            ["Owner", "Sthenics (Denis Piccolo)"],
            ["channel-owner", "title-ft"],
            "true",
        ),
        (
            "Interview with Jane Doe | Planche",
            ["Owner", "Jane Doe"],
            ["channel-owner", "title-with"],
            "true",
        ),
        ("Q&A with Jane Doe", ["Owner", "Jane Doe"], ["channel-owner", "title-with"], "true"),
        ("Front lever w/ Jane Doe", ["Owner", "Jane Doe"], ["channel-owner", "title-ft"], "true"),
        (
            "Calisthenics Q&A: How to train, Muscles & Skills",
            ["Owner"],
            ["channel-owner"],
            "unknown",
        ),
        (
            "The #1 Protraction Mistake (Real Coaching Call)",
            ["Owner"],
            ["channel-owner"],
            "unknown",
        ),
        ("Training with a plan VS no plan (Calisthenics)", ["Owner"], ["channel-owner"], "false"),
        ("Getting Strong with Rings", ["Owner"], ["channel-owner"], "false"),
    ],
)
def test_detect_speakers_from_titles(title, speakers, found_by, multi):
    r = asr.detect_speakers(channel_owner="Owner", title=title)
    assert (r.speakers, r.found_by, r.multi_speaker) == (speakers, found_by, multi)
    assert r.source == ("title-mention" if len(speakers) > 1 else "channel-owner")


def test_detect_speakers_override_is_manual():
    r = asr.detect_speakers(
        channel_owner="Owner",
        title="Solo talk",
        override=["Owner", "Guest"],
        override_source="speakers-file",
    )
    assert r.speakers == ["Owner", "Guest"]
    assert r.found_by == ["speakers-file", "speakers-file"]
    assert (r.source, r.multi_speaker) == ("manual", "true")


def test_speakers_file_yaml_without_library(tmp_path):
    (tmp_path / "speakers.yaml").write_text(
        "# who speaks\n"
        "channel_owner: SasaVenos\n"
        "aliases:\n"
        "  saša venos: SasaVenos\n"
        "videos:\n"
        '  lkcLJc2c0NQ: [SasaVenos, "David Packer"]\n'
        "  2o5uPfmsO48:\n"
        "    - sthenics_\n"
        "    - SasaVenos\n"
        "  solo: SasaVenos\n",
        encoding="utf-8",
    )
    data = asr.load_speakers_file(tmp_path)
    assert data["channel_owner"] == "SasaVenos"
    assert data["aliases"] == {"saša venos": "SasaVenos"}
    assert data["videos"] == {
        "lkcLJc2c0NQ": ["SasaVenos", "David Packer"],
        "2o5uPfmsO48": ["sthenics_", "SasaVenos"],
        "solo": ["SasaVenos"],
    }
    assert asr.parse_simple_yaml((tmp_path / "speakers.yaml").read_text(encoding="utf-8"))[
        "videos"
    ]["2o5uPfmsO48"] == ["sthenics_", "SasaVenos"]


def test_speakers_file_missing_gives_empty(tmp_path):
    assert asr.load_speakers_file(tmp_path) == {
        "channel_owner": None,
        "channel_kind": None,
        "aliases": {},
        "videos": {},
    }


# ── round 3: offsets, near copies, strict speakers file, channel kind ────────


def test_span_char_offsets_skip_real_speech_on_boundary_lines():
    """k1xTwmEd_XI case: the loop starts in the middle of a line."""
    lines = _normal_sentences(30)
    lines[12] = "fifth row right rest and then the last row right note and you're going"
    lines.insert(13, " ".join(["to leave it a little bit of white and you're going"] * 6))
    text = "\n".join(lines)
    report = asr.assess_transcript(text, duration=200.0, line_offset=3)
    (sp,) = report.spans
    assert (sp["first_line"], sp["last_line"]) == (16, 17)  # transcript lines 13-14 + 3
    first = lines[12]
    assert first[sp["start_char"] :] == "and you're going"
    assert first[: sp["start_char"]].endswith("note ")


def test_phrase_loop_covers_near_copies_but_not_real_speech():
    """eC5JKwac4jc case: "have to" before "need to"; the loop starts one line earlier."""
    near = "you don't have to bend your elbows too much and"
    loop = "you don't need to bend your elbows too much and"
    toks = "so it is important " + " ".join([near] * 2 + [loop] * 6) + " including me it"
    t = asr.QualityThresholds()
    words = asr._norm(toks).split()
    ((start, end, n, _reps),) = [x for x in asr._phrase_loops(words, t) if x[2] == 10]
    assert words[start : start + 4] == ["you", "don't", "have", "to"]
    assert words[end - 2 : end] == ["much", "and"]
    assert "including" not in words[start:end]


@pytest.mark.parametrize(
    "text,message",
    [
        ("videos:\n  x: [SasaVenos, David Packer\n", "speakers.yaml:2: unclosed '['"),
        ("channel_owner: [a, b]\n", "channel_owner must be one name"),
        ("videos:\n  a: [x]\n\tb: [y]\n", "speakers.yaml:3: tab in indentation"),
        ('videos:\n  a: "x\n', "speakers.yaml:2: unclosed quote"),
        ("videos\n", "speakers.yaml:1: expected 'key: value'"),
        ("videos:\n  - x\n  y: [z]\n", "speakers.yaml:3: unexpected indentation"),
        ("- x\n", "speakers.yaml:1: list item outside a list"),
        ("owner: x\n", "unknown key(s) ['owner']"),
        ("videos:\n  a: [x]\n  a: [y]\n", "speakers.yaml:3: duplicate key 'a'"),
        ("videos:\n  a: [x, , y]\n", "speakers.yaml:2: empty item"),
        ("channel_owner:\nvideos:\n  a: x\n", "speakers.yaml:1: key 'channel_owner' has no value"),
        ("videos:\n", "at the end has no value"),
    ],
)
def test_malformed_speakers_file_is_a_hard_error(tmp_path, text, message):
    (tmp_path / "speakers.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(asr.SpeakersFileError) as info:
        asr.load_speakers_file(tmp_path)
    assert message in str(info.value)
    assert str(tmp_path / "speakers.yaml") in str(info.value)


def test_malformed_speakers_json_is_a_hard_error(tmp_path):
    (tmp_path / "speakers.json").write_text('{"videos": {"a": 3}}', encoding="utf-8")
    with pytest.raises(asr.SpeakersFileError, match="videos.a must be a name"):
        asr.load_speakers_file(tmp_path)


def test_podcast_channel_kind_makes_single_speaker_unknown():
    r = asr.detect_speakers(
        channel_owner="sthenics_", title="Debunking Calisthenics Myths Ep.7", channel_kind="podcast"
    )
    assert (r.speakers, r.multi_speaker) == (["sthenics_"], "unknown")
    r = asr.detect_speakers(
        channel_owner="sthenics_", title="Debunking Calisthenics Myths Ep.7", channel_kind="solo"
    )
    assert r.multi_speaker == "false"
    r = asr.detect_speakers(
        channel_owner="sthenics_",
        title="Ep.7",
        channel_kind="podcast",
        override=["sthenics_"],
        override_source="speakers-file",
    )
    assert r.multi_speaker == "false"  # the owner confirmed one speaker
