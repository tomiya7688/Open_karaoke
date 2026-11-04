# Karaoke session integration design

> Status: **proposed design, not an implemented playback API**. Source review: `main` `907d166b2ca0bcfbaf739593a53004d9e0eef02e` (2026-11-04). Issue #106.
>
> See [Audio Engine](audio_engine.md), [Clock](audio_clock.md), [Scoring](scoring.md), [Desktop Client](desktop_client.md). PR #95 may change API reconnection/versioning; do not assume its unmerged changes are present.

## Why this contract is needed

Today, Rust has CPAL/WASAPI streams, a bounded audio queue, WAV feeder, clock/epoch mapping, and an independent mixer. Python can generate a song bundle and Rust can import it. Avalonia can fetch that bundle and draw a **static position-selected** lyric/pitch view. However, no one component yet owns **start song → audible playback + raw mic → realtime pitch/scoring → UI positions → result**.

**The missing owner is a Rust `KaraokeSessionController`.** It should coordinate existing parts via *composition*, not by making UI, audio device, or model Adapter classes inherit behavior from each other.

## Responsibility and ownership

| Module / object | Owns | Must not own |
|---|---|---|
| `KaraokeSessionController` (new Rust service) | Session state machine, `SongBundle`/asset validation, device lease, current transport epoch, worker lifetimes, start/pause/seek/stop, diagnostics and event publication | Physical callback sample conversions; Python inference; GUI rendering |
| `AudioDeviceStreams` + `AudioIo` (existing Rust) | Hardware stream, bounded queues, callback I/O, tagged playback/capture frames and counters | Song selection, grading, persistence |
| `ClockSession` (existing Rust) | 48 kHz integer `ClockEpoch`, separate output/capture anchors, latency quality, discontinuity detection | Device measurement or an independent GUI clock |
| `WavFeeder` (existing Rust) | Read normalized accompaniment on a bounded worker and enqueue tagged frames | Score computation |
| `Mixer` (existing Rust) | Gain, echo/reverb, monitor mixing into an output block; preallocation of DSP state | Raw microphone score samples; network/filesystem I/O |
| `RealtimePitchDetector` (proposed Rust trait) | Analyze bounded **raw pre-effect microphone** frames and return timestamped F0/voicing evidence | Device callbacks or offline Python model execution |
| `ScoringEngine` (proposed Rust struct) | Assign pitch observations to target notes, calculate independent metrics, persist versioned results | Model selection/download, GUI state |
| Avalonia `KaraokeViewModel` (existing) | Display current/next lyric, pitch bars, scores and actionable errors from Core snapshots | Audio clock, device selection implementation, note grading |
| Python Analysis Service (existing) | Offline song generation, optional reanalysis, provenance | Live audio callback or runtime grading |

Only one controller holds the active output-device lease in v1. A second user-facing session must be rejected or queued explicitly; it must never silently mix two songs.

### Proposed real-time topology

```text
Verified SongStore assets ─> WavFeeder ─> tagged Music PCM ────────────┐
                                                                     v
physical microphone ─> AudioIo raw queue ─┬─> realtime pitch worker    Mixer ─> device output
                                          │          │                  ^
                                          │          v                  │
                                          └──────> monitor branch ──────┘
                                                (gain/echo/reverb)
                         Score notes ─> ScoringEngine <─ timestamped F0
                                                │
                  Core state + clock + score events (bounded / SSE)
                                                │
                                         Avalonia renderer
```

The exact route from raw microphone frames into the playback mixer must be explicitly designed for **one stream clock domain**. A callback must not wait for a mutex, Python model, HTTP response, dynamic allocation, or disk access. If input/output devices have different clocks, use bounded queues with drift-compensated resampling on a **noncallback** worker. On underrun, emit diagnostics and silence; don't duplicate frames as if continuous.

## Session state machine

| State | Allowed commands/events | Contract |
|---|---|---|
| `preparing` | validation succeeds → `ready`; failure → `failed`; cancel → `stopped` | Verify stored song, backing WAV, note/lyric versions and model-provenance references before owning devices |
| `ready` | `play`, `seek`, `stop` | No score recorded. Input/output devices and latency status visible |
| `playing` | `pause`, `seek`, `stop`, end-of-song | Output position comes from Core presentation timestamp, **not** number of frames queued |
| `paused` | `resume`, `seek`, `stop` | Samples/score don't advance while paused; resume may require fresh epoch |
| `interrupted` | device reselect/recalibration → `ready` or `paused`; `stop` | Device removed, clock discontinuity, unrecoverable stream overrun; no hidden auto-switch |
| `finished` / `stopped` / `failed` | read final state, release resources | Terminal for this session, no stale events or duplicate final score |

A session state revision and `instance_id` accompany updates. `play` and `stop` should be idempotent; repeated/reordered commands must not create duplicate streams or grades. Termination cleans workers, queue ownership, and stream handles on every error path.

**Seek policy:** Increment the clock epoch, invalidate buffered playback/capture, reanchor and suppress scoring until aligned. A seek makes the current run a **practice attempt** or starts a new scoring segment; do not double-count already scored notes. A final, comparable whole-song score requires uninterrupted coverage or an explicitly defined policy for practice-segment aggregation. UI must show this distinction.

**Crash/restart policy:** Core process restart changes `instance_id`. Old sessions and in-memory Job IDs are not resurrected. GUI must rebootstrap/reauthenticate and offer to re-open a saved song; never resubmit a singing attempt automatically. Coordinate with PR #95.

## Proposed Core API boundary (not existing routes)

