# Audio clock / latency contract (Issue #40)

## Position and status

This is the **first contract and hardware-independent baseline**, not a completed CPAL or
WASAPI implementation. The Rust reference mapping lives in `core/src/audio_clock.rs`;
regressions are in `core/tests/audio_clock.rs`. Its arithmetic has no audio device
dependencies, allocations, locks, external clock reads, or Python/model calls.

The audio engine (#12), real-time scoring (#14), GUI (#16), and Windows device tests still
need to connect their actual timestamps and calibration UI to this contract. A successful
synthetic test never means that end-to-end monitoring or singing latency was measured.

## Four kinds of time

| Quantity | Type and owner | Meaning |
|---|---|---|
| Song sample | unsigned 64-bit integer, 48,000 Hz | Canonical seek/note/lyric position; valid at `[start, end)` |
| Output stream sample | integer 48 kHz *logical* counter | Frames delivered to the audio output backend; **not the time sound is heard** |
| Capture stream sample | integer 48 kHz *logical* counter | Frames delivered by a microphone backend; **not the time the sound was made** |
| Host monotonic time | platform monotonic clock, future #12 integration | Used to establish simultaneous anchors and estimate drift; never wall clock, local date, or NTP |

A device may run at 44.1 kHz or a different physical rate. Its native frames must be
transformed at the backend boundary into the **logical 48 kHz** stream counter. Native
clock positions and callback timestamps must be recorded separately where supported.
Do not interpret the logical stream counter as the actual hardware clock.

The UI obtains a *presentation* position from the Rust Core based on an output timestamp
plus a measured/estimated output path latency, **not by equating the callback-submitted
sample count with audible sample count**. The GUI must not derive its independent clock
from frame render intervals. The scoring branch uses the raw, pre-effect microphone
signal, maps it through its own capture anchor, and compares against the *same* song
timeline. DSP monitoring uses a separate path and must never feed reverb into F0.

## Mapping and drift

For a single stream domain and a single transport epoch:

```text
delta  = stream_sample - anchor.stream_sample
scaled = round_to_nearest_away_from_zero(
           delta * (1_000_000 + drift_ppm) / 1_000_000)
song_sample = anchor.song_sample + scaled + compensation_samples
```

All values are computed with signed 128-bit intermediate integers before checking
conversion to the canonical unsigned 64-bit song sample. `drift_ppm > 0` means that
song time advances faster than the logical source frame counter. The first implementation
allows `-2000 <= drift_ppm <= 2000` as an **input sanity guard**, not as a hardware
accuracy or oscillator-drift tolerance specification.

`compensation_samples` is signed. Positive moves the observed stream event
**later** in song time; negative moves it **earlier**. For example a measured
`-2_400` correction shifts a capture event earlier by 50 ms. This direction is
recorded explicitly; don't invert it again in the scoring layer.

Mappings are tagged `Output` or `Capture`. Each domain has an independent anchor.
The anchor's `stream_sample` and `song_sample` are assumed to represent the same
chosen time reference. The mapper itself does **not** measure when this happened or
derive a valid simultaneous anchor from unrelated device counters.

Negative results before the song start produce `BeforeSongStart`, not a clamped
sample zero. This means *pre-roll / do not score*, not a lost time position. Larger than
`u64::MAX` yields `ArithmeticOverflow`, never wraparound.

## Calibration and its limits

A mapping carries explicit quality state:

- `Unknown`: no measured correction. Arithmetic uses zero adjustment for its
  nominal mapping only; **this does not claim zero physical latency**.
- `Estimated { compensation_samples }`: OS/device estimate or user-provided
  nominal value. **Not hardware-verified**.
- `Measured { compensation_samples, uncertainty_samples }`: calibration performed
  using a defined method and a recorded uncertainty. This type is a carrier for
  measured evidence, not a measurement routine.

Input capture, monitor and output presentation have **different** delays:

```text
mic acoustic event -> capture ADC/buffer -> raw F0/scoring
                                    \-> effects -> mixer/output -> heard monitor
song PCM -> output buffer/DAC -> heard accompaniment
```

The physical monitor loopback usually measures a **round-trip** delay (input + DSP
+ output + acoustic propagation). It does not identify separate input and output
one-way latencies without additional reference measurements. Similarly, a person
singing after a metronome click includes human reaction; that is not hardware-only
latency. Record method, path, device identity, driver mode, sample rate, buffer
settings, versions, measurements and sample uncertainty with any persisted calibration.
For score offset vs monitoring latency, do **not** use the same number implicitly.

Candidate device-calibration key:

```text
(device identifiers, input and output selection, backend/WASAPI mode,
 native rates, physical buffer layout, monitoring path, DSP latency version)
```

A mismatch invalidates the calibration for scoring; the Core should continue
in best-effort/uncalibrated mode rather than claiming accurate measurements.

## Epoch and transport discontinuities

One `ClockSession` owns both mappings for a `ClockEpoch`. New epoch means
**both anchors are discarded**, and old-epoch frames are rejected.

Begin a new epoch on:

- seek or loop boundary
- pause/resume with stream discontinuity
- output device restart, capture device restart, device hot-plug
- underrun/overrun where stream time continuity cannot be guaranteed
- sample-rate/buffer renegotiation or non-monotonic callback timestamps

Drop queued frames of the previous epoch in consumers (pitch/score/render).
After re-anchoring, display/score updates may resume. Don't interpolate blindly
across a discontinuity. A stream with continuous frames and a smoothly updated
drift estimate may remain in its epoch; replacing an active anchor otherwise
requires a new epoch.

A Core timestamp event eventually exposed to GUI should carry at least
`epoch`, `song_sample`, source domain, calibration quality, and diagnostics
for stale/uncalibrated data. #41 defines secure/versioned API publication.

## Intended realtime callback design

The mapper is pure integer arithmetic; it does no memory allocation, locking,
logging, I/O, model inference or network operations. Its `ClockSession` is **not
thread-safe shared state**. Publish an immutable mapping snapshot into the audio
worker through a nonblocking message or atomically swapped state at epoch changes.
Configuration or calibration acquisition happens on a non-audio thread.

Playback/device/input counters are updated by #12. The callback passes read-only
timing data through preallocated/ring-buffer channels; F0, scoring, smoothing and
GUI emission operate on separate workers without blocking the callback.

## Regression and hardware evaluation

Run the deterministic unit/contract tests:

```powershell
cargo test --locked -p open-karaoke-core --test audio_clock
```

This test is included in the existing Rust `--all-targets` CI. It covers:

- unknown vs measured latency metadata, signed corrections and pre-roll
- ±100 ppm simulated drift across a 10-second window
- output vs capture independent domains
- seek/reset epoch invalidation and stale-frame rejection
- invalid drift, double-binding, overflow

The following **physical** checks are required before Issue #40 is fully accepted
and prior to 1.0.0; they are not automated by the current code:

1. Record at least 30 loopback impulses across WASAPI shared/exclusive and at
   least two supported device/buffer configurations; separate known acoustic
   propagation from round-trip measurement where feasible.
2. Report median, p95, standard deviation (both milliseconds and 48k samples),
   the measurement path, device/model IDs, software versions and raw observations.
3. Run 30+ minute drift trial with simultaneous monotonic timestamps from
   capture/output domains; record estimated ppm and residual error vs elapsed time.
4. Re-run after device unplug/replug, seek, pause/resume, input switch, under/overrun.
   Assert no cross-epoch frame is included in scoring and that an invalid calibration
   becomes Unknown or Estimated.
5. Verify audio callback underrun counts and that Python GPU/CPU inference can't
   block the callback, together with #43.

No numerical latency target or calibrated p95 claim is made until representative
hardware has been measured. Product decisions about acceptable latency and device
profiles belong to #12, #14, #19 and the release evaluator.
