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

use crate::audio_clock::ClockEpoch;

pub const SAMPLE_RATE: u32 = 48_000;
pub const DEFAULT_BUFFER_FRAMES: usize = 48_000;
const MAX_BUFFER_FRAMES: usize = 480_000;
const UNKNOWN_SONG_SAMPLE: u64 = u64::MAX;

/// {
///   責務: [`AudioError`: 音声境界で拒否した入力と状態を呼び出し元へ伝える]
///   フィールド: []
/// }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AudioError {
    InvalidCapacity,
    NonFiniteSample,
    StaleEpoch,
    SamplePositionOverflow,
}

impl std::fmt::Display for AudioError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidCapacity => write!(f, "Audio queue capacity must be 32..=480000 frames"),
            Self::NonFiniteSample => write!(f, "Audio samples must be finite"),
            Self::StaleEpoch => write!(f, "Audio data belongs to a stale clock epoch"),
            Self::SamplePositionOverflow => write!(f, "Audio sample position overflowed"),
        }
    }
}

impl std::error::Error for AudioError {}

/// {
///   責務: [`QueuedFrame`: ringを通過するPCMと時刻metadataをまとめる]
///   フィールド: [`epoch`: transport世代, `sample_position`: songまたはcapture位置, `samples`: 固定channel PCM]
/// }
struct QueuedFrame<const CHANNELS: usize> {
    epoch: u64,
    sample_position: u64,
    samples: [f32; CHANNELS],
}

/// {
///   責務: [`FrameRing`: callbackと制御側の間で固定容量の`PCM`と時刻情報を受け渡す]
///   フィールド: [frames: 容量, cells: PCM, epochs: transport世代, positions: sample位置, head: 書込位置, tail: 読取位置]
/// }
struct FrameRing<const CHANNELS: usize> {
    frames: usize,
    cells: Box<[AtomicU32]>,
    epochs: Box<[AtomicU64]>,
    positions: Box<[AtomicU64]>,
    head: AtomicUsize,
    tail: AtomicUsize,
}

impl<const CHANNELS: usize> FrameRing<CHANNELS> {
    /// {
    ///   責務: [`new`: 固定容量の`PCM`と時刻情報の領域を作る]
    ///   処理: [1: `PCM`と世代と位置の領域を確保する, 2: 読み書き位置を初期化する]
    ///   引数: [frames: queue容量]
    ///   戻り値: [`FrameRing`: 初期化されたring]
    /// }
    fn new(frames: usize) -> Self {
        let cells = std::iter::repeat_with(|| AtomicU32::new(0))
            .take(frames * CHANNELS)
            .collect();
        Self {
            frames,
            cells,
            epochs: std::iter::repeat_with(|| AtomicU64::new(0))
                .take(frames)
                .collect(),
            positions: std::iter::repeat_with(|| AtomicU64::new(0))
                .take(frames)
                .collect(),
            head: AtomicUsize::new(0),
            tail: AtomicUsize::new(0),
        }
    }

    /// {
    ///   責務: [push: 一つの時刻付きPCM frameをproducerから公開する]
    ///   処理: [1: 容量を確認する, 2: `PCM`と時刻情報を書き込む, 3: `head`を公開する]
    ///   引数: [`epoch`: transport世代, `sample_position`: 48 kHz位置, `values`: channel別`PCM`]
    ///   戻り値: [bool: 書込成功]
    /// }
    fn push(&self, epoch: u64, sample_position: u64, values: [f32; CHANNELS]) -> bool {
        let head = self.head.load(Ordering::Relaxed);
        let tail = self.tail.load(Ordering::Acquire);
        if head.wrapping_sub(tail) >= self.frames {
            return false;
        }
        let offset = (head % self.frames) * CHANNELS;
        for (index, value) in values.into_iter().enumerate() {
            self.cells[offset + index].store(value.to_bits(), Ordering::Relaxed);
        }
        let slot = head % self.frames;
        self.epochs[slot].store(epoch, Ordering::Relaxed);
        self.positions[slot].store(sample_position, Ordering::Relaxed);
        self.head.store(head.wrapping_add(1), Ordering::Release);
        true
    }

