# Stem separation (Issue #6)

## Implemented boundary

The default analysis registry now exposes `umxhq-vocals` for role `stems`, alongside
`mock-v1`. This is an actual pretrained Open-Unmix UMX-HQ vocals model, not a channel
subtraction placeholder. Other AI roles remain unimplemented.

Input must already be a nonempty **48 kHz stereo WAV** in the Core artifact root.
The audio importer in Issue #4 provides normalization; this adapter does not invoke
FFmpeg or decode arbitrary media. The current admission limit is 30 minutes.

The job returns three private artifact references:

- `vocals.wav`: stereo float32 PCM, 48 kHz, exactly the input frame count.
- `accompaniment.wav`: original normalized mixture minus the estimated vocals.
- `stems.json`: format/pipeline/model versions, full weight and input SHA-256,
  runtime versions, options, sample count, output checksums, cache status and paths.

No independent gain normalization or clipping is applied: values beyond +/-1 remain
in float32 output and set `exceeds_unity`. The playback mixer must provide headroom.
Mixture reconstruction is a structural invariant, **not** a separation-quality score.
Outputs are not yet attached automatically to SongStore or a library GUI.

## Developer invocation

Install matching PyTorch/torchaudio builds for the target device, then:

```powershell
python -m pip install -e './python[dev,stems]'
$env:OPEN_KARAOKE_PYTHON = (Get-Command python).Source
cargo run -p open-karaoke-core
```

Place an already normalized source below the configured Core artifact root, for
example `inputs/song.wav`, then use the public Core API:

```powershell
$job = Invoke-RestMethod http://127.0.0.1:32145/jobs -Method Post -ContentType application/json -Body @'
{"kind":"analysis.stems","analysis":{"model_id":"umxhq-vocals","input_artifact":"inputs/song.wav","options":{"device":"cpu","chunk_seconds":12,"overlap_seconds":2,"niter":1}}}
'@
Invoke-RestMethod "http://127.0.0.1:32145/jobs/$($job.id)"
# Cancellation uses POST /jobs/<Core job id>/cancel.
```

The optional `stems` extra adds Open-Unmix 1.3.0, torch/torchaudio, NumPy, SciPy and
SoundFile. Fast CI uses only the numerical/audio packages plus a test double; the
separate real-model job installs a matching CPU torch/torchaudio pair and runs actual
inference. Missing runtime packages produce `dependency_missing`, never mock output.
CUDA is opt-in and errors explicitly when unavailable; the initial CI target is CPU.

These are developer instructions, not the end-user distribution plan. Bundled runtime,
model provisioning UI and binary license packaging remain release work. Constructing
the service or listing models does **not** import torch/Open-Unmix or download weights.
The first real stem job downloads the pinned weight file into `models/umxhq/`; later
jobs can operate offline. A valid result-cache hit needs no weight loading.

## Model provenance and commercial-use selection

| Component | Selected artifact | Published license |
|---|---|---|
| Code | `openunmix==1.3.0` | MIT |
| Weights | UMX-HQ **1.0.1**, Zenodo record **3370489**, vocals model | MIT (`mit-license` in the publisher API) |
| File | `vocals-b62c91ce.pth`, 35,637,796 bytes | Same record |

Full SHA-256:
`b62c91cedbc7a066f1778ead5b5cecb377aa3a46a31af1cce7c5c8769339d083`

Publisher MD5 (provenance only, not the runtime integrity check):
`d918985fad0fedf6d9ce89e279aa7218`.

The record names Fabian-Robert Stöter and Antoine Liutkus, version 1.0.1, publication
2019-08-14, and explicitly describes these files as UMX-HQ model weights. Its JSON
metadata and the full downloaded SHA-256 were verified on 2026-09-22. A compact record
is retained in `docs/licenses/umxhq-provenance.json`, with the upstream copyright and
MIT notice in `docs/licenses/open-unmix-MIT.txt`.

Primary sources:

- Weights record: https://zenodo.org/records/3370489
- Machine-readable license/file record: https://zenodo.org/api/records/3370489
- Model loader and architecture: https://github.com/sigsep/open-unmix-pytorch/blob/master/openunmix/__init__.py
- Code license: https://github.com/sigsep/open-unmix-pytorch/blob/master/LICENSE
- Model comparison/licensing: https://sigsep.github.io/open-unmix/

**UMXL is not selected**: its published pretrained weights are CC-BY-NC-SA-4.0.
Demucs is not the default in this implementation: the current HTDemucs publisher
model card had its license field removed, so a code-level MIT statement alone was
not used to clear that weight distribution. This is a conservative dependency
selection, not a claim that every other model is legally unusable.

