"""Pure helpers for ASR (whisper.cpp) output.

This module has no imports from the rest of the package and no third-party
imports. The zettel scripts (``zettel_ralph/batch_channel.py`` and
``zettel_ralph/ingest_transcripts.py``) load it by file path, so it must stay
standard-library only.

It holds five things:

1. ``model_label``: the name of the model file that whisper really used.
2. The default vocabulary prompt for calisthenics channels.
3. Segment parsing and rendering (whisper JSON, our ``transcript.json``, VTT).
4. ``assess_transcript``: a deterministic quality check that finds repetition
   loops and missing speech.
5. The ``asr.json`` sidecar format that records model, prompt, and quality for
   one video.
"""

# No ``from __future__ import annotations``: with real (not string) annotations the
# dataclasses also work when a caller loads this file without ``sys.modules``.

import json
import os
import re
import unicodedata
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

ASR_SIDECAR_NAME = "asr.json"

QUALITY_OK = "ok"
QUALITY_PARTIAL = "partial"  # damaged spans exist; the rest is usable
QUALITY_DEGRADED = "degraded"
QUALITY_UNKNOWN = "unknown"

# Lit file layout (ingest_transcripts.py): "---", frontmatter, "---", then the body.
# Lit body line 1 is the first line after the closing "---". Body lines 1-3 are a
# blank line, "# <title>", and a blank line; the transcript starts at body line 4.
# A transcript line N is therefore lit body line N + LIT_BODY_LINE_OFFSET.
LIT_BODY_LINE_OFFSET = 3

# Domain words that whisper often gets wrong without a hint ("plunge" for
# "planche"). whisper.cpp uses the prompt as earlier context for the first
# window, so the model prefers these spellings. Keep the list short: whisper
# keeps at most about 224 prompt tokens.
DEFAULT_CALISTHENICS_VOCABULARY: tuple[str, ...] = (
    "calisthenics",
    "planche",
    "tuck planche",
    "straddle planche",
    "full planche",
    "planche lean",
    "front lever",
    "back lever",
    "maltese",
    "iron cross",
    "hefesto",
    "handstand",
    "handstand push-up",
    "muscle-up",
    "human flag",
    "V-sit",
    "manna",
    "L-sit",
    "pseudo planche push-up",
    "protraction",
    "scapula",
    "parallettes",
    "gymnastic rings",
    "isometric hold",
    "toe",
    "toes",
    "RPE",
    "deload",
    "periodization",
    "microcycle",
    "macrocycle",
)


def vocabulary_prompt(words: list[str] | tuple[str, ...] | None) -> str | None:
    """Return the whisper ``--prompt`` text for a word list, or None if empty."""
    if not words:
        return None
    cleaned = [w.strip() for w in words if w and w.strip()]
    if not cleaned:
        return None
    return "Glossary: " + ", ".join(cleaned) + "."


def parse_vocabulary(value: str | None) -> list[str]:
    """Parse a comma or newline separated word list. Blank lines and ``#`` lines are ignored."""
    if not value:
        return []
    words: list[str] = []
    for line in value.replace(",", "\n").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return words


# --------------------------------------------------------------------------- #
# Model identity
# --------------------------------------------------------------------------- #


def model_label(model_path: str | os.PathLike[str] | None) -> str:
    """Return the model file name (without ``.bin``) from a whisper.cpp model path.

    The name comes from the file that whisper loads, never from a constant:
    ``/x/ggml-small.en.bin`` gives ``ggml-small.en``, and
    ``/nix/store/<hash>-ggml-large-v3.bin`` gives ``ggml-large-v3`` (the Nix
    store hash is removed).
    """
    if not model_path:
        return "unknown"
    name = Path(model_path).name
    stem = name[:-4] if name.endswith(".bin") else name
    idx = stem.find("ggml-")
    if idx > 0:
        stem = stem[idx:]
    return stem or "unknown"


def is_english_only_model(model_path: str | os.PathLike[str] | None) -> bool:
    """Return True for whisper ``*.en`` models (they ignore ``-l``)."""
    return model_label(model_path).endswith(".en")


def model_path_from_whisper_json(data: dict) -> str | None:
    """Return ``params.model`` from a whisper-cli ``-oj`` document (the file whisper loaded)."""
    params = data.get("params") if isinstance(data, dict) else None
    model = params.get("model") if isinstance(params, dict) else None
    return model if isinstance(model, str) and model else None


class WhisperModelMissingError(FileNotFoundError):
    """The configured whisper model file does not exist."""


def require_model_file(model_path: str | os.PathLike[str] | None, *, setting: str) -> Path:
    """Return the model path, or raise a clear error if the file is missing.

    There is no fallback to another model. ``setting`` names the variable or
    flag that the operator must change.
    """
    if not model_path:
        raise WhisperModelMissingError(
            f"No whisper model is configured. Set {setting} to the path of a ggml model file."
        )
    path = Path(model_path)
    if not path.is_file():
        raise WhisperModelMissingError(
            f"Whisper model file not found: {path}. Set {setting} to an existing ggml model "
            "file. The pipeline does not fall back to another model."
        )
    return path


# --------------------------------------------------------------------------- #
# Segments
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Seg:
    """A transcript segment. Times are in seconds."""

    start: float
    end: float
    text: str


