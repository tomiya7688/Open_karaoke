# Lyric transcription (Issue #7)

## Implemented boundary

`analysis.lyrics` accepts a separated 48 kHz float32 WAV (one or two channels, at most
one hour). It produces **candidates**, not authoritative lyrics or forced alignment.
Default model: `whisper-large-v3`. Explicit faster alternative: `whisper-large-v3-turbo`.
Both use the pinned `openai-whisper==20250625` backend; there is no silent model fallback.
The existing ModelAdapter/Job contracts and Rust Song Data schema are unchanged.

## Development use

Install the desired PyTorch CPU/CUDA runtime, then `python -m pip install -e './python[dev,lyrics]'`.
No model framework is imported and no weights are downloaded at service startup. A non-silent
job downloads its pinned weights on first use. For offline operation, install the verified
`.pt` file below the configured artifact root's `models/whisper/` directory and set
`allow_model_download` to false. Missing or corrupt weights produce a structured error.
This is a development procedure; bundling runtimes/models for end users remains release work.

Through the running Rust Core:

```json
{
  "kind": "analysis.lyrics",
  "analysis": {
    "model_id": "whisper-large-v3",
    "input_artifact": "jobs/<stem-instance>/<stem-job>/vocals.wav",
    "options": {
      "language": "ja",
      "device": "cpu",
      "beam_size": 5,
      "analysis_version": 1,
      "allow_model_download": true,
      "reference_lyrics_artifact": null
    }
  }
}
```

Submit this JSON to `POST /jobs`, poll `GET /jobs/{id}`, or cancel through the existing Job API.
Paths are relative to the configured artifact root, not the process working directory.
`language: null` uses per-window language detection; other values use Whisper language codes.
Transcription never translates the lyrics to English. CUDA is explicit and fails if unavailable.
No input audio is sent to an external API. Network use is limited to public model downloads.

## Output and evidence

All references are published together after success under the job's private output directory:

- `lyrics.json`: the existing Rust LyricsDocument schema; integer `start_sample`/`end_sample`.
- `lyrics_evidence.json`: Rust-readable EvidenceDocument items plus diagnostic observations.
- `asr_raw.json`: original JSON ASR responses per window, input checksum, model identity,
  runtime versions, options and optional reference lyrics.

Reference lyrics are bounded UTF-8 text (64 KiB). They are copied with provenance into the raw
result **but are not injected into decoding, substituted for ASR or assigned invented times**.
Alignment/optimizer integration is later work. Model/weight identity lives in the evidence and
raw documents; it is not lost when the canonical LyricsDocument is deserialized by Rust.
The files are not automatically attached to an existing SongStore bundle in this issue.

ASR `avg_logprob`, `no_speech_prob`, compression ratio and temperature are diagnostic values,
not calibrated lyric correctness probabilities. Canonical text/timing confidence is therefore
`null`. The evidence explicitly labels coarse ASR timing and uncalibrated confidence.
Low log probability, possible non-speech, compression and repetition are review flags. Genuine
choruses are not deleted merely because their text repeats. Original ASR responses remain intact.

## Time, memory and cancellation

Read a job-local byte snapshot and process 28-second ownership intervals, with up to one second
of left/right context (maximum 30 seconds of audio per inference call). Downmix to mono,
resample 48 kHz to 16 kHz at the boundary, then convert model-local seconds using decimal half-up
rounding and add the exact integer window offset. Assign each segment by its midpoint to an
ownership interval; sort output chronologically. Clamp padded tail timestamps to actual audio
length and record the decision. Retain blank/zero-length raw segments as discarded observations.

Only digital silence (peak <= 1e-8) bypasses inference. Digital-silence segment hallucinations are
excluded from candidates but retained in raw/evidence. This is **not** a learned vocal detector;
noise, accompaniment leakage and soft singing still require later quality evaluation. Anti-phase
stereo that cancels completely during averaging uses the left channel and records that choice.

Cancellation checkpoints occur during snapshot/download, between windows, after inference and
before publication. Native inference is not preempted mid-call; Core process termination remains
the last resort for a hung worker. JobManager removes incomplete private outputs on failure or
cancellation. Download buffers are bounded, verified by SHA-256 and published atomically. No
unsafe pickle fallback, arbitrary model URLs, remote Python loading or input uploads are used.
There is no ASR-result cache yet. Weight caching is separate from results and survives jobs.

## Evaluation and limitations

```sh
python -m pytest python/tests/test_lyric*.py -q
cargo test --locked --test lyric_contract
python -m open_karaoke_analysis.lyric_evaluator pairs.json --output artifacts/lyrics/metrics.json
```

`pairs.json` is an array of `{ "id": "line-1", "reference": "青い空", "hypothesis": "青い海" }`.
CER is the primary Japanese metric. WER explicitly uses whitespace tokenization, not Japanese
morphological segmentation. Optional normalization is NFKC/case folding/punctuation removal.
Micro totals are retained, errors may exceed 100%, and empty-reference insertions are reported
without division by zero or a misleading perfect score. With no `--max-cer`, `passed` is null and
`quality_gate_applied` is false. An explicit failed threshold exits nonzero. Work is bounded per
utterance; divide long recordings into annotated phrases before evaluating.

The fixed Japanese text/ASR fixtures are authored contract regressions, not recordings of singing.
**No licensed, manually annotated Japanese singing audio set is supplied by this change.**
Real-model CI loads actual publisher checkpoints and performs inference on a generated tone;
it checks the runtime/artifact boundary, not transcription accuracy. Tiny is test-only and is not
registered as a production model. `large-v3` can be tested explicitly with:

```sh
OPEN_KARAOKE_REAL_LYRICS=1 OPEN_KARAOKE_LYRIC_MODEL=large-v3 python -m pytest python/tests/test_lyric_real_model.py -v
```

Track large-v3, large-v3-turbo, tiny, CUDA and real-singing validation separately in the PR. Do not
infer one model/device's success from another. A small rights-cleared Japanese singing regression
set and measured CER/WER thresholds remain an open validation item for Issue #7. Use original or
licensed recordings with reference transcripts; record provenance, recording hashes and model
identity with each metric run. Never reinterpret the tone or scripted fixtures as singer accuracy.

## Publisher provenance

Selected distribution: OpenAI's native Whisper checkpoints from the Azure publisher registry,
not converted Hugging Face artifacts. OpenAI's repository explicitly licenses **code and weights**
under MIT; retain [the notice](licenses/whisper-MIT.txt). This does not grant rights in input songs.

- Publisher code/weight statement: https://github.com/openai/whisper#license
- Native checkpoint registry: https://github.com/openai/whisper/blob/main/whisper/__init__.py
- Registry blob inspected: `f284ec0453b2c5efb025963f6a56a5dc404f78a7`.
- Pinned package release: https://pypi.org/project/openai-whisper/20250625/
- Model limitations: https://github.com/openai/whisper/blob/main/model-card.md

Full SHA-256 values are enforced in `whisper_backend.WEIGHTS` and copied into every inference
result. Review the final packaged dependency inventory and notices separately; this is not a
blanket license certification of all optional runtimes or users' music.
