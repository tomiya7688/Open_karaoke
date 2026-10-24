# Rust audio engine — Issue #12

## Current scope and target

The first hardware backend is Windows **WASAPI shared mode** through
[CPAL 0.16.0](https://docs.rs/cpal/0.16.0/cpal/). Its source license is Apache-2.0.
CPAL is a Windows-targeted dependency in core/Cargo.toml: the Linux CI build tests
the portable sample engine without requiring ALSA development packages. Native
Linux capture/playback and WASAPI exclusive mode are **not implemented**.

The service and UI never call Python from audio callbacks. The Rust engine can be tested,
instantiated and used without the GUI or Analysis Service.

## Core interfaces

- core/src/audio_engine.rs defines AudioIo, AudioCounters and a bounded SPSC ring.
- core/src/audio_device.rs enumerates WASAPI devices and opens CPAL input/output streams.
- core/src/audio_wav.rs parses and feeds canonical WAV on a worker thread.
- GET /devices returns a fresh enumeration snapshot on Windows. It returns 503 on platforms
  where this production backend is not implemented.
- core/examples/wasapi_smoke.rs is an opt-in **manual physical-device test**.

Only the producer side of the stereo playback queue acquires a mutex (to serialize
ordinary control-thread writers), and only the consumer side of the raw microphone queue
acquires a mutex (to serialize ordinary readers). The physical callbacks call neither
mutex. They only load/store atomics, transform samples and move frames through preallocated
memory. They do not allocate, sleep, log or do file I/O.

The ring buffers are bounded to 32–480000 frames. Playback enqueues 48 kHz stereo
f32 frames; the return value is the number accepted, and the caller retries the remainder.
The queue rejects NaN/infinity. Device-specific f32/i16/u16 conversion is limited to the
backend boundary. Microphone capture is downmixed to raw mono f32, independently of
playback and ahead of any later monitoring effects.

Every queued playback frame is tagged with its `ClockEpoch`; `enqueue_stereo_at` also
tags the source `song_sample` and rejects a stale producer message. The original
`enqueue_stereo` keeps unpositioned playback available and never invents a song time.
The output callback consumes at
most one queued frame per output frame; stale frames become silence and are discarded,
so epoch changes never make the callback scan an unbounded old queue. Ring occupancy
includes stale slots until the output callback drains them. `playback_timestamp()` returns
the last real PCM presentation as `(epoch, output_sample, song_sample)`; a reader must
compare its epoch with the current `ClockSession`, because the last presentation can
belong to the preceding epoch while the output is silent. An unpositioned frame clears
the presentation snapshot instead of reporting an incorrect song position.

Capture callbacks tag each frame with the active epoch and a monotonic 48 kHz input
position. Queue overflow still advances the capture position, so the next delivered
frame exposes the gap. `read_microphone_timestamped` drops stale frames on the non-realtime
reader and returns timestamped raw mono samples. The original `read_microphone` remains
available for raw mono clients, but does not expose timing. After `ClockSession` advances, the process owner
must send the returned epoch to `AudioIo::set_clock_epoch` and cancel/recreate old
producers at the desired song position. The WAV feeder marks a stale epoch enqueue as
failed; automatic seek/restart orchestration is not provided by this layer.

### Clock

AudioCounters.playback_sample_position is a u64 **48 kHz frame index** incremented
inside the output callback, while playback is active. It counts rendered silence on
underrun, so it is an output timeline, **not** proof of audibility or an
acoustic hardware/loopback timestamp. One stereo frame is one sample-index tick,
not two. Paused callbacks do not advance the clock.

Counters also track microphone frames received, playback underrun frames, input queue
overflow frames, and queue occupancy. Stream error callbacks set persistent output/input
error flags; a controller can enumerate devices again and rebuild the streams. The
engine intentionally does not auto-switch devices without user consent or silently replay
queued audio after hot-plug. A new engine instance starts a new clock.

### WAV playback

WavFeeder::start(audio, path) accepts normalized RIFF/WAVE stereo IEEE float32 48 kHz
PCM (including WAVEFORMATEXTENSIBLE f32). It validates headers and duration,
and reads in bounded blocks on a separate thread. The feeder reports
duration_samples, enqueued_samples, finished and failed.

It does not decode original MP3/AAC itself; these must go through the existing
48 kHz normalization flow. This initial reader does not accept RF64 or WAV files with
multiple data chunks. The feeder is cancelled/joined by its owner; it is not used in a
callback. Playback remains a separate explicit play/pause operation.

### Device selection / latency

Device enumeration reports display names, input/output direction, default labels,
48 kHz PCM support and host. Names are **not stable identifiers**; duplicate names are
rejected on selection. A future device-ID/settings implementation should replace name-based
selection, particularly for hot-plug recovery.

The backend chooses a supported 48 kHz configuration with f32 preferred, then i16/u16,
and a default device buffer size. If no supported 48 kHz mode exists it fails visibly,
rather than silently resampling inside the callback. Output mixes mono/stereo device
channels from the canonical stereo feed; the extra channels, if present, carry silence.

WASAPI shared mode is the product default: it works with normal system audio routing
and other applications. **Exclusive mode is deliberately rejected** until low-level
WASAPI buffer negotiation, recovery, device ownership, and latency testing are available.
CPAL does not expose an equivalent cross-platform exclusive-mode flag in the chosen
version. Playback/input latency compensation and echo/reverb are Issue #13.

## Validation

Portable/deterministic tests:

- queue order, backpressure and nonfinite validation
- f32/i16/u16 PCM transformation
- active and paused sample clock, silence/underrun counters
- raw multi-channel microphone capture and overflow counters
- 48 kHz simulated loopback synchronization
- 10-second simulated streaming stability
- WAV header parsing and disk-feeder smoke tests
- error flags and explicit recovery boundary

Run:

    cargo fmt --all -- --check
    cargo clippy --locked --workspace --all-targets -- -D warnings
    cargo test --locked --workspace --all-targets

The audio-engine GitHub workflow additionally runs the focused mock tests on Linux
and compiles/runs the backend tests on Windows. It fails if Cargo.lock is stale.

### Manual Windows hardware test (required before claiming actual device quality)

On Windows with a 48 kHz-capable speaker and microphone connected, first normalize
a test audio file to a 48 kHz stereo float32 WAV using the existing Core import flow.

    cargo run --locked -p open-karaoke-core --example wasapi_smoke -- path\to\accompaniment.wav

The example prints device enumeration, plays the provided accompaniment while
capturing raw microphone samples, and reports clock/underrun/overrun/error counters.
Verify by listening, physically recording (with consent), testing unplug/replug, device
switching, silence, pause, and a sustained session. Separately measure microphone-to-
output loopback latency and jitter using a physical test rig.

CI runners do not guarantee microphones/speakers. The automated loopback fixture
uses mock data, not WASAPI hardware. As of Issue #12, actual physical audible playback,
mic capture, latency, hot-plug recovery and long-running hardware stability remain
**Unverified**, not passing quality claims.
