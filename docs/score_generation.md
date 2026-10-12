# Score Generation

## Purpose

Issue #11 generates the canonical scoring note sequence from the offline F0 and Vocal Event
timelines. The result is the version-1 notes.json document consumed by Rust Song Data.

The first implementation deliberately separates three concerns:

1. note candidate generation,
2. whole-song pitch/key/time optimization,
3. publication of the small canonical notes.json contract.

This keeps note transcribers and optimizers replaceable without changing Song Data.

## Inputs

Required artifacts:

- pitch.json from the F0 ensemble
- vocal_events.json from Vocal Event / Boundary Fusion

Both artifacts must describe the same input SHA-256, duration, 48 kHz sample timeline and
480-sample analysis grid.

Optional evidence:

- alignment.json for lyric-segment association
- a candidate artifact from another note transcriber
- a repeated-section artifact

The Notes analysis role does not need to reopen the audio file. It operates on verified upstream
artifacts.

## Candidate layer

The repository-local baseline derives note candidates from voiced F0 runs and accepted Vocal Event
boundaries. Each candidate keeps:

- integer sample start/end,
- robust floating-point MIDI estimate,
- pitch confidence,
- pitch spread,
- boundary evidence,
- multiple MIDI pitch hypotheses,
- source/provenance,
- optional lyric segment id.

note_candidates.json is an intermediate analysis artifact, not the canonical Song Data format.

### External candidate contract

A future Basic Pitch adapter, imported MIDI, or another note transcriber can provide the same
candidate evidence through candidate_artifact. Version 1 accepts a document shaped like:

    {
      "format_version": 1,
      "sample_rate": 48000,
      "duration_samples": 96000,
      "input_sha256": "...",
      "provider": {"id": "basic-pitch", "version": "..."},
      "candidates": [
        {
          "id": "candidate-1",
          "start_sample": 12000,
          "end_sample": 36000,
          "midi_note": 64,
          "confidence": 0.91
        }
      ]
    }

The baseline does not bundle Basic Pitch code or weights. This Issue establishes the provider
boundary so an independently licensed/validated provider can be added without coupling it to the
optimizer or notes.json.

## Boundary and expressive-motion normalization

The score must model intended notes, not every singer fluctuation.

Candidate generation therefore:

- bridges very short unvoiced holes,
- suppresses moderate boundary peaks when there is no sustained pitch change, silence or breath,
- keeps strong onset/boundary evidence,
- keeps sustained pitch steps for melisma,
- trims transition frames before estimating a note's steady pitch,
- uses a confidence-weighted robust median rather than instantaneous F0.

The temporal optimizer then merges weak same-pitch splits and short weak-boundary fragments. This
is the initial normalization for vibrato, scoop/しゃくり, kobushi-like short motion and portamento.
The rules are deterministic and replaceable; they are not intended as a complete expressive-technique
classifier.

## Key / scale estimation

A weighted pitch-class profile evaluates all 12 tonics in major and minor. Duration and candidate
pitch confidence provide the observation weight.

The estimate is a soft prior only. Notes outside the estimated scale remain legal. This prevents a
wrong key estimate from hard-quantizing chromatic notes.

## Repeated-section evidence

The optional repeated-sections artifact contains source/repeat interval pairs:

    {
      "format_version": 1,
      "duration_samples": 192000,
      "pairs": [
        {
          "id": "chorus-repeat",
          "source_start_sample": 0,
          "source_end_sample": 96000,
          "repeat_start_sample": 96000,
          "repeat_end_sample": 192000,
          "confidence": 0.8
        }
      ]
    }

The baseline maps repeated candidates to the corresponding source-region candidate and supplies a
small pitch-consistency prior. It is intentionally a hook, not a hard equality constraint.

## Temporal optimizer

note_optimizer.py defines a NoteOptimizer protocol. The default implementation is
TemporalDynamicProgrammingOptimizer / temporal-dp-v1.

Its state is the pitch hypothesis selected for each time-ordered candidate. The objective combines:

- candidate confidence,
- a small key/scale prior,
- a small local interval/octave-transition penalty,
- optional repeated-section prior.

Normalization runs after path selection. A future optimizer may replace the implementation while
preserving the candidate and canonical output contracts.

## Canonical output

notes.json remains intentionally small and matches core/src/song_data.rs:

    {
      "format_version": 1,
      "analysis_version": 1,
      "notes": [
        {
          "id": "note-0001",
          "start_sample": 12000,
          "end_sample": 36000,
          "midi_note": 64,
          "pitch_confidence": 0.88,
          "lyric_segment_id": "line-0001"
        }
      ]
    }

Detailed decisions are written separately to note_evidence.json so model-specific evidence does not
leak into the stable Song Data contract.

## Evaluator

open-karaoke-note-evaluator reports:

- note pitch precision / recall / F1,
- mean/max onset error,
- mean/max offset error,
- octave error count/rate.

The committed score_notes_reference.json fixture is a manually annotated synthetic regression set
covering expressive fluctuation, a real pitch step, repeated same-pitch notes separated by silence,
and a short kobushi-like excursion.

It validates deterministic algorithm behavior and plumbing only. real_singing_accuracy_verified is
false. A real hand-annotated singer/reference-score corpus is still required before claiming
production note-transcription accuracy.

## Accuracy and licensing boundary

No new pretrained model, model weight, or third-party inference runtime is added by this baseline.
External candidate providers remain optional inputs and must satisfy the project's licensing policy
before being included in an official distribution.