def segments_from_whisper_json(data: dict) -> list[Seg]:
    """Parse the whisper-cli ``-oj`` document (``{"transcription": [...]}``)."""
    out: list[Seg] = []
    for entry in data.get("transcription", []) or []:
        offsets = entry.get("offsets") or {}
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        out.append(
            Seg(
                start=float(offsets.get("from", 0)) / 1000.0,
                end=float(offsets.get("to", 0)) / 1000.0,
                text=text,
            )
        )
    return out


def segments_from_transcript_json(data: dict) -> list[Seg]:
    """Parse our ``transcript.json`` document (``{"segments": [{start,end,text}]}``)."""
    out: list[Seg] = []
    for entry in data.get("segments", []) or []:
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        out.append(Seg(float(entry.get("start", 0.0)), float(entry.get("end", 0.0)), text))
    return out


_VTT_TIME = re.compile(
    r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s+-->\s+(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})"
)


def _vtt_seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def segments_from_vtt(text: str) -> list[Seg]:
    """Parse a WebVTT document into segments."""
    out: list[Seg] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _VTT_TIME.match(lines[i].strip())
        if not m:
            i += 1
            continue
        start = _vtt_seconds(*m.group(1, 2, 3, 4))
        end = _vtt_seconds(*m.group(5, 6, 7, 8))
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i].strip())
            i += 1
        joined = " ".join(body).strip()
        if joined:
            out.append(Seg(start, end, joined))
    return out


_STDOUT_LINE = re.compile(
    r"^\[(\d+):(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+(\d+):(\d{2}):(\d{2})[.,](\d{3})\]\s*(.*)$"
)


def segments_from_stdout_text(text: str) -> list[Seg] | None:
    """Parse whisper-cli console output (``[00:00:01.000 --> 00:00:04.000]  text``).

    Some old ``transcript.txt`` files hold this format. Returns None if most
    non-empty lines do not have it.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    out: list[Seg] = []
    for ln in lines:
        m = _STDOUT_LINE.match(ln.strip())
        if not m:
            continue
        g = m.groups()
        start = _vtt_seconds(g[0], g[1], g[2], g[3])
        end = _vtt_seconds(g[4], g[5], g[6], g[7])
        if g[8].strip():
            out.append(Seg(start, end, g[8].strip()))
    return out if len(out) >= 0.8 * len(lines) else None


# File names, in order of preference. ``audio.*`` is the layout of a plain
# ``whisper-cli -of <dir>/audio`` run (for example ``retranscribe_large_v3/<id>/``).
SEGMENT_JSON_NAMES = ("transcript.json", "whisper.json", "audio.json")
SEGMENT_VTT_NAMES = ("transcript.vtt", "audio.vtt")


def _read_json_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_segments(video_dir: Path) -> list[Seg] | None:
    """Load timed segments for a video directory, or None if none exist.

    Order of sources: ``transcript.json`` (ours), ``whisper.json`` and
    ``audio.json`` (raw whisper-cli output), ``transcript.vtt``, ``audio.vtt``,
    then whisper console lines inside ``transcript.txt`` or ``audio.txt``.
    """
    video_dir = Path(video_dir)
    for name in SEGMENT_JSON_NAMES:
        path = video_dir / name
        if not path.is_file():
            continue
        data = _read_json_file(path)
        if data is None:
            continue
        if "segments" in data:
            return segments_from_transcript_json(data)
        if "transcription" in data:
            return segments_from_whisper_json(data)
    for name in SEGMENT_VTT_NAMES:
        path = video_dir / name
        if path.is_file():
            try:
                segs = segments_from_vtt(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if segs:
                return segs
    for name in ("transcript.txt", "audio.txt"):
        path = video_dir / name
        if path.is_file():
            try:
                segs = segments_from_stdout_text(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if segs:
                return segs
    return None


def model_path_from_dir(video_dir: Path) -> str | None:
    """Return the model path that whisper-cli wrote into a raw ``-oj`` file, if any."""
    for name in ("whisper.json", "audio.json", "transcript.json"):
        path = Path(video_dir) / name
        if path.is_file():
            data = _read_json_file(path)
            if data and "transcription" in data:
                model = model_path_from_whisper_json(data)
                if model:
                    return model
    return None


def format_marker(seconds: float) -> str:
    """Return ``MM:SS`` below one hour, else ``H:MM:SS`` (NOTE_CONTRACT.md section 1)."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def render_timestamped(segments: list[Seg]) -> str:
    """Render one line per segment: ``[MM:SS] text`` (``[H:MM:SS]`` after one hour)."""
    return "\n".join(
        f"[{format_marker(s.start)}] {s.text.strip()}" for s in segments if s.text.strip()
    )


