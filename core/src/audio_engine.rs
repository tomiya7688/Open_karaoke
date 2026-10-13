//! Lock-free audio callback core: canonical 48 kHz stereo playback and mono raw mic capture.
//!
//! The real-time callbacks only use atomics and stack storage. Queue writers/readers are
//! serialized outside callbacks; do not invoke the mock render method concurrently with a
//! physical output stream. The 48 kHz clock advances by rendered **frames**, not channels.

use std::{
    array,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering},
    },
};

pub const SAMPLE_RATE: u32 = 48_000;
pub const DEFAULT_BUFFER_FRAMES: usize = 48_000;
const MAX_BUFFER_FRAMES: usize = 480_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AudioError {
    InvalidCapacity,
    NonFiniteSample,
}

impl std::fmt::Display for AudioError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidCapacity => write!(f, "Audio queue capacity must be 32..=480000 frames"),
            Self::NonFiniteSample => write!(f, "Audio samples must be finite"),
        }
    }
}

impl std::error::Error for AudioError {}

/// One-producer, one-consumer frame ring. The publishing store is the head or tail cursor.
/// Cells are atomic, so concurrent playback and producer access has no mutable aliasing.
struct FrameRing<const CHANNELS: usize> {
    frames: usize,
    cells: Box<[AtomicU32]>,
    head: AtomicUsize,
    tail: AtomicUsize,
}

impl<const CHANNELS: usize> FrameRing<CHANNELS> {
    fn new(frames: usize) -> Self {
        let cells = std::iter::repeat_with(|| AtomicU32::new(0))
            .take(frames * CHANNELS)
            .collect();
        Self {
            frames,
            cells,
            head: AtomicUsize::new(0),
            tail: AtomicUsize::new(0),
        }
    }

    fn push(&self, values: [f32; CHANNELS]) -> bool {
        let head = self.head.load(Ordering::Relaxed);
        let tail = self.tail.load(Ordering::Acquire);
        if head.wrapping_sub(tail) >= self.frames {
            return false;
        }
        let offset = (head % self.frames) * CHANNELS;
        for (index, value) in values.into_iter().enumerate() {
            self.cells[offset + index].store(value.to_bits(), Ordering::Relaxed);
        }
        self.head.store(head.wrapping_add(1), Ordering::Release);
        true
    }

    fn pop(&self) -> Option<[f32; CHANNELS]> {
        let tail = self.tail.load(Ordering::Relaxed);
        let head = self.head.load(Ordering::Acquire);
        if tail == head {
            return None;
        }
        let offset = (tail % self.frames) * CHANNELS;
        let frame = array::from_fn(|index| {
            f32::from_bits(self.cells[offset + index].load(Ordering::Relaxed))
        });
        self.tail.store(tail.wrapping_add(1), Ordering::Release);
        Some(frame)
    }

    fn queued(&self) -> usize {
        self.head
            .load(Ordering::Acquire)
            .wrapping_sub(self.tail.load(Ordering::Acquire))
            .min(self.frames)
    }
}

struct Shared {
    output: FrameRing<2>,
    microphone: FrameRing<1>,
    // Neither mutex is acquired in a device callback.
    output_writer: Mutex<()>,
    microphone_reader: Mutex<()>,
    playing: AtomicBool,
    output_position: AtomicU64,
    captured_frames: AtomicU64,
    output_underruns: AtomicU64,
    input_overruns: AtomicU64,
    output_stream_error: AtomicBool,
    input_stream_error: AtomicBool,
}

