//! Lock-free hand-over of mixer settings from the control side to the audio callback.
//!
//! The Core API (or any control thread) writes a [`MonitorSettings`] snapshot; the output
//! callback reads it through a sequence lock, so the callback never blocks, allocates or
//! takes a mutex. Writers are serialized by a mutex that only control threads use.

use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
};

use crate::mixer::{MAX_ECHO_DELAY_FRAMES, Mixer, MixerError, MixerSettings};

/// Frames of music and microphone the callback mixes per `Mixer::process` call.
pub const MONITOR_BLOCK_FRAMES: usize = 1024;
/// Reads a callback retries while a writer is mid-update before keeping its last values.
const SNAPSHOT_RETRIES: u32 = 4;

/// {
///   責務: [`MonitorSettings`: ミキサー設定に、マイクをモニタへ流すかの切替を足した、API・UI向けの1組]
///   フィールド: [
///     mixer: 音量・エコー・リバーブ・bypassの設定,
///     `mic_monitor`: マイクを自分のスピーカーへ戻すか。既定はfalse(ハウリング防止)で、採点用の生信号には無関係
///   ]
/// }
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct MonitorSettings {
    pub mixer: MixerSettings,
    pub mic_monitor: bool,
}

/// {
///   責務: [`ControlInner`: 設定をatomicで保持する。奇数のsequenceは書込み中]
///   フィールド: [sequence: 書込み世代, 各値: f32はbit列で保存, writer: 制御側だけが取る書込み直列化]
/// }
struct ControlInner {
    sequence: AtomicU64,
    music_gain: AtomicU32,
    mic_gain: AtomicU32,
    echo_level: AtomicU32,
    echo_delay_frames: AtomicU32,
    echo_feedback: AtomicU32,
    reverb_level: AtomicU32,
    effects_bypass: AtomicBool,
    mic_monitor: AtomicBool,
    writer: Mutex<()>,
}

/// {
///   責務: [`MixerControl`: 制御側とaudio callbackが共有するミキサー設定。cloneは同じ設定を指す]
///   フィールド: [inner: 共有されるatomic群]
/// }
#[derive(Clone)]
pub struct MixerControl {
    inner: Arc<ControlInner>,
}

impl Default for MixerControl {
    /// # Panics
    /// Never: the default settings are statically valid.
    fn default() -> Self {
        Self::new(MonitorSettings::default()).expect("default mixer settings are valid")
    }
}

impl MixerControl {
    /// {
    ///   責務: [`new`: 検証済みの初期設定で共有設定を作る]
    ///   引数: [settings: 初期設定]
    ///   戻り値: [`MixerControl`。範囲外なら`InvalidSetting`]
    /// }
    ///
    /// # Errors
    /// Returns `InvalidSetting` for a non-finite or out-of-range value.
    pub fn new(settings: MonitorSettings) -> Result<Self, MixerError> {
        settings.mixer.validate()?;
        let control = Self {
            inner: Arc::new(ControlInner {
                sequence: AtomicU64::new(0),
                music_gain: AtomicU32::new(0),
                mic_gain: AtomicU32::new(0),
                echo_level: AtomicU32::new(0),
                echo_delay_frames: AtomicU32::new(0),
                echo_feedback: AtomicU32::new(0),
                reverb_level: AtomicU32::new(0),
                effects_bypass: AtomicBool::new(false),
                mic_monitor: AtomicBool::new(false),
                writer: Mutex::new(()),
            }),
        };
        control.store(&settings);
        Ok(control)
    }

    fn store(&self, settings: &MonitorSettings) {
        let inner = &self.inner;
        let mixer = settings.mixer;
        inner
            .music_gain
            .store(mixer.music_gain.to_bits(), Ordering::Relaxed);
        inner
            .mic_gain
            .store(mixer.mic_gain.to_bits(), Ordering::Relaxed);
        inner
            .echo_level
            .store(mixer.echo_level.to_bits(), Ordering::Relaxed);
        inner.echo_delay_frames.store(
            u32::try_from(mixer.echo_delay_frames).unwrap_or(u32::MAX),
            Ordering::Relaxed,
        );
        inner
            .echo_feedback
            .store(mixer.echo_feedback.to_bits(), Ordering::Relaxed);
        inner
            .reverb_level
            .store(mixer.reverb_level.to_bits(), Ordering::Relaxed);
        inner
            .effects_bypass
            .store(mixer.effects_bypass, Ordering::Relaxed);
        inner
            .mic_monitor
            .store(settings.mic_monitor, Ordering::Relaxed);
    }

    /// {
    ///   責務: [`set`: 設定を検証し、callbackから見て1組として更新する]
    ///   処理: [1: 範囲を検証する, 2: 書込みを直列化し、sequenceを奇数にして値を書き、偶数に戻す]
    ///   引数: [settings: 新しい設定]
    ///   戻り値: [成功なら`Ok`。範囲外なら何も変えず`InvalidSetting`]
    /// }
    ///
    /// # Errors
    /// Returns `InvalidSetting` and keeps the previous settings for an invalid value.
    pub fn set(&self, settings: MonitorSettings) -> Result<(), MixerError> {
        settings.mixer.validate()?;
        let _writer = self
            .inner
            .writer
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        self.inner.sequence.fetch_add(1, Ordering::AcqRel);
        self.store(&settings);
        self.inner.sequence.fetch_add(1, Ordering::Release);
        Ok(())
    }

