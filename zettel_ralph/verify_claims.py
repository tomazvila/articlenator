#!/usr/bin/env python3
"""Compare permanent notes with their transcripts. Deterministic. No LLM.

This is the claim check step of the pipeline. It reads notes that follow
NOTE_CONTRACT.md and the transcripts in one or more `lit/` folders (the
transcript registry, see `lit_dirs()`). For each note it checks:

  (a) frontmatter keys and values, the allowed sections, one lead paragraph,
      H1 = file name = frontmatter `title` (when present);
  (b) every Evidence and Disagreement quote is in the cited transcript
      (NOTE_CONTRACT.md section 11) and starts within one marker of the cited
      timestamp; `...` joins only two parts of one passage; a bracket holds
      one known term after one word;
  (c) every quantity (number + unit + period + limit word) in the title,
      lead, Details, Example and Why sections matches a quantity in a quote
      that its own source tag cites; `scope.quantities` agree with the quotes;
  (d) every source tag resolves to a `sources` entry, a transcript and an
      Evidence item at that marker;
  (e) speakers: in the transcript list, or proved by `speaker_evidence`;
      Details from one speaker only;
  (f) no cited transcript has `asr_quality: degraded`;
  (g) a word-overlap check: each tagged claim shares most content words with
      the quotes its tag cites (a heuristic for "no text without a quote").

Usage:

    python verify_claims.py NOTE_OR_DIR [...] [--lit DIR ...]
        [--write] [--out report.json] [--marker-tolerance 1]
        [--provenance provenance.json ...] [--queue queue.json ...]

Default is report-only. With --write, the tool sets `verification:` to
`quote-checked` or `failed` in each note. The exit code is 1 when any note
fails, 0 when all notes pass, 2 for a usage error. Under the agent
(`ZR_AGENT=1`), --write is refused and --out must point into the agent's
shadow folder.

Notes that do not follow the contract (no `sources:` list) get the status
`not-contract`. If --provenance or --queue maps the note to a video, the
report also gives an APPROXIMATE number check (value anywhere in the
transcript). That check uses the tolerant number matching (number words =
digits) and ignores quotes, units and periods. Use it only to plan a repair.

README.md, section "Verification and run integrity", lists the exact rules
and what stays outside a mechanical check.
"""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# Contract vocabulary
# --------------------------------------------------------------------------- #

# `source-checked` = mechanical check AND LLM source-check passed (the published state).
# `quote-checked` = mechanical check only (never published). `needs-repair` = a review
# found a defect in a published note; repair_loop.sh takes these notes.
VERIFICATION_VALUES = ("unverified", "quote-checked", "source-checked", "failed", "unsupported", "needs-repair",
                       "superseded")  # round 21: a pipeline stub
STATUS_VALUES = ("expanded", "reviewed", "superseded")
BASIS_VALUES = ("general rule", "speaker's own practice", "single example", "reported third party")
MODALITY_VALUES = ("requirement", "recommendation", "option", "prediction", "observation", "not stated")
QUANTITY_PERIODS = ("per set", "per session", "per meal", "per day", "per week", "per month", "per cycle",
                    "for the week", "for the month", "this week", "this day", "this month", "not stated")
FINITE_DURATION = re.compile(r"^(?:(?:for|over|in|last) )?(?:[a-z]+|\d+(?:\.\d+)?) years?(?: and a half)?$|^year and a half$")
EVERY_N = re.compile(r"^every \S+ (weeks?|days?|months?|sessions?)$")
THESE_N = re.compile(r"^these \S+ (weeks?|days?|months?)$")
OVER_N = re.compile(r"^over \S+ (weeks?|days?|months?)$")


def _allowed_quantity_period(period: str) -> bool:
    """Accept the explicit week/month window pair without treating it as a rate."""
    parts = [part.strip() for part in period.split(" or ")]
    if len(parts) == 2:
        return set(parts) == {"for the week", "for the month"}
    if len(parts) != 1:
        return False
    part = parts[0]
    return (part in QUANTITY_PERIODS or EVERY_N.fullmatch(part) is not None
            or THESE_N.fullmatch(part) is not None or OVER_N.fullmatch(part) is not None
            or FINITE_DURATION.fullmatch(part) is not None)


SCOPE_KEYS = ("skill", "level", "equipment", "basis", "modality")
SOURCE_KEYS = ("id", "url", "speaker", "channel")
ALLOWED_SECTIONS = ("Details", "Evidence", "Example From The Source", "Why This Matters",
                    "Disagreement", "Connected Ideas")
NOT_STATED = "not stated"
MAX_QUOTE_WORDS = 60
MAX_PART_GAP = 200  # characters between two '...' parts of one quote
SUPPORT_MIN = 0.5  # share of claim content words that must occur in the cited quotes
SUPPORT_MIN_WORDS = 3

VID = r"vid-[A-Za-z0-9_-]+"
TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
SRC_TAG = re.compile(r"\[src:\s*(" + VID + r")(?:\s*@\s*(" + TS + r"))?\s*\]")
EVIDENCE_ITEM = re.compile(
    r"^\s*[-*]\s*(" + VID + r")(?:\s*@\s*(" + TS + r"))?\s*\(([^)]*)\)\s*:\s*(.+?)\s*$"
)
TS_MARKER = re.compile(r"\[((?:\d{1,2}:)?\d{1,2}:\d{2})(?:\.\d+)?\]")
WIKILINK = re.compile(r"\[\[([^\]|#]*)(?:[^\]]*)\]\]")

# Contract 5.4 plus package A's Whisper vocabulary (loaded below when present).
CONTRACT_VOCABULARY = (
    "planche", "front lever", "back lever", "maltese", "iron cross", "victorian", "tuck",
    "straddle", "pike", "handstand", "muscle-up", "pull-up", "dip", "ring", "rpe", "bodyweight",
    "toe", "toes",
)


def _load_vocabulary() -> set[str]:
    vocab = {v.lower() for v in CONTRACT_VOCABULARY}
    asr = HERE.parent / "src" / "twitter_articlenator" / "sources" / "asr_tools.py"
    if asr.is_file():
        # Register the module before exec_module: dataclasses look it up in sys.modules.
        # An error here is not hidden; a broken vocabulary module must be fixed.
        spec = importlib.util.spec_from_file_location("_zr_asr_tools", asr)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_zr_asr_tools"] = mod
        spec.loader.exec_module(mod)
        vocab |= {str(v).lower() for v in getattr(mod, "DEFAULT_CALISTHENICS_VOCABULARY", ())}
    return vocab


VOCABULARY = _load_vocabulary()


def ts_format_ok(ts: str | None) -> bool:
    """Contract: MM:SS below one hour, H:MM:SS from one hour on."""
    if not ts:
        return True
    parts = ts.split(":")
    if len(parts) == 2:
        return int(parts[0]) < 60
    return int(parts[1]) < 60 and int(parts[2]) < 60


def ts_to_seconds(ts: str | None) -> int | None:
    if not ts:
        return None
    secs = 0
    for p in ts.split(":"):
        secs = secs * 60 + int(p)
    return secs


# --------------------------------------------------------------------------- #
# Minimal YAML subset parser (mappings, lists, lists of mappings, inline lists)
# --------------------------------------------------------------------------- #


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote and (i == 0 or line[i - 1] != "\\"):
                quote = None
        elif ch in "\"'" and (i == 0 or not line[i - 1].isalnum()):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_inline(s: str) -> list[str]:
    items, cur, quote = [], [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        items.append("".join(cur))
    return [x.strip() for x in items if x.strip()]


def _split_inline_nested(s: str) -> list[str]:
    """Split a flow list at top-level commas (keeps [a, b] items whole)."""
    items, cur, depth, quote = [], [], 0, None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        items.append("".join(cur))
    return [x.strip() for x in items if x.strip()]


def _scalar(raw: str) -> Any:
    v = raw.strip()
    if v in ("", "~", "null", "Null", "NULL"):
        return None
    if v.startswith("[") and v.endswith("]") and not re.fullmatch(r"\[\[[^\[\]\"']+\]\]", v):
        return [_scalar(x) for x in _split_inline_nested(v[1:-1])]
    if v.startswith("{") and v.endswith("}"):
        out: dict[str, Any] = {}
        for part in _split_inline_nested(v[1:-1]):
            if ":" in part:
                k, _, val = part.partition(":")
                out[k.strip().strip("\"'")] = _scalar(val)
        return out
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


_KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_ -]*?):(?:\s+(.*))?$")


