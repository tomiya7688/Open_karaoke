# Lyric alignment and imported lyrics (Issue #8)

## Boundary

`wav2vec2-ja-alignment` accepts normalized vocals plus the version-1 `lyrics.json`
produced by Issue #7. Imported UTF-8 plain text is optional. Without imported text,
the ASR lines are aligned; with it, reference lines and their differences are also
mapped onto the measured character timeline. Neither ASR nor imported text is truth.

The pipeline uses a WhisperX-style acoustic-model/CTC forced-alignment boundary.
Our bounded Viterbi implementation is independent, with explicit blank states and
mandatory blank separation between repeated labels. We do not import the entire
WhisperX stack, its ASR/diarization dependencies, or its default model downloaders.

Initial acoustic language: **Japanese only**. Other language codes are rejected,
not silently aligned using the wrong model. Backend replacement is through
`AlignmentAdapter(backend_factory=...)`: `prepare`, `emissions`, `identity`, `close`,
plus `vocabulary` and `blank_id`. No heavy framework import or download at startup.

## Input / output

From Core's existing `POST /jobs`:

```json
{
  "kind": "analysis.alignment",
  "analysis": {
    "model_id": "wav2vec2-ja-alignment",
    "input_artifact": "jobs/your-stem-instance/your-stem-job/vocals.wav",
    "options": {
      "lyrics_artifact": "jobs/your-asr-instance/your-asr-job/lyrics.json",
      "reference_lyrics_artifact": "imports/reference.txt",
      "language": "ja",
      "device": "cpu",
      "allow_model_download": true,
      "unit_kind": "character",
      "min_token_score": 0.05,
      "analysis_version": 1
    }
  }
}
```

Omit `reference_lyrics_artifact` when no reference exists. Paths are relative to the
existing artifact root, never arbitrary absolute paths or URLs. Existing Core
progress/cancel/error endpoints apply. Cancelling native inference is cooperative
between calls; the process supervisor remains the final stop boundary.

- `lyrics.json`: backward-compatible `LyricsDocument`. Preserve ASR IDs/text and
  refine line timing only when every normalized character has an accepted CTC span.
  Otherwise preserve the original coarse ASR span and label that fallback in evidence.
- `alignment.json`: original ASR document, line/character/unit observations, imported
  original text/hash, monotonic reference mapping, edit operations and reference lines.
- `alignment_evidence.json`: `EvidenceDocument`, including model/version/hash,
  runtime versions, original source, confidence interpretation and coverage.

Input documents are never overwritten. JobManager advertises the three artifacts
only after successful completion and cleans failed/cancelled private output folders.

## Timing, confidence and mismatch policy

All saved times use integer 48 kHz samples, with end-exclusive spans. Audio is
resampled to 16 kHz for inference. CTC frame edges map by integer rational rounding
(ties upward) to the original ASR interval. This **does not imply sample-accurate
acoustic precision**: resolution is limited by the model frames and ASR window.

Japanese `unit_kind=character` follows the non-space-delimited alignment convention;
`words` entries explicitly say `unit_kind=character`. They are not morphological
Japanese words. `whitespace_word` aggregates complete non-whitespace runs, useful
when an input transcript already supplies word boundaries. No morphological parser,
phoneme timing or equal-duration interpolation is claimed.

Normalization is NFKC/casefold with original codepoint offsets, including combining
marks and halfwidth kana. Punctuation/whitespace do not create artificial timed
characters. Repeated labels use distinct CTC states. Out-of-vocabulary letters do
not disappear: the line remains unaligned rather than forcing partial text across
unknown audio. Silence and too-short intervals similarly have no invented unit times.
Low-score observed spans stay in evidence but are not published as accepted unit times.
Canonical timing confidence remains null; CTC emission scores are diagnostic values,
not calibrated probabilities of accurate timing. The 0.05 threshold is a heuristic,
not a singing-quality calibration.