    /// {
    ///   責務: [`snapshot`: 書込み中でない一貫した設定を、待たずに読む]
    ///   処理: [1: sequenceが偶数か確認する, 2: 値を読む, 3: sequenceが変わっていなければ返す, 4: 数回失敗したらNone]
    ///   引数: []
    ///   戻り値: [`(sequence, MonitorSettings)`。書込みと競合し続けたらNone]
    /// }
    #[must_use]
    pub fn snapshot(&self) -> Option<(u64, MonitorSettings)> {
        let inner = &self.inner;
        for _ in 0..SNAPSHOT_RETRIES {
            let before = inner.sequence.load(Ordering::Acquire);
            if !before.is_multiple_of(2) {
                std::hint::spin_loop();
                continue;
            }
            let settings = MonitorSettings {
                mixer: MixerSettings {
                    music_gain: f32::from_bits(inner.music_gain.load(Ordering::Relaxed)),
                    mic_gain: f32::from_bits(inner.mic_gain.load(Ordering::Relaxed)),
                    echo_level: f32::from_bits(inner.echo_level.load(Ordering::Relaxed)),
                    echo_delay_frames: (inner.echo_delay_frames.load(Ordering::Relaxed) as usize)
                        .clamp(1, MAX_ECHO_DELAY_FRAMES),
                    echo_feedback: f32::from_bits(inner.echo_feedback.load(Ordering::Relaxed)),
                    reverb_level: f32::from_bits(inner.reverb_level.load(Ordering::Relaxed)),
                    effects_bypass: inner.effects_bypass.load(Ordering::Relaxed),
                },
                mic_monitor: inner.mic_monitor.load(Ordering::Relaxed),
            };
            std::sync::atomic::fence(Ordering::Acquire);
            if inner.sequence.load(Ordering::Relaxed) == before {
                return Some((before, settings));
            }
        }
        None
    }

    /// {
    ///   責務: [`get`: 現在の設定を制御側から読む]
    ///   戻り値: [現在の設定。書込みと競合し続けた場合は既定値ではなく再試行する]
    /// }
    #[must_use]
    pub fn get(&self) -> MonitorSettings {
        loop {
            if let Some((_, settings)) = self.snapshot() {
                return settings;
            }
            std::thread::yield_now();
        }
    }

    /// {
    ///   責務: [`mic_monitor_enabled`: capture callbackが、モニタ用のマイクtapへ書くか1回のatomic読みで判断する]
    ///   戻り値: [モニタ中ならtrue]
    /// }
    #[must_use]
    pub fn mic_monitor_enabled(&self) -> bool {
        self.inner.mic_monitor.load(Ordering::Relaxed)
    }
}

/// {
///   責務: [`MonitorMixer`: 出力callbackが所有する、Mixerと固定長の作業領域。callback中は確保しない]
///   フィールド: [
///     control: 制御側と共有する設定,
///     mixer: DSP本体,
///     seen: 最後に反映したsequence,
///     `mic_monitor`: 反映済みのモニタ切替,
///     music / mic / out: `MONITOR_BLOCK_FRAMES`分の作業領域
///   ]
/// }
pub struct MonitorMixer {
    control: MixerControl,
    pub(crate) mixer: Mixer,
    seen: u64,
    pub(crate) mic_monitor: bool,
    pub(crate) music: Vec<[f32; 2]>,
    pub(crate) mic: Vec<f32>,
    pub(crate) out: Vec<[f32; 2]>,
}

impl MonitorMixer {
    /// {
    ///   責務: [`new`: 設定を読み、callback開始前にMixerと作業領域を確保する]
    ///   引数: [control: 共有設定]
    ///   戻り値: [`MonitorMixer`。設定が不正なら`InvalidSetting`]
    /// }
    ///
    /// # Errors
    /// Returns `InvalidSetting` when the control holds an invalid value.
    pub fn new(control: MixerControl) -> Result<Self, MixerError> {
        let (seen, settings) = control.snapshot().unwrap_or_else(|| (0, control.get()));
        Ok(Self {
            mixer: Mixer::new(settings.mixer)?,
            seen,
            mic_monitor: settings.mic_monitor,
            music: vec![[0.0; 2]; MONITOR_BLOCK_FRAMES],
            mic: vec![0.0; MONITOR_BLOCK_FRAMES],
            out: vec![[0.0; 2]; MONITOR_BLOCK_FRAMES],
            control,
        })
    }

    /// {
    ///   責務: [`refresh`: callbackの先頭で、設定が変わっていれば一貫した組を反映する]
    ///   処理: [1: snapshotを試す, 2: sequenceが進んでいればMixerとモニタ切替を更新する]
    ///   戻り値: []
    /// }
    pub fn refresh(&mut self) {
        if let Some((sequence, settings)) = self.control.snapshot()
            && sequence != self.seen
        {
            // The control validated the values; a refusal keeps the previous mixer state.
            if self.mixer.set_settings(settings.mixer).is_ok() {
                self.mic_monitor = settings.mic_monitor;
                self.seen = sequence;
            }
        }
    }

    /// {
    ///   責務: [`mixer`: 観測値(clip数など)を読むためのMixerへの参照]
    ///   戻り値: [Mixerの共有参照]
    /// }
    #[must_use]
    pub const fn mixer(&self) -> &Mixer {
        &self.mixer
    }
}
