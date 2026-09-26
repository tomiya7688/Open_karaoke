# Vocal Event / Boundary Fusion (Issue #10)

## Purpose

Generate one shared 48 kHz Vocal Event Timeline for downstream score generation and lyric
alignment. The timeline keeps independent evidence and exposes two heuristic boundary scores:
note_boundary_probability and lyric_boundary_probability.

These values are bounded to 0..1 but are not calibrated probabilities of correctness.

## Inputs

Core submits role events with normalized separated vocals.wav as input. Options require the
Issue #9 pitch.json from the same vocals hash. Issue #8 alignment.json and a future version-1
note-candidate artifact are optional.

The pitch artifact must use 48 kHz, a 480-sample hop, the same duration and the same input
SHA-256. Cross-song evidence is rejected.

## Independent evidence

All evidence is projected to the pitch grid: 10 ms hop and 40 ms acoustic window.

- f0_transition: pitch movement after a 35-cent dead band, so small vibrato is not automatically
  strong boundary evidence.
- voiced_transition: voiced-probability change plus explicit voiced/unvoiced changes.
- spectral_flux: positive normalized spectrum change.
- energy_delta: frame-to-frame energy change.
- acoustic_onset: spectral flux plus positive energy rise.
- lyric_timing: line/character/unit/phoneme/syllable timestamps from optional alignment evidence.
  Current Japanese alignment supplies character/unit evidence; it is not relabeled as phonemes.
- note_onset: optional future note-transcription candidate starts.
- silence_breath: low-energy unvoiced transitions plus breath-like unvoiced energy.

Stereo phase cancellation uses the louder-channel fallback and records that fallback in evidence.

## Fusion and comparison

mode=ensemble combines selected available features with transparent fixed weights. Missing optional
alignment/note evidence is omitted from weight normalization.

mode=single requires exactly one selected feature and publishes that feature directly as both
boundary scores. This is for feature ablation/regression comparison, not a claim that every feature
has identical semantics.

The adapter publishes vocal_events.json with normalized features, both boundary scores,
silence/breath probabilities and non-max-suppressed note/lyric candidates. It also publishes
vocal_event_evidence.json with F0, voiced probability, raw acoustic values, per-feature
contributions, source artifacts, phase-cancellation flags and optional linguistic/note events.

Candidate suppression defaults to 30 ms and does not alter per-frame probabilities.

## Accuracy boundary

No new model weights are introduced. The baseline uses repository-local DSP with NumPy and
SoundFile.

The regression set covers generated clean transitions, low-quality/noisy cases and a
legato/melisma case. It validates deterministic plumbing and behavior only. It does not establish
real-singer precision, calibrated boundary probability, breath-classification accuracy or final
note segmentation quality.

Issue #11 remains responsible for whole-song note optimization and deciding when vibrato, scoops
or melisma should become score notes.

## Evaluator

open-karaoke-vocal-event-evaluator performs one-to-one boundary matching inside a tolerance and
reports precision, recall, F1 and matched timing error.

CI gates the committed generated fixture at 30 ms tolerance, note F1 >= 0.90 and lyric F1 >= 0.95.
Those are regression thresholds for the synthetic fixture, not real-singing quality targets.

## Validation

Focused validation:
- python -m pytest python/tests/test_vocal_event*.py -q
- open-karaoke-vocal-event-evaluator with python/tests/fixtures/vocal_event_boundaries.json,
  30 ms tolerance, min note F1 0.90 and min lyric F1 0.95.

Shared contract changes also require the normal Python suite, Rust workspace tests, explicit
Core-to-Python lifecycle test, and Windows/Linux CI.
