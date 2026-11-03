//! Karaoke mixer and monitoring effects as pure 48 kHz sample processing.
//!
//! The mixer borrows the raw microphone block immutably and writes a separate output
//! block, so echo and reverb can never leak into the signal used for scoring. After
//! construction `process` does not allocate, lock, log or touch files, which keeps it
//! usable from an audio callback. Device wiring and loopback latency measurement are
//! not part of this module.

use std::fmt;

use crate::audio_engine::SAMPLE_RATE;

/// Frames of delay the mixer itself adds to the monitored microphone. The dry and
/// bypassed paths are sample-aligned; echo repeats are intentional delay, not latency.
pub const ADDED_MONITOR_LATENCY_FRAMES: usize = 0;
/// Longest echo delay: one second of frames at the engine sample rate.
pub const MAX_ECHO_DELAY_FRAMES: usize = SAMPLE_RATE as usize;
const MAX_GAIN: f32 = 2.0;
const MAX_ECHO_FEEDBACK: f32 = 0.9;

// Freeverb comb / all-pass lengths (tuned for 44.1 kHz) scaled to 48 kHz.
const COMB_FRAMES: [usize; 4] = [1215, 1293, 1390, 1476];
const ALLPASS_FRAMES: [usize; 2] = [605, 480];
const COMB_FEEDBACK: f32 = 0.84;
const COMB_DAMPING: f32 = 0.2;
const ALLPASS_FEEDBACK: f32 = 0.5;
const REVERB_INPUT_GAIN: f32 = 0.1;

/// Why settings or buffers were refused.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MixerError {
    InvalidSetting(&'static str),
    LengthMismatch,
}

impl fmt::Display for MixerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidSetting(name) => write!(formatter, "invalid mixer setting: {name}"),
            Self::LengthMismatch => {
                formatter.write_str("music, microphone and output lengths differ")
            }
        }
    }
}

impl std::error::Error for MixerError {}

/// {
///   責務: [`MixerSettings`: 伴奏・マイクの音量とエコー・リバーブの設定。独立に調整できる]
///   フィールド: [
///     `music_gain`: 伴奏の音量(0..=2),
///     `mic_gain`: モニタ用マイクの音量(0..=2)。採点用の生信号には影響しない,
///     `echo_level`: エコー音量(0..=1),
///     `echo_delay_frames`: エコーの遅延(1..=`MAX_ECHO_DELAY_FRAMES`),
///     `echo_feedback`: エコーの繰り返し量(0..=0.9),
///     `reverb_level`: リバーブ音量(0..=1),
///     `effects_bypass`: trueでエコー・リバーブを通さず、マイクを遅れなしで直接モニタする
///   ]
/// }
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MixerSettings {
    pub music_gain: f32,
    pub mic_gain: f32,
    pub echo_level: f32,
    pub echo_delay_frames: usize,
    pub echo_feedback: f32,
    pub reverb_level: f32,
    pub effects_bypass: bool,
}

impl Default for MixerSettings {
    fn default() -> Self {
        Self {
            music_gain: 1.0,
            mic_gain: 1.0,
            echo_level: 0.25,
            echo_delay_frames: 9_600,
            echo_feedback: 0.35,
            reverb_level: 0.2,
            effects_bypass: false,
        }
    }
}

impl MixerSettings {
    fn validate(&self) -> Result<(), MixerError> {
        let in_range = |value: f32, max: f32| value.is_finite() && (0.0..=max).contains(&value);
        if !in_range(self.music_gain, MAX_GAIN) {
            return Err(MixerError::InvalidSetting("music_gain"));
        }
        if !in_range(self.mic_gain, MAX_GAIN) {
            return Err(MixerError::InvalidSetting("mic_gain"));
        }
        if !in_range(self.echo_level, 1.0) {
            return Err(MixerError::InvalidSetting("echo_level"));
        }
        if !(1..=MAX_ECHO_DELAY_FRAMES).contains(&self.echo_delay_frames) {
            return Err(MixerError::InvalidSetting("echo_delay_frames"));
        }
        if !in_range(self.echo_feedback, MAX_ECHO_FEEDBACK) {
            return Err(MixerError::InvalidSetting("echo_feedback"));
        }
        if !in_range(self.reverb_level, 1.0) {
            return Err(MixerError::InvalidSetting("reverb_level"));
        }
        Ok(())
    }
}