#[derive(Clone)]
pub struct AudioIo {
    shared: Arc<Shared>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AudioCounters {
    /// Frames emitted to the physical output while playback was enabled (48 kHz).
    pub playback_sample_position: u64,
    pub microphone_frames_received: u64,
    /// Frames filled with silence because the playback queue was empty.
    pub output_underrun_frames: u64,
    /// Raw captured frames discarded because the microphone queue was full.
    pub input_overrun_frames: u64,
    pub output_queued_frames: usize,
    pub input_queued_frames: usize,
    pub output_device_error: bool,
    pub input_device_error: bool,
    pub playing: bool,
}

impl AudioIo {
    /// Allocates bounded rings before a stream is started.
    ///
    /// # Errors
    /// Returns an error for a zero, tiny or excessively large queue.
    pub fn new(queue_capacity_frames: usize) -> Result<Self, AudioError> {
        if !(32..=MAX_BUFFER_FRAMES).contains(&queue_capacity_frames) {
            return Err(AudioError::InvalidCapacity);
        }
        Ok(Self {
            shared: Arc::new(Shared {
                output: FrameRing::new(queue_capacity_frames),
                microphone: FrameRing::new(queue_capacity_frames),
                output_writer: Mutex::new(()),
                microphone_reader: Mutex::new(()),
                playing: AtomicBool::new(false),
                output_position: AtomicU64::new(0),
                captured_frames: AtomicU64::new(0),
                output_underruns: AtomicU64::new(0),
                input_overruns: AtomicU64::new(0),
                output_stream_error: AtomicBool::new(false),
                input_stream_error: AtomicBool::new(false),
            }),
        })
    }

    /// Enqueue 48 kHz, interleaved stereo frames, with backpressure.
    /// Only the number actually accepted is returned. The caller must retry the remainder.
    ///
    /// # Errors
    /// Rejects NaN/infinity instead of publishing invalid PCM into a callback.
    pub fn enqueue_stereo(&self, frames: &[[f32; 2]]) -> Result<usize, AudioError> {
        if frames.iter().flatten().any(|value| !value.is_finite()) {
            return Err(AudioError::NonFiniteSample);
        }
        let _writer = self
            .shared
            .output_writer
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let mut accepted = 0;
        for frame in frames {
            if !self.shared.output.push(*frame) {
                break;
            }
            accepted += 1;
        }
        Ok(accepted)
    }

    /// Fetch raw mono microphone PCM before any future monitoring effects.
    pub fn read_microphone(&self, output: &mut [f32]) -> usize {
        let _reader = self
            .shared
            .microphone_reader
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let mut received = 0;
        for sample in output {
            if let Some([value]) = self.shared.microphone.pop() {
                *sample = value;
                received += 1;
            } else {
                break;
            }
        }
        received
    }

    /// This is the 48 kHz integer sample clock; it includes underrun silence.
    #[must_use]
    pub fn counters(&self) -> AudioCounters {
        AudioCounters {
            playback_sample_position: self.shared.output_position.load(Ordering::Acquire),
            microphone_frames_received: self.shared.captured_frames.load(Ordering::Acquire),
            output_underrun_frames: self.shared.output_underruns.load(Ordering::Relaxed),
            input_overrun_frames: self.shared.input_overruns.load(Ordering::Relaxed),
            output_queued_frames: self.shared.output.queued(),
            input_queued_frames: self.shared.microphone.queued(),
            output_device_error: self.shared.output_stream_error.load(Ordering::Acquire),
            input_device_error: self.shared.input_stream_error.load(Ordering::Acquire),
            playing: self.shared.playing.load(Ordering::Acquire),
        }
    }

    /// Called by the stream controller after a successful CPAL play/pause.
    pub fn set_playing(&self, playing: bool) {
        self.shared.playing.store(playing, Ordering::Release);
    }

    /// Stream callbacks report errors as flags; consumers decide when to rebuild the streams.
    pub fn mark_output_device_error(&self) {
        self.shared
            .output_stream_error
            .store(true, Ordering::Release);
    }

    pub fn mark_input_device_error(&self) {
        self.shared
            .input_stream_error
            .store(true, Ordering::Release);
    }

    /// Deterministic device mock entry point. Hardware callbacks use the same implementation.
    pub fn render_mock(&self, samples: &mut [f32], channels: usize) {
        self.render(samples, channels);
    }

    /// Deterministic device mock entry point for 48 kHz raw input.
    pub fn capture_mock(&self, samples: &[f32], channels: usize) {
        self.capture(samples, channels);
    }