    /// {
    ///   責務: [pop: 一つの時刻付きPCM frameをconsumerへ渡す]
    ///   処理: [1: `head`を確認する, 2: `PCM`と時刻情報を読む, 3: `tail`を公開する]
    ///   引数: []
    ///   戻り値: [Option<QueuedFrame>: queueが空ならNone]
    /// }
    fn pop(&self) -> Option<QueuedFrame<CHANNELS>> {
        let tail = self.tail.load(Ordering::Relaxed);
        let head = self.head.load(Ordering::Acquire);
        if tail == head {
            return None;
        }
        let offset = (tail % self.frames) * CHANNELS;
        let slot = tail % self.frames;
        let frame = array::from_fn(|index| {
            f32::from_bits(self.cells[offset + index].load(Ordering::Relaxed))
        });
        let epoch = self.epochs[slot].load(Ordering::Relaxed);
        let sample_position = self.positions[slot].load(Ordering::Relaxed);
        self.tail.store(tail.wrapping_add(1), Ordering::Release);
        Some(QueuedFrame {
            epoch,
            sample_position,
            samples: frame,
        })
    }

    fn queued(&self) -> usize {
        self.head
            .load(Ordering::Acquire)
            .wrapping_sub(self.tail.load(Ordering::Acquire))
            .min(self.frames)
    }
}

/// {
///   責務: [Shared: AudioIoのqueue、clock世代、診断counterを共有する]
///   フィールド: [`output`: 再生queue, `microphone`: capture queue, `clock_epoch`: 現世代, `presentation_sequence`: 時刻snapshotの整合番号]
/// }
struct Shared {
    output: FrameRing<2>,
    microphone: FrameRing<1>,
    // Neither mutex is acquired in a device callback.
    output_writer: Mutex<()>,
    microphone_reader: Mutex<()>,
    clock_epoch: AtomicU64,
    playing: AtomicBool,
    output_position: AtomicU64,
    presentation_sequence: AtomicU64,
    presentation_epoch: AtomicU64,
    presentation_output_sample: AtomicU64,
    presentation_song_sample: AtomicU64,
    captured_frames: AtomicU64,
    output_underruns: AtomicU64,
    input_overruns: AtomicU64,
    output_stream_error: AtomicBool,
    input_stream_error: AtomicBool,
}

/// {
///   責務: [`AudioIo`: 48 kHz再生とraw captureのqueueとepoch境界を所有する]
///   フィールド: [shared: callbackと制御側が共有する状態]
/// }
#[derive(Clone)]
pub struct AudioIo {
    shared: Arc<Shared>,
}

/// {
///   責務: [`TimestampedMicrophoneSample`: 採点Processへepochとcapture位置を伴うraw PCMを渡す]
///   フィールド: [
///     epoch: capture時に有効だったclock epoch
///     `stream_sample`: 欠落frameも数える48 kHz capture位置
///     `sample`: effects前のdownmix済みmono PCM
///   ]
/// }
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TimestampedMicrophoneSample {
    pub epoch: ClockEpoch,
    pub stream_sample: u64,
    pub sample: f32,
}

/// {
///   責務: [`AudioPlaybackTimestamp`: `出力callbackが最後に提示したPCMの時刻対応を公開する`]
///   フィールド: [
///     `epoch`: 出力したPCMが属する`transport epoch`
///     `output_sample`: 出力deviceへ進めた48 kHz位置
///     `song_sample`: 対応するsource song位置
///   ]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AudioPlaybackTimestamp {
    pub epoch: ClockEpoch,
    pub output_sample: u64,
    pub song_sample: u64,
}

/// {
///   責務: [`AudioCounters`: 出力とcaptureの論理位置と異常を観測する]
///   フィールド: [`playback_sample_position`: 出力位置, `microphone_frames_received`: capture位置, `output_underrun_frames`: 無音補完数, `input_overrun_frames`: 破棄数]
/// }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AudioCounters {
    /// Frames emitted to the physical output while playback was enabled (48 kHz).
    pub playback_sample_position: u64,
    pub microphone_frames_received: u64,
    /// Frames filled with silence because the playback queue was empty.
    pub output_underrun_frames: u64,
    /// Raw captured frames discarded because the microphone queue was full.
    pub input_overrun_frames: u64,
    /// Occupied ring slots, including stale frames waiting for the reader to discard them.
    pub output_queued_frames: usize,
    /// Occupied ring slots, including stale frames waiting for the reader to discard them.
    pub input_queued_frames: usize,
    pub output_device_error: bool,
    pub input_device_error: bool,
    pub playing: bool,
}