UMX-HQ is an initial, replaceable baseline, not a claim of the best available vocal
isolation. Commercial distribution still needs the complete runtime/binary dependency
inventory and required notices (including SoundFile/libsndfile). The model license
does not grant rights in user-supplied songs. No training dataset or song recordings
are redistributed by this change.

## Execution and cancellation

Inference uses the model's native 44.1 kHz rate. SciPy polyphase resampling converts
from/to 48 kHz; output is trimmed to the **original integer sample count**. Chunk and
overlap lengths are integer seconds, so boundaries align in both time bases. A
weighted overlap/add pass avoids hard splices. Accumulators are disk-backed, and
model inference only holds a bounded window rather than a whole song in the heap.
The default window is 12 seconds with a 2-second overlap, and Wiener refinement uses
one iteration. Very short tails are padded for the model's centered STFT.

The vocals-only model and residual Wiener target produce the vocal estimate. Backing
is recomputed from the original 48 kHz mixture, preserving out-of-model-band energy.
The initial implementation sacrifices some whole-song recurrent context for bounded
window processing; this tradeoff must be evaluated on real music.

Cancellation checkpoints run during input/output copying, weight download, between
inference windows, and before publication. A native model call cannot be interrupted
mid-window. The service's existing process shutdown deadline remains the final bound
for an uncooperative model. Failed/cancelled jobs expose no partial output references.

## Cache and integrity

The key includes SHA-256 of a private snapshot of the exact input bytes, the pipeline
version, model/weight identity, relevant runtime versions, device and all options.
Changes to input, options or model versions therefore invalidate reuse. Deterministic
CPU fixtures are tested; bitwise equivalence across different hardware is not promised.

Entries are private to the OS account below `cache/stems/v1/<key>/`. A complete staging
directory is renamed into place, so readers do not observe half an entry. Existing
entries are immutable; concurrent writers may duplicate inference but never replace
a complete entry. Each hit validates checksums and actual WAV format/sample counts.
A corrupt entry is recomputed without overwriting the corrupt directory; removing that
entry permits caching again. Automatic corruption quarantine and cache GC are deferred.

Results are copied, not hardlinked, into the job directory so editing a result cannot
silently mutate the cache. A final checksum comparison detects changes during copying.
A crash can leave orphan staging/output directories. Directory publication is not a
claim of a multi-file power-loss durable transaction. The artifact root is trusted and
private; the inherited path checks do not sandbox malicious same-user filesystem races.

Weights are fetched only from the fixed publisher URL, with a size limit, a unique
temporary file and full SHA-256 verification. Cached weights are reverified before
loading. `torch.load(weights_only=True)` and strict state-dict loading are mandatory:
there is no unsafe-pickle fallback, user-supplied checkpoint URL or remote Python loader.

## Evaluation

`python/tests/stem_fixtures.py` defines a deterministic synthetic lead/backing recipe.
It contains no third-party recordings. Fast tests cover exact lengths, stereo, silence,
non-finite data, peaks/headroom, cache identity/integrity, path rejection, cancellation,
API contracts and metric known-pass/known-fail cases.

The real-model test is explicitly run by `.github/workflows/stem-model.yml`. It downloads
and verifies the real weights, runs separation, checks both WAVs, then checks exact cache
reuse. `real-stem-metrics.json` is uploaded as a CI artifact. The synthetic test validates
the implementation, **not accuracy on real singers or commercial karaoke readiness**.

For a rights-cleared reference dataset:

```powershell
python -m open_karaoke_analysis.stem_evaluator --reference-vocals reference-vocals.wav --reference-accompaniment reference-backing.wav --vocals vocals.wav --accompaniment accompaniment.wav --dataset-version my-dataset-v1 --output metrics.json
```

Metrics include joint-stereo, zero-mean SI-SDR, SI-SDR improvement relative to the
mixture, simple waveform SDR (not BSS Eval SDR), RMS error and mixture reconstruction
error. Silent references have undefined SI-SDR (`null`), not fictitious infinite scores.
The report includes a reference-content fingerprint. Add `--min-vocals-si-sdr <dB>`
for an explicit quality gate; it returns exit code 1 on a regression. Invalid inputs
return 2. Without a calibrated threshold `quality_gate_applied` is false; a structural
pass must not be presented as a music-quality pass. Real-song baselines/thresholds and
comparisons with higher-quality, license-cleared models remain release evaluation work.
