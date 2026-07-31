# Ground-truth transcripts

One file per corpus video: `<video_id>.txt`, holding the authoritative human
transcript for that recording (plain UTF-8 text). Speaker labels
(`JUSTICE KAGAN:`) and stage directions (`[crosstalk]`, `(Laughter)`) may be
left in — `twitter_articlenator.wer.normalize` strips them before scoring.

The WER suite (`tests/integration/test_transcription_wer.py`, gated by
`RUN_TRANSCRIPTION_WER=1`) skips any video whose transcript file is absent, so
the corpus can be filled in incrementally. See `transcription_corpus.py` for the
ground-truth source of each video.