Do not route GUI directly to `AudioIo` or to Python's private port.

```text
POST /karaoke/sessions             choose persisted song + device profile
GET  /karaoke/sessions/{id}        authoritative state + position
POST /karaoke/sessions/{id}/play
POST /karaoke/sessions/{id}/pause
POST /karaoke/sessions/{id}/seek   integer song_sample + expected_epoch
POST /karaoke/sessions/{id}/stop
GET  /karaoke/sessions/{id}/events  bounded SSE state/pitch/score
```

Endpoints above are **conceptual**; #41 controls the final Core API version and client migration. Every mutating message must verify the authenticated owner, current session revision/epoch (where relevant), and bounded payload size. SSE must have bounded consumers/backpressure; loss of UI subscription cannot stop the audio callback.

A proposed snapshot (illustrative **not current API**):

```json
{
  "session_id": "<uuid>",
  "core_instance_id": "<uuid>",
  "state": "playing",
  "revision": 3,
  "song_id": "<uuid>",
  "sample_rate": 48000,
  "epoch": 2,
  "presented_song_sample": 128000,
  "clock_quality": "estimated",
  "scoring_mode": "practice",
  "audio": {
    "output_underrun_frames": 0,
    "input_overrun_frames": 0
  }
}
```

All persistent song/note/lyric times remain **u64 sample indices** at 48 kHz; JSON times expressed as seconds are display-only. Clients discard events with old `core_instance_id`/`session_id`/`epoch` or lower revisions.

## Clock and observability rules

- **Output counter is not acoustic presentation time.** Compute song presentation from hardware measurements or explicitly marked estimated correction; if unknown, report `clock_quality: unknown`.
- Raw capture frames carry `epoch`, `stream_sample`, valid/invalid and discontinuity indicators. Clock mapping uses output/capture anchors separately; do not reuse echo latency for scoring.
- Score observations from stale epochs or input-frame gaps are **excluded and reported**, not treated as missed singing. Record the count and duration of excluded samples.
- An OS-provided timestamp is not automatically a calibrated acoustic delay. 30+ minute drift and physical loopback p95 belong to #40/#12/#19.
- The UI may interpolate smoothly between **validated** Core snapshots with a bounded, locally monotonic display estimator, but the estimator is never the scoring truth. Do not compare GUI-host and Core-host monotonic timestamps as if they had the same origin.

## Real-time pitch and scoring contract

This is a **target** for #14; the offline `pitch.py` ensemble is not a realtime microphone implementation.

1. The realtime worker consumes timestamped raw mono 48 kHz PCM with bounded latency. A lightweight input-health stage detects silence, saturation, noise, gaps and low-energy frames; it must not silently invent an F0 on unvoiced inputs.
2. A replaceable Rust `RealtimePitchDetector` returns finite `f0_hz | null`, `voiced`, diagnostic `confidence` and `voiced_probability`, the observation window `[start_sample,end_sample)`, `epoch`, algorithm/version, and rejection reason. These values are **not automatically calibrated accuracy probabilities**.
3. `ScoringEngine` maps the observation's measured/estimated **acoustic** time into the canonical song timeline, then matches the normalized target notes. At note boundaries use a documented transition allowance, not unlimited lag forgiveness.
4. Preserve independent `pitch`, `timing`, `coverage`, `stability` metrics and a mask of invalid/uncalibrated intervals. For voiced observations with valid target note, define signed cent error as `1200 * log2(f0_hz / target_hz)`. Refuse nonfinite/zero pitch.
5. Realtime indicators are **provisional** and use causal (bounded-lookahead) smoothing. Optional post-song DTW may correct a bounded singer timing variation, but its warp budget and version must be fixed; unlimited DTW would make timing mistakes look correct.
6. Final score includes `score_version`, `note_chart_version`, `detector_version`, `latency_calibration_id`, `unscored_reason_counts`, and confidence/coverage of usable data. Without calibrated human ground truth, do not advertise a scientifically validated 0–100 result.

No score input flows from the effect-processed monitoring branch. If the chart cannot be trusted or hardware timing is unknown, keep a playable practice mode and mark grading as provisional or unavailable.

## Validation matrix / exit criteria

| Test | Automated expectation | Not proved by it |
|---|---|---|
| State machine contract | Play/pause/seek/stop, repeated requests, Core restart, stale event handling | Physical device stability |
| Clock/epoch | Simulated ±drift, negative offsets, queue gaps, seek/device change and invalid timestamps | Measured acoustic latency |
| Audio/Mixer | No allocation/lock in callback, bounded queues, device outage/overflow, raw mic not effected | Singing accuracy |
| Scoring | Artificial ±cents, offsets, silence, octave errors and device gaps; monotonic metrics; score version | Human judged quality |
| GUI integration | UI follows only Core authoritative position, stale epochs discarded, large song pages, zoom/keyboard/high DPI smoke | Real monitor/presentation synchronization |
| Physical release | Windows WASAPI devices, measured loopback median/p95, drift, mic/speaker switching, long playback | Claims on untested devices |

A useful **first vertical slice** is to render a manually supplied MIDI chart over a real WAV and display timestamped mic pitch in practice mode, without requiring high-quality automatic note generation. Then attach scoring and the full-song analysis import flow.

## Dependencies and sequencing

P1: #40 clock measurement, #41 API, #14 realtime pitch/scoring, #36 Core import, #42 real-singing set. P2: #13 mixer connection, #16 GUI, #43 resource isolation, #39 Windows packaging. Keep #38 optional editor as later work; keep #44 advanced ASR research after objective baselines.