/// {
///   責務: [`MixerCounters`: 出力の飽和と、不正入力の数を観測する]
///   フィールド: [
///     `frames`: 処理したフレーム数,
///     `clipped_frames`: 出力が±1を超えて制限されたフレーム数,
///     `rejected_samples`: NaN/無限大として0に置き換えた入力sample数
///   ]
/// }
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct MixerCounters {
    pub frames: u64,
    pub clipped_frames: u64,
    pub rejected_samples: u64,
}

struct Comb {
    buffer: Vec<f32>,
    index: usize,
    filtered: f32,
}

impl Comb {
    fn new(frames: usize) -> Self {
        Self {
            buffer: vec![0.0; frames],
            index: 0,
            filtered: 0.0,
        }
    }

    fn process(&mut self, input: f32) -> f32 {
        let output = self.buffer[self.index];
        self.filtered = output * (1.0 - COMB_DAMPING) + self.filtered * COMB_DAMPING;
        self.buffer[self.index] = input + self.filtered * COMB_FEEDBACK;
        self.index = (self.index + 1) % self.buffer.len();
        output
    }

    fn clear(&mut self) {
        self.buffer.fill(0.0);
        self.filtered = 0.0;
    }
}

struct AllPass {
    buffer: Vec<f32>,
    index: usize,
}

impl AllPass {
    fn new(frames: usize) -> Self {
        Self {
            buffer: vec![0.0; frames],
            index: 0,
        }
    }

    fn process(&mut self, input: f32) -> f32 {
        let delayed = self.buffer[self.index];
        self.buffer[self.index] = input + delayed * ALLPASS_FEEDBACK;
        self.index = (self.index + 1) % self.buffer.len();
        delayed - input
    }

    fn clear(&mut self) {
        self.buffer.fill(0.0);
    }
}

/// {
///   責務: [`Mixer`: 伴奏とモニタ用マイク(エコー・リバーブ付き)をlimit付きで混ぜる]
///   フィールド: [
///     `settings`: 目標設定。4つの音量は1ブロックで滑らかに追従し、遅延とfeedbackは即時,
///     `music_now` / `mic_now` / `echo_now` / `reverb_now`: 現在の実効値,
///     `echo_line` / `echo_index`: エコーの遅延線,
///     `combs` / `allpasses`: リバーブ,
///     `counters`: 観測値
///   ]
/// }
pub struct Mixer {
    settings: MixerSettings,
    music_now: f32,
    mic_now: f32,
    echo_now: f32,
    reverb_now: f32,
    echo_line: Vec<f32>,
    echo_index: usize,
    combs: Vec<Comb>,
    allpasses: Vec<AllPass>,
    counters: MixerCounters,
}

impl Mixer {
    /// {
    ///   責務: [`new`: 設定を検証して、遅延線を一度だけ確保したMixerを作る]
    ///   処理: [1: 設定の範囲を検証する, 2: エコー・リバーブの状態を確保する]
    ///   引数: [settings: 初期設定]
    ///   戻り値: [Mixer。設定が範囲外なら`MixerError::InvalidSetting`]
    /// }
    ///
    /// # Errors
    /// Returns `InvalidSetting` when a value is non-finite or outside its range.
    pub fn new(settings: MixerSettings) -> Result<Self, MixerError> {
        settings.validate()?;
        Ok(Self {
            music_now: settings.music_gain,
            mic_now: settings.mic_gain,
            echo_now: settings.echo_level,
            reverb_now: settings.reverb_level,
            settings,
            echo_line: vec![0.0; MAX_ECHO_DELAY_FRAMES],
            echo_index: 0,
            combs: COMB_FRAMES.iter().map(|&n| Comb::new(n)).collect(),
            allpasses: ALLPASS_FRAMES.iter().map(|&n| AllPass::new(n)).collect(),
            counters: MixerCounters::default(),
        })
    }

    /// {
    ///   責務: [`set_settings`: 再確保なしで設定を更新する]
    ///   処理: [1: 範囲を検証する, 2: bypassに入るとき古い残響が戻らないよう状態を消す]
    ///   引数: [settings: 新しい目標設定]
    ///   戻り値: [成功なら`Ok`。範囲外なら現在の設定を変えずに`InvalidSetting`]
    /// }
    ///
    /// # Errors
    /// Returns `InvalidSetting` and keeps the previous settings when a value is invalid.
    pub fn set_settings(&mut self, settings: MixerSettings) -> Result<(), MixerError> {
        settings.validate()?;
        if settings.effects_bypass && !self.settings.effects_bypass {
            self.clear_effects();
        }
        self.settings = settings;
        Ok(())
    }

    #[must_use]
    pub const fn settings(&self) -> MixerSettings {
        self.settings
    }

