# Pitch / F0 Analysis (Issue #9)

## Scope

Offline pitch analysis consumes the separated `vocals.wav` and publishes a normalized 48 kHz
F0 timeline. It is not used in the realtime microphone path; realtime pitch detection remains a
Rust Core responsibility.

The baseline intentionally uses algorithmic detectors with no pretrained weights:

- `yin`: FFT-accelerated YIN-style cumulative mean normalized difference.
- `nacf`: normalized autocorrelation with a mild long-lag fundamental preference.

Both are implemented in this repository behind the common `PitchDetector` protocol. No new
third-party model or model-weight license is introduced by this issue. NumPy and SoundFile are
already project dependencies; the production optional dependency group is `python[pitch]`.

## Job API

Role: `pitch`

Default model: `f0-ensemble-v1` version `1.0.0`.

Example options:

```json
{
  "mode": "ensemble",
  "detectors": ["yin", "nacf"],
  "min_frequency_hz": 55.0,
  "max_frequency_hz": 1760.0,
  "energy_floor": 0.0001,
  "voiced_threshold": 0.55,
  "analysis_version": 1
}
```

Single-detector comparison is explicit:

```json
{"mode":"single","detectors":["yin"]}
```

`single` requires exactly one detector; `ensemble` requires at least two. Detector names are
validated and duplicates are rejected.

## Timeline

Input must be <=30 minutes, 48 kHz, float32 WAV/WAVEX/RF64, mono or stereo. Analysis uses:

- window: 1,920 samples (40 ms)
- hop: 480 samples (10 ms)
- canonical time: integer 48 kHz sample indices

Stereo is averaged. When opposite-phase channels nearly cancel the mono sum, the louder channel
is used and the fallback is recorded in evidence.

`pitch.json` contains the canonical result. Each frame stores:

- `start_sample`, `end_sample`, `center_sample`
- `f0_hz` or `null`
- fractional `midi` or `null`
- `confidence`
- `voiced_probability`
- `voiced`

Confidence and voiced probability are bounded diagnostics, **not calibrated probabilities of
correctness**.

`pitch_evidence.json` retains detector-by-detector observations, detector versions and reliability
weights, fusion candidates, octave shifts, continuity penalties, input SHA-256 and analysis
options.

## Ensemble fusion

Fusion is transparent and replaceable rather than a learned black box.

For each frame it:

1. gathers each detector's F0, confidence and voiced probability;
2. creates octave-related candidate states within the configured F0 range;
3. scores detector agreement while penalizing octave shifts;
4. applies a previous-frame continuity penalty;
5. chooses the strongest supported state;
6. applies the voiced threshold.

This is enough to recover isolated octave mistakes while retaining the raw detector output for
audit. The `nacf` baseline deliberately has a different failure mode from YIN, which makes the
regression suite useful for verifying ensemble behavior.

This issue does not claim that these two baseline algorithms are the final best detector set.
CREPE-family or other commercially cleared detectors can be added later behind the same protocol
after both runtime and weight licenses are verified.

## Evaluator

`open-karaoke-pitch-evaluator` runs the deterministic
`generated-harmonic-tone-sweep-v1` fixture:

- 24 logarithmically spaced tones from 70 Hz to 1 kHz
- deterministic harmonics/noise
- 8 unvoiced frames
- an explicit isolated octave-error continuity fixture

Metrics:

- voiced recall
- unvoiced false-positive rate
- median / p95 absolute cents error
- gross pitch error rate (>50 cents; missing voiced predictions count as gross errors)
- octave error rate

The report compares every detector and the ensemble. CI gates the ensemble synthetic median cents,
gross-error rate and octave-recovery fixture.

This fixture proves algorithm and regression behavior only. It is not a recording of a singer and
does not establish real-song or real-singing accuracy.

## Validation

Focused:

```powershell
python -m pytest python/tests/test_pitch.py python/tests/test_pitch_evaluator.py -q
python -m open_karaoke_analysis.pitch_evaluator `
  --output artifacts/pitch-regression.json `
  --max-ensemble-median-cents 5 `
  --max-ensemble-gross-rate 0.05
```

Shared contracts require the normal Python, Rust/Core-to-Python and Windows/Linux CI gates as well.
