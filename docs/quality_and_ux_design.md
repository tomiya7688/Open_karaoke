# Analysis quality and karaoke UX design

> **Proposed architecture and product acceptance criteria** as of `main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e` (2026-11-04), Issue #106.
>
> It is not a claim that quality thresholds, auto-reanalysis, installer or real-device UX are implemented. Existing evidence: [Full-song analysis](full_song_analysis.md), [Dataset evaluation](dataset_evaluation.md), [Testing](testing.md), [Desktop client](desktop_client.md), [Model Plugins](model_plugins.md).

## Product objective and constraints

The expected experience is: **choose a song → prepare with the least required work → sing → understand feedback**. Normally no manual lyric/note correction, model installation by hand or audio developer knowledge should be required.

Keep these independent:

1. **Integrity:** Is the artifact valid (source hash, timing, schema, language and model identity)?
2. **Estimated quality:** How trustworthy is this segment for the intended use? Derived model confidences and heuristic fusion scores are not necessarily calibrated.
3. **User action:** Can we sing now? Should one uncertain interval be retried? What can the user do if the result is poor?

Do not collapse these into an invented global “95% accurate” label. A synthetic regression pass or successfully downloaded model weight is **not** real-singing quality evidence.

## Actual pipeline and missing quality feedback loop

Current full-song pipeline is one sequential `analysis.song` Job:

```text
normalized 48kHz WAV
   → UMX-HQ stems
   → Whisper lyric candidates
   → Japanese CTC lyric alignment
   → YIN/NACF offline F0
   → Vocal Event boundary fusion
   → note candidate optimization
   → SongBundle
   → separate song.import Job
```

This already checks source hashes, format, time ranges and artifact ownership. **It does not yet re-run low-quality sections, automatically calibrate confidence, or choose an alternate path for a MIDI/ready-made instrumental input.** A failure at a required stage currently fails the Job; an inaccurate-but-valid result can pass structural tests.

The target pipeline adds a **QualityAssessor / RepairPlanner** after each stage and again before song publication:

```text
stage artifacts (immutable)
     │
     ├── hard validation ── integrity failure → fail, do not publish
     │
     └── quality assessment ── acceptable → continue
                    │
                    └── uncertain → bounded retry / alternative evidence
                                 │
                                 └── still uncertain → best-effort playable
                                     with transparent warnings, not fake scores
```

The RepairPlanner must be deterministic for a fixed artifact set and configuration. It must not invoke arbitrary new models or unbounded recursive retries. Keep audio/lyrics/notes evidence as distinct versioned objects. Do not let text fluency alone overwrite unusual but acoustically plausible lyrics.

## Quality report contract (design proposal)

Store optional quality metadata as a **separate versioned artifact** to avoid silently changing v1 `song.json`/`notes.json`. A draft layout:

```json
{
  "quality_report_version": 1,
  "song_analysis_fingerprint": "<verified hash>",
  "model_snapshots": [
    {
      "role": "lyrics",
      "adapter_id": "whisper-large-v3",
      "version": "<pinned version>",
      "weight_sha256": "<verified hash>"
    }
  ],
  "segments": [
    {
      "start_sample": 48000,
      "end_sample": 144000,
      "dimension": "lyric_alignment",
      "status": "needs_review",
      "signals": {
        "alignment_coverage": 0.62
      },
      "signal_interpretation": "diagnostic_not_calibrated",
      "flags": ["ctc_fallback"],
      "attempts": 1
    }
  ],
  "overall": "playable_with_warnings"
}
```

The sample values above are illustrative, **not measured project statistics**. Report should also carry algorithm/config versions, input/annotation provenance, successful/failed attempts, and effective model-selection source. `quality_report` is **not a legal or scientific certification**.

Recommended state values:

- `blocked`: corrupted/untrusted artifact or required playable assets missing. Do not publish as ready.
- `needs_review`: valid artifact, but some observations are uncertain. Keep evidence; bounded repair may improve it.
- `playable_with_warnings`: audio and timeline are safe to use, but some generated content is approximate.
- `ready`: all required structural contracts pass **and** any defined product gates pass. This is not equivalent to perfect lyrics or score.
- `not_evaluated`: no quality measurement, distinguish from failure and from perfect zero error.

Avoid conflating `confidence` with correctness probability. Require `signal_interpretation` such as `heuristic`, `calibrated`, `measured` on each signal where important.

### Proposed quality signals

