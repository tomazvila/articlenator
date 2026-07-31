"""The 10-video transcription test corpus.

Every entry is a real, duration-verified YouTube video that is longer than one
hour and notoriously hard to transcribe, paired with a category of authoritative
human ground-truth transcript. Reference transcripts live in
``tests/data/transcripts/<video_id>.txt``; calibrated baselines live in
``tests/data/wer_baselines.json``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CorpusVideo:
    video_id: str
    label: str
    difficulty: str
    ground_truth_source: str
    default_margin: float = 0.05

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


CORPUS: list[CorpusVideo] = [
    CorpusVideo(
        "Unzc731iCUY",
        "MIT — How to Speak (Patrick Winston)",
        "fast lecturer, asides, audience laughter",
        "MIT OpenCourseWare RES.TLL-005 transcript PDF",
    ),
    CorpusVideo(
        "lUUte2o2Sn8",
        "MIT — Gil Strang's final 18.06 lecture",
        "spoken mathematical notation, applause",
        "MIT OpenCourseWare 18.06 transcript",
    ),
    CorpusVideo(
        "MRe4mYcEqBM",
        "SCOTUS — Dobbs v. Jackson oral argument",
        "legal jargon, nine justices rapid-fire",
        "supremecourt.gov argument transcript PDF",
    ),
    CorpusVideo(
        "WChmmdFNlIY",
        "SCOTUS — Bush v. Gore (2000) oral argument",
        "degraded 2000-era audio, legal jargon",
        "Oyez / supremecourt.gov transcript",
        default_margin=0.07,
    ),
    CorpusVideo(
        "ex87haMPB5s",
        "SCOTUS — Trump immunity oral argument",
        "dense legal argument, very long",
        "supremecourt.gov argument transcript PDF",
    ),
    CorpusVideo(
        "LXhzp0omPe0",
        "SCOTUS — Trump tariffs oral argument",
        "endurance (4.6h), legal jargon",
        "supremecourt.gov argument transcript PDF",
    ),
    CorpusVideo(
        "wW1lY5jFNcQ",
        "2020 first presidential debate (C-SPAN)",
        "chaotic crosstalk and interruptions",
        "Commission on Presidential Debates / Rev transcript",
        default_margin=0.08,
    ),
    CorpusVideo(
        "t_G0ia3JOVs",
        "2020 vice-presidential debate (C-SPAN)",
        "moderated, intermittent crosstalk",
        "Rev professional transcript",
        default_margin=0.07,
    ),
    CorpusVideo(
        "kfzp_IgA6YQ",
        "Berkshire Hathaway 2023 meeting, AM (CNBC)",
        "finance jargon, elderly accents, audience mics",
        "CNBC Warren Buffett Archive transcript",
        default_margin=0.07,
    ),
    CorpusVideo(
        "RYecNCgwSfE",
        "SCOTUS — birthright citizenship oral argument",
        "legal jargon, overlapping speakers",
        "supremecourt.gov argument transcript PDF",
    ),
]