def _vtt_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def render_vtt(segments: list[Seg]) -> str:
    """Render segments as WebVTT."""
    lines = ["WEBVTT", ""]
    for s in segments:
        if not s.text.strip():
            continue
        lines.append(f"{_vtt_time(s.start)} --> {_vtt_time(s.end)}")
        lines.append(s.text.strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def wav_duration(path: Path) -> float | None:
    """Return the duration of a PCM WAV file in seconds, or None if unreadable."""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / float(rate) if rate else None
    except (wave.Error, OSError, EOFError):
        return None


# --------------------------------------------------------------------------- #
# Quality check
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QualityThresholds:
    """Limits for ``assess_transcript``.

    The check marks damaged units (segments or lines) and joins them into
    damaged spans. If the spans hold at least ``degraded_share`` of the
    transcript, the quality is ``degraded``. If smaller spans exist, the
    quality is ``partial`` and each span is listed. Spans below the size limits
    are minor issues (prefix ``minor:``) and do not change the quality.
    """

    # A unit needs this many words to count as a copy of a neighbor.
    min_unit_words: int = 3
    # A unit is a copy if this share of its word trigrams (or the whole unit,
    # for units of 3-4 words) also occurs in the previous or the next unit.
    # This finds identical repeats and rolling captions ("A B", "B", "B C").
    copy_overlap: float = 0.8
    # A phrase of up to ``max_phrase_words`` words repeated back to back at
    # least ``min_phrase_repeats`` times, covering ``min_phrase_loop_words``.
    max_phrase_words: int = 25
    min_phrase_repeats: int = 4
    min_phrase_loop_words: int = 20
    # A phrase loop also covers near copies of the phrase next to it (same word at
    # this share of positions).
    near_copy_share: float = 0.7
    # Damaged units with at most this many clean units between them are one span.
    span_gap_units: int = 2
    # A span is listed (``partial``) if it has this many damaged words and units.
    min_span_words: int = 40
    min_span_units: int = 3
    # Damage that dominates the file: share of words, or of audio time.
    degraded_share: float = 0.5
    # Speech density after removal of repeated units, for audio of at least
    # ``min_duration_for_rate`` seconds. Normal speech here is 120-220 wpm.
    min_distinct_wpm: float = 60.0
    min_duration_for_rate: float = 60.0
    # More words per minute than people speak: hidden duplication.
    max_wpm: float = 300.0
    # Timed segments must reach the end of the audio.
    max_tail_gap_seconds: float = 120.0
    max_tail_gap_share: float = 0.15
    # A silent gap between two timed segments longer than this is a minor issue.
    max_inner_gap_seconds: float = 120.0


@dataclass
class QualityReport:
    """Result of ``assess_transcript``.

    ``spans`` lists the damaged spans as dicts with ``start`` and ``end``
    (seconds, or None without timestamps), ``first_line`` and ``last_line``
    (1-based line numbers plus ``line_offset``; for timed segments, the segment
    number), ``start_char`` and ``end_char`` (0-based character offsets of the
    damage inside the first and the last line; end is exclusive), ``words``,
    ``kind``, and ``example``.
    """

    quality: str
    issues: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    spans: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def span_markers(self) -> list[list[str]]:
        """Damaged spans as ``[["MM:SS", "MM:SS"], ...]`` (timed spans only)."""
        return [
            [format_marker(s["start"]), format_marker(s["end"])]
            for s in self.spans
            if s.get("start") is not None
        ]


_NON_WORD = re.compile(r"[^\w\s']+", re.UNICODE)
_SPACES = re.compile(r"\s+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _norm(text: str) -> str:
    return _SPACES.sub(" ", _NON_WORD.sub(" ", text.casefold())).strip()


def _units_from_text(text: str) -> tuple[list[str], list[int], list[int]]:
    """Split a plain transcript into units, their 1-based line numbers, and the
    character offset of each unit inside its line.

    A line of more than 60 words is split into sentences (all with the same
    line number).
    """
    units: list[str] = []
    lines: list[int] = []
    offsets: list[int] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        content = line.strip()
        base = len(line) - len(line.lstrip())
        parts = (
            [x for x in _SENTENCE_SPLIT.split(content) if x.strip()]
            if len(content.split()) > 60
            else [content]
        )
        cursor = 0
        for part in parts:
            pos = content.find(part, cursor)
            pos = cursor if pos < 0 else pos
            units.append(part)
            lines.append(number)
            offsets.append(base + pos)
            cursor = pos + len(part)
    return units, lines, offsets


_TOKEN = re.compile(r"[\w']+", re.UNICODE)


def _token_spans(unit: str, count: int) -> list[tuple[int, int]] | None:
    """Character spans of the normalized tokens of ``unit`` (None if they do not align)."""
    spans = [(m.start(), m.end()) for m in _TOKEN.finditer(unit)]
    return spans if len(spans) == count else None


def _phrase_loops(tokens: list[str], t: "QualityThresholds") -> list[tuple[int, int, int, int]]:
    """Return back-to-back phrase loops as ``(start, end, n, repeats)`` token ranges."""
    found: list[tuple[int, int, int, int]] = []
    size = len(tokens)
    for n in range(1, t.max_phrase_words + 1):
        i = 0
        while i + 2 * n <= size:
            if tokens[i : i + n] == tokens[i + n : i + 2 * n]:
                reps = 2
                j = i + 2 * n
                while j + n <= size and tokens[j : j + n] == tokens[i : i + n]:
                    reps += 1
                    j += n
                if reps >= t.min_phrase_repeats and n * reps >= t.min_phrase_loop_words:
                    # Extend over near copies before and after ("you don't have to bend
                    # your elbows" before "you don't need to bend your elbows" ...).
                    phrase = tokens[i : i + n]
                    start = i
                    while start - n >= 0 and _similar(tokens[start - n : start], phrase, t):
                        start -= n
                    while j + n <= size and _similar(tokens[j : j + n], phrase, t):
                        j += n
                    # Trim words at both ends that do not match the phrase at their
                    # position, so real speech next to the loop stays clean.
                    while start < i and tokens[start] != phrase[(start - i) % n]:
                        start += 1
                    while j > i + 2 * n and tokens[j - 1] != phrase[(j - 1 - i) % n]:
                        j -= 1
                    found.append((start, j, n, max(1, (j - start) // n)))
                i = j
            else:
                i += 1
    return found


def _similar(a: list[str], b: list[str], t: "QualityThresholds") -> bool:
    """True if ``a`` and ``b`` agree in ``near_copy_share`` of their positions."""
    return bool(b) and sum(x == y for x, y in zip(a, b)) / len(b) >= t.near_copy_share


def _trigrams(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[k : k + 3]) for k in range(len(words) - 2)}


def _content_neighbors(i: int, words: list[list[str]], min_words: int) -> list[list[str]]:
    """The nearest unit before and after ``i`` that has ``min_words`` words or more.

    Short units in between ("fibers.", "Yeah.") are skipped, so a loop that
    alternates a long and a short line is still found.
    """
    out: list[list[str]] = []
    for step in (-1, 1):
        j = i + step
        while 0 <= j < len(words) and len(words[j]) < min_words:
            j += step
        if 0 <= j < len(words):
            out.append(words[j])
    return out


def _is_copy(i: int, words: list[list[str]], t: "QualityThresholds") -> bool:
    w = words[i]
    if len(w) < t.min_unit_words:
        return False
    neighbors = _content_neighbors(i, words, t.min_unit_words)
    text = " ".join(w)
    for nb in neighbors:
        nb_text = " ".join(nb)
        if text == nb_text or f" {text} " in f" {nb_text} ":
            return True
    if len(w) < 5:
        return False
    own = _trigrams(w)
    near: set[tuple[str, ...]] = set()
    for nb in neighbors:
        near |= _trigrams(nb)
    return bool(own) and len(own & near) / len(own) >= t.copy_overlap


def _clip(text: str, limit: int = 80) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def assess_transcript(
    text: str | None,
    *,
    segments: list[Seg] | None = None,
    duration: float | None = None,
    thresholds: QualityThresholds | None = None,
    line_offset: int = 0,
) -> QualityReport:
    """Run the deterministic transcript quality check.

    ``line_offset`` is added to every reported line number. Ingest passes the
    number of lit-file body lines before the transcript, so spans use lit body
    line numbers (see ``LIT_BODY_LINE_OFFSET``).

    Result values:

    - ``degraded``: the transcript is empty, has too little speech for its
      audio, has more words per minute than people speak, stops long before
      the end of the audio, or damaged spans hold at least half of it.
    - ``partial``: damaged spans exist but do not dominate. Each span is an
      issue ``loop: [MM:SS]-[MM:SS] (...)`` (``loop: lines A-B`` without
      timestamps). The rest of the transcript is usable.
    - ``unknown``: no damage found, but the audio duration is not known, so
      the density, rate, and end-of-audio checks did not run. An issue
      ``not checked: ...`` names them.
    - ``ok``: all checks ran and found no damage.

    Damaged units are repeats of a neighbor unit (identical lines, rolling
    captions) and units inside a phrase that repeats back to back.
    """
    t = thresholds or QualityThresholds()
    if not segments and text is not None:
        parsed = segments_from_stdout_text(text)
        if parsed:
            segments = parsed
    if segments:
        timed = [s for s in segments if s.text.strip()]
        raw_units = [s.text.strip() for s in timed]
        line_numbers = list(range(1, len(raw_units) + 1))
        unit_offsets = [0] * len(raw_units)
    elif text is not None:
        raw_units, line_numbers, unit_offsets = _units_from_text(text)
        timed = []
    else:
        return QualityReport(QUALITY_UNKNOWN, ["no transcript text to check"], {})

    issues: list[str] = []
    minor: list[str] = []
    words = [_norm(u).split() for u in raw_units]
    tokens = [w for u in words for w in u]
    total_words = len(tokens)
    metrics: dict = {"words": total_words, "units": len(raw_units)}
    if duration:
        metrics["duration_seconds"] = round(float(duration), 1)
    if total_words == 0:
        return QualityReport(QUALITY_DEGRADED, ["empty transcript"], metrics)

    # Damaged words per unit: copies of a neighbor, and tokens inside phrase loops.
    damaged = [0] * len(words)
    kinds: list[set[str]] = [set() for _ in words]
    for i in range(len(words)):
        if _is_copy(i, words, t):
            damaged[i] = len(words[i])
            kinds[i].add("repeat of the next or previous segment")
    owner: list[int] = []
    for i, u in enumerate(words):
        owner.extend([i] * len(u))
    loop_tokens = [False] * total_words
    loops = _phrase_loops(tokens, t)
    for start, end, _n, _reps in loops:
        for k in range(start, end):
            loop_tokens[k] = True
    in_loop = [0] * len(words)
    for k, flag in enumerate(loop_tokens):
        if flag:
            in_loop[owner[k]] += 1
    for i, count in enumerate(in_loop):
        if count:
            if count > damaged[i]:
                damaged[i] = count
            kinds[i].add("repeated phrase")
    # First token index of each unit, for character offsets.
    unit_first_token: list[int] = []
    pos = 0
    for u in words:
        unit_first_token.append(pos)
        pos += len(u)

    def damaged_chars(i: int) -> tuple[int, int]:
        """Start and end character (end exclusive) of the damage inside unit ``i``,
        relative to its source line."""
        whole = (unit_offsets[i], unit_offsets[i] + len(raw_units[i]))
        if "repeat of the next or previous segment" in kinds[i]:
            return whole
        tspans = _token_spans(raw_units[i], len(words[i]))
        if not tspans:
            return whole
        base = unit_first_token[i]
        idx = [k for k in range(len(words[i])) if loop_tokens[base + k]]
        if not idx:
            return whole
        return unit_offsets[i] + tspans[idx[0]][0], unit_offsets[i] + tspans[idx[-1]][1]

    # Join damaged units into spans.
    spans: list[dict] = []
    current: list[int] = []
    gap = 0
    for i in range(len(words)):
        if damaged[i]:
            current.append(i)
            gap = 0
        elif current:
            gap += 1
            if gap > t.span_gap_units:
                spans.append({"units": current})
                current, gap = [], 0
    if current:
        spans.append({"units": current})

    listed: list[dict] = []
    damaged_words = 0
    damaged_seconds = 0.0
    for sp in spans:
        units = sp["units"]
        n_words = sum(damaged[i] for i in units)
        first, last = units[0], units[-1]
        start = timed[first].start if timed else None
        end = timed[last].end if timed else None
        kind = ", ".join(sorted(set().union(*(kinds[i] for i in units))))
        info = {
            "start": start,
            "end": end,
            "first_line": line_numbers[first] + line_offset,
            "last_line": line_numbers[last] + line_offset,
            "start_char": damaged_chars(first)[0],
            "end_char": damaged_chars(last)[1],
            "words": n_words,
            "kind": kind,
            "example": _clip(raw_units[max(units, key=lambda i: damaged[i])], 60),
        }
        where = (
            f"[{format_marker(start)}]-[{format_marker(end)}]"
            if start is not None
            else f"lines {info['first_line']}-{info['last_line']}"
        )
        msg = f'loop: {where} ({n_words} words, {kind}: "{info["example"]}")'
        # A phrase loop inside one long line is one unit; size alone counts.
        big_loop = n_words >= t.min_span_words and any("repeated phrase" in kinds[i] for i in units)
        if (n_words >= t.min_span_words and len(units) >= t.min_span_units) or big_loop:
            listed.append(info)
            issues.append(msg)
            damaged_words += n_words
            if start is not None and end is not None:
                damaged_seconds += max(0.0, end - start)
        else:
            minor.append(msg)

    share = damaged_words / total_words
    if duration and damaged_seconds:
        share = max(share, damaged_seconds / duration)
    metrics["damaged_spans"] = len(listed)
    metrics["damaged_words"] = damaged_words
    metrics["damaged_share"] = round(share, 3)
    severe: list[str] = []
    if listed and share >= t.degraded_share:
        severe.append(f"damaged spans hold {share:.0%} of the transcript")

    # Distinct words: each repeated unit text counts once.
    seen: set[str] = set()
    distinct_words = 0
    for u in words:
        key = " ".join(u)
        if len(u) < t.min_unit_words or key not in seen:
            distinct_words += len(u)
            seen.add(key)
    metrics["distinct_words"] = distinct_words

    not_checked: list[str] = []
    if duration and duration >= t.min_duration_for_rate:
        minutes = duration / 60.0
        wpm = total_words / minutes
        distinct_wpm = distinct_words / minutes
        metrics["words_per_minute"] = round(wpm, 1)
        metrics["distinct_words_per_minute"] = round(distinct_wpm, 1)
        if distinct_wpm < t.min_distinct_wpm:
            severe.append(
                f"low speech density: {distinct_wpm:.0f} distinct words per minute over "
                f"{minutes:.1f} min of audio (limit {t.min_distinct_wpm:.0f})"
            )
        if wpm > t.max_wpm:
            severe.append(
                f"implausible speech rate: {wpm:.0f} words per minute (limit "
                f"{t.max_wpm:.0f}); the text is likely duplicated"
            )
    elif not duration:
        not_checked += ["speech density", "speech rate"]

    if timed and duration:
        last_end = max(s.end for s in timed)
        tail = duration - last_end
        metrics["tail_gap_seconds"] = round(tail, 1)
        if tail > t.max_tail_gap_seconds and tail > t.max_tail_gap_share * duration:
            severe.append(
                f"transcript stops at {format_marker(last_end)} of {format_marker(duration)} audio"
            )
    elif not duration:
        not_checked.append("end of audio")

    if len(timed) > 1:
        ordered = sorted(timed, key=lambda s: s.start)
        gap_len, gap_at = 0.0, 0.0
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.start - prev.end > gap_len:
                gap_len, gap_at = cur.start - prev.end, prev.end
        metrics["longest_inner_gap_seconds"] = round(gap_len, 1)
        if gap_len > t.max_inner_gap_seconds:
            minor.append(f"no speech for {gap_len:.0f} s after {format_marker(gap_at)}")

    if severe:
        quality = QUALITY_DEGRADED
    elif listed:
        quality = QUALITY_PARTIAL
    elif not_checked:
        quality = QUALITY_UNKNOWN
    else:
        quality = QUALITY_OK
    out = severe + issues + [f"minor: {m}" for m in minor]
    if not_checked:
        out.append("not checked (audio duration unknown): " + ", ".join(not_checked))
    return QualityReport(quality, out, metrics, listed)


# --------------------------------------------------------------------------- #
# asr.json sidecar
# --------------------------------------------------------------------------- #


def build_sidecar(
    *,
    model_path: str | os.PathLike[str] | None,
    prompt: str | None,
    language: str | None,
    report: QualityReport,
    timestamps: bool,
    engine: str = "whisper.cpp",
    warnings: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    """Return the ``asr.json`` document for one transcribed video."""
    doc = {
        "engine": engine,
        "model": model_label(model_path),
        "model_file": Path(model_path).name if model_path else None,
        "model_path": str(model_path) if model_path else None,
        "language": language,
        "prompt": prompt,
        "timestamps": bool(timestamps),
        "quality": report.quality,
        "issues": list(report.issues),
        "metrics": dict(report.metrics),
        "warnings": list(warnings or []),
    }
    if extra:
        doc.update(extra)
    return doc


def read_sidecar(video_dir: Path) -> dict | None:
    """Read ``asr.json`` from a video directory, or None if absent or corrupt."""
    path = Path(video_dir) / ASR_SIDECAR_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: object) -> None:
    """Write JSON to ``path`` with a temp file and an atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Speakers
# --------------------------------------------------------------------------- #

# Words in a title that suggest more than one speaker.
_CONVERSATION = re.compile(
    r"\b(?:interview|podcast|q\s*&\s*a|conversation|coaching call|"
    r"debate|panel|guest|ft\.?|feat\.?|featuring)\b|\bw/|\bw\.\s*@",
    re.IGNORECASE,
)
# End of a name: a separator, an episode tag, or the end of the title.
_NAME_END = r"(?=\s*(?:[|:()\[\],/?!]|\s[-–—]\s|\bEp\b|\bEp\.|\bpt\.|\bpart\b|$))"
# "ft. X", "feat. X", "featuring X", "w/ X": always a person or a channel.
_FT = re.compile(
    r"(?:\bft\.?|\bfeat\.?|\bfeaturing|\bw/)\s*@?\s*([^|:()\[\],/?!]+?)" + _NAME_END, re.I
)
# "w. @x", "with @x": a mention.
_MENTION = re.compile(r"(?:\bw\.|\bwith)\s*@\s*([^|:()\[\],/?!]+?)" + _NAME_END, re.I)
# "with X Y", "w. X Y", "Q&A with X Y", "Interview with X Y": capitalized words only.
_CAP_WORD = r"[A-ZÀ-ÖØ-ÞĀ-Ž][\w'’.-]*"
_WITH_NAME = re.compile(
    r"(?:\bw\.|\bwith)\s+(" + _CAP_WORD + r"(?:\s+" + _CAP_WORD + r"){1,3})" + _NAME_END
)
# "The David Packer Interview", "David Packer interview", "Podcast #3: Jane Doe".
_BEFORE_INTERVIEW = re.compile(
    r"(?:^|\bThe\s+|[|:/]\s*)("
    + _CAP_WORD
    + r"(?:\s+"
    + _CAP_WORD
    + r"){1,3})\s+(?:Interview|Podcast)\b"
)
_AFTER_INTERVIEW = re.compile(
    r"\b(?:Interview|Podcast)\s*(?:with|w/|w\.)?\s*[:\-–—]?\s*@?("
    + _CAP_WORD
    + r"(?:\s+"
    + _CAP_WORD
    + r"){1,3})"
    + _NAME_END
)
# A person name in parentheses directly after a name: "Sthenics (Denis Piccolo)".
_PAREN_NAME = re.compile(r"^\s*\((" + _CAP_WORD + r"(?:\s+" + _CAP_WORD + r"){1,3})\)")
_NOT_NAMES = {"the", "a", "an", "my", "your", "our", "full", "complete", "calisthenics", "skills"}


def speaker_key(name: str) -> str:
    """Return a comparison key: no accents, no spaces or punctuation, case-folded."""
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]", "", ascii_only.casefold())


@dataclass
class TitleSpeakers:
    """Guests found in a title, and if the title suggests a conversation."""

    guests: list[tuple[str, str]] = field(default_factory=list)  # (name, found_by)
    conversation: bool = False


def _clean_name(name: str) -> str:
    name = name.replace("​", "").strip().strip(".").strip()
    return re.sub(r"\s+", " ", name)


def speakers_from_title(title: str | None) -> TitleSpeakers:
    """Find guest names in a title, as written in the title.

    ``found_by`` values: ``title-mention`` ("w. @x", "with @x"), ``title-ft``
    ("ft.", "feat.", "featuring", "w/"), ``title-with`` ("with Firstname
    Lastname"), ``title-interview`` (a name before or after "Interview" or
    "Podcast").
    """
    out = TitleSpeakers()
    if not title:
        return out
    title = title.replace("​", " ")
    out.conversation = bool(_CONVERSATION.search(title))
    seen: set[str] = set()

    def add(name: str, how: str, end: int | None = None) -> None:
        name = _clean_name(name)
        if how == "title-interview":
            name = re.sub(r"^(?:The|An?)\s+", "", name)
        if end is not None:
            paren = _PAREN_NAME.match(title[end:])
            if paren:
                name = f"{name} ({paren.group(1)})"
        key = speaker_key(name)
        if not key or name.casefold() in _NOT_NAMES or key in seen:
            return
        seen.add(key)
        out.guests.append((name, how))

    for m in _MENTION.finditer(title):
        add(m.group(1), "title-mention", m.end(1))
    for m in _FT.finditer(title):
        add(m.group(1), "title-ft", m.end(1))
    for m in _WITH_NAME.finditer(title):
        add(m.group(1), "title-with", m.end(1))
    for m in _BEFORE_INTERVIEW.finditer(title):
        add(m.group(1), "title-interview")
    for m in _AFTER_INTERVIEW.finditer(title):
        add(m.group(1), "title-interview", m.end(1))
    if out.guests:
        out.conversation = True
    return out


def guests_from_title(title: str | None) -> list[str]:
    """Return the guest names that a title gives (see ``speakers_from_title``)."""
    return [name for name, _how in speakers_from_title(title).guests]


@dataclass
class SpeakerResult:
    """Speakers of one video and how each name was found."""

    speakers: list[str]
    found_by: list[str]  # one value per speaker
    source: str  # summary: manual | title-mention | channel-owner
    multi_speaker: str  # "true" | "false" | "unknown"


def detect_speakers(
    *,
    channel_owner: str,
    title: str | None,
    override: list[str] | None = None,
    override_source: str = "manifest",
    aliases: dict[str, str] | None = None,
    channel_kind: str | None = None,
) -> SpeakerResult:
    """Return the speakers of one video.

    - ``override`` (from ``speakers.yaml`` or the manifest) wins. Every name
      gets ``found_by`` = ``override_source``; the summary ``source`` is
      ``manual``.
    - Else the channel owner is the main speaker (``channel-owner``) and the
      title guests follow (``title-*``); the summary is ``title-mention`` if a
      guest was found.
    - ``multi_speaker``: ``true`` with two or more names, ``unknown`` if the
      title suggests a conversation (interview, podcast, Q&A, coaching call,
      ft.) or ``channel_kind`` is ``podcast``/``interview`` but no guest name
      was found, else ``false``. Only an override can make a single speaker
      certain for such a channel.

    ``aliases`` maps a speaker key (see ``speaker_key``) to a display name.
    """
    alias_map = {speaker_key(k): v for k, v in (aliases or {}).items()}

    def canon(name: str) -> str:
        return alias_map.get(speaker_key(name), name)

    if override:
        cleaned = [canon(s.strip()) for s in override if s and str(s).strip()]
        if cleaned:
            return SpeakerResult(
                cleaned,
                [override_source] * len(cleaned),
                "manual",
                "true" if len(cleaned) > 1 else "false",
            )
    found = speakers_from_title(title)
    speakers = [canon(channel_owner)]
    found_by = ["channel-owner"]
    keys = {speaker_key(speakers[0])}
    for guest, how in found.guests:
        g = canon(guest)
        if speaker_key(g) not in keys:
            speakers.append(g)
            found_by.append(how)
            keys.add(speaker_key(g))
    conversation_channel = (channel_kind or "").strip().casefold() in CONVERSATION_CHANNEL_KINDS
    if len(speakers) > 1:
        multi = "true"
    elif found.conversation or conversation_channel:
        multi = "unknown"
    else:
        multi = "false"
    return SpeakerResult(
        speakers, found_by, "title-mention" if len(speakers) > 1 else "channel-owner", multi
    )


def resolve_speakers(
    *,
    channel_owner: str,
    override: list[str] | None,
    title: str | None,
    aliases: dict[str, str] | None = None,
) -> tuple[list[str], str]:
    """Return ``(speakers, source)``; see ``detect_speakers``.

    ``source`` is ``manifest`` for an override (older callers), else the summary.
    """
    r = detect_speakers(
        channel_owner=channel_owner, title=title, override=override, aliases=aliases
    )
    return r.speakers, ("manifest" if r.source == "manual" else r.source)


# --------------------------------------------------------------------------- #
# Override files
# --------------------------------------------------------------------------- #


class SpeakersFileError(ValueError):
    """A malformed ``speakers.yaml`` / ``speakers.json``. The message names file and line."""


_SPEAKER_KEYS = {"channel_owner", "channel_kind", "aliases", "videos"}
CONVERSATION_CHANNEL_KINDS = {"podcast", "interview"}


def _scalar(value: str, where: str) -> object:
    """Parse one YAML scalar or flow list. Raise ``SpeakersFileError`` on bad syntax."""
    value = value.strip()
    if not value:
        return None
    if value[0] in "\"'":
        if len(value) < 2 or value[-1] != value[0]:
            raise SpeakersFileError(f"{where}: unclosed quote in {value!r}")
        return value[1:-1]
    if value.startswith("["):
        if not value.endswith("]"):
            raise SpeakersFileError(f"{where}: unclosed '[' in {value!r}")
        inner = value[1:-1].strip()
        if "[" in inner or "]" in inner or "{" in inner:
            raise SpeakersFileError(f"{where}: nested list or map in {value!r}")
        items = [_scalar(x, where) for x in _split_flow(inner, where)] if inner else []
        if any(not isinstance(x, str) or not x.strip() for x in items):
            raise SpeakersFileError(f"{where}: empty item in {value!r}")
        return items
    if value.startswith("{") or value.endswith(("]", "}")) or value[0] in "&*!|>%@`":
        raise SpeakersFileError(f"{where}: unsupported YAML value {value!r}")
    if value in ("~", "null"):
        return None
    return value


def _split_flow(text: str, where: str) -> list[str]:
    parts, cur, quote = [], "", ""
    for ch in text:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if quote:
        raise SpeakersFileError(f"{where}: unclosed quote in [{text}]")
    parts.append(cur)
    if any(not p.strip() for p in parts):
        raise SpeakersFileError(f"{where}: empty item in [{text}]")
    return [p.strip() for p in parts]


def _strip_comment(line: str) -> str:
    quote = ""
    for k, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (k == 0 or line[k - 1] in " \t"):
            return line[:k].rstrip()
    return line.rstrip()


def parse_simple_yaml(text: str, source: str = "speakers.yaml") -> dict:
    """Parse the small YAML subset of ``speakers.yaml`` without a YAML library.

    Supported: ``key: value`` maps nested by indentation (spaces only), flow
    lists ``[a, b]``, block lists (``- a``) directly under a ``key:`` line,
    quoted strings, and ``#`` comments. Every other form raises
    ``SpeakersFileError`` with ``<source>:<line>``.
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(0, root)]  # (indent of keys, map)
    pending: tuple[dict, str, int] | None = None  # map, key, indent of "key:"
    current_list: tuple[list, int] | None = None  # list, indent of its "- " items
    for number, raw in enumerate(text.splitlines(), start=1):
        where = f"{source}:{number}"
        line = _strip_comment(raw)
        if not line.strip():
            continue
        lead = line[: len(line) - len(line.lstrip())]
        if "\t" in lead:
            raise SpeakersFileError(f"{where}: tab in indentation (use spaces)")
        indent = len(lead)
        body = line.strip()
        if pending is not None:
            pmap, pkey, pindent = pending
            pending = None
            if indent > pindent:
                if body.startswith("- ") or body == "-":
                    pmap[pkey] = []
                    current_list = (pmap[pkey], indent)
                else:
                    pmap[pkey] = {}
                    stack.append((indent, pmap[pkey]))
                    current_list = None
            else:
                raise SpeakersFileError(f"{source}:{number - 1}: key {pkey!r} has no value")
        if body.startswith("- ") or body == "-":
            if current_list is None or indent != current_list[1]:
                raise SpeakersFileError(f"{where}: list item outside a list")
            item = _scalar(body[2:], where)
            if not isinstance(item, str) or not item.strip():
                raise SpeakersFileError(f"{where}: list item must be a name")
            current_list[0].append(item)
            continue
        current_list = None
        while len(stack) > 1 and indent < stack[-1][0]:
            stack.pop()
        if indent != stack[-1][0]:
            raise SpeakersFileError(f"{where}: unexpected indentation")
        key, sep, value = body.partition(":")
        if not sep or not key.strip():
            raise SpeakersFileError(f"{where}: expected 'key: value', got {body!r}")
        if value and not value.startswith(" "):
            raise SpeakersFileError(f"{where}: missing space after ':' in {body!r}")
        key_value = _scalar(key, where)
        if not isinstance(key_value, str):
            raise SpeakersFileError(f"{where}: bad key {key!r}")
        current = stack[-1][1]
        if key_value in current:
            raise SpeakersFileError(f"{where}: duplicate key {key_value!r}")
        if value.strip():
            current[key_value] = _scalar(value, where)
        else:
            current[key_value] = None
            pending = (current, key_value, indent)
    if pending is not None:
        raise SpeakersFileError(f"{source}: key {pending[1]!r} at the end has no value")
    return root


def _check_speakers_doc(data: object, source: str) -> dict:
    """Check the ``speakers.yaml`` schema. Raise ``SpeakersFileError`` on any mismatch."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SpeakersFileError(f"{source}: the file must be a map")
    unknown = set(data) - _SPEAKER_KEYS
    if unknown:
        raise SpeakersFileError(
            f"{source}: unknown key(s) {sorted(unknown)}; allowed: {sorted(_SPEAKER_KEYS)}"
        )
    for key in ("channel_owner", "channel_kind"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise SpeakersFileError(f"{source}: {key} must be one name, not {data[key]!r}")
    aliases = data.get("aliases")
    if aliases is not None:
        if not isinstance(aliases, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()
        ):
            raise SpeakersFileError(f"{source}: aliases must map a name to a name")
    videos = data.get("videos")
    if videos is not None:
        if not isinstance(videos, dict):
            raise SpeakersFileError(f"{source}: videos must map a video id to names")
        for vid, names in videos.items():
            if isinstance(names, str):
                continue
            if (
                not isinstance(names, list)
                or not names
                or not all(isinstance(n, str) and n.strip() for n in names)
            ):
                raise SpeakersFileError(
                    f"{source}: videos.{vid} must be a name or a list of names, not {names!r}"
                )
    return data


def load_speakers_file(job_dir: Path) -> dict:
    """Read ``speakers.yaml`` (or ``speakers.json``) next to a channel manifest.

    Format::

        channel_owner: SasaVenos          # main speaker of the channel
        channel_kind: podcast             # optional: podcast | interview | solo
        aliases:                          # other spellings -> display name
          saša venos: SasaVenos
        videos:                           # per video: speakers, main speaker first
          lkcLJc2c0NQ: [SasaVenos, David Packer]
          2o5uPfmsO48:
            - sthenics_
            - SasaVenos

    A malformed file raises ``SpeakersFileError`` (file and line in the message);
    it is never read in part. Returns ``{"channel_owner", "channel_kind",
    "aliases", "videos": {id: [names]}}``.
    """
    job_dir = Path(job_dir)
    yml = job_dir / "speakers.yaml"
    js = job_dir / "speakers.json"
    if yml.is_file():
        text = yml.read_text(encoding="utf-8")
        # Always run the strict parser, also when PyYAML exists, so a bad line is
        # an error on every host.
        data = _check_speakers_doc(parse_simple_yaml(text, str(yml)), str(yml))
    elif js.is_file():
        try:
            loaded = json.loads(js.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SpeakersFileError(f"{js}:{exc.lineno}: {exc.msg}") from exc
        data = _check_speakers_doc(loaded, str(js))
    else:
        data = {}
    videos = data.get("videos") or {}
    return {
        "channel_owner": data.get("channel_owner") or None,
        "channel_kind": (data.get("channel_kind") or None),
        "aliases": dict(data.get("aliases") or {}),
        "videos": {
            str(vid): [names] if isinstance(names, str) else list(names)
            for vid, names in videos.items()
        },
    }