    #[must_use]
    pub const fn counters(&self) -> MixerCounters {
        self.counters
    }

    fn clear_effects(&mut self) {
        self.echo_line.fill(0.0);
        self.combs.iter_mut().for_each(Comb::clear);
        self.allpasses.iter_mut().for_each(AllPass::clear);
    }

    fn reverb(&mut self, input: f32) -> f32 {
        let feed = input * REVERB_INPUT_GAIN;
        let mut sum = 0.0;
        for comb in &mut self.combs {
            sum += comb.process(feed);
        }
        let mut output = sum / 4.0;
        for allpass in &mut self.allpasses {
            output = allpass.process(output);
        }
        output
    }

    /// {
    ///   責務: [`process`: 伴奏と生マイクの1ブロックを、モニタ出力へ混ぜる]
    ///   処理: [
    ///     1: 3つのバッファ長が同じか確認する,
    ///     2: 設定の変化を1ブロックで滑らかに追従させる,
    ///     3: マイクに音量をかけ、bypassでなければエコーとリバーブを足す,
    ///     4: 伴奏と足して±1に制限し、制限したフレームを数える
    ///   ]
    ///   引数: [music: 伴奏のステレオ, `mic_raw`: 生マイク(読み取り専用), output: 書き込み先]
    ///   戻り値: [成功なら`Ok`。長さが違えば何も書かず`LengthMismatch`]
    /// }
    ///
    /// # Errors
    /// Returns `LengthMismatch` when the three blocks do not have the same frame count.
    #[allow(clippy::cast_precision_loss)]
    pub fn process(
        &mut self,
        music: &[[f32; 2]],
        mic_raw: &[f32],
        output: &mut [[f32; 2]],
    ) -> Result<(), MixerError> {
        if music.len() != mic_raw.len() || music.len() != output.len() {
            return Err(MixerError::LengthMismatch);
        }
        if output.is_empty() {
            return Ok(());
        }
        let settings = self.settings;
        let (echo_target, reverb_target) = if settings.effects_bypass {
            (0.0, 0.0)
        } else {
            (settings.echo_level, settings.reverb_level)
        };
        let length = output.len() as f32;
        let music_step = (settings.music_gain - self.music_now) / length;
        let mic_step = (settings.mic_gain - self.mic_now) / length;
        let echo_step = (echo_target - self.echo_now) / length;
        let reverb_step = (reverb_target - self.reverb_now) / length;
        for ((frame, &mic_sample), out) in music.iter().zip(mic_raw).zip(output.iter_mut()) {
            self.music_now += music_step;
            self.mic_now += mic_step;
            self.echo_now += echo_step;
            self.reverb_now += reverb_step;
            let mic = if mic_sample.is_finite() {
                mic_sample
            } else {
                self.counters.rejected_samples += 1;
                0.0
            };
            let mut monitored = mic * self.mic_now;
            if !monitored.is_finite() {
                // 有限でも極端に大きい入力の乗算がInfになると、遅延線に残り続けるため捨てる。
                self.counters.rejected_samples += 1;
                monitored = 0.0;
            }
            let mut wet = 0.0;
            if !settings.effects_bypass {
                let read = (self.echo_index + MAX_ECHO_DELAY_FRAMES - settings.echo_delay_frames)
                    % MAX_ECHO_DELAY_FRAMES;
                let echo = self.echo_line[read];
                self.echo_line[self.echo_index] = monitored + echo * settings.echo_feedback;
                self.echo_index = (self.echo_index + 1) % MAX_ECHO_DELAY_FRAMES;
                wet = echo * self.echo_now + self.reverb(monitored) * self.reverb_now;
                if !wet.is_finite() {
                    self.counters.rejected_samples += 1;
                    wet = 0.0;
                    self.clear_effects();
                }
            }
            let mut clipped = false;
            for (&music_sample, slot) in frame.iter().zip(out.iter_mut()) {
                let source = if music_sample.is_finite() {
                    music_sample
                } else {
                    self.counters.rejected_samples += 1;
                    0.0
                };
                let mixed = source * self.music_now + monitored + wet;
                clipped |= mixed.abs() > 1.0;
                *slot = mixed.clamp(-1.0, 1.0);
            }
            self.counters.frames += 1;
            self.counters.clipped_frames += u64::from(clipped);
        }
        self.music_now = settings.music_gain;
        self.mic_now = settings.mic_gain;
        self.echo_now = echo_target;
        self.reverb_now = reverb_target;
        Ok(())
    }
}