    pub(crate) fn render<S: OutputSample>(&self, samples: &mut [S], channels: usize) {
        if channels == 0 || !self.shared.playing.load(Ordering::Acquire) {
            samples.fill(S::silence());
            return;
        }
        let mut emitted = 0_u64;
        let mut underruns = 0_u64;
        let mut chunks = samples.chunks_exact_mut(channels);
        for frame in chunks.by_ref() {
            let stereo = self.shared.output.pop();
            let [left, right] = stereo.unwrap_or([0.0, 0.0]);
            if stereo.is_none() {
                underruns += 1;
            }
            for (channel, sample) in frame.iter_mut().enumerate() {
                let value = if channels == 1 {
                    (left + right) * 0.5
                } else if channel == 0 {
                    left
                } else if channel == 1 {
                    right
                } else {
                    0.0
                };
                *sample = S::from_normalized(value);
            }
            emitted += 1;
        }
        chunks.into_remainder().fill(S::silence());
        self.shared
            .output_position
            .fetch_add(emitted, Ordering::Release);
        self.shared
            .output_underruns
            .fetch_add(underruns, Ordering::Relaxed);
    }

    #[allow(clippy::cast_precision_loss)]
    pub(crate) fn capture<S: InputSample>(&self, samples: &[S], channels: usize) {
        if channels == 0 {
            return;
        }
        let mut received = 0_u64;
        let mut dropped = 0_u64;
        for frame in samples.chunks_exact(channels) {
            // All channel conversions are pure and allocation-free.
            let sample = frame.iter().copied().map(S::to_normalized).sum::<f32>() / channels as f32;
            if !self.shared.microphone.push([sample]) {
                dropped += 1;
            }
            received += 1;
        }
        self.shared
            .captured_frames
            .fetch_add(received, Ordering::Release);
        self.shared
            .input_overruns
            .fetch_add(dropped, Ordering::Relaxed);
    }
}

impl Default for AudioIo {
    fn default() -> Self {
        // DEFAULT_BUFFER_FRAMES is a validated constant.
        Self::new(DEFAULT_BUFFER_FRAMES).expect("valid static audio buffer capacity")
    }
}

pub(crate) trait OutputSample: Copy {
    fn silence() -> Self;
    fn from_normalized(value: f32) -> Self;
}

pub(crate) trait InputSample: Copy {
    fn to_normalized(self) -> f32;
}

impl OutputSample for f32 {
    fn silence() -> Self {
        0.0
    }
    fn from_normalized(value: f32) -> Self {
        value.clamp(-1.0, 1.0)
    }
}
impl InputSample for f32 {
    fn to_normalized(self) -> f32 {
        self
    }
}
impl OutputSample for i16 {
    fn silence() -> Self {
        0
    }
    #[allow(clippy::cast_possible_truncation)]
    fn from_normalized(value: f32) -> Self {
        (value.clamp(-1.0, 1.0) * 32767.0).round() as i16
    }
}
impl InputSample for i16 {
    fn to_normalized(self) -> f32 {
        f32::from(self) / 32768.0
    }
}
impl OutputSample for u16 {
    fn silence() -> Self {
        32768
    }
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    fn from_normalized(value: f32) -> Self {
        ((value.clamp(-1.0, 1.0) + 1.0) * 32767.5).round() as u16
    }
}
impl InputSample for u16 {
    fn to_normalized(self) -> f32 {
        (f32::from(self) - 32768.0) / 32768.0
    }
}

#[cfg(test)]
mod tests {
    use super::{AudioError, AudioIo, SAMPLE_RATE};

    #[test]
    fn playback_clock_counts_frames_and_outputs_silence_when_empty() {
        let io = AudioIo::new(64).unwrap();
        let frames = [[0.2, -0.2]; 3];
        assert_eq!(io.enqueue_stereo(&frames).unwrap(), 3);
        let mut paused = [1.0_f32; 6];
        io.render_mock(&mut paused, 2);
        assert_eq!(paused, [0.0; 6]);
        assert_eq!(io.counters().playback_sample_position, 0);

        io.set_playing(true);
        let mut output = [0.0_f32; 10];
        io.render_mock(&mut output, 2);
        assert_eq!(&output[..6], &[0.2, -0.2, 0.2, -0.2, 0.2, -0.2]);
        assert_eq!(&output[6..], &[0.0; 4]);
        assert_eq!(io.counters().playback_sample_position, 5);
        assert_eq!(io.counters().output_underrun_frames, 2);
        io.set_playing(false);
        io.render_mock(&mut output, 2);
        assert_eq!(io.counters().playback_sample_position, 5);
    }

