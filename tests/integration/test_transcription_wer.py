"""End-to-end WER suite over the 10-video corpus.

This is the slow, opt-in suite the project is built around. For each video it
downloads the audio, transcribes it with whisper.cpp large-v3, stitches the
transcript, and measures WER against the stored ground-truth transcript.

It is gated behind ``RUN_TRANSCRIPTION_WER=1`` because each run takes hours to
days. Job directories are persistent, so an interrupted run resumes rather than
restarting. Pass/fail uses *baseline + regression margin*: the first completed
run records each video's WER as a baseline (the test is skipped, not failed);
later runs fail only if WER regresses beyond the per-video margin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from twitter_articlenator.config import get_config
from twitter_articlenator.sources.transcription_service import build_job
from twitter_articlenator.wer import word_error_rate

from .transcription_corpus import CORPUS, CorpusVideo

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TRANSCRIPTION_WER") != "1",
    reason="set RUN_TRANSCRIPTION_WER=1 to run the slow transcription WER suite",
)

_DATA = Path(__file__).resolve().parent.parent / "data"
_TRANSCRIPTS = _DATA / "transcripts"
_BASELINES = _DATA / "wer_baselines.json"
_MEASURED = _DATA / "wer_measured.json"


def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _record_measured(video_id: str, wer: float) -> None:
    measured = _load_json(_MEASURED)
    measured[video_id] = round(wer, 4)
    _MEASURED.parent.mkdir(parents=True, exist_ok=True)
    _MEASURED.write_text(json.dumps(measured, indent=2, sort_keys=True), encoding="utf-8")


@pytest.mark.transcription
@pytest.mark.parametrize("video", CORPUS, ids=[v.video_id for v in CORPUS])
def test_transcription_wer_under_baseline(video: CorpusVideo):
    config = get_config()
    if not config.whisper_model_path.exists():
        pytest.skip(f"whisper model not found at {config.whisper_model_path}")

    reference_file = _TRANSCRIPTS / f"{video.video_id}.txt"
    if not reference_file.exists():
        pytest.skip(f"ground-truth transcript missing: {reference_file}")
    reference = reference_file.read_text(encoding="utf-8")

    job_dir = config.transcription_dir / f"wer-{video.video_id}"
    manifest = build_job(job_dir).run(video.url)
    assert manifest.status == "complete"

    hypothesis = (job_dir / "transcript.txt").read_text(encoding="utf-8")
    wer = word_error_rate(reference, hypothesis)
    _record_measured(video.video_id, wer)

    baselines = _load_json(_BASELINES)
    if video.video_id not in baselines:
        pytest.skip(f"baseline recorded for {video.video_id}: WER={wer:.4f}")

    baseline = float(baselines[video.video_id]["wer"])
    margin = float(baselines[video.video_id].get("margin", video.default_margin))
    assert wer <= baseline + margin, (
        f"{video.label}: WER {wer:.4f} regressed beyond baseline "
        f"{baseline:.4f} + margin {margin:.4f}"
    )