def parse_yaml_subset(text: str) -> dict[str, Any]:
    """Parse the frontmatter subset that NOTE_CONTRACT.md uses.

    Supported: `key: scalar`, `key: [a, b]`, `key:` followed by an indented
    mapping or list, lists of scalars, lists of mappings. Unknown forms become
    strings, never exceptions.
    """
    rows: list[tuple[int, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        clean = _strip_comment(line)
        if clean.strip():
            rows.append((len(clean) - len(clean.lstrip(" ")), clean.strip()))
    value, _ = _parse_block(rows, 0)
    return value if isinstance(value, dict) else {}


def _parse_block(rows: list[tuple[int, str]], i: int) -> tuple[Any, int]:
    if i >= len(rows):
        return None, i
    if rows[i][1].startswith("- ") or rows[i][1] == "-":
        return _parse_list(rows, i, rows[i][0])
    return _parse_map(rows, i, rows[i][0])


def _parse_map(rows: list[tuple[int, str]], i: int, indent: int) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while i < len(rows):
        ind, content = rows[i]
        if ind < indent or (ind == indent and content.startswith("- ")):
            break
        if ind > indent:
            i += 1
            continue
        m = _KEY.match(content)
        if not m:
            i += 1
            continue
        key, val = m.group(1).strip(), m.group(2)
        i += 1
        if val is None or val.strip() == "":
            if i < len(rows) and (rows[i][0] > indent or (rows[i][0] == indent and rows[i][1].startswith("- "))):
                child, i = _parse_block(rows, i)
                out[key] = child
            else:
                out[key] = None
        else:
            out[key] = _scalar(val)
    return out, i


def _parse_list(rows: list[tuple[int, str]], i: int, indent: int) -> tuple[list, int]:
    out: list[Any] = []
    while i < len(rows):
        ind, content = rows[i]
        if ind != indent or not (content.startswith("- ") or content == "-"):
            break
        rest = content[1:].strip()
        i += 1
        if not rest:
            if i < len(rows) and rows[i][0] > indent:
                child, i = _parse_block(rows, i)
                out.append(child)
            else:
                out.append(None)
            continue
        m = _KEY.match(rest)
        if m and not rest.startswith(("\"", "'", "[")):
            sub_indent = indent + 2
            sub_rows = [(sub_indent, rest)]
            while i < len(rows) and rows[i][0] > indent:
                sub_rows.append(rows[i])
                i += 1
            child, _ = _parse_map(sub_rows, 0, sub_indent)
            out.append(child)
        else:
            out.append(_scalar(rest))
    return out, i


def split_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
    """Return (frontmatter, body, has_frontmatter)."""
    if not text.startswith("---"):
        return {}, text, False
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text, False
    raw = text[3:end]
    body = text[end + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    return parse_yaml_subset(raw), body, True


def duplicate_keys(text: str) -> list[str]:
    """Top-level frontmatter keys that occur twice ("unsupported_reason: a" ... "unsupported_reason: b")."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    keys = re.findall(r"(?m)^([A-Za-z_][\w-]*)\s*:", text[3:end])
    return sorted({k for k in keys if keys.count(k) > 1})


# --------------------------------------------------------------------------- #
# Number words
# --------------------------------------------------------------------------- #

ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
FRACTIONS = {"half": 0.5, "quarter": 0.25}
MULTIPLE_WORDS = {"once": 1, "twice": 2, "thrice": 3}
# Fixed terms that contain "one" but are not a quantity.
FIXED_TERMS = [
    (re.compile(r"\b(?:one|1)[\s-]*rep[\s-]*max(?:es)?\b"), " onerepmax "),
    (re.compile(r"\brep[\s-]*max(?:es)?\b"), " repmax "),
    (re.compile(r"\b(?:one|1)[\s-]*(arm|leg|hand)(ed|s)?\b"), r" one\1 "),
    (re.compile(r"\b(?:one|1)[\s-]*legged\b"), " oneleg "),
    (re.compile(r"\bno one\b"), " nobody "),
    (re.compile(r"\bone another\b"), " eachother "),
]

# Curly quotes and apostrophes become straight on both sides of every comparison.
QUOTE_CHARS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
})
DASH_CHARS = str.maketrans({"–": "-", "—": "-", "−": "-", " ": " "})


def number_word_value(words: list[str], i: int) -> tuple[float | None, int]:
    """Parse a spoken number at words[i]: "ten", "twenty five", "two hundred and fifty"."""
    def small(j: int) -> tuple[int | None, int]:
        if j < len(words) and words[j] in TENS:
            v = TENS[words[j]]
            if j + 1 < len(words) and words[j + 1] in ONES and 0 < ONES[words[j + 1]] < 10:
                return v + ONES[words[j + 1]], 2
            return v, 1
        if j < len(words) and words[j] in ONES:
            return ONES[words[j]], 1
        return None, 0

    val, n = small(i)
    if val is None:
        return None, 0
    j = i + n
    if j < len(words) and words[j] == "hundred" and 0 < val < 10:
        val, j = val * 100, j + 1
        k = j + 1 if j < len(words) and words[j] == "and" else j
        rest, m = small(k)
        if rest is not None:
            val, j = val + rest, k + m
    return float(val), j - i


def _fmt_num(v: float) -> str:
    return str(int(v)) if v == int(v) else repr(v)


def normalize_for_match(text: str, drop_brackets: bool = True) -> list[str]:
    """Tolerant tokens (case, punctuation and number words ignored).

    Used only for hints after a strict mismatch and for the APPROXIMATE
    report on old notes. The contract quote rule uses `strict` text.
    """
    t = text.translate(QUOTE_CHARS).translate(DASH_CHARS).lower()
    if drop_brackets:
        t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"(?<=[a-z])'(?=[a-z])", "", t)
    t = re.sub(r"(\d),(\d{3})\b", r"\1\2", t)
    raw = re.findall(r"\d+(?:\.\d+)?|[a-z]+", t)
    out: list[str] = []
    i = 0
    while i < len(raw):
        val, n = number_word_value(raw, i) if raw[i].isalpha() else (None, 0)
        if val is not None:
            out.append(_fmt_num(val))
            i += n
            continue
        tok = raw[i]
        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            tok = _fmt_num(float(tok))
        out.append(tok)
        i += 1
    return out


# --------------------------------------------------------------------------- #
# Quantity extraction: number + unit + period + limit word
# --------------------------------------------------------------------------- #

UNITS = {
    "set": "set", "sets": "set",
    "rep": "rep", "reps": "rep", "repetition": "rep", "repetitions": "rep",
    "second": "second", "seconds": "second", "sec": "second", "secs": "second", "s": "second",
    "minute": "minute", "minutes": "minute", "min": "minute", "mins": "minute",
    "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour", "h": "hour",
    "day": "day", "days": "day",
    "week": "week", "weeks": "week",
    "month": "month", "months": "month",
    "year": "year", "years": "year",
    # One frequency class: "2 times per week" == "2 sessions per week" == "2 workouts per week".
    "time": "times", "times": "times", "session": "times", "sessions": "times",
    "workout": "times", "workouts": "times", "training": "times", "trainings": "times",
    "percent": "percent", "percentage": "percent", "%": "percent",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "degree": "degree", "degrees": "degree",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm",
    "hold": "hold", "holds": "hold",
    "attempt": "attempt", "attempts": "attempt",
    "round": "round", "rounds": "round",
    "exercise": "exercise", "exercises": "exercise",
    "movement": "movement", "movements": "movement",
    "progression": "progression", "progressions": "progression",
    "step": "step", "steps": "step",
    "pull": "pull-up", "pullup": "pull-up", "pullups": "pull-up",
    "push": "push-up", "pushup": "push-up", "pushups": "push-up",
    "dip": "dip", "dips": "dip",
    "squat": "squat", "squats": "squat",
    "row": "row", "rows": "row",
    "muscle": "muscle-up", "muscleup": "muscle-up", "muscleups": "muscle-up",
    "skill": "skill", "skills": "skill",
    "meal": "meal", "meals": "meal",
    "calorie": "calorie", "calories": "calorie",
    "gram": "gram", "grams": "gram", "g": "gram",
    "bodyweight": "bodyweight",
}
PERIOD_NOUNS = {
    "set": "per set", "sets": "per set",
    "rep": "per rep", "repetition": "per rep",
    "session": "per session", "workout": "per session", "training": "per session",
    "practice": "per session",
    "day": "per day",
    "week": "per week", "microcycle": "per week",
    "month": "per month",
    "cycle": "per cycle", "mesocycle": "per cycle", "block": "per cycle",
}
PLURAL_PERIOD_NOUNS = {"weeks": "weeks", "days": "days", "months": "months", "sessions": "sessions",
                       "week": "weeks", "day": "days", "month": "months"}
PERIOD_ADVERBS = {"weekly": "per week", "daily": "per day", "monthly": "per month"}
PERIOD_LEADS = {"per", "each", "every", "a", "an"}
SENTENCE_END = {".", ";", "!", "?"}
BOUNDARY = SENTENCE_END | {":", "(", ")"}
RANGE_JOIN = {"to", "-", "or"}
COORD = {"or", "and"}
CLAUSE_WORDS = {"and", "but", "so", "then", "because", "while", "when", "if", "after", "before", "means"}
UNIT_WINDOW = 3
PERIOD_FWD = 12
PERIOD_BACK = 8
INHERIT_GAP = 6
# Limit words (contract 5.3), longest phrase first. Class names are the verdict vocabulary.
QUALIFIERS: list[tuple[tuple[str, ...], str]] = [
    (("no", "more", "than"), "upper"), (("not", "more", "than"), "upper"),
    (("at", "most"), "upper"), (("up", "to"), "upper"), (("maximum",), "upper"), (("max",), "upper"),
    (("at", "least"), "lower"), (("minimum",), "lower"), (("min",), "lower"),
    (("more", "than"), "more"), (("less", "than"), "less"), (("fewer", "than"), "less"),
    (("around",), "approx"), (("about",), "approx"), (("approximately",), "approx"),
    (("roughly",), "approx"), (("only",), "only"), (("exactly",), "exact"),
]
RANGE_QUALIFIERS = {("maximum",): "upper", ("max",): "upper", ("up", "to"): "upper", ("at", "most"): "upper"}
QUAL_STOPWORDS = {"of", "for", "a", "an", "the"}
# Verbs that state an upper limit for the next quantity in the clause ("Limits ... To 30").
LIMIT_VERBS = {"limit", "limits", "limited", "limiting", "cap", "caps", "capped", "capping"}
NEGATIONS = {"dont", "not", "never", "no", "doesnt", "didnt", "cant", "shouldnt", "wont"}


@dataclass
class Quantity:
    values: tuple[float, ...]
    unit: str | None
    period: str | None
    text: str
    forms: tuple[str, ...] = ()
    quals: frozenset[str] = frozenset()
    ref: tuple[float, ...] | None = None  # the frame quantity this count depends on
    start: int = 0
    unit_end: int = 0
    sentence: int = 0
    inherited: bool = False  # the unit came from the quantity before it, not from a word
    is_range: bool = False  # "A to B", "A-B", "between A and B": every value from A to B

    def as_dict(self) -> dict[str, Any]:
        return {
            "values": [_fmt_num(v) for v in self.values],
            "forms": list(self.forms),
            "unit": self.unit,
            "period": self.period,
            "qualifiers": sorted(self.quals),
            "ref": [_fmt_num(v) for v in self.ref] if self.ref else None,
            "text": self.text,
        }


def _claim_tokens(text: str) -> list[str]:
    """Tokens for quantity extraction. Source tags and wikilinks are removed;
    other brackets stay (a number in a bracket is a number in the claim)."""
    t = text.translate(QUOTE_CHARS).translate(DASH_CHARS).lower()
    t = SRC_TAG.sub(" ; ", t)
    t = re.sub(r"\[src:[^\]]*\]", " ; ", t)
    t = WIKILINK.sub(" ", t)
    # A number in a video or episode name is a name, not a quantity ("Ep.7", "Episode 12", "#3").
    t = re.sub(r"\b(?:ep|episode)\s*\.?\s*#?\d+\b|#\d+\b", " ", t)
    t = re.sub(r"[\[\]*_`\"]", " ", t)
    for rx, rep in FIXED_TERMS:
        t = rx.sub(rep, t)
    t = re.sub(r"(\d),(\d{3})\b", r"\1\2", t)
    t = re.sub(r"\b(\d)\s+(\d{3})\b", r"\1\2", t)
    # Round 9: a counted adjective ("two-armed", "one-arm", "three-quarter") is not a quantity.
    t = re.sub(r"\b(one|two|three|four|five|single|double|\d)-(arm(?:ed|s)?|leg(?:ged|s)?|hand(?:ed)?|"
               r"quarters?|sided|foot(?:ed)?|finger(?:ed)?)\b", r"\1\2", t)
    t = re.sub(r"(\d)\s*-\s*(\d)", r"\1 to \2", t)
    t = re.sub(r"(\d)\s*[x×]\s*(\d)", r"\1 by \2", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*[x×](?![a-z0-9])", r"\1 times", t)
    t = re.sub(r"\b[x×]\s*(\d+)\b", r"\1 times", t)
    t = re.sub(r"(\d)(s|sec|secs|kg|kgs|lb|lbs|min|mins|h|hr|hrs|cm|g)\b", r"\1 \2", t)
    t = re.sub(r"(?<=[a-z])'(?=[a-z])", "", t)
    t = t.replace("-", " ")
    raw = re.findall(r"\d+(?:\.\d+)?|[a-z]+|%|[.;:!?(),]", t)
    out: list[str] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok in MULTIPLE_WORDS:
            out += ["#" + _fmt_num(MULTIPLE_WORDS[tok]) + "~" + tok, "times"]
            i += 1
            continue
        if tok in FRACTIONS:
            out.append("#w" + _fmt_num(FRACTIONS[tok]) + "~" + tok)
            i += 1
            continue
        if tok.isalpha():
            val, n = number_word_value(raw, i)
            if val is not None:
                surface = " ".join(raw[i : i + n])
                out.append(("#w" if raw[i] == "one" and n == 1 else "#") + _fmt_num(val) + "~" + surface)
                i += n
                continue
        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            out.append("#" + _fmt_num(float(tok)) + "~" + tok)
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _is_num(tok: str) -> bool:
    return tok.startswith("#")


def _num(tok: str) -> float:
    body = tok[2:] if tok.startswith("#w") else tok[1:]
    return float(body.split("~", 1)[0])


def _is_year(tok: str) -> bool:
    """A year ("2024") is a date, not an amount: never joined into a range or list."""
    return _is_num(tok) and re.fullmatch(r"(19|20)\d\d", _form(tok)) is not None


def _form(tok: str) -> str:
    return tok.split("~", 1)[1] if "~" in tok else _fmt_num(_num(tok))


def _show(tok: str) -> str:
    return _form(tok) if _is_num(tok) else tok


def _finite_duration_at(toks: list[str], k: int) -> tuple[str, int] | None:
    """Recognize bounded year spans without converting their spoken quantity."""
    if k >= len(toks):
        return None
    start = k
    if toks[k] in ("for", "over", "in", "last"):
        start += 1
    if start >= len(toks):
        return None
    if toks[start] in ("a", "an") and start + 3 < len(toks) \
            and toks[start + 1] == "year" and toks[start + 2:start + 4] == ["and", "a"] \
            and start + 4 < len(toks) and toks[start + 4] == "#w0.5~half":
        return "year and a half", start + 5 - k
    if not _is_num(toks[start]):
        if toks[start] == "year" and start + 3 < len(toks) \
                and toks[start + 1:start + 4] == ["and", "a", "#w0.5~half"]:
            return "year and a half", start + 4 - k
        return None
    number = _form(toks[start])
    unit_at = start + 1
    if unit_at >= len(toks) or toks[unit_at] not in ("year", "years"):
        return None
    end = unit_at + 1
    value = f"{number} {toks[unit_at]}"
    if end + 2 < len(toks) and toks[end:end + 3] == ["and", "a", "#w0.5~half"]:
        value += " and a half"
        end += 3
    # Prefixes locate the finite interval; the stored period preserves the spoken span.
    return value, end - k


def _duration_has_amount(toks: list[str], start: int, length: int) -> bool:
    """A prefixed year span is a period only when tied locally to a distinct amount."""
    if start >= len(toks):
        return False
    end = start + length
    prefixed = toks[start] in ("for", "over", "in", "last")
    bare_half = "and" in toks[start:end] and "#w0.5~half" in toks[start:end]
    if not prefixed and not bare_half:
        return False
    for direction in (-1, 1):
        if direction < 0:
            candidates = range(start - 1, max(-1, start - 9), -1)
        else:
            candidates = range(end, min(len(toks), end + 9))
        for i in candidates:
            if toks[i] in BOUNDARY or toks[i] == "," or toks[i] in CLAUSE_WORDS:
                break
            if not _is_num(toks[i]):
                continue
            if direction < 0 and i >= start:
                continue
            if direction > 0 and i < end:
                continue
            return True
    return False


def _has_diet_rate_frame(toks: list[str], period_at: int) -> bool:
    """Require the explicit conditional diet/count context before normalizing meal/day."""
    lo = max(0, period_at - 32)
    for j in range(period_at - 1, lo - 1, -1):
        if toks[j] in SENTENCE_END or toks[j] in (";", "?", "!"):
            lo = j + 1
            break
    before = toks[lo:period_at]
    has_diet = any(before[j:j + 2] == ["on", "diet"] for j in range(max(0, len(before) - 6)))
    has_trying = any(t in ("try", "trying") for t in before)
    has_meal_count = any(_is_num(before[j]) and j + 1 < len(before) and before[j + 1] in ("meal", "meals")
                         for j in range(len(before)))
    return has_diet and has_trying and has_meal_count


def _period_at(toks: list[str], k: int) -> tuple[str, int] | None:
    """Period phrase that starts at toks[k]: (canonical period, length)."""
    t = toks[k]
    duration = _finite_duration_at(toks, k)
    if duration and _duration_has_amount(toks, k, duration[1]):
        return duration
    if t in PERIOD_ADVERBS:
        return PERIOD_ADVERBS[t], 1
    if t in ("every", "these") and k + 2 < len(toks) and _is_num(toks[k + 1]) and toks[k + 2] in PLURAL_PERIOD_NOUNS:
        return f"{t} {_form(toks[k + 1])} {PLURAL_PERIOD_NOUNS[toks[k + 2]]}", 3
    if t == "this" and k + 1 < len(toks) and toks[k + 1] in ("week", "day", "month"):
        return f"this {toks[k + 1]}", 2
    if t == "the" and k + 1 < len(toks) and toks[k + 1] in ("meal", "meals"):
        return ("per meal" if _has_diet_rate_frame(toks, k) else "the meal"), 2
    if t == "for" and k + 2 < len(toks) and toks[k + 1:k + 3] == ["the", "day"]:
        return ("per day" if _has_diet_rate_frame(toks, k) else "for the day"), 3
    if t == "for" and k + 2 < len(toks) and toks[k + 1] == "the" and toks[k + 2] in ("week", "month"):
        return f"for the {toks[k + 2]}", 3
    if t == "of" and k > 0 and toks[k - 1] in ("day", "days") and k + 2 < len(toks) \
            and toks[k + 1:k + 3] == ["the", "week"]:
        return "per week", 3
    if t == "over" and k + 2 < len(toks) and _is_num(toks[k + 1]) \
            and toks[k + 2] in PLURAL_PERIOD_NOUNS and toks[k + 2] != "sessions":
        return f"over {_form(toks[k + 1])} {PLURAL_PERIOD_NOUNS[toks[k + 2]]}", 3
    if t in PERIOD_LEADS and k + 1 < len(toks):
        nxt = toks[k + 1]
        if nxt in PERIOD_NOUNS:
            return PERIOD_NOUNS[nxt], 2
        if nxt in ("single", "training") and k + 2 < len(toks) and toks[k + 2] in PERIOD_NOUNS:
            return PERIOD_NOUNS[toks[k + 2]], 3
        # Bare "each" is a period shorthand only when it closes the amount phrase.
        # An object phrase such as "each exercise" must not mask a nearby rate like
        # "every month"; explicit period nouns above retain their normal handling.
        if t == "each" and nxt in (BOUNDARY | {",", "and", "but", "then"}):
            return "each", 1
        return None
    if t == "each":
        return "each", 1
    if t == "in" and k + 2 < len(toks) and (
        toks[k + 1] in ("a", "each", "every", "the") or toks[k + 1].startswith(("#w1~", "#1~"))
    ):
        if toks[k + 2] in PERIOD_NOUNS:
            return PERIOD_NOUNS[toks[k + 2]], 3
    return None


def _qualifier_before(toks: list[str], start: int) -> set[str]:
    """The limit-word phrase that ends right before toks[start] (one phrase only),
    or a limit verb earlier in the same clause."""
    for k in range(start - 1, max(-1, start - 7), -1):
        if toks[k] in BOUNDARY or toks[k] == "," or _is_num(toks[k]):
            break
        if toks[k] in LIMIT_VERBS and _limit_verb(toks, k, start):
            return {"upper"}
    if start > 0 and toks[start - 1] == "from":
        start -= 1  # "around from three to five seconds"
    # Small words between the limit word and the number do not count:
    # "a minimum of 10" == "minimum 10", "only for four weeks" == "only four weeks".
    skipped = 0
    while start > 0 and toks[start - 1] in QUAL_STOPWORDS and skipped < 2:
        start -= 1
        skipped += 1
    for phrase, cls in QUALIFIERS:
        n = len(phrase)
        s = start - n
        if s >= 0 and tuple(toks[s:start]) == phrase:
            if cls == "more" and any(t in NEGATIONS for t in toks[max(0, s - 3):s]):
                cls = "upper"  # "don't do more than 20"
            if cls == "less" and any(t in NEGATIONS for t in toks[max(0, s - 3):s]):
                cls = "lower"
            return {cls}
    return set()


LIMIT_NOUN_LEADS = {"a", "an", "the", "your", "my", "his", "her", "their", "our", "its", "own", "this", "that"}


def _limit_verb(toks: list[str], k: int, start: int) -> bool:
    """"limit" or "cap" is a limit word only as a verb that binds the number: "limit
    sets to 30", "capped at 3". A noun or an adjective ("your limit", "limit sessions
    at four sessions per week") is not a limit word."""
    if k > 0 and toks[k - 1] in LIMIT_NOUN_LEADS:
        return False
    between = toks[k + 1:start]
    if toks[k].startswith("cap"):
        return "to" in between or "at" in between
    return "to" in between


def _inside_finite_duration(toks: list[str], i: int) -> bool:
    for start in range(max(0, i - 7), i + 1):
        hit = _finite_duration_at(toks, start)
        if hit and start <= i < start + hit[1]:
            return True
    return False


def _in_period_phrase(toks: list[str], i: int) -> bool:
    """True when toks[i] is a count inside a scoped period phrase."""
    for start in range(max(0, i - 6), i + 1):
        duration = _finite_duration_at(toks, start)
        if duration and start <= i < start + duration[1]:
            if _duration_has_amount(toks, start, duration[1]):
                return True
    for back in range(1, 7):
        s = i - back
        if s >= 0:
            hit = _period_at(toks, s)
            if hit and s + hit[1] > i:
                return True
    return False


def extract_quantities(text: str) -> list[Quantity]:
    toks = _claim_tokens(text)
    out: list[Quantity] = []
    sentence = 0
    i = 0
    while i < len(toks):
        if toks[i] in SENTENCE_END:
            sentence += 1
        if not _is_num(toks[i]) or _in_period_phrase(toks, i):
            i += 1
            continue
        start = i
        weak = toks[i].startswith("#w")
        values = [_num(toks[i])]
        forms = [_form(toks[i])]
        quals = _qualifier_before(toks, start)
        is_range = False
        j = i + 1
        # Ranges: "30 to 40", "30-40", "3 or 4", "one maximum two", "one, maximum two".
        while not _is_year(toks[i]):
            k = j + 1 if j < len(toks) and toks[j] == "," else j
            joined = None
            for phrase, cls in RANGE_QUALIFIERS.items():
                if tuple(toks[k:k + len(phrase)]) == phrase and k + len(phrase) < len(toks) \
                        and _is_num(toks[k + len(phrase)]):
                    joined = (k + len(phrase), cls)
                    break
            if joined is None and k == j and j + 1 < len(toks) and toks[j] in RANGE_JOIN and _is_num(toks[j + 1]):
                joined = (j + 1, None)
                is_range = is_range or toks[j] in ("to", "-")
            if joined is None and j + 2 < len(toks) and toks[k] in ("or", "even") and toks[k + 1] == "even" \
                    and k + 2 < len(toks) and _is_num(toks[k + 2]):
                joined = (k + 2, None)  # "four or even six months"
            if joined is None and k == j + 1 and k + 1 < len(toks) and toks[k] == "even" and _is_num(toks[k + 1]):
                joined = (k + 1, None)  # "four, even six months"
            if joined is None and k == j and j < len(toks) and _is_num(toks[j]) \
                    and not toks[j].startswith("#w") and _num(toks[j]) > values[-1]:
                joined = (j, None)  # spoken range: "one two times per week" = one to two
            if joined is None and k == j + 1 and k < len(toks):
                # Lists: "50, 60 push-ups", "6, 7, 8, 9 or 10 seconds", "9, or 10 seconds".
                if toks[k] in RANGE_JOIN and k + 1 < len(toks) and _is_num(toks[k + 1]):
                    joined = (k + 1, None)
                    is_range = is_range or toks[k] in ("to", "-")
                elif _is_num(toks[k]) and not toks[k].startswith("#w") and _num(toks[k]) > values[-1]:
                    joined = (k, None)
            if joined is not None and _is_year(toks[joined[0]]):
                joined = None
            if joined is None:
                break
            n_at, cls = joined
            values.append(_num(toks[n_at]))
            forms.append(_form(toks[n_at]))
            if cls:
                quals = quals | {cls}
            weak = False
            j = n_at + 1
        if start > 0 and toks[start - 1] == "between" and j + 1 < len(toks) \
                and toks[j] == "and" and _is_num(toks[j + 1]):
            values.append(_num(toks[j + 1]))
            forms.append(_form(toks[j + 1]))
            is_range = True
            j += 2
        end = j
        unit, unit_end = None, end - 1
        for k in range(end, min(end + UNIT_WINDOW, len(toks))):
            if toks[k] in BOUNDARY or toks[k] == "," or _is_num(toks[k]):
                break
            if toks[k] in UNITS:
                if toks[k] in ("week", "day", "month") and k > end and toks[k - 1] in PERIOD_LEADS:
                    break  # "2 a week": a period, not the unit
                unit, unit_end = UNITS[toks[k]], k
                break
        # A bare "one" (or "half") is a pronoun unless a unit, a limit word or a
        # following number ("one of the four") makes it a quantity.
        if weak and unit is None and not quals:
            follow = any(_is_num(t) for t in toks[end:end + 4])
            if not (follow and end < len(toks) and toks[end] == "of"):
                i = end
                continue
        period = _find_period(toks, start, unit_end)
        if unit is None and period in PERIOD_ADVERBS.values() and end + 1 < len(toks) \
                and toks[end] in PERIOD_ADVERBS and toks[end + 1] not in BOUNDARY | {","}:
            period = None  # P8: "six weekly levels" (an adjective of the noun), not "six per week"
        if unit in ("week", "day", "month") and period == f"per {unit}":
            period = None  # P8: "for six weeks" next to "four times per week" is a duration
        if len(values) == 1 and start > 0 and toks[start - 1] in NAME_LABELS \
                and (unit is None or unit == "step" or toks[start - 1] in ("step", "number")):
            i = end  # P8: "rule number three", "step 2": a label, not an amount
            continue
        if unit == "degree" and len(values) == 1 and start + 1 < len(toks) and toks[start + 1] == "degree" \
                and unit_end + 1 < len(toks) and toks[unit_end + 1] in DEGREE_NAMES:
            i = end  # P8: "the 90 degree handstand push-up" names a skill
            continue
        snippet = " ".join(_show(t) for t in toks[max(0, start - 3): min(len(toks), unit_end + 6)])
        q = Quantity(tuple(sorted(set(values))), unit, period, snippet, tuple(forms), frozenset(quals),
                     None, start, unit_end, sentence, is_range=is_range)
        prev = out[-1] if out else None
        if prev is not None and (_is_year(toks[prev.start]) or _is_year(toks[start])):
            prev = None  # a year shares no unit, period or list with an amount
        if prev is not None and prev.sentence == sentence:
            gap = toks[prev.unit_end + 1:start]
            if len(gap) <= INHERIT_GAP and not any(t in SENTENCE_END for t in gap):
                if len(gap) >= 1 and gap[-1] in COORD and prev.unit_end + 1 == start - 1 and not q.quals:
                    q.quals = prev.quals  # "no more than 20 reps or 30 seconds"
                if q.unit is None and prev.unit is not None:
                    q.unit, q.ref, q.inherited = prev.unit, prev.values, True
                    q.period = q.period or prev.period
                elif q.unit is not None and q.unit == prev.unit and q.period is None and prev.period:
                    q.period, q.ref = prev.period, prev.values
        if prev is not None and prev.sentence == sentence and q.unit is not None and q.unit == prev.unit \
                and not q.inherited:
            # One list in two pieces: "6, 7, 8, 9 seconds, 10 seconds", "seven seconds or five seconds".
            gap = toks[prev.unit_end + 1:start]
            joiners = {",", "or", "and", "even"}
            ascending = min(q.values) > max(prev.values)
            two_alternatives = gap == ["or"] and len(prev.values) == 1 and len(q.values) == 1
            combined_values = tuple(sorted(set(prev.values) | set(q.values)))
            consecutive_run = ascending and _consecutive(combined_values)
            spoken_repeated_unit = not gap and consecutive_run
            conditional_rest_alternative = gap == ["even", "if", "you", "rest"] and consecutive_run
            if spoken_repeated_unit or conditional_rest_alternative or (
                    gap and all(t in joiners for t in gap) and (ascending or two_alternatives)):
                prev.values = tuple(sorted(set(prev.values) | set(q.values)))
                prev.forms = prev.forms + q.forms
                prev.unit_end = q.unit_end
                prev.period = prev.period or q.period
                i = end
                continue
        out.append(q)
        i = end
    # "One, Maximum Two Failure Workouts At Four Workouts Per Week": the count
    # takes its frame (and period) from the quantity after "at".
    for a, b in zip(out, out[1:]):
        if a.sentence == b.sentence and a.ref is None and "at" in toks[a.unit_end + 1:b.start] \
                and len(toks[a.unit_end + 1:b.start]) <= 3:
            a.ref = b.values
            a.period = a.period or b.period
    return out


NAME_LABELS = {"number", "rule", "step", "level", "phase", "part", "chapter", "episode", "ep", "lesson", "stage",
               "version", "module", "tip", "mistake", "reason"}
DEGREE_NAMES = {"handstand", "push", "pushup", "pushups", "hold", "holds", "position", "planche", "lean", "bent", "dip",
                "dips", "angle"}
LABEL_WORDS = {"number", "no", "step", "rule", "level", "phase", "part", "chapter", "episode", "ep", "lesson",
               "stage", "version", "module", "week", "day", "tip", "mistake", "reason", "exercise"}
ORDINAL_WORDS = {"first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"}


def counted_quantities(text: str) -> list[Quantity]:
    """Quantities that scope.quantities must list. Not counted: a label ("step 3", "rule
    number one"), an ordinal ("3rd", "first"), a year, and an angle used as a name
    ("the 90 degree position", "90 degree hold")."""
    toks = _claim_tokens(text)
    out = []
    for q in extract_quantities(text):
        before = toks[q.start - 1] if q.start > 0 else ""
        after = toks[q.start + 1] if q.start + 1 < len(toks) else ""
        if before in LABEL_WORDS and len(q.values) == 1 and q.unit in (None, "times", "week", "day", "exercise", "step"):
            continue
        if after in ("st", "nd", "rd", "th"):
            continue
        if len(q.values) == 1 and _is_year(toks[q.start]) and q.unit is None:
            continue
        if q.unit is None and len(q.values) == 1 and q.values[0] == 100 and after not in UNITS:
            continue  # "not 100 tight", "100 focused": spoken "fully", not an amount
        if q.unit == "degree" and after == "degree":
            continue  # "90 degree" (singular) names a position; "90 degrees" is an amount
        out.append(q)
    return out


def _find_period(toks: list[str], start: int, unit_end: int) -> str | None:
    # Preserve the narrowly source-explicit workout window without treating a
    # nearby phrase such as "for the week you have three rest days" as a period.
    workout_window = toks[unit_end] in ("workout", "workouts")
    # A duration is a one-off window only when an explicit total links it to this
    # quantity: "four weeks ... that means 16 workouts". Do not infer one from
    # an unrelated nearby duration or from arithmetic alone.
    if start >= 2 and toks[start - 2:start] == ["that", "means"]:
        for j in range(start - 3, max(-1, start - 25), -1):
            if toks[j] in SENTENCE_END or toks[j] in (";", "?", "!"):
                break
            if _is_num(toks[j]) and j + 1 < start and toks[j + 1] in ("week", "weeks", "day", "days", "month", "months"):
                unit = toks[j + 1]
                if j + 2 < start and toks[j + 2] == "per":
                    continue  # this duration is the numerator of a recurring rate
                return f"over {_form(toks[j])} {unit}"
    # Forward: the nearest period phrase in the same clause, up to PERIOD_FWD tokens.
    # It may cross only a quantity joined DIRECTLY by "or"/"and" right after this one
    # ("20 to 30 reps or 30 to 40 seconds of volume per session").
    k = unit_end + 1
    last_end = unit_end
    limit = min(len(toks), unit_end + 1 + PERIOD_FWD)
    while k < limit:
        if toks[k] in BOUNDARY:
            break
        hit = _period_at(toks, k)
        if hit and hit[0] in ("for the week", "for the month") and not workout_window:
            hit = None
        if hit:
            period = hit[0]
            # Keep the source's explicit bounded-window alternatives together. This
            # records the disjunction without converting either window to a rate.
            if period in ("for the week", "for the month"):
                alt_at = k + hit[1]
                if alt_at + 1 < len(toks) and toks[alt_at] == "or":
                    alt = _period_at(toks, alt_at + 1)
                    if alt and alt[0] in ("for the week", "for the month") and alt[0] != period:
                        period = f"{period} or {alt[0]}"
            # A one-off window before a later amount binds backward to that amount,
            # not forward across it to an earlier quantity in the same clause.
            if toks[k] == "these":
                after_window = k + hit[1]
                later_amount = next((j for j in range(after_window, min(len(toks), after_window + PERIOD_FWD))
                                     if toks[j] in BOUNDARY or _is_num(toks[j])), None)
                if later_amount is not None and _is_num(toks[later_amount]):
                    break
            return period
        if _is_num(toks[k]):
            if toks[k - 1] in COORD and k - 2 == last_end:
                j = k
                while j + 2 < len(toks) and toks[j + 1] in RANGE_JOIN and _is_num(toks[j + 2]):
                    j += 2
                u = j
                for m in range(j + 1, min(j + 1 + UNIT_WINDOW, len(toks))):
                    if toks[m] in BOUNDARY or _is_num(toks[m]):
                        break
                    if toks[m] in UNITS:
                        u = m
                        break
                last_end = u
                k = u + 1
                limit = min(len(toks), max(limit, u + 1 + PERIOD_FWD // 2))
                continue
            break
        k += 1
    # A short explicit finite span can scope a nearby total across its spoken clause
    # ("year and a half we have 500 students"). Keep this search local and stop at
    # another amount, clause boundary, or sentence so unrelated durations never bind.
    for s in range(max(0, start - 16), start):
        hit = _finite_duration_at(toks, s)
        if not hit or not _duration_has_amount(toks, s, hit[1]):
            continue
        after = s + hit[1]
        between = toks[after:start]
        if len(between) <= 8 and not any(t in BOUNDARY or t in CLAUSE_WORDS or _is_num(t) for t in between):
            return hit[0]
    # Backward: the nearest period phrase in the same clause (a colon does not end
    # it: "At four workouts per week: only one"), stopping at a comma, a clause
    # word or another number.
    lo = max(0, start - PERIOD_BACK)
    k = start - 1
    while k >= lo:
        if toks[k] in SENTENCE_END or toks[k] in ("(", ")", ",") \
                or (toks[k] in CLAUSE_WORDS and not _inside_finite_duration(toks, k)) \
                or (_is_num(toks[k]) and not _in_period_phrase(toks, k)):
            lo = k + 1
            break
        k -= 1
    k = start - 1
    while k >= lo:
        for back in range(7, 0, -1):
            s = k - back + 1
            if s < lo:
                continue
            hit = _period_at(toks, s)
            if hit and hit[1] == back and toks[s] not in ("a", "an") and hit[0] != "the meal":
                if hit[0] in ("for the week", "for the month") and not workout_window:
                    continue
                # P8: "finish a workout 70% exhausted": "a workout" before the number is
                # the object of the verb, not a period ("70% per workout").
                return hit[0]
        k -= 1
    return None


def _period_key(period: str | None) -> str | None:
    if period is None:
        return None
    value = re.sub(r"^(?:for|over|last|in) ", "", period)
    if value == "one year and a half":
        return "year and a half"
    return value


def _period_keys(period: str | None) -> tuple[str, ...] | None:
    if period is None:
        return None
    parts = tuple(part.strip() for part in period.split(" or "))
    keys = tuple(_period_key(part) for part in parts)
    return tuple(sorted(set(keys))) if all(key is not None for key in keys) else None


def _same_period(left: str | None, right: str | None) -> bool:
    if left == right:
        return True
    left_keys, right_keys = _period_keys(left), _period_keys(right)
    return left_keys is not None and left_keys == right_keys


def _claim_period_matches(claim_period: str | None, quote_period: str | None) -> bool:
    """Match exact period alternatives, with the existing diet-frame equivalents."""
    if claim_period == quote_period:
        return True
    if not claim_period or not quote_period:
        return False
    claim_parts = claim_period.split(" or ")
    quote_parts = quote_period.split(" or ")
    if len(claim_parts) != len(quote_parts):
        return False
    mapped = {"the meal": "per meal", "for the day": "per day"}
    claim_keys = {mapped.get(part.strip(), _period_key(part.strip())) for part in claim_parts}
    quote_keys = {_period_key(part.strip()) for part in quote_parts}
    return None not in claim_keys and claim_keys == quote_keys


def match_quantity(q: Quantity, pool: list[Quantity], strict_form: bool = True) -> tuple[str, dict | None]:
    """Return (verdict, matched quote quantity).

    Checks in order: value set, unit, period, frame quantity, written form,
    limit words. A claim that drops a unit or period that the quote has fails
    too (`dropped-unit`, `dropped-period`).
    """
    same = [p for p in pool if _same_values(q, p)]
    if not same:
        overlap = [p for p in pool if set(p.values) & set(q.values)]
        if overlap:
            return "range-mismatch", overlap[0].as_dict()
        return "number-not-in-quote", None
    if q.unit is not None:
        cand = [p for p in same if p.unit == q.unit]
        if not cand:
            return "changed-unit", same[0].as_dict()
    else:
        # A unit that the quote quantity only inherited from its neighbor is not dropped.
        cand = [p for p in same if p.unit is None or p.inherited]
        if not cand:
            return "dropped-unit", same[0].as_dict()
    same = cand
    if q.period is not None:
        # P8: "one hour each" == "one hour each workout" (claim "each", quote per session,
        # set or rep). "each" for a quote "per week" stays a changed period.
        cand = [p for p in same if _claim_period_matches(q.period, p.period)
                or (q.period == "each" and p.period in ("per session", "per set", "per rep"))]
        if not cand:
            return "changed-period", same[0].as_dict()
    else:
        cand = [p for p in same if p.period is None]
        if not cand:
            return "dropped-period", same[0].as_dict()
    same = cand
    cand = [p for p in same if q.ref is None or p.ref is None or p.ref == q.ref]
    if not cand:
        return "changed-reference", same[0].as_dict()
    same = cand
    if strict_form:
        cand = [p for p in same if _forms_ok(q, p)]
        if not cand:
            return "changed-number-form", same[0].as_dict()
        same = cand
    cand = [p for p in same if _quals_ok(q.quals, p.quals)]
    if not cand:
        return "changed-qualifier", same[0].as_dict()
    return "supported", cand[0].as_dict()


# Round 11 (pilot 5): a cited time up to 30 s after the quote's segment is the same
# statement (the quote gives the frame, the cited segment the number).
QUOTE_TIME_TOLERANCE_S = 30
MULTIPLE_FORMS = {"once": "one", "twice": "two", "thrice": "three"}


def _forms_ok(q: Quantity, p: Quantity) -> bool:
    """Same written forms. A claim range "A to B" over a spoken list A, A+1 .. B
    compares with the forms of the first and the last value of the list."""
    if sorted(p.forms) == sorted(q.forms):
        return True
    if sorted(MULTIPLE_FORMS.get(f, f) for f in p.forms) == sorted(MULTIPLE_FORMS.get(f, f) for f in q.forms):
        return True  # round 11 (coordinator decision, K21): "twice" == "two times", "once" == "one time"
    if q.is_range and len(q.values) == 2 and len(p.values) > 2 and len(p.forms) >= 2:
        return sorted(q.forms) == sorted((p.forms[0], p.forms[-1]))
    return False


def _consecutive(values: tuple[float, ...]) -> bool:
    return all(float(v).is_integer() for v in values) and \
        list(values) == [float(x) for x in range(int(values[0]), int(values[-1]) + 1)]


def _same_values(q: Quantity, p: Quantity) -> bool:
    """Contract 4, spoken runs. A claim range "A to B" needs a quote range "A to B", or a
    quote that lists every value from A to B. A claim "A or B" (two values) needs the
    quote's two values. Adjacent integers (3 to 4, 3 or 4) are the same either way.
    A widened or narrowed range, and a list that skips values written as a range, fail."""
    if q.is_range and len(q.values) == 2:
        if p.is_range:
            return p.values == q.values
        if p.values == q.values:
            return _consecutive(q.values)  # "3 4" -> "3 to 4" yes; "50 60" -> "50 to 60" no
        return len(p.values) > 2 and p.values[0] == q.values[0] and p.values[-1] == q.values[-1] \
            and _consecutive(p.values)
    if p.values != q.values:
        return False
    if p.is_range and len(p.values) == 2 and not q.is_range:
        return _consecutive(p.values)  # "5 to 7" in the quote is not "5 or 7"
    return True


def _quals_ok(claim: frozenset[str], quote: frozenset[str]) -> bool:
    """Fail only a CHANGED or DROPPED limit word: one that the quote has next to the
    same number. A limit word that only the claim has is left to the LLM review."""
    need = set(quote)
    if "only" in need and "upper" in claim:
        need.discard("only")  # "only one maximum two" == "one, maximum two"
    return need <= set(claim)


# --------------------------------------------------------------------------- #
# Transcripts and the transcript registry
# --------------------------------------------------------------------------- #


@dataclass
class Transcript:
    vid: str
    path: Path
    meta: dict[str, Any]
    has_ts: bool
    strict: str = ""
    marker_pos: list[int] = field(default_factory=list)
    marker_time: list[int] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    joined: str = ""
    word_set: set[str] = field(default_factory=set)
    speakers_derived: bool = False
    damaged_times: list[tuple[int, int]] = field(default_factory=list)  # asr_damaged_spans, seconds
    damaged_ranges: list[tuple[int, int, str]] = field(default_factory=list)  # strict offsets
    span_unit_unknown: bool = False

    def damaged_at(self, pos: int, part: str) -> str | None:
        """The damaged span that a quote part at strict offset `pos` overlaps."""
        end = pos + len(part)
        if self.damaged_times and self.has_ts:
            for p in (pos, max(pos, end - 1)):
                mi = self.marker_index_at(p)
                t = self.marker_time[mi] if mi >= 0 else 0
                for a, b in self.damaged_times:
                    if a <= t <= b:
                        return f"{_fmt_ts(a)}-{_fmt_ts(b)}"
        for a, b, label in self.damaged_ranges:
            if pos < b and end > a:
                return label
        return None

    def marker_index_at(self, offset: int) -> int:
        return bisect.bisect_right(self.marker_pos, offset) - 1

    def marker_index_for_time(self, secs: int) -> int:
        return bisect.bisect_right(self.marker_time, secs) - 1

    @property
    def speakers(self) -> list[str] | None:
        sp = self.meta.get("speakers")
        if isinstance(sp, str):
            sp = [sp]
        if isinstance(sp, list):
            return [str(s) for s in sp]
        # Old lit files: author/channel is the only speaker hint (re-ingest them).
        alt = self.meta.get("channel") or self.meta.get("author")
        return [str(alt)] if alt else None

    @property
    def asr_quality(self) -> str:
        return str(self.meta.get("asr_quality") or "unknown")

    def find_tolerant(self, quote_tokens: list[str]) -> bool:
        return bool(quote_tokens) and (" " + " ".join(quote_tokens) + " ") in self.joined

    def number_values(self) -> set[str]:
        return {t for t in self.tokens if re.fullmatch(r"\d+(?:\.\d+)?", t)}


def _strict_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.translate(QUOTE_CHARS))


def load_transcript(path: Path) -> Transcript:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body, _ = split_frontmatter(text)
    # Drop the H1 title line: it is not speech.
    body = re.sub(r"(?m)^# .*$", "", body, count=1)
    vid = str(meta.get("id") or path.stem)
    tr = Transcript(vid, path, meta, has_ts=False)
    tr.speakers_derived = not isinstance(meta.get("speakers"), list | str)
    strict, pos = "", 0
    for m in TS_MARKER.finditer(body):
        strict = _join_ws(strict, body[pos : m.start()])
        tr.marker_pos.append(len(strict))
        tr.marker_time.append(ts_to_seconds(m.group(1)) or 0)
        tr.has_ts = True
        pos = m.end()
    strict = _join_ws(strict, body[pos:])
    lead = len(strict) - len(strict.lstrip(" "))
    tr.strict = strict.strip(" ")
    tr.marker_pos = [max(0, x - lead) for x in tr.marker_pos]
    _load_damaged_spans(tr, meta, body)
    plain = TS_MARKER.sub(" ", body)
    tr.tokens = normalize_for_match(plain)
    tr.joined = " " + " ".join(tr.tokens) + " "
    tr.word_set = {_stem(w) for w in re.findall(r"[a-z]+", plain.translate(QUOTE_CHARS).lower().replace("'", ""))}
    return tr


def _strict_len(text: str) -> int:
    """Length of the strict form of `text` (markers removed, whitespace collapsed,
    leading space removed): maps a body offset to a strict-text offset."""
    acc, pos = "", 0
    for m in TS_MARKER.finditer(text):
        acc = _join_ws(acc, text[pos:m.start()])
        pos = m.end()
    acc = _join_ws(acc, text[pos:])
    return len(acc.lstrip(" "))


def _load_damaged_spans(tr: Transcript, meta: dict[str, Any], body: str) -> None:
    """Read the damaged spans of a `partial` transcript (package A, round 3).

    - `asr_span_unit: timestamp`: `asr_damaged_spans` = [["MM:SS", "MM:SS"], ...];
      a quote whose start or end marker time lies in a span is damaged.
    - `asr_span_unit: lit-body-line`: lit body line 1 is the first line after the
      closing `---`. `asr_damaged_spans_detail` items {start_line, start_char,
      end_line, end_char} (0-based offsets in the line as written, end exclusive)
      are used when present, so the clean text before `start_char` and after
      `end_char` stays quotable. Else "lines A-B" marks whole lines.
    - No `asr_span_unit` (old files): the numbering is unknown; the spans are not
      used and the note gets the warning "span unit unknown - re-ingest".
    The damaged part is kept as a strict-text offset range; a quote that overlaps it fails.
    """
    spans = meta.get("asr_damaged_spans") or []
    detail = meta.get("asr_damaged_spans_detail") or []
    unit = str(meta.get("asr_span_unit") or "")
    # A `minor:` loop in asr_issues ("minor: loop: lines 95-95" or a marker range
    # "minor: loop: 01:20-01:35") is looped text too: a quote in it fails the same way.
    minor_lines: list[tuple[int, int, str]] = []
    minor_times: list[tuple[int, int]] = []
    for issue in meta.get("asr_issues") or []:
        txt = str(issue)
        if not txt.lstrip().lower().startswith("minor:") or "loop" not in txt.lower():
            continue
        m = re.search(r"lines?\s+(\d+)\s*-\s*(\d+)", txt)
        if m:
            minor_lines.append((int(m.group(1)), int(m.group(2)), f"minor loop lines {m.group(1)}-{m.group(2)}"))
            continue
        m = re.search(r"((?:\d{1,2}:)?\d{1,2}:\d{2})\s*-\s*((?:\d{1,2}:)?\d{1,2}:\d{2})", txt)
        if m and ts_to_seconds(m.group(1)) is not None and ts_to_seconds(m.group(2)) is not None:
            minor_times.append((int(ts_to_seconds(m.group(1)) or 0), int(ts_to_seconds(m.group(2)) or 0)))
    if not spans and not detail and not minor_lines and not minor_times:
        return
    if unit not in ("timestamp", "lit-body-line"):
        tr.span_unit_unknown = True
        return
    if unit == "timestamp":
        for sp in spans:
            if isinstance(sp, list) and len(sp) == 2:
                a, b = ts_to_seconds(str(sp[0])), ts_to_seconds(str(sp[1]))
                if a is not None and b is not None:
                    tr.damaged_times.append((a, b))
        # Character offsets (when given) refine the timed spans too.
    lines = body.splitlines(keepends=True)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln))
    items: list[tuple[int, int, int | None, int | None, str]] = []
    for d in detail if isinstance(detail, list) else []:
        if isinstance(d, dict) and d.get("start_line") is not None and d.get("end_line") is not None:
            items.append((int(d["start_line"]), int(d["end_line"]), d.get("start_char"), d.get("end_char"),
                          f"lines {d['start_line']}-{d['end_line']}"))
    if not items and unit == "lit-body-line":
        for sp in spans:
            m = re.match(r"lines?\s+(\d+)\s*-\s*(\d+)", str(sp))
            if m:
                items.append((int(m.group(1)), int(m.group(2)), None, None, str(sp)))
    if unit == "lit-body-line":
        items += [(a, b, None, None, label) for a, b, label in minor_lines]
    for la, lb, c0, c1, label in items:
        if la < 1 or la > len(lines):
            continue
        lb = min(max(lb, la), len(lines))
        s0 = starts[la - 1] + (int(c0) if c0 is not None else 0)
        s1 = starts[lb - 1] + (int(c1) if c1 is not None else len(lines[lb - 1].rstrip("\n")))
        tr.damaged_ranges.append((_strict_len(body[:s0]), _strict_len(body[:s1]), label))
    if items and unit == "timestamp":
        tr.damaged_times = []  # the character ranges are exact
    if unit == "timestamp":
        tr.damaged_times += minor_times


SENTENCE_END_RE = re.compile(r"[.?!](?=\s|$)")
SENTENCE_SEARCH = 600  # characters; without punctuation nearby, the quote is its own "sentence"
# Without punctuation (old ASR text), one "sentence" around a quote is this many characters
# (about two transcript lines, roughly 25 s of speech).
UNPUNCTUATED_SENTENCE = 400


def quote_sentences(strict: str, pos: int, end: int, extra: int = 0) -> tuple[int, int]:
    """Offsets of the sentence(s) of `strict` that contain the span pos..end, plus `extra`
    sentences on each side. Without sentence punctuation near the quote, the quoted span
    itself is the sentence, and each extra sentence is UNPUNCTUATED_SENTENCE characters."""
    if not SENTENCE_END_RE.search(strict, max(0, pos - SENTENCE_SEARCH), min(len(strict), end + SENTENCE_SEARCH)):
        return max(0, pos - UNPUNCTUATED_SENTENCE * extra), min(len(strict), end + UNPUNCTUATED_SENTENCE * extra)
    s = pos
    for _k in range(1 + extra):
        lo = max(0, s - SENTENCE_SEARCH)
        ends = [m.end() for m in SENTENCE_END_RE.finditer(strict, lo, max(lo, s - 1))]
        if ends:
            s = ends[-1]
        elif lo == 0:
            s = 0  # the first sentence of the transcript
        else:
            s = max(0, s - 200)
    e = end
    for k in range(1 + extra):
        m = SENTENCE_END_RE.search(strict, max(pos, e - 1) if k == 0 else e, min(len(strict), e + SENTENCE_SEARCH))
        if m:
            e = m.end()
        elif k:
            e = min(len(strict), e + 200)
    while s < e and strict[s] == " ":
        s += 1
    return s, e


def adjacent_sentences(strict: str, s0: int, s1: int) -> tuple[int, int]:
    """The sentence directly before s0 and after s1 (punctuated text only), unless it is a
    question (usually the other voice of an interview). Returns the widened offsets."""
    if not SENTENCE_END_RE.search(strict, max(0, s0 - SENTENCE_SEARCH), min(len(strict), s1 + SENTENCE_SEARCH)):
        return s0, s1
    a, _ = quote_sentences(strict, max(0, s0 - 2), max(0, s0 - 1)) if s0 > 1 else (s0, s0)
    before = strict[a:s0].strip()
    p0 = a if before and not before.endswith("?") and s0 - a <= SENTENCE_SEARCH else s0
    m = SENTENCE_END_RE.search(strict, min(len(strict), s1 + 1), min(len(strict), s1 + SENTENCE_SEARCH))
    after = strict[s1:m.end()].strip() if m else ""
    p1 = m.end() if m and after and not after.endswith("?") else s1
    return p0, p1


def _join_ws(acc: str, seg: str) -> str:
    seg = _strict_text(seg)
    if acc.endswith(" ") and seg.startswith(" "):
        seg = seg[1:]
    if acc and seg and not acc.endswith(" ") and not seg.startswith(" "):
        seg = " " + seg  # a marker between two words stands for a space
    return acc + seg


def strict_quote_parts(quote: str) -> list[str]:
    """Contract section 11: corrections removed, split at '...', whitespace collapsed."""
    q = re.sub(r"\s*\[[^\]]*\]", "", quote)
    parts = re.split(r"\.\.\.|…", q)
    return [_strict_text(p).strip() for p in parts if p.strip()]


def lit_dirs(staging: Path | None = None, extra: list[Path] | None = None) -> list[Path]:
    """All lit folders that feed one vault (the transcript registry).

    Sources, in this order: `<staging>/lit`, the env var ZR_LIT_DIRS (os.pathsep
    separated), and the file `<staging parent>/lit_dirs.txt` (one folder per
    line, relative to that parent; `#` starts a comment).
    """
    out: list[Path] = []
    if staging is not None:
        out.append(Path(staging) / "lit")
    for raw in (os.environ.get("ZR_LIT_DIRS") or "").split(os.pathsep):
        if raw.strip():
            out.append(Path(raw.strip()))
    if staging is not None:
        reg = Path(staging).resolve().parent / "lit_dirs.txt"
        if reg.is_file():
            for line in reg.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    p = Path(line)
                    out.append(p if p.is_absolute() else reg.parent / p)
    out += list(extra or [])
    seen, uniq = set(), []
    for p in out:
        r = p.resolve()
        if r not in seen and r.is_dir():
            seen.add(r)
            uniq.append(r)
    return uniq


class LitIndex:
    """Map `vid-<id>` to its transcript across all registered lit folders.

    When one video has more than one file, the canonical one is the file with
    the newest `asr_source`, else the newest `extracted_at` (contract 13).
    """

    def __init__(self, dirs: list[Path]):
        self.dirs = [Path(d) for d in dirs]
        found: dict[str, list[Path]] = {}
        for d in self.dirs:
            index = d.parent / "transcripts.json"
            if index.is_file():
                # Package A: items[<id>].lit_note (relative to the staging dir) is canonical;
                # files in lit/_superseded/ are history and never quoted.
                try:
                    items = json.loads(index.read_text(encoding="utf-8")).get("items", {})
                except ValueError:
                    items = {}
                for vid, it in items.items():
                    rel = (it or {}).get("lit_note")
                    if rel and (d.parent / rel).is_file() and "_superseded" not in Path(rel).parts:
                        found.setdefault(vid, []).append(d.parent / rel)
                continue
            if d.is_dir():
                for p in sorted(d.glob("*.md")):  # top level only: lit/_superseded/ is skipped
                    found.setdefault(p.stem, []).append(p)
        self.paths = {vid: (ps[0] if len(ps) == 1 else self._canonical(ps)) for vid, ps in found.items()}
        self._cache: dict[str, Transcript] = {}

    @staticmethod
    def _canonical(paths: list[Path]) -> Path:
        """Without transcripts.json: a file marked `canonical: false` loses; then the
        newest `extracted_at`; then the newest file modification time."""
        def key(p: Path) -> tuple[int, str, float]:
            fm, _, _ = split_frontmatter(p.read_text(encoding="utf-8", errors="replace")[:4000])
            canon = 0 if str(fm.get("canonical")).lower() == "false" else 1
            return (canon, str(fm.get("extracted_at") or ""), p.stat().st_mtime)
        return max(paths, key=key)

    def get(self, vid: str) -> Transcript | None:
        if vid in self._cache:
            return self._cache[vid]
        p = self.paths.get(vid)
        if p is None:
            return None
        tr = load_transcript(p)
        # pilot 8 (round 21): names the owner mapped with `respeak --map` for this video
        # (`<staging>/speakers_resolved.json`) count as speakers of the video
        for d in self.dirs:
            reg = d.parent / "speakers_resolved.json"
            if reg.is_file() and tr is not None:
                try:
                    names = (json.loads(reg.read_text(encoding="utf-8")).get(vid) or {}).get("names") or []
                except ValueError:
                    names = []
                if names:
                    tr.meta = dict(tr.meta, speakers=list(dict.fromkeys(list(tr.speakers or []) + [str(x) for x in names])))
        self._cache[vid] = tr
        return tr


# --------------------------------------------------------------------------- #
# Note parsing
# --------------------------------------------------------------------------- #


@dataclass
class Section:
    name: str
    lines: list[str]

    def bullets(self) -> list[str]:
        items: list[str] = []
        for ln in self.lines:
            if re.match(r"^[-*]\s+\S", ln):
                items.append(ln[1:].strip())
            elif items and ln.startswith((" ", "\t")) and ln.strip():
                items[-1] += " " + ln.strip()
        return items

    def text(self) -> str:
        return "\n".join(self.lines).strip()


@dataclass
class Body:
    h1: str
    lead: str
    lead_paragraphs: int
    sections: dict[str, Section]
    section_order: list[str]
    extra_h1: int


def parse_body(body: str) -> tuple[str, str, dict[str, Section]]:
    b = parse_body_full(body)
    return b.h1, b.lead, b.sections


def parse_body_full(body: str) -> Body:
    h1, sections, order = "", {}, []
    current: Section | None = None
    in_fence = False
    paragraphs: list[list[str]] = []
    extra_h1 = 0
    for ln in body.splitlines():
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and ln.startswith("# "):
            if not h1:
                h1 = ln[2:].strip()
            else:
                extra_h1 += 1
            continue
        if not in_fence and ln.startswith("## "):
            current = Section(ln[3:].strip(), [])
            sections.setdefault(current.name, current)
            order.append(current.name)
            continue
        if current is None:
            if not h1:
                continue
            if ln.strip():
                if not paragraphs or paragraphs[-1] is None:  # type: ignore[comparison-overlap]
                    paragraphs.append([])
                paragraphs[-1].append(ln.strip())
            elif paragraphs and paragraphs[-1] is not None:
                paragraphs.append(None)  # type: ignore[arg-type]
        else:
            current.lines.append(ln)
    paras = [p for p in paragraphs if p]
    lead = " ".join(paras[0]) if paras else ""
    return Body(h1, lead, len(paras), sections, order, extra_h1)


# --------------------------------------------------------------------------- #
# Word-overlap support (heuristic)
# --------------------------------------------------------------------------- #

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has
have having he her here hers herself him himself his how i if in into is it its itself just me more
most my myself no nor not of off on once only or other our ours out over own same she should so some
such than that the their theirs them themselves then there these they this those through to too under
until up very was we were what when where which while who whom why will with would you your yours
says said say speaker speakers note notes video he's she's they're it's one two three four five six
seven eight nine ten per week weeks session sessions day days month set sets rep reps second seconds
minute minutes time times his own trial result conclusion recommends recommend advises advise reports
report allows allow predicts predict requires require needs need tells explains mentions according
transcript quote quotes passage line clear interview channel calls call opinion
""".split())


def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("ings", "ing", "ies", "ied", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _content_words(text: str, names: set[str]) -> list[str]:
    t = SRC_TAG.sub(" ", text.translate(QUOTE_CHARS).lower())
    t = WIKILINK.sub(" ", t).replace("'", "")
    words = re.findall(r"[a-z]+", t)
    names_st = {_stem(n) for n in names}
    return [_stem(w) for w in words
            if len(w) >= 4 and w not in STOPWORDS and w not in names and _stem(w) not in names_st]


def support_ratio(text: str, quotes: list[str], names: set[str]) -> tuple[float, list[str]]:
    words = _content_words(text, names)
    if len(words) < SUPPORT_MIN_WORDS:
        return 1.0, []
    qwords = {_stem(w) for q in quotes for w in re.findall(r"[a-z]+", q.translate(QUOTE_CHARS).lower().replace("'", ""))}
    missing = [w for w in words if w not in qwords]
    return 1 - len(missing) / len(words), missing


QUOTE_HEDGES = ("maybe", "probably", "usually", "most of the time", "for most", "i think", "in my opinion",
                "let's say", "lets say", "for example", "kind of", "around", "roughly")
CLAIM_HEDGES = re.compile(
    r"\b(maybe|perhaps|possibly|probably|likely|usually|typically|generally|often|sometimes|mostly|most|"
    r"think|thinks|believes?|opinion|say|for example|e\.g\.|kind of|around|roughly|about|approximately|"
    r"can|could|may|might|option|suggests?|tends?|in general|for instance|imagine)\b", re.I)


EXAMPLE_HEDGES = ("let's say", "lets say", "for example")


def dropped_hedge(claim: str, title: str, quotes: list[str], numbers: list[tuple[float, ...]],
                  example_marked: bool = False) -> str | None:
    """The hedge of the quote that carries the claim (its number, else the most shared
    content words), when neither the claim nor the title has any hedge word."""
    if not quotes or CLAIM_HEDGES.search(claim) or CLAIM_HEDGES.search(title):
        return None
    cw = set(_content_words(claim, set()))
    best, best_score = None, 0
    for q in quotes:
        qv = {v for x in extract_quantities(q) for v in x.values}
        score = (10 if any(set(n) & qv for n in numbers) else 0)
        score += len(cw & {_stem(w) for w in re.findall(r"[a-z]+", q.lower().replace("'", ""))})
        if score > best_score:
            best, best_score = q, score
    if best is None or best_score < 2:
        return None
    low = " " + re.sub(r"\s+", " ", best.translate(QUOTE_CHARS).lower()) + " "
    for h in QUOTE_HEDGES:
        if example_marked and h in EXAMPLE_HEDGES:
            continue  # the note already marks the content as an example (round 8)
        m = re.search(r"(?<![a-z])" + re.escape(h) + r"(?![a-z])", low)
        if not m:
            continue
        if h in EXAMPLE_HEDGES and not _hedge_clause_holds_claim(low, m.start(), m.end(), cw, numbers):
            continue  # round 9: "let's say" opens another clause than the claim's words
        return h
    return None


CLAUSE_SPLIT = re.compile(r"[,.;:!?]| but | and | so | because | then | if | when ")


def _hedge_clause_holds_claim(low: str, a: int, b: int, claim_words: set[str],
                              numbers: list[tuple[float, ...]]) -> bool:
    """The clause of the quote that holds the hedge (from the hedge to the next clause
    break) carries a number of the claim or two of its content words."""
    rest = low[b:]
    m = CLAUSE_SPLIT.search(rest)
    clause = low[a:b + (m.start() if m else len(rest))]
    vals = {v for x in extract_quantities(clause) for v in x.values}
    if any(set(n) & vals for n in numbers):
        return True
    words = {_stem(w) for w in re.findall(r"[a-z]+", clause)}
    return len(words & claim_words) >= 2


def _stems_of(texts: list[str]) -> set[str]:
    return {_stem(w) for q in texts for w in re.findall(r"[a-z]+", q.translate(QUOTE_CHARS).lower().replace("'", ""))}


def _word_in(w: str, stems: set[str]) -> bool:
    st = _stem(w)
    return st in stems or (len(st) >= 5 and any(q.startswith(st[:5]) for q in stems))


def title_drops_frame(tq: Quantity, pool: list[Quantity]) -> str | None:
    """A title quantity whose match in the lead or a quote has a frame quantity
    ("at four times per week") or a limit word that the title does not have."""
    for lq in pool:
        if lq.values != tq.values:
            continue
        if lq.ref and not tq.ref:
            return f"the frame {[_fmt_num(v) for v in lq.ref]}"
        lost = set(lq.quals) - set(tq.quals)
        if "upper" in tq.quals:
            lost.discard("only")
        if lost:
            return f"the limit word(s) {sorted(lost)}"
    return None


def scope_not_in_quote(scope: dict[str, Any], quotes: list[str], windows: list[str] | None = None) -> list[tuple[str, str]]:
    """`skill`, `level`, `equipment` values (other than `not stated`) that are not words of
    the cited passages (contract hard rule 8). A part (split at commas and semicolons)
    passes when one of its content words is in an Evidence quote, or when ALL of its
    content words are in the quote's sentence +-1 sentence (`windows`)."""
    qstems = _stems_of(quotes)
    wstems = _stems_of(windows or [])
    out = []
    for key in ("skill", "level", "equipment"):
        val = scope.get(key)
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            sv = str(v or "").strip()
            if not sv or sv.lower() == NOT_STATED:
                continue
            for part in re.split(r"[;,]", sv):
                words = [w for w in re.findall(r"[a-z]+", part.lower().replace("'", "")) if len(w) >= 3
                         and w not in STOPWORDS]
                if words and not any(_word_in(w, qstems) for w in words) \
                        and not all(_word_in(w, wstems) for w in words):
                    out.append((key, part.strip()))
    return out


# --------------------------------------------------------------------------- #
# Verification of one note
# --------------------------------------------------------------------------- #


@dataclass
class Options:
    marker_tolerance: int = 1  # contract section 11: one marker each way
    strict_number_form: bool = True  # digits stay digits, words stay words
    repair: bool = False  # accept stubs and `verification: unsupported`
    title_number_form: bool = True  # False: repair in place keeps the old file name
    check_support: bool = True


def _fail(failures: list[dict], ftype: str, where: str, detail: str) -> None:
    failures.append({"type": ftype, "where": where, "detail": detail})


def _warn(warnings: list[dict], wtype: str, where: str, detail: str) -> None:
    warnings.append({"type": wtype, "where": where, "detail": detail})


def _source_ids(fm: dict[str, Any]) -> list[str]:
    out = []
    for s in fm.get("sources") or []:
        if isinstance(s, dict) and s.get("id"):
            out.append(str(s["id"]))
        elif isinstance(s, str) and s.startswith("vid-"):
            out.append(s)
    return out


def is_contract_note(fm: dict[str, Any]) -> bool:
    return isinstance(fm.get("sources"), list) and bool(fm.get("sources"))


def is_stub(fm: dict[str, Any]) -> bool:
    return bool(fm.get("superseded_by")) or str(fm.get("status") or "") == "superseded"


OWNER_SPLIT = re.compile(r"\s*[\u23ae|\u2022\u2013\u2014]\s*")


def _name_key(name: str) -> str:
    """The person's name before a channel separator, case-folded (package A's
    channel_owner rule): "Radoslav Radev ⎮ Calisthenics Mastery" -> "radoslav radev"."""
    return " ".join(OWNER_SPLIT.split(name.strip())[0].split()).casefold()


def _name_tokens(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", name.lower()) if len(w) >= 3}


def _bad_tag_times(s: str) -> list[str]:
    return [m.group(2) for m in SRC_TAG.finditer(s) if not ts_format_ok(m.group(2))]


def _tags_of(s: str) -> list[tuple[str, int | None]]:
    return [(m.group(1), ts_to_seconds(m.group(2))) for m in SRC_TAG.finditer(s)]


class _NoteCheck:
    """State for one note: transcripts, Evidence pools, failures, warnings."""

    def __init__(self, path: Path, text: str, lit: LitIndex, opts: Options):
        self.path, self.lit, self.opts = path, lit, opts
        self.fm, body, self.has_fm = split_frontmatter(text)
        self.body = parse_body_full(body)
        self.failures: list[dict] = []
        self.warnings: list[dict] = []
        self.speaker_cues: dict[str, str] = {}
        self.sources = {str(s.get("id")): s for s in self.fm.get("sources") or [] if isinstance(s, dict)}
        self.transcripts: dict[str, Transcript] = {}
        self.evidence: list[dict] = []
        self.missing_transcripts: set[str] = set()
        names = set()
        for s in self.sources.values():
            names |= _name_tokens(str(s.get("speaker") or "")) | _name_tokens(str(s.get("channel") or ""))
        self.names = names

    def fail(self, t: str, where: str, detail: str) -> None:
        _fail(self.failures, t, where, detail)

    def warn(self, t: str, where: str, detail: str) -> None:
        _warn(self.warnings, t, where, detail)

    # -- transcripts and speakers --------------------------------------------
    def transcript(self, vid: str, where: str) -> Transcript | None:
        if vid in self.transcripts:
            return self.transcripts[vid]
        tr = self.lit.get(vid)
        if tr is None:
            if vid not in self.missing_transcripts:
                self.missing_transcripts.add(vid)
                self.fail("transcript-missing", where, f"no lit file for {vid} in {len(self.lit.dirs)} lit folder(s)")
            return None
        self.transcripts[vid] = tr
        if tr.asr_quality == "degraded":
            self.fail("degraded-source", where, f"{vid} has asr_quality: degraded; no note may cite it")
        elif tr.asr_quality == "unknown":
            self.warn("asr-quality-unknown", where, f"{vid}: asr_quality is unknown or missing")
        if tr.span_unit_unknown:
            self.warn("span-unit-unknown", where,
                      f"{vid} has damaged spans without asr_span_unit: span unit unknown - re-ingest "
                      "(ingest_transcripts.py --upgrade-frontmatter); the spans are not checked")
        if tr.speakers_derived:
            self.warn("transcript-speakers-derived", where,
                      f"{vid} has no 'speakers' list; author/channel {tr.speakers} used. Re-ingest this transcript.")
        return tr

    def note_speakers(self) -> set[str]:
        return {str(s.get("speaker") or "") for s in self.sources.values()} - {"", NOT_STATED}

    def speaker_ok(self, tr: Transcript, speaker: str, where: str) -> bool:
        if speaker == NOT_STATED:
            return True
        unresolved = str(self.fm.get("speaker_status") or "").strip() == "unresolved"
        if speaker.strip().lower() == "unknown":
            # B round 13: an unknown voice with its SPEAKERS label in a several-voice video
            several = str(tr.meta.get("multi_speaker")).strip().lower() != "false" or len(tr.speakers or []) > 1
            if several and str(self.fm.get("speaker_label") or "").strip():
                self.warn("speaker-unknown", where, f"{tr.vid}: speaker unknown (label {self.fm.get('speaker_label')})")
                return True
            self.fail("speaker-unknown", where, "`unknown` needs speaker_label in a video with several voices; a "
                      "one-voice video names its speaker from the lit data")
            return False
        if speaker.startswith("unresolved speaker"):
            # Round 15: a video with several voices that the lit data does not name
            if not unresolved:
                self.fail("speaker-unresolved", where, "an `unresolved speaker, ...` value needs speaker_status: unresolved")
                return False
            return True
        if unresolved:
            self.fail("speaker-unresolved-named", where,
                      f"'{speaker}': the speakers of {tr.vid} are unresolved; no person's name until the owner "
                      "maps them (respeak)")
            return False
        spk = tr.speakers
        if spk is None:
            self.fail("transcript-metadata-missing", where, f"{tr.vid} has no speakers, author or channel")
            return False
        if speaker in spk or _name_key(speaker) in {_name_key(x) for x in spk}:
            return True
        # Contract section 3: a speaker outside the list needs `speaker_evidence`
        # that is in this transcript and names the speaker.
        ev = str(self.fm.get("speaker_evidence") or "")
        if ev:
            parts = strict_quote_parts(ev)
            in_tr = bool(parts) and all(p in tr.strict for p in parts)
            if in_tr:
                self.warn("speaker-from-evidence", where,
                          f"'{speaker}' is not in {tr.vid} speakers {spk}; accepted by speaker_evidence")
                return True
            self.fail("wrong-speaker", where,
                      f"'{speaker}' not in {tr.vid} speakers {spk}; speaker_evidence is not in the transcript")
            return False
        self.fail("wrong-speaker", where, f"'{speaker}' is not in {tr.vid} speakers {spk} and there is no speaker_evidence")
        return False

    # -- quotes ---------------------------------------------------------------
    def check_quote(self, tr: Transcript, quote: str, ts: int | None, ts_raw: str | None, where: str) -> dict:
        """Strict quote rule. Return {ok, marker, verdicts}."""
        res: dict[str, Any] = {"ok": False, "marker": None, "verdicts": []}
        if not ts_format_ok(ts_raw):
            self.fail("timestamp-format", where, f"'@ {ts_raw}': use MM:SS below one hour and H:MM:SS above")
            res["verdicts"].append("timestamp-format")
        self._check_brackets(tr, quote, where, res)
        parts = strict_quote_parts(quote)
        words = sum(len(p.split()) for p in parts)
        if words > MAX_QUOTE_WORDS:
            self.fail("quote-too-long", where, f"quote has {words} words (max {MAX_QUOTE_WORDS})")
            res["verdicts"].append("quote-too-long")
        if not parts:
            self.fail("quote-mismatch", where, "quote is empty")
            res["verdicts"].append("quote-mismatch")
            return res
        cited = tr.marker_index_for_time(ts) if (ts is not None and tr.has_ts) else None
        # Try every start of the first part; the rest must follow within MAX_PART_GAP.
        first = parts[0]
        starts_found = []
        pos = tr.strict.find(first)
        best_fail = None
        while pos != -1:
            prev_end, ok, chain = pos + len(first), True, [pos]
            for p in parts[1:]:
                nxt = tr.strict.find(p, prev_end)
                if nxt == -1 or nxt - prev_end > MAX_PART_GAP:
                    ok = False
                    best_fail = ("quote-gap" if nxt != -1 else "quote-mismatch", p, nxt - prev_end if nxt != -1 else None)
                    break
                chain.append(nxt)
                prev_end = nxt + len(p)
            if ok:
                starts_found.append(chain)
            pos = tr.strict.find(first, pos + 1)
        if not starts_found:
            if best_fail and best_fail[0] == "quote-gap":
                self.fail("quote-mismatch", where,
                          f"'...' joins two passages {best_fail[2]} characters apart (max {MAX_PART_GAP})")
            else:
                bad = best_fail[1] if best_fail else next((p for p in parts if p not in tr.strict), first)
                self.fail("quote-mismatch", where, _mismatch_detail(tr, bad))
            res["verdicts"].append("quote-mismatch")
            return res
        res["ok"] = True
        for chain in starts_found[:1]:
            for i in range(len(parts) - 1):
                gap = tr.strict[chain[i] + len(parts[i]):chain[i + 1]]
                cue = ellipsis_cue(gap, tr.speakers or [], self.note_speakers())
                if cue:
                    self.fail("ellipsis-hides-attribution", where,
                              f"the '...' skips '{gap.strip()[:160]}', which holds '{cue}' (an attribution or a "
                              "negation); quote that part, or split the quote")
                    res["verdicts"].append("ellipsis-hides-attribution")
                    res["ok"] = False
                    break
            for spos, part in zip(chain, parts):
                span = tr.damaged_at(spos, part)
                if span:
                    self.fail("quote-in-damaged-span", where,
                              f"{tr.vid} has asr_quality {tr.asr_quality}; the quote lies in the damaged span {span}")
                    res["verdicts"].append("quote-in-damaged-span")
                    res["ok"] = False
                    break
        if tr.has_ts:
            markers = [[tr.marker_index_at(s) for s in chain] for chain in starts_found]
            if ts is None:
                self.fail("missing-timestamp", where, f"{tr.vid} has timestamps; the item has none")
                res["verdicts"].append("missing-timestamp")
                res["marker"] = markers[0][0]
            else:
                near = [m for m in markers if all(abs(x - cited) <= self.opts.marker_tolerance for x in m)]
                if not near:
                    # Round 11 (pilot 5): a quote across several segments may cite any marker
                    # inside its span (the start of its last segment is a common choice).
                    tol = self.opts.marker_tolerance
                    for chain in starts_found:
                        lo = tr.marker_index_at(chain[0])
                        hi = tr.marker_index_at(chain[-1] + len(parts[-1]) - 1)
                        t_lo = tr.marker_time[lo] if lo >= 0 else 0
                        t_hi = tr.marker_time[hi] if hi >= 0 else 0
                        t_cited = tr.marker_time[cited] if 0 <= cited < len(tr.marker_time) else None
                        within = t_cited is not None and t_lo - QUOTE_TIME_TOLERANCE_S <= t_cited <= \
                            t_hi + QUOTE_TIME_TOLERANCE_S
                        if (lo - tol <= cited <= hi + tol and hi - lo <= 6) or within:
                            near = [[tr.marker_index_at(s) for s in chain]]
                            break
                if near:
                    res["marker"] = near[0][0]
                else:
                    res["marker"] = markers[0][0]
                    t0 = tr.marker_time[markers[0][0]] if markers[0][0] >= 0 else None
                    self.fail("timestamp-far", where,
                              f"cited @{ts_raw}; the quote (all parts) is not within "
                              f"{self.opts.marker_tolerance} marker(s); it starts after {_fmt_ts(t0)}")
                    res["verdicts"].append("timestamp-far")
        elif ts is not None:
            self.warn("timestamp-unverifiable", where, f"{tr.vid} has no timestamp markers")
        return res

    def _check_brackets(self, tr: Transcript, quote: str, where: str, res: dict) -> None:
        q = quote.translate(QUOTE_CHARS)
        for m in re.finditer(r"\[([^\]]*)\]", q):
            content = m.group(1).strip()
            before = q[: m.start()]
            word_before = re.search(r"([A-Za-z][A-Za-z'-]*)\s?$", before)
            reason = None
            if not word_before:
                reason = "a bracket must follow one word"
            elif re.search(r"\d", content) or any(w in ONES or w in TENS for w in content.lower().split()):
                reason = "a number inside a bracket"
            else:
                low = content.lower()
                single = re.fullmatch(r"[a-z][a-z'-]*", low) is not None
                in_vocab = low in VOCABULARY
                in_tr = single and _stem(low) in tr.word_set
                if not (in_vocab or in_tr):
                    reason = f"'{content}' is not a vocabulary term or a word of this transcript"
            if reason:
                self.fail("invented-correction", where, f"[{content}]: {reason}")
                res["verdicts"].append("invented-correction")


def _mismatch_detail(tr: Transcript, part: str) -> str:
    toks = normalize_for_match(part)
    if tr.find_tolerant(toks):
        hint = "; it matches only when case, punctuation and number words are ignored. Copy the exact text"
    else:
        lo, hi = 0, len(part)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if part[:mid] in tr.strict:
                lo = mid
            else:
                hi = mid - 1
        hint = f"; the first {lo} of {len(part)} characters match"
    return f"part not in {tr.vid}: '{part[:80]}'{hint}"


def _fmt_ts(sec: int | None) -> str:
    if sec is None:
        return "start"
    if sec >= 3600:
        return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _parse_quote(raw: str) -> str | None:
    """Text between the first and the last double quote (straight or curly), kept exactly."""
    s = raw.strip()
    starts = [i for i in (s.find('"'), s.find("“")) if i != -1]
    ends = [i for i in (s.rfind('"'), s.rfind("”")) if i != -1]
    if not starts or not ends:
        return None
    a, b = min(starts), max(ends)
    if b <= a:
        return None
    return s[a + 1 : b]


def verify_note(path: Path, lit: LitIndex, opts: Options | None = None,
                legacy_sources: list[str] | None = None, text: str | None = None) -> dict[str, Any]:
    opts = opts or Options()
    text = text if text is not None else path.read_text(encoding="utf-8", errors="replace")
    nc = _NoteCheck(path, text, lit, opts)
    fm, b = nc.fm, nc.body
    title = b.h1 or path.stem
    report: dict[str, Any] = {
        "note": str(path),
        "title": title,
        "contract": is_contract_note(fm),
        "verification_before": fm.get("verification"),
        "failures": nc.failures,
        "warnings": nc.warnings,
        "speaker_cues": nc.speaker_cues,
    }
    for key in duplicate_keys(text):
        nc.fail("frontmatter", key, f"the frontmatter key '{key}' occurs more than once (only one value counts)")
    if is_stub(fm):
        return _stub_report(report, nc, path, opts)
    if str(fm.get("verification") or "") == "unsupported":
        return _unsupported_report(report, nc, opts)
    if not report["contract"]:
        return _legacy_report(report, fm, nc.has_fm, title, b.lead, b.sections, lit, legacy_sources)

    _check_frontmatter(fm, nc)
    _check_structure(nc, path)

    _body0 = split_frontmatter(text)[1]
    _m0 = re.search(r"(?m)^# ", _body0)
    _pre = _body0[:_m0.start()] if _m0 else ""
    if _pre.strip() and is_contract_note(fm):  # V17: nothing unreviewed before the H1
        nc.fail("text-before-h1", "body", f"text before the H1: {_pre.strip()[:80]!r}")
    vague = VAGUE_SPEAKER.search(b.h1 or "") and str(fm.get("speaker_status") or "") != "unresolved"
    # (d)(f)(e) transcripts of all sources
    for sid, s in nc.sources.items():
        tr = nc.transcript(sid, f"sources:{sid}")
        if tr is not None and vague and str(tr.meta.get("multi_speaker")).strip().lower() == "false" \
                and len(tr.speakers or []) == 1:
            vague = False
            nc.fail("vague-speaker", "title", f"'{(b.h1 or '')[:80]}': name the speaker of this one-speaker video "
                                              f"from the lit data ({(tr.speakers or [''])[0]})")
        if tr is not None:
            nc.speaker_ok(tr, str(s.get("speaker") or ""), f"sources:{sid}")

    # (b) Evidence
    nc.evidence = _check_evidence(nc)
    report["evidence"] = [{k: v for k, v in e.items() if not k.startswith("_")} for e in nc.evidence]

    # (d) tags, (c) numbers, (g) support for each tagged claim text
    def pool_entries(tags: list[tuple[str, int | None]]) -> list[dict]:
        out = []
        for vid, ts in tags:
            tr = nc.transcripts.get(vid)
            for e in nc.evidence:
                if e["vid"] != vid or not e["quote_ok"]:
                    continue
                if ts is not None and tr is not None and tr.has_ts and e["marker"] is not None:
                    if abs(e["marker"] - tr.marker_index_for_time(ts)) > opts.marker_tolerance:
                        continue
                out.append(e)
        return out

    def check_tags(where: str, tags: list[tuple[str, int | None]]) -> None:
        for vid, ts in tags:
            if vid not in nc.sources:
                nc.fail("unknown-source", where, f"tag cites {vid}, which is not in 'sources'")
                continue
            tr = nc.transcripts.get(vid)
            if tr is None:
                continue
            if tr.has_ts and ts is None:
                nc.fail("missing-timestamp", where, f"transcript {vid} has timestamps; the tag has none")
            if not pool_entries([(vid, ts)]):
                nc.fail("no-evidence-for-tag", where,
                        f"no matching Evidence item for {vid}" + (f" within {opts.marker_tolerance} marker(s)" if ts is not None else ""))

    all_pool = [q for e in nc.evidence if e["quote_ok"] for q in e["_quantities"]]

    scope_fm = fm.get("scope") if isinstance(fm.get("scope"), dict) else {}
    example_marked = str(scope_fm.get("modality") or "").strip().lower() == "observation" \
        or "Example From The Source" in b.sections

    def check_claim(where: str, text_: str, tags: list[tuple[str, int | None]] | None,
                    need_tag: bool = True, support: bool = True, whole_video: bool = False) -> dict:
        before = len(nc.failures)
        for bad in _bad_tag_times(text_):
            nc.fail("timestamp-format", where, f"'@ {bad}': use MM:SS below one hour and H:MM:SS above")
        if tags is not None:
            if need_tag and not tags:
                nc.fail("missing-source-tag", where, "no [src: vid-<id> @ MM:SS] tag")
            check_tags(where, tags)
            entries = pool_entries([(v, None) for v, _ in tags] if whole_video else tags)
            pool = [q for e in entries for q in e["_quantities"]]
        else:
            entries = [e for e in nc.evidence if e["quote_ok"]]
            pool = all_pool
        nums = []
        for q in extract_quantities(text_):
            verdict, matched = match_quantity(q, pool, opts.strict_number_form)
            nums.append({"quantity": q.as_dict(), "verdict": verdict, "matched": matched})
            if verdict != "supported":
                hint = (" In repair mode a title that must change needs a new note plus a stub (move_file)."
                        if verdict == "changed-number-form" and where == "title" and opts.repair else "")
                nc.fail(verdict, where, f"'{q.text}' {q.as_dict()} vs quote {matched}.{hint}")
            elif matched is not None:
                added = set(q.quals) - set(matched.get("qualifiers") or [])
                if "upper" in matched.get("qualifiers", []):
                    added.discard("only")
                if added:
                    # Not a failure (round 3 decision); a hint for the source-check review.
                    nc.warn("added-qualifier", where,
                            f"'{q.text}': the claim adds {sorted(added)} next to the number; the quote has "
                            f"{matched.get('qualifiers') or 'no limit word'}")
        # Word support: all quotes of the tagged videos (paraphrase across one passage
        # often uses words of the neighboring passage).
        vids = {v for v, _ in (tags or [])}
        sup = [e for e in nc.evidence if e["quote_ok"] and e["vid"] in vids]
        if where == "title" or where == "lead" or where.startswith("Details"):
            hedge = dropped_hedge(text_, title, [e["quote"] for e in (sup or entries)],
                                  [q.values for q in extract_quantities(text_)], example_marked)
            if hedge:
                # A hint for the source-check review, never a failure on its own.
                nc.warn("dropped-hedge", where, f"the quote says '{hedge}'; neither this item nor the title has a hedge word")
        if support and opts.check_support and tags and sup:
            ratio, missing = support_ratio(text_, [e["quote"] for e in sup], nc.names)
            if ratio < SUPPORT_MIN:
                # A hint for the LLM source-check review, never a failure on its own.
                nc.warn("low-quote-support", where,
                        f"only {ratio:.0%} of the content words occur in the cited quotes; missing {missing[:8]}")
        return {"text": text_, "tags": [{"vid": v, "ts": t} for v, t in (tags or [])], "numbers": nums,
                "verdict": "supported" if len(nc.failures) == before else "failed",
                "failures": nc.failures[before:]}

    # Title: H1, file name and frontmatter title; numbers against all quotes.
    before_title = len(nc.failures)
    report["title_check"] = check_claim("title", title, None)
    if not opts.title_number_form:
        # Repair in place keeps the old file name; its spoken number form is not the
        # new note's. A title that must change uses a new note plus a stub.
        for f in list(nc.failures[before_title:]):
            if f["type"] == "changed-number-form":
                nc.failures.remove(f)
                nc.warn("changed-number-form", "title", f["detail"] + " (old file name kept in repair; "
                        "to change the title, write a new note and leave a stub with move_file)")
    fm_title = fm.get("title")
    if fm_title and str(fm_title) != title:
        report["fm_title_check"] = check_claim("frontmatter title", str(fm_title), None)

    lead_tags = _tags_of(b.lead)
    if not b.lead:
        nc.fail("missing-lead", "lead", "no lead sentence after the H1")
    # The lead sums up the note; the word check runs on Details, Example and Why only.
    # The lead sums up the note: its numbers may come from any quote of the tagged videos.
    report["lead_check"] = check_claim("lead", b.lead, lead_tags, support=False, whole_video=True)

    details = b.sections.get("Details")
    report["details"] = []
    if details is not None:
        bl = details.bullets()
        if not bl:
            nc.fail("missing-section", "Details", "'## Details' has no bullets")
        speakers_in_details: set[str] = set()
        for n, bullet in enumerate(bl, 1):
            tags = _tags_of(bullet)
            if tags and not re.search(r"\[src:[^\]]*\]\s*[.;]?\s*$", bullet):
                nc.warn("tag-not-at-end", f"Details[{n}]", "the source tag is not at the end of the bullet")
            for vid, _ in tags:
                s = nc.sources.get(vid)
                if s:
                    speakers_in_details.add(str(s.get("speaker") or NOT_STATED))
            entry = check_claim(f"Details[{n}]", bullet, tags)
            entry["n"] = n
            report["details"].append(entry)
        if len(speakers_in_details) > 1:
            nc.fail("mixed-speakers", "Details",
                    f"Details cite more than one speaker {sorted(speakers_in_details)} "
                    "('not stated' counts as another speaker); use a separate note and '## Disagreement'")

    for name in ("Example From The Source", "Why This Matters"):
        sec = b.sections.get(name)
        if sec is None:
            continue
        if not sec.text():
            nc.fail("missing-section", name, f"'## {name}' is empty; omit the section")
            continue
        report.setdefault("sections", {})[name] = check_claim(name, sec.text(), _tags_of(sec.text()))

    _check_disagreement(nc)
    scope = fm.get("scope") if isinstance(fm.get("scope"), dict) else {}
    if "quantities" in scope and not scope.get("quantities"):
        with_numbers = [d["n"] for d in report["details"] if counted_quantities(d["text"])]
        if with_numbers:
            nc.fail("quantities-missing", "scope.quantities",
                    f"scope.quantities is empty, but Details {with_numbers} hold numbers; list each quantity with its period")
    _check_scope_quantities(nc, all_pool)
    quotes_ok = [e["quote"] for e in nc.evidence if e["quote_ok"]]
    windows = [_quote_window(nc, e) for e in nc.evidence if e["quote_ok"]]
    if quotes_ok:
        for key, part in scope_not_in_quote(scope, quotes_ok, windows):
            nc.fail("scope-not-in-quote", f"scope.{key}",
                    f"'{part}' is not in the sentence of any Evidence quote (or the sentence before or after); "
                    "extend the Evidence quote to include the word, or write not stated")
    _check_multi_speaker(nc)
    for tq in extract_quantities(title):
        lost = title_drops_frame(tq, extract_quantities(b.lead) + all_pool)
        if lost:
            nc.warn("title-drops-frame", "title", f"'{tq.text}': the lead or the quote has {lost}; the title drops it")

    spk_all = {str(s.get("speaker")) for s in nc.sources.values() if s.get("speaker") not in (None, NOT_STATED)}
    if len(spk_all) > 1 and "Disagreement" not in b.sections:
        nc.warn("no-disagreement-section", "sources", f"sources name {sorted(spk_all)} but there is no '## Disagreement'")

    report["transcripts_missing"] = sorted(nc.missing_transcripts)
    report["status"] = "failed" if nc.failures else "quote-checked"
    return report


def _check_structure(nc: _NoteCheck, path: Path) -> None:
    b = nc.body
    if not b.h1:
        nc.fail("missing-h1", "body", "no H1 title")
    elif b.h1 != path.stem:
        nc.fail("title-mismatch", "title", f"H1 '{b.h1}' differs from the file name '{path.stem}'")
    if nc.fm.get("title") and str(nc.fm["title"]) != b.h1:
        nc.fail("title-mismatch", "title", f"frontmatter title '{nc.fm['title']}' differs from the H1")
    if b.extra_h1:
        nc.fail("unknown-section", "body", "more than one H1")
    if b.lead_paragraphs > 1:
        nc.fail("multi-paragraph-lead", "lead", f"{b.lead_paragraphs} paragraphs before the first section; the lead is one sentence")
    for name in ("Details", "Evidence"):
        if name not in b.sections:
            nc.fail("missing-section", name, f"required section '## {name}' is absent")
    for name in b.section_order:
        if name == "Grounded Example":
            nc.fail("forbidden-section", name, "'## Grounded Example' is not allowed; use '## Example From The Source'")
        elif name not in ALLOWED_SECTIONS:
            nc.fail("unknown-section", name, f"section '## {name}' is not in the contract {list(ALLOWED_SECTIONS)}")
    if len(b.section_order) != len(set(b.section_order)):
        nc.fail("unknown-section", "body", "a section heading occurs twice")


def _check_frontmatter(fm: dict[str, Any], nc: _NoteCheck) -> None:
    ntype = str(fm.get("type") or "")
    if ntype not in ("permanent note", "permanent"):
        nc.fail("frontmatter", "type", f"type is '{ntype}', expected 'permanent note'")
    st = fm.get("status")
    if st is not None and str(st) not in STATUS_VALUES:
        nc.fail("frontmatter", "status", f"status '{st}' not in {list(STATUS_VALUES)}")
    for n, s in enumerate(fm.get("sources") or []):
        if not isinstance(s, dict):
            nc.fail("frontmatter", f"sources[{n}]", "entry is not a mapping with id/url/speaker/channel")
            continue
        missing = [k for k in SOURCE_KEYS if not s.get(k)]
        if missing:
            nc.fail("frontmatter", f"sources[{n}]", f"missing keys {missing}")
        extra = sorted(set(s) - set(SOURCE_KEYS))
        if extra:
            nc.fail("frontmatter", f"sources[{n}]", f"unknown keys {extra}")
        if s.get("id") and not re.fullmatch(VID, str(s["id"])):
            nc.fail("frontmatter", f"sources[{n}]", f"id '{s['id']}' is not 'vid-<id>'")
    scope = fm.get("scope")
    if not isinstance(scope, dict):
        nc.fail("frontmatter", "scope", "missing 'scope' mapping")
    else:
        for k in SCOPE_KEYS:
            if not scope.get(k):
                nc.fail("frontmatter", f"scope.{k}", f"missing 'scope.{k}' (use 'not stated')")
        if scope.get("basis") and str(scope["basis"]) not in BASIS_VALUES:
            nc.fail("frontmatter", "scope.basis", f"'{scope['basis']}' not in {list(BASIS_VALUES)}")
        if scope.get("modality") and str(scope["modality"]) not in MODALITY_VALUES:
            nc.fail("frontmatter", "scope.modality", f"'{scope['modality']}' not in {list(MODALITY_VALUES)}")
        if "quantities" not in scope:
            nc.fail("frontmatter", "scope.quantities", "missing 'scope.quantities' (use [])")
        for n, q in enumerate(scope.get("quantities") or []):
            if not isinstance(q, dict) or "value" not in q:
                nc.fail("frontmatter", f"scope.quantities[{n}]", "entry needs 'value' and 'period'")
            elif not _allowed_quantity_period(str(q.get("period"))):
                nc.fail("frontmatter", f"scope.quantities[{n}]", f"period '{q.get('period')}' not allowed")
    ver = fm.get("verification")
    if str(ver) not in VERIFICATION_VALUES:
        nc.fail("frontmatter", "verification", f"'{ver}' not in {list(VERIFICATION_VALUES)}")


def _check_evidence(nc: _NoteCheck) -> list[dict]:
    out: list[dict] = []
    sec = nc.body.sections.get("Evidence")
    if sec is None:
        return out
    items = sec.bullets()
    if not items:
        nc.fail("missing-section", "Evidence", "'## Evidence' has no items")
    for n, raw in enumerate(items, 1):
        where = f"Evidence[{n}]"
        m = EVIDENCE_ITEM.match("- " + raw)
        entry: dict[str, Any] = {"n": n, "raw": raw, "vid": None, "ts": None, "speaker": None,
                                 "quote_ok": False, "marker": None, "verdicts": [], "_quantities": []}
        out.append(entry)
        if not m:
            nc.fail("evidence-format", where, 'expected: - vid-<id> @ MM:SS (Speaker): "verbatim quote"')
            entry["verdicts"].append("evidence-format")
            continue
        vid, ts_raw, speaker, rest = m.groups()
        entry.update(vid=vid, ts=ts_to_seconds(ts_raw), speaker=speaker.strip())
        quote = _parse_quote(rest)
        if quote is None:
            nc.fail("evidence-format", where, "quote is not in double quotes")
            entry["verdicts"].append("evidence-format")
            continue
        entry["quote"] = quote
        if vid not in nc.sources:
            nc.fail("unknown-source", where, f"{vid} is not in 'sources' (Evidence uses only sources)")
            entry["verdicts"].append("unknown-source")
        tr = nc.transcript(vid, where)
        if tr is None:
            entry["verdicts"].append("transcript-missing")
            continue
        src_spk = str((nc.sources.get(vid) or {}).get("speaker") or "")
        if src_spk and entry["speaker"] != src_spk:
            nc.fail("wrong-speaker", where, f"speaker '{entry['speaker']}' differs from sources speaker '{src_spk}'")
            entry["verdicts"].append("wrong-speaker")
        elif not nc.speaker_ok(tr, entry["speaker"], where):
            entry["verdicts"].append("wrong-speaker")
        res = nc.check_quote(tr, quote, entry["ts"], ts_raw, where)
        entry["verdicts"] += res["verdicts"]
        entry["marker"] = res["marker"]
        entry["quote_ok"] = res["ok"]
        if res["ok"]:
            entry["_quantities"] = extract_quantities(" ; ".join(strict_quote_parts(quote)))
        if not entry["verdicts"]:
            entry["verdicts"].append("quote-ok")
    return out


DISAGREE_THIS = re.compile(r"^This note:\s*(.+?)\s*(\[src:[^\]]*\])\s*:\s*(.+)$")
DISAGREE_OTHER = re.compile(r"^Other side:\s*\[\[([^\]]+)\]\]\s*\(([^)]*)\)\s*(\[src:[^\]]*\])?\s*:?\s*(.*)$")
DISAGREE_PLAIN = re.compile(r"^(.+?)\s*(\[src:[^\]]*\])\s*:\s*(.+)$")


def _check_disagreement(nc: _NoteCheck) -> None:
    sec = nc.body.sections.get("Disagreement")
    if sec is None:
        return
    items = sec.bullets()
    if not items:
        nc.fail("disagreement-format", "Disagreement", "'## Disagreement' has no items")
    for n, raw in enumerate(items, 1):
        where = f"Disagreement[{n}]"
        m_other = DISAGREE_OTHER.match(raw)
        if m_other and not m_other.group(3):
            # Old note without sources: one line, no numbers, no quote.
            if extract_quantities(m_other.group(4)) or '"' in m_other.group(4):
                nc.fail("disagreement-format", where, "an old-note line holds no number and no quote")
            if "older note without sources" not in m_other.group(2):
                nc.fail("disagreement-format", where, "a line without a source tag must say '(older note without sources)'")
            continue
        if m_other:
            speaker, tag, rest = m_other.group(2), m_other.group(3), m_other.group(4)
        else:
            m = DISAGREE_THIS.match(raw) or DISAGREE_PLAIN.match(raw)
            if not m:
                nc.fail("disagreement-format", where, 'expected: - This note: <Speaker> [src: ...]: "quote"')
                continue
            speaker, tag, rest = m.group(1), m.group(2), m.group(3)
        tm = SRC_TAG.match(tag)
        quote = _parse_quote(rest)
        if not tm or quote is None:
            nc.fail("disagreement-format", where, "needs a [src: vid-<id> @ MM:SS] tag and a quoted passage")
            continue
        vid, ts_raw = tm.group(1), tm.group(2)
        tr = nc.transcript(vid, where)
        if tr is None:
            continue
        speaker = speaker.strip().removeprefix("This note:").strip()
        nc.speaker_ok(tr, speaker, where)
        nc.check_quote(tr, quote, ts_to_seconds(ts_raw), ts_raw, where)


def _quote_window(nc: _NoteCheck, e: dict) -> str:
    """The sentence(s) that hold an Evidence quote, plus one sentence on each side
    (round 7: the "cited passage" of contract hard rule 8 for scope words)."""
    tr = nc.transcripts.get(e["vid"])
    parts = strict_quote_parts(e["quote"])
    pos = tr.strict.find(parts[0]) if tr is not None and parts else -1
    if pos == -1:
        return e["quote"]
    last = tr.strict.find(parts[-1], pos)
    end = (last + len(parts[-1])) if last != -1 else pos + len(parts[0])
    a, b = quote_sentences(tr.strict, pos, end, extra=1)
    return tr.strict[a:b]


def _passage_text(nc: _NoteCheck, e: dict) -> str:
    """The cited passage of one Evidence item: the quote with +-90 s of context (about
    1,500 characters without timestamps), as the review sees it."""
    tr = nc.transcripts.get(e["vid"])
    parts = strict_quote_parts(e["quote"])
    pos = tr.strict.find(parts[0]) if tr is not None and parts else -1
    if pos == -1:
        return e["quote"]
    if tr.has_ts and tr.marker_pos:
        mi = tr.marker_index_at(pos)
        t = tr.marker_time[mi] if mi >= 0 else 0
        lo = tr.marker_index_for_time(max(0, t - MULTI_SPEAKER_WINDOW_S))
        hi = tr.marker_index_for_time(t + MULTI_SPEAKER_WINDOW_S) + 1
        a = tr.marker_pos[lo] if lo >= 0 else 0
        b = tr.marker_pos[hi] if hi < len(tr.marker_pos) else len(tr.strict)
        return tr.strict[a:max(b, pos + len(e["quote"]))]
    return tr.strict[max(0, pos - MULTI_SPEAKER_WINDOW_CHARS):pos + len(e["quote"]) + MULTI_SPEAKER_WINDOW_CHARS]


MULTI_SPEAKER_WINDOW_S = 90
MULTI_SPEAKER_WINDOW_CHARS = 1500


ELLIPSIS_CUES = re.compile(
    r"\b(he|she|they)( \w+)?( says| said| say| told| tells| got me| taught| showed| thinks| thought)\b|"
    r"\baccording to\b|\b\w+ (told|tells) (me|us|him|her|them)\b|\b(his|her|their) idea\b|"
    r"\bwhat (he|she|they)\b|\b(not|never|don'?t|doesn'?t|didn'?t|isn'?t|wasn'?t|no longer|won'?t|can'?t)\b", re.I)


def ellipsis_cue(gap: str, transcript_speakers: list[str] | str, note_speakers: set[str]) -> str | None:
    """What a '...' must not skip: another person's name (from the transcript speakers),
    an attribution ("he says", "according to", "X told me") or a negation."""
    low = " " + gap.lower() + " "
    names = transcript_speakers if isinstance(transcript_speakers, list) else [transcript_speakers]
    mine = {p for n in note_speakers for p in re.split(r"[\s_]+", n.lower()) if len(p) >= 3}
    for n in names:
        for part in re.split(r"[\s_]+", str(n).lower()):
            part = part.strip(" @.")
            if len(part) >= 3 and part not in mine and re.search(r"\b" + re.escape(part) + r"\b", low):
                return part
    m = ELLIPSIS_CUES.search(gap)
    return m.group(0) if m else None


SPEAKER_CUE = re.compile(r"\?|\byou\b|\byour\b|\byou're\b|\bmy guest\b|\btell (me|us)\b|\bwelcome\b|"
                         r"\bthank you for\b|\bthanks for (coming|joining)\b|\bi'm here with\b|\bwith me\b", re.I)


HOST_QUESTION = re.compile(r"\?|^\W*(?:(?:so|um|uh|and|okay|ok|well)\W+)*(?:how|what|why|when|where|which|who|"
                           r"can you|could you|do you|did you|would you|is it|are you|have you)\b|"
                           r"\bmy guest\b|\btell (me|us)\b|\bwelcome\b|"
                           # round 11 (pilot 5): "so another question how often should you ..."
                           r"\b(?:another|next|my|a) question\b|^\W*(?:\w+\W+){0,5}?(?:how|what|why|which)\b[^.!]{0,60}\byou\b", re.I)


VAGUE_SPEAKER = re.compile(r"^(?:A|An|The) (?:Speaker|Voice|Person|Guest|Host|Coach) (?:In|From|Of) ", re.I)


def speaker_cue_match(evidence: str, speakers: list[str] | str) -> str:
    """Which cue shows the speaker (round 11, recorded in the report): `name:<part>`,
    `host-question:<text>`, `cue:<text>`, or ""."""
    names = speakers if isinstance(speakers, list) else [speakers]
    low = evidence.lower()
    for n in names:
        for part in re.split(r"[\s_]+|⎮|\|", str(n).lower()):
            part = part.strip(" @.")
            if len(part) >= 3 and part in low:
                return f"name:{part}"
    m = HOST_QUESTION.search(evidence)
    if m:
        return f"host-question:{m.group(0).strip()[:40]}"
    m = SPEAKER_CUE.search(evidence)
    return f"cue:{m.group(0).strip()[:40]}" if m else ""


def speaker_cue(evidence: str, speakers: list[str] | str, strict: bool = False) -> bool:
    """speaker_evidence shows who speaks: a name (or a part of a name) from `speakers`,
    or a host cue (a question, second person, "my guest", "tell me")."""
    names = speakers if isinstance(speakers, list) else [speakers]
    low = evidence.lower()
    for n in names:
        for part in re.split(r"[\s_]+|⎮|\|", str(n).lower()):
            part = part.strip(" @.")
            if len(part) >= 3 and part in low:
                return True
    return bool((HOST_QUESTION if strict else SPEAKER_CUE).search(evidence))


def speaker_evidence_is_claim(ev: str, quotes: list[str], lead: str, speakers: list[str] | str) -> bool:
    """speaker_evidence that is the claim itself: its words lie inside an Evidence quote
    and hold no speaker cue, or it repeats the lead (80 % of the lead's content words).
    Round 11: the host's QUESTION before the guest's answer is speaker evidence, never
    the claim."""
    if HOST_QUESTION.search(ev):
        return False
    et = normalize_for_match(ev)
    if len(et) >= 4 and not speaker_cue(ev, speakers, strict=True):
        es = " " + " ".join(et) + " "
        for q in quotes:
            qs = " " + " ".join(normalize_for_match(q)) + " "
            if es in qs or (qs.strip() and qs in es):
                return True
    lw = set(_content_words(lead, set()))
    ew = set(_content_words(ev, set()))
    return bool(lw) and len(lw & ew) >= 0.8 * len(lw)


def _check_multi_speaker(nc: _NoteCheck) -> None:
    """Contract 3: in a transcript with `multi_speaker: true` or "unknown", a note that
    names a speaker carries `speaker_evidence` that is in the transcript within the
    cited passage +-90 s (about 1,500 characters without timestamps)."""
    ev = str(nc.fm.get("speaker_evidence") or "").strip()
    for sid, s in nc.sources.items():
        speaker = str(s.get("speaker") or "")
        tr = nc.transcripts.get(sid)
        if tr is None or speaker in ("", NOT_STATED):
            continue
        if str(tr.meta.get("multi_speaker")).strip().lower() not in ("true", "unknown"):
            continue
        where = f"sources:{sid}"
        if not ev:
            nc.fail("speaker-evidence-missing", where,
                    f"{sid} has more than one voice (multi_speaker: {tr.meta.get('multi_speaker')}); name "
                    f"'{speaker}' only with speaker_evidence from the cited passage, else write 'not stated'")
            continue
        if speaker_evidence_is_claim(ev, [e["quote"] for e in nc.evidence if e["vid"] == sid], nc.body.lead,
                                     tr.speakers or []):
            nc.fail("speaker-evidence-is-claim", where,
                    "speaker_evidence repeats the claim (an Evidence quote or the lead); it must show WHO speaks: "
                    "the host's question before the answer, or a line that names the speaker. Else 'not stated'")
            continue
        parts = strict_quote_parts(ev)
        pos = tr.strict.find(parts[0]) if parts else -1
        if pos == -1:
            nc.fail("speaker-evidence-missing", where, "speaker_evidence is not in the transcript")
            continue
        near = False
        for e in nc.evidence:
            if e["vid"] != sid or not e["quote_ok"]:
                continue
            qparts = strict_quote_parts(e["quote"])
            qpos = tr.strict.find(qparts[0]) if qparts else -1
            if qpos == -1:
                continue
            if tr.has_ts:
                ta = tr.marker_time[tr.marker_index_at(pos)] if tr.marker_index_at(pos) >= 0 else 0
                tb = tr.marker_time[tr.marker_index_at(qpos)] if tr.marker_index_at(qpos) >= 0 else 0
                near = near or abs(ta - tb) <= MULTI_SPEAKER_WINDOW_S
            else:
                near = near or abs(pos - qpos) <= MULTI_SPEAKER_WINDOW_CHARS
        cue = speaker_cue_match(ev, tr.speakers or [])
        if cue:
            nc.speaker_cues[sid] = cue
        if not near:
            nc.fail("speaker-evidence-missing", where,
                    "speaker_evidence is not within 90 s (about 1,500 characters) of a cited passage")
        elif not speaker_cue(ev, tr.speakers or []):
            nc.warn("speaker-evidence-no-cue", where,
                    "speaker_evidence names no speaker and has no host cue (a question, 'you', 'my guest', "
                    "'tell me'); it does not show who speaks")


def _period_in_context(nc: _NoteCheck, period: str, chars: int = 400) -> bool:
    """The period phrase ("per week" -> per/a/each/every week, weekly) in the transcript
    text up to `chars` characters before an Evidence quote of the note."""
    noun = period.split()[-1] if period.startswith(("per ", "each ")) else ""
    if not noun:
        return False
    adv = {"week": "weekly", "day": "daily", "month": "monthly"}.get(noun, "")
    pat = re.compile(rf"\b(per|a|each|every|in a) {noun}\b" + (rf"|\b{adv}\b" if adv else ""), re.I)
    for e in nc.evidence:
        tr = nc.transcripts.get(e["vid"])
        parts = strict_quote_parts(e["quote"]) if tr is not None else []
        pos = tr.strict.find(parts[0]) if parts else -1
        if pos != -1 and pat.search(tr.strict[max(0, pos - chars):pos + len(e["quote"])]):
            return True
    return False


def _check_scope_quantities(nc: _NoteCheck, pool: list[Quantity]) -> None:
    scope = nc.fm.get("scope") if isinstance(nc.fm.get("scope"), dict) else {}
    for n, item in enumerate(scope.get("quantities") or []):
        if not isinstance(item, dict):
            continue
        value, period = str(item.get("value") or ""), str(item.get("period") or NOT_STATED)
        qs = extract_quantities(value)
        if not qs:
            nc.warn("scope-value-no-number", f"scope.quantities[{n}]", f"no number in value '{value}'")
            continue
        for q in qs:
            # Lenient: a value set inside a quote range counts ("three three to four times").
            cand = [p for p in pool if set(q.values) <= set(p.values) and (q.unit is None or p.unit == q.unit)]
            if period != NOT_STATED:
                loose = [p for p in cand if p.period is None]
                cand = [p for p in cand if _claim_period_matches(period, p.period)]
                if not cand and loose and _period_in_context(nc, period):
                    # Round 11 (pilot 5): the period is in the transcript sentence before the quote.
                    nc.warn("scope-period-from-context", f"scope.quantities[{n}]",
                            f"'{value}': period '{period}' is in the transcript just before the quote, not in it")
                    continue
            if not cand:
                nc.fail("scope-mismatch", f"scope.quantities[{n}]",
                        f"'{value}' with period '{period}' has no matching quantity in the Evidence quotes")
                break


def _stub_report(report: dict, nc: _NoteCheck, path: Path, opts: Options) -> dict[str, Any]:
    fm, b = nc.fm, nc.body
    report["stub"] = True
    target = str(fm.get("superseded_by") or "")
    # One target, or several for a split by speaker: "[[A]]; [[B]]".
    targets = [m.group(1).strip() for m in WIKILINK.finditer(target)]
    if not opts.repair:
        nc.fail("stub-outside-repair", "note", "a superseded stub is accepted only in repair mode")
    if not targets:
        nc.fail("stub-format", "superseded_by", "superseded_by must be a [[wikilink]], or [[A]]; [[B]] for a split")
    elif WIKILINK.sub("", target).strip(" ;") != "":
        nc.fail("stub-format", "superseded_by", "superseded_by holds only wikilinks separated by a semicolon")
    if str(fm.get("status") or "") != "superseded":
        nc.fail("stub-format", "status", "a stub has status: superseded")
    if b.sections or extract_quantities(b.lead):
        nc.fail("stub-format", "body", "a stub has only the H1 and one line with the link")
    for t in targets:
        if f"[[{t}" not in b.lead:
            nc.fail("stub-format", "body", f"the stub line must link each new note ([[{t}]] is missing)")
    if b.h1 != path.stem:
        nc.fail("title-mismatch", "title", f"H1 '{b.h1}' differs from the file name '{path.stem}'")
    report["superseded_by"] = targets[0] if len(targets) == 1 else (targets or None)
    report["status"] = "failed" if nc.failures else "stub"
    return report


def _unsupported_report(report: dict, nc: _NoteCheck, opts: Options) -> dict[str, Any]:
    marked = str(nc.fm.get("unsupported_marked") or "").strip() and str(nc.fm.get("unsupported_reason") or "").strip() in (
        "source transcript missing", "no source video states any claim of this note")
    if not opts.repair and not marked:
        # round 24: the harness mark (unsupported_marked + a known reason) is a defined state;
        # validate --staging checks that only the three keys changed
        nc.fail("unsupported-outside-repair", "verification", "verification: unsupported is set only by the repair step")
    if not str(nc.fm.get("unsupported_reason") or "").strip():
        nc.fail("frontmatter", "unsupported_reason", "verification: unsupported needs unsupported_reason")
    report["status"] = "failed" if nc.failures else "unsupported"
    return report


# --------------------------------------------------------------------------- #
# Notes that do not follow the contract (report-only)
# --------------------------------------------------------------------------- #


def _legacy_report(report: dict, fm: dict, has_fm: bool, title: str, lead: str,
                   sections: dict[str, Section], lit: LitIndex,
                   legacy_sources: list[str] | None) -> dict[str, Any]:
    missing = []
    if not has_fm:
        missing.append("frontmatter")
    for k in ("sources", "scope"):
        if not fm.get(k):
            missing.append(f"frontmatter:{k}")
    if str(fm.get("verification")) not in VERIFICATION_VALUES:
        missing.append(f"frontmatter:verification (value '{fm.get('verification')}' not allowed)")
    for name in ("Details", "Evidence"):
        if name not in sections:
            missing.append(f"section:{name}")
    if "Grounded Example" in sections:
        missing.append("forbidden-section:Grounded Example")
    bullets = sections["Details"].bullets() if "Details" in sections else []
    untagged = sum(1 for b in bullets if not SRC_TAG.search(b))
    if untagged:
        missing.append(f"source-tags:{untagged} of {len(bullets)} Details bullets")
    report["status"] = "not-contract"
    report["missing"] = missing
    for m in missing:
        _fail(report["failures"], "not-contract", "note", m)

    claim_texts = [("title", title), ("lead", lead)] + [(f"Details[{i}]", b) for i, b in enumerate(bullets, 1)]
    quantities = [(where, q) for where, t in claim_texts for q in extract_quantities(t)]
    ex = sections.get("Grounded Example")
    example_q = extract_quantities(ex.text()) if ex else []
    approx: dict[str, Any] = {
        "label": "APPROXIMATE: value-only presence in the whole source transcript "
                 "(number words = digits); no quote, unit or period check",
        "numbers": len(quantities),
        "example_numbers": len(example_q),
        "sources": legacy_sources or [],
    }
    trs = [lit.get(s) for s in (legacy_sources or [])]
    trs = [t for t in trs if t is not None]
    approx["transcripts_found"] = [t.vid for t in trs]
    if trs:
        present_vals = set().union(*(t.number_values() for t in trs))
        unit_pairs: set[tuple[str, str | None]] = set()
        for t in trs:
            for q in extract_quantities(" ".join(t.tokens)):
                for v in q.values:
                    unit_pairs.add((_fmt_num(v), q.unit))

        def present(q: Quantity) -> bool:
            return all(_fmt_num(v) in present_vals for v in q.values)

        def present_with_unit(q: Quantity) -> bool:
            if q.unit is None:
                return present(q)
            return all((_fmt_num(v), q.unit) in unit_pairs for v in q.values)

        approx["numbers_found"] = sum(1 for _, q in quantities if present(q))
        approx["numbers_found_with_unit"] = sum(1 for _, q in quantities if present_with_unit(q))
        approx["example_numbers_found"] = sum(1 for q in example_q if present(q))
        approx["not_found"] = [{"where": w, **q.as_dict()} for w, q in quantities if not present(q)]
    report["approx_numbers"] = approx
    return report


# --------------------------------------------------------------------------- #
# Writing the verification field
# --------------------------------------------------------------------------- #


def set_verification_text(text: str, value: str) -> str:
    if value not in VERIFICATION_VALUES:
        raise ValueError(f"verification value '{value}' not in {VERIFICATION_VALUES}")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head, rest = text[:end], text[end:]
            if re.search(r"(?m)^verification:.*$", head):
                head = re.sub(r"(?m)^verification:.*$", f"verification: {value}", head, count=1)
            else:
                head = head.rstrip("\n") + f"\nverification: {value}"
            return head + rest
    return f"---\nverification: {value}\n---\n" + text


def set_verification(path: Path, value: str) -> bool:
    """Set `verification:` in the frontmatter. Atomic. Return True if changed."""
    text = path.read_text(encoding="utf-8")
    new = set_verification_text(text, value)
    if new == text:
        return False
    tmp = path.with_name(f".{path.name}.verify.tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    return True


# --------------------------------------------------------------------------- #
# Legacy source maps (report-only use)
# --------------------------------------------------------------------------- #


def load_legacy_sources(prov_files: list[Path], queue_files: list[Path]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    def add(key: str, vid: str) -> None:
        if not vid.startswith("vid-"):
            return
        lst = out.setdefault(key, [])
        if vid not in lst:
            lst.append(vid)

    for p in prov_files:
        if not p.exists():
            continue
        if p.suffix == ".jsonl":
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for vid in rec.get("sources", []) or []:
                    add(str(rec.get("note", "")), str(vid))
                    add(Path(str(rec.get("note", ""))).stem, str(vid))
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for key, vids in (data.items() if isinstance(data, dict) else []):
            for vid in vids if isinstance(vids, list) else []:
                add(key, str(vid))
                add(Path(key).stem, str(vid))
    for q in queue_files:
        if not q.exists():
            continue
        try:
            items = json.loads(q.read_text(encoding="utf-8")).get("items", [])
        except ValueError:
            continue
        for it in items:
            for t in it.get("notes_emitted") or []:
                add(str(t).removesuffix(".md"), str(it.get("id", "")))
            for t in it.get("published_notes") or []:  # round 10: every source of truth
                add(str(t), str(it.get("id", "")))
                add(Path(str(t)).stem, str(it.get("id", "")))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def collect_notes(paths: list[Path]) -> list[Path]:
    notes: list[Path] = []
    for p in paths:
        if p.is_dir():
            sub = p / "01 Permanent Notes"
            base = sub if sub.is_dir() else p
            notes += sorted(
                x for x in base.rglob("*")
                if x.is_file() and x.suffix.lower() == ".md"
                and not any(part.startswith((".", "_")) for part in x.relative_to(base).parts)
            )
        elif p.suffix.lower() == ".md":
            notes.append(p)
    return notes


def summarize(reports: list[dict]) -> dict[str, Any]:
    by_status = Counter(r["status"] for r in reports)
    by_type = Counter(f["type"] for r in reports for f in r["failures"] if r["status"] != "not-contract")
    summary: dict[str, Any] = {"notes": len(reports), "by_status": dict(by_status), "failures_by_type": dict(by_type)}
    legacy = [r for r in reports if r["status"] == "not-contract"]
    if legacy:
        with_nums = [r for r in legacy if r["approx_numbers"]["numbers"]]
        known = [r for r in with_nums if r["approx_numbers"]["transcripts_found"]]
        summary["legacy"] = {
            "label": "APPROXIMATE number presence (value only, anywhere in the source transcript)",
            "notes": len(legacy),
            "notes_with_numbers": len(with_nums),
            "numbers_total": sum(r["approx_numbers"]["numbers"] for r in legacy),
            "notes_with_known_source": sum(1 for r in legacy if r["approx_numbers"]["transcripts_found"]),
            "notes_with_numbers_and_known_source": len(known),
            "numbers_in_known_source_notes": sum(r["approx_numbers"]["numbers"] for r in known),
            "numbers_found_value_only": sum(r["approx_numbers"].get("numbers_found", 0) for r in known),
            "numbers_found_value_and_unit": sum(r["approx_numbers"].get("numbers_found_with_unit", 0) for r in known),
            "notes_all_numbers_found": sum(
                1 for r in known if r["approx_numbers"].get("numbers_found") == r["approx_numbers"]["numbers"]),
            "example_numbers_in_known_source_notes": sum(r["approx_numbers"]["example_numbers"] for r in known),
            "example_numbers_found": sum(r["approx_numbers"].get("example_numbers_found", 0) for r in known),
            "missing_items": dict(Counter(re.sub(r":\d.*", "", m.split(" (")[0]) for r in legacy for m in r["missing"])),
        }
        summary["legacy"]["notes_with_a_number_not_found"] = (
            summary["legacy"]["notes_with_numbers_and_known_source"] - summary["legacy"]["notes_all_numbers_found"])
    return summary


def _agent_path(p: Path) -> Path:
    """Under the agent: a vault path (absolute or "01 Permanent Notes/X.md") means the
    unit's shadow version when there is one, else the vault file."""
    zk, sh = os.environ.get("ZK_DIR"), os.environ.get("ZR_SHADOW_DIR")
    rel = None
    if zk and p.is_absolute():
        try:
            rel = p.resolve().relative_to(Path(zk).resolve())
        except ValueError:
            rel = None
    elif not p.is_absolute() and not p.exists():
        rel = p
    if rel is None:
        return p
    for root in (sh, zk):
        if root and (Path(root) / rel).exists():
            return Path(root) / rel
    return p


def _inside(path: Path, root: Path) -> bool:
    p, r = path.resolve(), root.resolve()
    return p == r or r in p.parents


def _finalize_checks(notes: list[Path], reports: list[dict]) -> None:
    """The agent self-check runs every check that finalize runs on a shadow note:
    form and required sections (validate), orphan and `## Connected Ideas`, dead links,
    names, the contract and the quotes. A failure here is a failure at finalize."""
    shadow = os.environ.get("ZR_SHADOW_DIR")
    zk = os.environ.get("ZK_DIR")
    staging = os.environ.get("ZR_STAGING")
    if not (shadow and zk and staging):
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import run_integrity  # noqa: PLC0415 - run_integrity imports this module

    unit_dir = Path(shadow).resolve().parent
    info = run_integrity._read_json(unit_dir / "unit.json", {}) or {}
    kind = os.environ.get("ZR_UNIT_KIND") or str(info.get("kind") or "synth")
    if kind == "source-check":
        return
    ids = [x for x in os.environ.get("ZR_UNIT_IDS", "").split(",") if x]
    ck = run_integrity._Checker(Path(staging), unit_dir, Path(zk), kind, run_integrity._queue_kinds(Path(staging), ids))
    keys = {run_integrity.name_key(p.stem): p.stem for p in ck.shadow_files}
    for note, rep in zip(notes, reports):
        if not _inside(note, Path(shadow)):
            continue
        res = ck.check_file(note.resolve(), keys)
        seen = {(f["type"], f["where"]) for f in rep["failures"]}
        # link-waiting: a link to a note of another unit that is not published yet. Not a
        # failure to fix: the harness keeps the note pending until that note is published.
        waiting = [f for f in res["failures"] if f["type"] in run_integrity.WAIT_TYPES]
        extra = [f for f in res["failures"] if (f["type"], f["where"]) not in seen and f not in waiting]
        rep["warnings"] = rep.get("warnings", []) + waiting
        rep["finalize_check"] = {"ok": not extra and not [f for f in rep["failures"]], "failures": extra}
        if extra:
            rep["failures"] = rep["failures"] + extra
            rep["status"] = "failed"


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare notes with their transcripts (no LLM).", allow_abbrev=False)
    ap.add_argument("paths", nargs="+", type=Path, help="note files or folders")
    ap.add_argument("--lit", action="append", type=Path, default=[], help="lit folder (repeatable)")
    ap.add_argument("--staging", type=Path, help="staging folder: adds the transcript registry (lit_dirs)")
    ap.add_argument("--write", action="store_true", help="set verification: quote-checked|failed in each note")
    ap.add_argument("--out", type=Path, help="write the JSON report to this file")
    ap.add_argument("--marker-tolerance", type=int, default=1, help="allowed distance in timestamp markers")
    ap.add_argument("--repair", action="store_true", help="accept stubs and verification: unsupported")
    ap.add_argument("--vault-root", type=Path, help="folder that holds '01 Permanent Notes' (for source maps)")
    ap.add_argument("--provenance", action="append", type=Path, default=[], help="legacy provenance file")
    ap.add_argument("--queue", action="append", type=Path, default=[], help="legacy queue.json with notes_emitted")
    ap.add_argument("--quiet", action="store_true", help="print only the summary")
    a = ap.parse_args(argv)

    if os.environ.get("ZR_AGENT") == "1":
        if a.write:
            print("verify_claims: --write is for the harness only", file=sys.stderr)
            return 2
        shadow = os.environ.get("ZR_SHADOW_DIR")
        if a.out and not (shadow and _inside(a.out, Path(shadow))):
            print("verify_claims: under the agent, --out must be inside the shadow folder", file=sys.stderr)
            return 2
    # The registry (ZR_LIT_DIRS, <staging parent>/lit_dirs.txt) applies also when only
    # --lit is given, so the agent self-check finds transcripts of other stagings.
    staging = a.staging or (Path(os.environ["ZR_STAGING"]) if os.environ.get("ZR_STAGING") else None)
    dirs = lit_dirs(staging, a.lit)
    if not dirs:
        print("verify_claims: give at least one --lit folder or --staging", file=sys.stderr)
        return 2
    paths = [_agent_path(p) for p in a.paths] if os.environ.get("ZR_AGENT") == "1" else a.paths
    notes = collect_notes(paths)
    if not notes:
        print("verify_claims: no notes found", file=sys.stderr)
        return 2
    lit = LitIndex(dirs)
    legacy = load_legacy_sources(a.provenance, a.queue)
    opts = Options(marker_tolerance=a.marker_tolerance, repair=a.repair)
    reports = []
    for note in notes:
        key_rel = None
        if a.vault_root:
            try:
                key_rel = str(note.resolve().relative_to(a.vault_root.resolve()))
            except ValueError:
                key_rel = None
        srcs = legacy.get(key_rel or "", []) or legacy.get(note.stem, [])
        try:
            rep = verify_note(note, lit, opts, legacy_sources=srcs)
        except Exception as exc:  # noqa: BLE001 - one bad note must not stop the report
            rep = {"note": str(note), "status": "failed", "contract": False,
                   "failures": [{"type": "internal-error", "where": "note", "detail": repr(exc)}], "warnings": []}
        if a.write and rep["status"] in ("quote-checked", "failed", "not-contract"):
            set_verification(note, "quote-checked" if rep["status"] == "quote-checked" else "failed")
            rep["written"] = True
        reports.append(rep)
    if os.environ.get("ZR_AGENT") == "1":
        _finalize_checks(notes, reports)
    result = {"summary": summarize(reports), "notes": reports}
    payload = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    if a.out:
        a.out.write_text(payload + "\n", encoding="utf-8")
    if a.quiet or a.out:
        print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    else:
        print(payload)
    return 0 if all(r["status"] in ("quote-checked", "stub", "unsupported") for r in reports) else 1


if __name__ == "__main__":
    sys.exit(run())