impl AudioIo {
    /// {
    ///   責務: [new: callback開始前に容量が制限された音声queueとepoch状態を作る]
    ///   処理: [1: queue容量を検証する, 2: output/capture ringと初期epochを確保する]
    ///   引数: [`queue_capacity_frames`: 各ringのframe容量]
    ///   戻り値: [`AudioIo`: 共有音声状態]
    ///   エラー: [`InvalidCapacity`: 容量が許容範囲外]
    /// }
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
                clock_epoch: AtomicU64::new(1),
                playing: AtomicBool::new(false),
                output_position: AtomicU64::new(0),
                presentation_sequence: AtomicU64::new(0),
                presentation_epoch: AtomicU64::new(0),
                presentation_output_sample: AtomicU64::new(0),
                presentation_song_sample: AtomicU64::new(0),
                captured_frames: AtomicU64::new(0),
                output_underruns: AtomicU64::new(0),
                input_overruns: AtomicU64::new(0),
                output_stream_error: AtomicBool::new(false),
                input_stream_error: AtomicBool::new(false),
            }),
        })
    }

    /// {
    ///   責務: [`clock_epoch`: `AudioIoがcallbackとqueueに適用しているepochを読む`]
    ///   処理: [1: 共有epochをAcquireで読みclock型へ変換する]
    ///   引数: []
    ///   戻り値: [`ClockEpoch`: 現在のaudio世代]
    /// }
    #[must_use]
    pub fn clock_epoch(&self) -> ClockEpoch {
        ClockEpoch::from_sequence(self.shared.clock_epoch.load(Ordering::Acquire))
    }

    /// {
    ///   責務: [`set_clock_epoch`: callbackのcapture tagとqueue consumerの現在世代を更新する]
    ///   処理: [1: epochを単調増加で共有し旧PCMを読み手に破棄させる]
    ///   引数: [epoch: `ClockSessionが確定した新しいtransport世代`]
    ///   戻り値: []
    ///   エラー: [`AudioError::StaleEpoch`: 現在より古い世代を設定しようとした]
    /// }
    ///
    /// # Errors
    /// Returns `StaleEpoch` if this message would move the shared audio epoch backwards.
    pub fn set_clock_epoch(&self, epoch: ClockEpoch) -> Result<(), AudioError> {
        let mut current = self.shared.clock_epoch.load(Ordering::Acquire);
        loop {
            if epoch.sequence() < current {
                return Err(AudioError::StaleEpoch);
            }
            match self.shared.clock_epoch.compare_exchange_weak(
                current,
                epoch.sequence(),
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return Ok(()),
                Err(observed) => current = observed,
            }
        }
    }

    /// {
    ///   責務: [`enqueue_stereo_at`: song時刻を持つ48 kHz `PCMを同じepochのoutput` queueへ送る]
    ///   処理: [
    ///     1: epochと範囲overflowとfinite PCMを検証する
    ///     2: 残り容量の範囲で各frameへsong sample位置とepochを付ける
    ///     3: accepted frame数を返す
    ///   ]
    ///   引数: [epoch: `PCMのtransport世代`, `song_sample`: 最初のframe位置, frames: interleaved stereo PCM]
    ///   戻り値: [usize: queueが受け入れたframe数]
    ///   エラー: [`AudioError`: stale epoch、位置overflow、非finite PCM]
    /// }
    ///
    /// # Errors
    /// Rejects stale epochs, overflowing positions and NaN/infinity before publishing PCM.
    pub fn enqueue_stereo_at(
        &self,
        epoch: ClockEpoch,
        song_sample: u64,
        frames: &[[f32; 2]],
    ) -> Result<usize, AudioError> {
        self.enqueue_stereo_internal(epoch, Some(song_sample), frames)
    }

    /// {
    ///   責務: [`enqueue_stereo`: 従来の時刻未指定`PCM`を現在のepochへ送る]
    ///   処理: [1: 現epochを読む, 2: 時刻不明としてqueueへ送る]
    ///   引数: [frames: 48 kHz stereo PCM]
    ///   戻り値: [usize: 受け入れたframe数]
    ///   エラー: [epoch変更または非finite PCM]
    /// }
    ///
    /// # Errors
    /// Returns an error for stale epochs or nonfinite PCM.
    pub fn enqueue_stereo(&self, frames: &[[f32; 2]]) -> Result<usize, AudioError> {
        self.enqueue_stereo_internal(self.clock_epoch(), None, frames)
    }

    /// {
    ///   責務: [`enqueue_stereo_internal`: 時刻指定の有無を保ったまま`PCM`をqueueへ送る]
    ///   処理: [1: 入力を検証する, 2: producerを直列化する, 3: frameごとにepochと位置を付ける]
    ///   引数: [`epoch`: transport世代, `song_sample`: source位置または不明, `frames`: stereo `PCM`]
    ///   戻り値: [usize: 受け入れたframe数]
    ///   エラー: [stale epoch、位置overflow、非finite PCM]
    /// }
    fn enqueue_stereo_internal(
        &self,
        epoch: ClockEpoch,
        song_sample: Option<u64>,
        frames: &[[f32; 2]],
    ) -> Result<usize, AudioError> {
        if let Some(first) = song_sample {
            first
                .checked_add(
                    u64::try_from(frames.len()).map_err(|_| AudioError::SamplePositionOverflow)?,
                )
                .ok_or(AudioError::SamplePositionOverflow)?;
        }
        let active_epoch = self.shared.clock_epoch.load(Ordering::Acquire);
        if active_epoch != epoch.sequence() {
            return Err(AudioError::StaleEpoch);
        }
        if frames.iter().flatten().any(|value| !value.is_finite()) {
            return Err(AudioError::NonFiniteSample);
        }
        let _writer = self
            .shared
            .output_writer
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let mut accepted = 0;
        for (index, frame) in frames.iter().enumerate() {
            if self.shared.clock_epoch.load(Ordering::Acquire) != epoch.sequence() {
                return Err(AudioError::StaleEpoch);
            }
            let position = if let Some(first) = song_sample {
                first + u64::try_from(index).map_err(|_| AudioError::SamplePositionOverflow)?
            } else {
                UNKNOWN_SONG_SAMPLE
            };
            if !self.shared.output.push(epoch.sequence(), position, *frame) {
                break;
            }
            accepted += 1;
        }
        Ok(accepted)
    }

    /// {
    ///   責務: [`read_microphone_timestamped`: 現epochのraw mono frameだけを時刻付きでProcessへ渡す]
    ///   処理: [
    ///     1: consumer lockをcontrol/scoring threadだけで取得する
    ///     2: stale frameを捨てcurrent frameを出力へ写す
    ///     3: queue枯渇時は読めた件数を返す
    ///   ]
    ///   引数: [output: epoch、capture sample位置、mono値を受け取るbuffer]
    ///   戻り値: [usize: current epochから読み取ったframe数]
    /// }
    pub fn read_microphone_timestamped(&self, output: &mut [TimestampedMicrophoneSample]) -> usize {
        let _reader = self
            .shared
            .microphone_reader
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let mut received = 0;
        let epoch = self.shared.clock_epoch.load(Ordering::Acquire);
        for frame in output {
            loop {
                match self.shared.microphone.pop() {
                    Some(sample) if sample.epoch == epoch => {
                        *frame = TimestampedMicrophoneSample {
                            epoch: ClockEpoch::from_sequence(sample.epoch),
                            stream_sample: sample.sample_position,
                            sample: sample.samples[0],
                        };
                        received += 1;
                        break;
                    }
                    Some(_) => {}
                    None => return received,
                }
            }
        }
        received
    }

    /// {
    ///   責務: [`read_microphone`: 従来のmono `PCM`読取`API`で現epochのframeだけを渡す]
    ///   処理: [1: consumerを直列化する, 2: stale frameを捨ててPCMを写す]
    ///   引数: [output: raw mono PCMの出力領域]
    ///   戻り値: [usize: 読み取ったframe数]
    ///   補足: [採点と時刻同期には`read_microphone_timestamped`を使用する]
    /// }
    pub fn read_microphone(&self, output: &mut [f32]) -> usize {
        let _reader = self
            .shared
            .microphone_reader
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let epoch = self.shared.clock_epoch.load(Ordering::Acquire);
        let mut received = 0;
        for value in output {
            loop {
                match self.shared.microphone.pop() {
                    Some(sample) if sample.epoch == epoch => {
                        *value = sample.samples[0];
                        received += 1;
                        break;
                    }
                    Some(_) => {}
                    None => return received,
                }
            }
        }
        received
    }

    /// {
    ///   責務: [`playback_timestamp`: callbackが最後に提示したPCMとoutput counterの組を読む]
    ///   処理: [1: seqlockを検証しながらepochと両sample位置をsnapshotする]
    ///   引数: []
    ///   戻り値: [`Option<AudioPlaybackTimestamp>`: `PCMが一度も出力されていなければNone`]
    /// }
    #[must_use]
    pub fn playback_timestamp(&self) -> Option<AudioPlaybackTimestamp> {
        loop {
            let before = self.shared.presentation_sequence.load(Ordering::Acquire);
            if before == 0 {
                return None;
            }
            if !before.is_multiple_of(2) {
                std::hint::spin_loop();
                continue;
            }
            let timestamp = AudioPlaybackTimestamp {
                epoch: ClockEpoch::from_sequence(
                    self.shared.presentation_epoch.load(Ordering::Relaxed),
                ),
                output_sample: self
                    .shared
                    .presentation_output_sample
                    .load(Ordering::Relaxed),
                song_sample: self.shared.presentation_song_sample.load(Ordering::Relaxed),
            };
            if self.shared.presentation_sequence.load(Ordering::Acquire) == before {
                return (timestamp.epoch.sequence() != 0).then_some(timestamp);
            }
        }
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

    /// {
    ///   責務: [render: callbackの出力PCMを現epochから作り提示時刻を記録する]
    ///   処理: [1: 再生状態を確認する, 2: 各frameの世代を照合し旧frameを無音化する, 3: counterと最後の提示時刻を公開する]
    ///   引数: [samples: device出力, channels: device channel数]
    ///   戻り値: []
    /// }
    pub(crate) fn render<S: OutputSample>(&self, samples: &mut [S], channels: usize) {
        if channels == 0 || !self.shared.playing.load(Ordering::Acquire) {
            samples.fill(S::silence());
            return;
        }
        let output_start = self.shared.output_position.load(Ordering::Relaxed);
        let mut emitted = 0_u64;
        let mut underruns = 0_u64;
        let mut last_presentation = None;
        let mut chunks = samples.chunks_exact_mut(channels);
        for frame in chunks.by_ref() {
            let epoch = self.shared.clock_epoch.load(Ordering::Acquire);
            let queued = self.shared.output.pop();
            let stereo = queued.filter(|item| item.epoch == epoch);
            let [left, right] = stereo.as_ref().map_or([0.0, 0.0], |item| item.samples);
            if let Some(item) = stereo {
                last_presentation = Some(if item.sample_position == UNKNOWN_SONG_SAMPLE {
                    (0, 0, 0)
                } else {
                    (
                        epoch,
                        output_start.wrapping_add(emitted),
                        item.sample_position,
                    )
                });
            } else {
                underruns += 1;
            }
            for (channel, sample) in frame.iter_mut().enumerate() {
                let value = if channels == 1 {
                    f32::midpoint(left, right)
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
        if let Some((epoch, output_sample, song_sample)) = last_presentation {
            self.shared
                .presentation_sequence
                .fetch_add(1, Ordering::AcqRel);
            self.shared
                .presentation_epoch
                .store(epoch, Ordering::Relaxed);
            self.shared
                .presentation_output_sample
                .store(output_sample, Ordering::Relaxed);
            self.shared
                .presentation_song_sample
                .store(song_sample, Ordering::Relaxed);
            self.shared
                .presentation_sequence
                .fetch_add(1, Ordering::Release);
        }
    }

    /// {
    ///   責務: [`capture`: callbackのraw mono `PCM`へ現epochと欠落を含むcapture位置を付ける]
    ///   処理: [1: channelをdownmixする, 2: 時刻付きframeを送る, 3: 受信数と欠落数を公開する]
    ///   引数: [samples: device入力, channels: device channel数]
    ///   戻り値: []
    /// }
    #[allow(clippy::cast_precision_loss)]
    pub(crate) fn capture<S: InputSample>(&self, samples: &[S], channels: usize) {
        if channels == 0 {
            return;
        }
        let epoch = self.shared.clock_epoch.load(Ordering::Acquire);
        let first_sample = self.shared.captured_frames.load(Ordering::Relaxed);
        let mut received = 0_u64;
        let mut dropped = 0_u64;
        for frame in samples.chunks_exact(channels) {
            // All channel conversions are pure and allocation-free.
            let sample = frame.iter().copied().map(S::to_normalized).sum::<f32>() / channels as f32;
            if !self
                .shared
                .microphone
                .push(epoch, first_sample.wrapping_add(received), [sample])
            {
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
    use super::{AudioError, AudioIo, SAMPLE_RATE, TimestampedMicrophoneSample};
    use crate::audio_clock::ClockEpoch;

    /// {
    ///   責務: [`microphone_buffer`: テスト用の未読時刻付きframe領域を作る]
    ///   処理: [1: 指定epochとzero値で固定長bufferを初期化する]
    ///   引数: [epoch: bufferの初期epoch]
    ///   戻り値: [時刻付きmono frameの固定長配列]
    /// }
    fn microphone_buffer<const FRAMES: usize>(
        epoch: ClockEpoch,
    ) -> [TimestampedMicrophoneSample; FRAMES] {
        [TimestampedMicrophoneSample {
            epoch,
            stream_sample: 0,
            sample: 0.0,
        }; FRAMES]
    }

    #[test]
    fn playback_clock_counts_frames_and_outputs_silence_when_empty() {
        let io = AudioIo::new(64).unwrap();
        let frames = [[0.2, -0.2]; 3];
        let epoch = io.clock_epoch();
        assert_eq!(io.enqueue_stereo_at(epoch, 0, &frames).unwrap(), 3);
        let mut paused = [1.0_f32; 6];
        io.render_mock(&mut paused, 2);
        // Paused output must contain exact PCM silence.
        assert_eq!(paused.map(f32::to_bits), [0.0_f32; 6].map(f32::to_bits));
        assert_eq!(io.counters().playback_sample_position, 0);

        io.set_playing(true);
        let mut output = [0.0_f32; 10];
        io.render_mock(&mut output, 2);
        assert_eq!(&output[..6], &[0.2, -0.2, 0.2, -0.2, 0.2, -0.2]);
        assert_eq!(&output[6..], &[0.0; 4]);
        assert_eq!(io.counters().playback_sample_position, 5);
        assert_eq!(io.counters().output_underrun_frames, 2);
        assert_eq!(
            io.playback_timestamp(),
            Some(super::AudioPlaybackTimestamp {
                epoch,
                output_sample: 2,
                song_sample: 2,
            })
        );
        io.set_playing(false);
        io.render_mock(&mut output, 2);
        assert_eq!(io.counters().playback_sample_position, 5);
    }

    #[test]
    fn input_capture_converts_channels_and_reports_overflow() {
        let io = AudioIo::new(32).unwrap();
        io.capture_mock(&[0.7, 0.3, -0.4, -0.2], 2);
        let mut out = microphone_buffer::<2>(io.clock_epoch());
        assert_eq!(io.read_microphone_timestamped(&mut out), 2);
        assert!((out[0].sample - 0.5).abs() < 1e-6);
        assert!((out[1].sample + 0.3).abs() < 1e-6);
        assert_eq!(out[0].stream_sample, 0);
        assert_eq!(out[1].stream_sample, 1);
        io.capture_mock(&[0.25; 64], 1);
        assert_eq!(io.counters().microphone_frames_received, 66);
        assert_eq!(io.counters().input_overrun_frames, 32);
        assert_eq!(io.counters().input_queued_frames, 32);
    }

    #[test]
    fn queue_backpressure_rejects_non_finite_data_atomically() {
        let io = AudioIo::new(32).unwrap();
        let epoch = io.clock_epoch();
        assert_eq!(
            io.enqueue_stereo_at(epoch, 0, &[[1.0, 1.0]; 40]).unwrap(),
            32
        );
        let mut output = [0.0; 64];
        io.set_playing(true);
        io.render_mock(&mut output, 2);
        assert_eq!(io.counters().output_underrun_frames, 0);
        assert!(matches!(
            io.enqueue_stereo_at(epoch, 32, &[[0.0, f32::NAN]]),
            Err(AudioError::NonFiniteSample)
        ));
        assert_eq!(io.counters().output_queued_frames, 0);
    }

    #[test]
    fn converts_signed_and_unsigned_pcm_without_crossing_streams() {
        let io = AudioIo::new(32).unwrap();
        let epoch = io.clock_epoch();
        io.enqueue_stereo_at(epoch, 0, &[[-1.0, 1.0]]).unwrap();
        io.set_playing(true);
        let mut signed = [0_i16; 2];
        io.render(&mut signed, 2);
        assert_eq!(signed, [-32767, 32767]);
        io.capture(&[0_u16, 65535_u16], 2);
        let mut mic = microphone_buffer::<1>(epoch);
        assert_eq!(io.read_microphone_timestamped(&mut mic), 1);
        assert!(mic[0].sample.abs() < 0.0001);
        assert_eq!(io.counters().playback_sample_position, 1);
    }

    #[test]
    fn loopback_clock_and_raw_microphone_align_on_48khz_timeline() {
        let io = AudioIo::new(1024).unwrap();
        io.set_playing(true);
        for block in 0..100 {
            let frames = [[0.35, 0.15]; 480];
            assert_eq!(
                io.enqueue_stereo_at(io.clock_epoch(), block * 480, &frames)
                    .unwrap(),
                480
            );
            let mut output = [0.0; 960];
            io.render_mock(&mut output, 2);
            for sample in output.as_chunks::<2>().0 {
                assert!((sample[0] - 0.35).abs() < 1e-6);
                assert!((sample[1] - 0.15).abs() < 1e-6);
            }
            let raw = [0.4_f32; 480];
            io.capture_mock(&raw, 1);
            let mut result = microphone_buffer::<480>(io.clock_epoch());
            assert_eq!(io.read_microphone_timestamped(&mut result), 480);
            assert!(
                result
                    .iter()
                    .all(|sample| (sample.sample - 0.4).abs() < 1e-6)
            );
            assert_eq!(result[0].stream_sample, block * 480);
            assert_eq!(result[479].stream_sample, block * 480 + 479);
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
            assert_eq!(
                io.enqueue_stereo_at(io.clock_epoch(), 0, &[[0.0, 0.0]; 480])
                    .unwrap(),
                480
            );
            io.render_mock(&mut [0.0_f32; 960], 2);
        }
        assert_eq!(io.counters().playback_sample_position, 480_000);
        assert_eq!(io.counters().output_underrun_frames, 0);
        io.mark_output_device_error();
        io.mark_input_device_error();
        assert!(io.counters().output_device_error);
        assert!(io.counters().input_device_error);
    }

    /// {
    ///   責務: [`epoch_transition_discards_queued_output_and_rejects_old_producers`: 世代変更後のqueue分離を検査する]
    ///   処理: [1: 旧epochのpresentationを記録する, 2: epochを進める, 3: `古いqueueを無音として捨て新PCMだけを出す`]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn epoch_transition_discards_queued_output_and_rejects_old_producers() {
        let io = AudioIo::new(32).unwrap();
        let old_epoch = io.clock_epoch();
        io.enqueue_stereo_at(old_epoch, 10, &[[0.2, 0.2]; 2])
            .unwrap();
        io.set_playing(true);
        let mut first_output = [0.0; 2];
        io.render_mock(&mut first_output, 2);
        assert_eq!(
            io.playback_timestamp(),
            Some(super::AudioPlaybackTimestamp {
                epoch: old_epoch,
                output_sample: 0,
                song_sample: 10,
            })
        );
        let new_epoch = ClockEpoch::from_sequence(old_epoch.sequence() + 1);
        io.set_clock_epoch(new_epoch).unwrap();
        assert_eq!(io.set_clock_epoch(new_epoch), Ok(()));
        assert_eq!(io.set_clock_epoch(old_epoch), Err(AudioError::StaleEpoch));
        assert_eq!(io.playback_timestamp().unwrap().epoch, old_epoch);
        assert_eq!(
            io.enqueue_stereo_at(old_epoch, 12, &[[0.8, 0.8]]),
            Err(AudioError::StaleEpoch)
        );
        io.enqueue_stereo_at(new_epoch, 80, &[[0.5, -0.5]; 2])
            .unwrap();
        let mut output = [1.0; 6];
        io.render_mock(&mut output, 2);
        assert_eq!(
            output.map(f32::to_bits),
            [0.0_f32, 0.0, 0.5, -0.5, 0.5, -0.5].map(f32::to_bits)
        );
        assert_eq!(
            io.playback_timestamp(),
            Some(super::AudioPlaybackTimestamp {
                epoch: new_epoch,
                output_sample: 3,
                song_sample: 81,
            })
        );
        assert_eq!(io.counters().output_underrun_frames, 1);
    }

    /// {
    ///   責務: [`capture_epoch_change_discards_old_frames_and_preserves_sample_positions`: capture messageの世代と連番を検査する]
    ///   処理: [1: `旧epochのPCMをcaptureする`, 2: `新epochへ切り替えPCMを追加する`, 3: `新epochのframeと元counter位置を読む`]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn capture_epoch_change_discards_old_frames_and_preserves_sample_positions() {
        let io = AudioIo::new(32).unwrap();
        let old_epoch = io.clock_epoch();
        io.capture_mock(&[0.1, 0.2], 1);
        let new_epoch = ClockEpoch::from_sequence(old_epoch.sequence() + 1);
        io.set_clock_epoch(new_epoch).unwrap();
        io.capture_mock(&[0.3, 0.4], 1);
        let mut output = microphone_buffer::<2>(new_epoch);
        assert_eq!(io.read_microphone_timestamped(&mut output), 2);
        assert_eq!(output[0].epoch, new_epoch);
        assert_eq!(output[0].stream_sample, 2);
        assert!((output[0].sample - 0.3).abs() < 1e-6);
        assert_eq!(output[1].stream_sample, 3);
        assert!((output[1].sample - 0.4).abs() < 1e-6);
    }

    /// {
    ///   責務: [`capture_overrun_remains_visible_as_a_timestamp_gap`: queue落ちを後続frameの位置から検出する]
    ///   処理: [1: queueをoverflowさせる, 2: 残存frameを読む, 3: 後続captureのsample位置に欠落を確認する]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn capture_overrun_remains_visible_as_a_timestamp_gap() {
        let io = AudioIo::new(32).unwrap();
        let epoch = io.clock_epoch();
        io.capture_mock(&[0.25; 40], 1);
        let mut first = microphone_buffer::<32>(epoch);
        assert_eq!(io.read_microphone_timestamped(&mut first), 32);
        assert_eq!(first[31].stream_sample, 31);
        io.capture_mock(&[0.5; 2], 1);
        let mut after_gap = microphone_buffer::<2>(epoch);
        assert_eq!(io.read_microphone_timestamped(&mut after_gap), 2);
        assert_eq!(after_gap[0].stream_sample, 40);
        assert_eq!(after_gap[1].stream_sample, 41);
        assert_eq!(io.counters().input_overrun_frames, 8);
    }

    /// {
    ///   責務: [`legacy_audio_messages_remain_usable_without_inventing_song_time`: 従来`API`の互換性と時刻不明を検査する]
    ///   処理: [1: 時刻付き再生を行う, 2: 従来の再生で提示時刻を消す, 3: 従来のcapture読取で旧epochを除く]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn legacy_audio_messages_remain_usable_without_inventing_song_time() {
        let io = AudioIo::new(32).unwrap();
        let old_epoch = io.clock_epoch();
        io.set_playing(true);
        assert_eq!(io.enqueue_stereo_at(old_epoch, 10, &[[0.2, -0.2]]), Ok(1));
        io.render_mock(&mut [0.0; 2], 2);
        assert_eq!(io.playback_timestamp().unwrap().song_sample, 10);

        assert_eq!(io.enqueue_stereo(&[[0.5, -0.5]]), Ok(1));
        let mut output = [0.0; 2];
        io.render_mock(&mut output, 2);
        assert_eq!(output.map(f32::to_bits), [0.5_f32, -0.5].map(f32::to_bits));
        assert_eq!(io.playback_timestamp(), None);

        io.capture_mock(&[0.1], 1);
        let new_epoch = ClockEpoch::from_sequence(old_epoch.sequence() + 1);
        io.set_clock_epoch(new_epoch).unwrap();
        io.capture_mock(&[0.6], 1);
        let mut microphone = [0.0];
        assert_eq!(io.read_microphone(&mut microphone), 1);
        assert!((microphone[0] - 0.6).abs() < 1e-6);
    }
}