    #[test]
    fn input_capture_converts_channels_and_reports_overflow() {
        let io = AudioIo::new(32).unwrap();
        io.capture_mock(&[0.7, 0.3, -0.4, -0.2], 2);
        let mut out = [0.0; 2];
        assert_eq!(io.read_microphone(&mut out), 2);
        assert!((out[0] - 0.5).abs() < 1e-6);
        assert!((out[1] + 0.3).abs() < 1e-6);
        io.capture_mock(&[0.25; 64], 1);
        assert_eq!(io.counters().microphone_frames_received, 66);
        assert_eq!(io.counters().input_overrun_frames, 32);
        assert_eq!(io.counters().input_queued_frames, 32);
    }

    #[test]
    fn queue_backpressure_rejects_non_finite_data_atomically() {
        let io = AudioIo::new(32).unwrap();
        assert_eq!(io.enqueue_stereo(&[[1.0, 1.0]; 40]).unwrap(), 32);
        let mut output = [0.0; 64];
        io.set_playing(true);
        io.render_mock(&mut output, 2);
        assert_eq!(io.counters().output_underrun_frames, 0);
        assert!(matches!(
            io.enqueue_stereo(&[[0.0, f32::NAN]]),
            Err(AudioError::NonFiniteSample)
        ));
        assert_eq!(io.counters().output_queued_frames, 0);
    }

    #[test]
    fn converts_signed_and_unsigned_pcm_without_crossing_streams() {
        let io = AudioIo::new(32).unwrap();
        io.enqueue_stereo(&[[-1.0, 1.0]]).unwrap();
        io.set_playing(true);
        let mut signed = [0_i16; 2];
        io.render(&mut signed, 2);
        assert_eq!(signed, [-32767, 32767]);
        io.capture(&[0_u16, 65535_u16], 2);
        let mut mic = [0.0_f32];
        assert_eq!(io.read_microphone(&mut mic), 1);
        assert!(mic[0].abs() < 0.0001);
        assert_eq!(io.counters().playback_sample_position, 1);
    }

    #[test]
    fn loopback_clock_and_raw_microphone_align_on_48khz_timeline() {
        let io = AudioIo::new(1024).unwrap();
        io.set_playing(true);
        for block in 0..100 {
            let frames = [[0.35, 0.15]; 480];
            assert_eq!(io.enqueue_stereo(&frames).unwrap(), 480);
            let mut output = [0.0; 960];
            io.render_mock(&mut output, 2);
            assert!(output.chunks_exact(2).all(|v| v == [0.35, 0.15]));
            let raw = [0.4_f32; 480];
            io.capture_mock(&raw, 1);
            let mut result = [0.0; 480];
            assert_eq!(io.read_microphone(&mut result), 480);
            assert!(result.iter().all(|sample| *sample == 0.4));
            assert_eq!(io.counters().playback_sample_position, (block + 1) * 480);
        }
        assert_eq!(
            io.counters().playback_sample_position,
            u64::from(SAMPLE_RATE)
        );
        assert_eq!(io.counters().output_underrun_frames, 0);
        assert_eq!(io.counters().input_overrun_frames, 0);
    }

    #[test]
    fn simulated_long_run_and_device_disconnection_are_observable() {
        let io = AudioIo::new(2048).unwrap();
        io.set_playing(true);
        for _ in 0..1000 {
            assert_eq!(io.enqueue_stereo(&[[0.0, 0.0]; 480]).unwrap(), 480);
            io.render_mock(&mut [0.0_f32; 960], 2);
        }
        assert_eq!(io.counters().playback_sample_position, 480_000);
        assert_eq!(io.counters().output_underrun_frames, 0);
        io.mark_output_device_error();
        io.mark_input_device_error();
        assert!(io.counters().output_device_error);
        assert!(io.counters().input_device_error);
    }
}