| Stage | Diagnostic evidence | User impact / retry choice |
|---|---|---|
| Stems | Vocal energy leakage, estimated masking, clip/nonfinite, reconstruction error (structural only) | High leakage may pollute mic monitoring and ASR; offer model comparison when licensed |
| Whisper ASR | Coarse segments, no-speech flags, repeated hallucination patterns, language agreement, reference text difference | Re-decode only uncertain windows with context; never replace unique lyric on grammar grounds |
| Alignment | Character/line span coverage, missing/OOV characters, score/time discrepancy, duration overlap | Re-align an expanded window or mark line timing approximate |
| F0 | YIN/NACF agreement, octave alternatives, voicing consistency, gap and harmonic indicators | Re-estimate suspicious windows, retain original evidence and detector outputs |
| Vocal Event | Acoustic/voicing/lyric boundary agreement; uncertain legato/melisma segmentation | Consider neighboring windows and lyric evidence, avoid splitting vibrato |
| Note generation | Note onset/offset uncertainty, short fragment count, chromatic soft-prior conflict, reference MIDI differences | Re-score candidate timeline and local optimization, retain alternatives |

**Hard failures** (hash mismatch, invalid PCM, incompatible schema) are not “low confidence”. They must fail safely; retries must not hide provenance mismatch. **Data quality warnings** should not automatically prevent singing.

## Bounded automatic repair

- Set deterministic per-stage and whole-song budgets: max attempts, max window duration, time/CPU/VRAM budget, cancellation checkpoints. No indefinite search for a “perfect” result.
- Record attempt identity and exact artifact dependencies; keep original observations. If a window is reanalyzed, regenerate only downstream artifacts whose input hashes actually changed.
- Compare candidates against an objective for each dimension. Preserve exact lyric text, character alignment and note timing separately; don't reward grammatical fluency as “song accuracy”.
- Prefer conservative non-destructive fallbacks: provide lyric line timing without invented syllable times; display pitch bar only where notes are valid; allow unscored/practice mode when score evidence is weak.
- Allow optional user-provided lyric / MIDI to increase evidence. They are references, not always ground truth. A source version/offset/tempo mismatch must surface a warning.
- Do not call heavyweight Python from realtime playback callbacks. Offline reanalysis may run after song playback if #43 decides it would threaten audio latency.

## Model selection and reproducibility

The model UI currently records a **role-level selection** and can resolve it for individual analysis Jobs. Full-song `SongAdapter` selects internal stages from `options.models` or the registry default, not from stored role selections. Also, a package manifest can identify a registered backend without loading distinct weights. Therefore **“package available” does not mean the weights in its folder are in use**.

Before full-song analysis starts, create an **effective model plan snapshot**:

```text
request explicit model/override
    > valid stored selection
    > product default
```

Per stage capture requested ID, actual adapter, model artifact/weight checksum, backend/runtime version, expected language, device, options, and reason for any fallback. Pass the plan to the Job as immutable input; do not resolve defaults again in later stages. If a previously selected model disappears, default fallback may be allowed for ordinary use **only with an explicit UI notice**; an evaluator or reproducibility mode must **fail closed** rather than silently compare different models.

User-installed MOD licenses are **not** blocked by model loader policy; this is separate from official distribution screening. But third-party Python code is executable and not inherently sandboxed. A manifest's readability/availability must not be presented as runtime safety verification.

## Accuracy evaluation gates and realistic data

The project already has dataset manifest validation, synthetic CER regressions and a private-data reporting interface. That is valuable infrastructure, but much of the accuracy evidence remains **synthetic**. `kind=lyrics` annotations are currently the more developed path; real-singing datasets and cross-task reference annotations are still work under #42/#18.

Required comparisons:

| Dimension | Metrics / diagnostic | Important stratification |
|---|---|---|
| Source separation | SI-SDR/SDR and human listening panel | Solo/chorus, instrumental density, reverb, duet, bleed |
| Lyrics | CER, deletion/hallucination rate, repetition behavior | Japanese/other languages, lyrics style, breaths, fast passages |
| Lyric alignment | Boundary median/p95 milliseconds, coverage and missing lines | Held vowels, melisma, repeated lyrics, backing leakage |
| Pitch | Cent median/p95, voicing precision/recall, octave error | Low/high voices, falsetto, noise, instrument bleed |
| Note generation | Pitch/onset/offset F1, over/under segmentation, inter-rater agreement | Legato, vibrato, scoops, rap, chromatic passages |
| Realtime scoring | Monotonicity, timing fairness, per-note reproducibility, correlation with blinded human ratings | Mic/interface types, USB latency, singers, genres |
| User experience | Time-to-first-song, import success, first-run failures, device recoverability, user edits required | CPU-only/low RAM, no network, high DPI, accessible input |

- Split by song/recording/singer/session **groups**, not random tiny windows, to avoid leakage from repeat choruses or near-duplicate audio.
- Keep `development` and `holdout` independent; don't tune to holdout repeatedly. Version inputs, annotations, model weights, options and thresholds.
- Use rights-cleared recordings/annotations and explicit publication policy. Do **not** upload private music, raw vocals, lyrics or ID-bearing individual reports as public CI artifacts.
- Synthetic fixtures are used for fast deterministic regressions; actual model-load smoke is separate; human-annotated recordings are used for threshold calibration; Windows physical-device tests are the final product gate.
- Define per-metric candidate thresholds only after a baseline dataset exists. Do **not** invent a “production-grade” CER/F1/latency target in advance.