Reference mapping is bounded monotonic character matching, not phonetic matching.
The unmodified reference and ASR are preserved along with equal/replace/insert/delete
spans. Reference lines receive timing only when all normalized characters map to
accepted, contiguous ASR characters. Missing/extra/alternate-version words remain
untimed; repeated reference lines are retained and flagged for review. A correct
reference that differs from ASR is a review candidate, **not silently substituted**.
This initial implementation does not recover omitted verses or force-align a whole
untimed song from reference text alone: it requires coarse ASR windows from Issue #7.

## Bounds / reproducibility

Audio: 48 kHz float32 mono/stereo WAV, <=1 h, with a private hashed snapshot.
ASR windows: <=30 seconds, within audio, ordered starts and unique IDs; overlaps
are retained and flagged. Maximum 2048 segments and 16,000 source characters.
Reference: <=64 KiB UTF-8 plain text. Mapping budget: 16 million character-pair cells.
CTC: <=2000 frames, <=8192 classes, <=512 target characters per window and a
4-million-state-cell trellis. Invalid audio, spans, model files and excessive inputs
return structured errors. Digital silence bypasses model loading.

## Model / license provenance

WhisperX's Japanese default points to
https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py
and the selected publisher model is
https://huggingface.co/jonatasgrosman/wav2vec2-large-xlsr-53-japanese .

Pinned revision: `cf031e020336460d15a417eba710bbc5bb43be9a`.
Pinned `pytorch_model.bin` SHA-256:
`4f6821dcd79770fcd4c11487c4ee5c040b3a7e1863425224014ba5ea8fbc1b67`.
The publisher declares **Apache-2.0** for this model; this is a speech model, not a
singing-trained accuracy claim. Transformers code is Apache-2.0. Keep upstream
licenses/notices in any redistributed runtime/model package; release packaging and
input-music rights are separate reviews. See `licenses/alignment-provenance.json`.

Only fixed filenames at the pinned revision are downloaded. Weight SHA-256 is
verified before restricted `weights_only=True` loading; learned-parameter mismatches
are rejected. Config/preprocessor/vocabulary hashes are recorded on initial HTTPS
fetch and rechecked against the snapshot manifest when reused. These small files are
revision-pinned, not independently pre-pinned by hash in source. Cache directories
must be private to the OS account, like all other analysis artifacts.

Primary algorithm/API references:
- https://docs.pytorch.org/audio/stable/tutorials/forced_alignment_tutorial.html
- https://huggingface.co/docs/transformers/v4.57.1/en/model_doc/wav2vec2
- https://github.com/huggingface/transformers/blob/v4.57.1/LICENSE

## Developer validation

Install a matching torch runtime, then `python -m pip install -e './python[dev,alignment]'`.
Run targeted checks with `python -m pytest python/tests -q -k alignment`.
The dedicated read-only workflow also runs the real Japanese checkpoint explicitly:

```powershell
$env:OPEN_KARAOKE_REAL_ALIGNMENT = '1'
python -m pytest python/tests/test_alignment_real_model.py -q
```

The real-model smoke uses generated tone input and a forced Japanese target. It
checks loading/inference/CTC/publication, **not singing alignment accuracy**.

`open-karaoke-alignment-evaluator pairs.json metrics.json --max-mean-ms 50 --max-p95-ms 100`
evaluates rows `{id, reference:{start_sample,end_sample}, hypothesis:{...}|null}`
in `{dataset_version, items}`. It reports mean/p95 absolute boundary error in ms,
coverage and missing IDs. Missing timings never count as zero error. Error thresholds
require full coverage by default; override only explicitly with `--min-coverage`.
No threshold means `quality_gate_applied=false`. Exit: 0 success, 1 quality failure,
2 invalid input. A licensed, manually timed Japanese singing set and calibrated
mean/p95/coverage thresholds remain open acceptance work; do not close #8 on smoke
results alone. SongStore attachment, GUI interaction and CUDA validation are not
established by this feature's synthetic fixtures.