## First-run and daily-use UX (proposed)

### First launch (no developer tooling exposed to ordinary users)

1. Check packaged Rust/Core/Python runtime and installed models locally. Show **installed and verified**, **download available**, **runtime missing**, **unsupported**, **user-provided/unverified** as distinct states.
2. Ask which microphone and output device to use; play a safe low-volume test, then show an input level/VU meter. If available, measure input/output path delays; otherwise mark `uncalibrated` and offer practice mode. Do not suggest that 0 ms was measured.
3. Show available disk space, approximate model footprint (from actual package manifest), network/offline requirements and progress/cancel before downloading. An offline/manual model package remains optional.
4. Save device profiles per stable physical device ID + buffer/rate/backend; names alone are not reliable and duplicate-name devices are possible. Device changes invalidate measured latency.
5. Allow skipping optional fine-tuning. First-run setup must not become an obstacle to playing a supplied accompaniment + MIDI score.

### Song import, analysis and playback

```text
Choose local song / accompaniment
    → optional existing lyrics or MIDI
    → preflight media, devices, models and space
    → prepare (stage progress + cancellable task)
    → ready / ready with warnings / input action required
    → sing (one-click)
    → result with explainable per-note metrics
    → optional edit/retry the uncertain passage
```

- Support two production paths: **auto-generated** song and **supplied accompaniment + MIDI/lyrics**. Current MIDI importer and core SongStore should be reused, not rewritten inside GUI.
- A multi-minute analysis progress UI should identify the stage, model, input errors and last durable cache checkpoint. Only show an ETA if measured empirically; never derive one solely from a nominal percentage.
- Show **why** a song is not ready and the one action that fixes it (e.g. missing Python runtime, missing weight, bad audio format, no microphone). Transport/network retry must not re-import or double-register a song.
- Distinguish expected content warnings from blocking errors. Keep user-edited lyrics/notes in a separate versioned layer so optional reanalysis does not overwrite edits.
- During karaoke, show one clear current/next lyric pair, stable high-contrast pitch bars, a distinct singer pitch trace, and controls for play/pause, mic/music/effect gain and seek. Avoid dense diagnostics on the singing screen.
- On-device audio health indicators (clip/low input/overflow) must be actionable and not masquerade as singing mistakes. Show scoring as **provisional** when chart or latency calibration is unverified.
- Responsive layout must account for high DPI, small/larger stage displays, keyboard input, dark mode and high contrast. Headless UI screenshots are useful but not a substitute for real Windows accessibility/IME/device testing.

### Failure and cancellation behavior

| Failure | Expected UI/core behavior |
|---|---|
| Model missing/unavailable | Offer installed official-compatible option or safe setup route; do not quietly call a different model in reproducibility mode |
| Analysis stage error | Preserve earlier **verified, immutable cache** if valid, but do not publish a partial SongBundle as complete |
| Very low-confidence lyric/note section | Offer local retry or continue playing with quality warning, avoid blocking the full song |
| Device disconnected | Pause session with explicit reason; reselect device, re-anchor clock and invalidate calibration |
| Repeated seek/restart | Epoch-scoped pitch and score frames dropped; UI returns to authoritative Core position |
| No reliable score available | Allow practice playback with pitch bars where reliable; don't award fictitious grade |
| Disk full / interrupted import | Don't corrupt SongStore; make retry/resume conditions explicit |
| Core restart | Lost in-memory Job not shown as completed; reconnect/reauthenticate via #41, retain persisted songs |

## Acceptance and implementation order

**P1:** Complete actual song registration from arbitrary WAV/MP3/MP4 import (#36); define Core-owned karaoke session; realtime pitch/scoring (#14); real-singing dataset and objective quality gates (#42/#18); measured latency method (#40).

**P2:** Integrate the mixer and device settings (#13), UI playback and current/next lyric / pitch trace (#16), effective model-plan snapshot and honest status (#17), resource isolation (#43), packaging (#39), first-run device preflight. The later editable lyric/note editor (#38) remains optional in the normal flow but a 1.0.0 requirement.

**P3:** Explore multi-ASR, song-global reanalysis or LoRA (#44) only after the simpler objective baselines expose where current models fail.

Other design risks: Japanese-only initial CTC aligner despite multilingual Whisper; a single model cannot assume every supported song language, and fallback must never fabricate timings. Name-based WASAPI device selection cannot reliably survive replug. Evaluate confidence before using it to declare precision; keep the GUI/score consistent with the chosen annotation and analysis snapshot.
