//! Pluggable realtime F0 estimators that run on the scoring worker.
//!
//! Estimators consume raw, pre-effect microphone samples from
//! `AudioIo::read_microphone_timestamped` (capture timeline) and emit frames on the
//! capture timeline. [`map_capture_frame`] then maps them onto the song timeline with
//! the epoch's `ClockSession`. Buffers are allocated at construction only.

use std::fmt;

use crate::audio_clock::{
    ClockDomain, ClockEpoch, ClockError, ClockSession, MappedSongSample, SONG_SAMPLE_RATE,
};
use crate::audio_engine::TimestampedMicrophoneSample;

use super::{PitchFrame, PitchObservation};

/// Version label stored in results produced from [`YinEstimator`] frames.
pub const YIN_ESTIMATOR_VERSION: &str = "yin-rt-v1";

/// {
///   責務: [`CapturePitchFrame`: capture timeline上のF0 frameを表す]
///   フィールド: [`epoch`: PCM転送の世代番号, `stream_sample`: 解析窓中心のcapture位置, observation: F0推定結果]
/// }
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CapturePitchFrame {
    pub epoch: ClockEpoch,
    pub stream_sample: u64,
    pub observation: PitchObservation,
}

/// {
///   責務: [`PitchEstimator`: 48 kHz mono PCMからF0 frameを作る推定器の共通契約]
///   フィールド: []
///   補足: [pushはallocationしない。epoch変化やcapture位置の欠落では内部bufferを捨て、異なる世代のPCMを混ぜない]
/// }
pub trait PitchEstimator {
    /// {
    ///   責務: [version: 結果へ保存する推定器の版を返す]
    ///   処理: [1: 固定の版名を返す]
    ///   引数: []
    ///   戻り値: [`&str`: 版名]
    /// }
    fn version(&self) -> &str;

    /// {
    ///   責務: [`hop_samples`: frameを出す間隔を返す（`ScoringConfig::frame_hop_samples`と揃える）]
    ///   処理: [1: 設定値を返す]
    ///   引数: []
    ///   戻り値: [`u32`: 48 kHz sample数]
    /// }
    fn hop_samples(&self) -> u32;

    /// {
    ///   責務: [push: 1 sampleを取り込み、hopごとにframeを返す]
    ///   処理: [1: 不連続なら内部状態を捨てる, 2: bufferへ追加する, 3: 窓が満ちたら解析する]
    ///   引数: [sample: epochとcapture位置付きraw PCM]
    ///   戻り値: [`Option<CapturePitchFrame>`: frameが完成した場合]
    /// }
    fn push(&mut self, sample: TimestampedMicrophoneSample) -> Option<CapturePitchFrame>;

    /// {
    ///   責務: [reset: 蓄積したPCMを捨てる]
    ///   処理: [1: buffer位置と直前sampleを初期化する]
    ///   引数: []
    ///   戻り値: []
    /// }
    fn reset(&mut self);
}

/// {
///   責務: [`EstimatorError`: 推定器設定の不正を判別可能に返す]
///   フィールド: []
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EstimatorError {
    InvalidConfig {
        field: &'static str,
        reason: &'static str,
    },
}

impl fmt::Display for EstimatorError {
    /// {
    ///   責務: [fmt: 不正項目と理由を書く]
    ///   処理: [1: 項目名と理由を書式化する]
    ///   引数: [f: 出力先]
    ///   戻り値: [`fmt::Result`: 書式化結果]
    /// }
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfig { field, reason } => {
                write!(f, "pitch_estimator_invalid_config: {field} {reason}")
            }
        }
    }
}

impl std::error::Error for EstimatorError {}

/// {
///   責務: [`YinConfig`: YIN推定器の探索範囲・窓・閾値を保持する]
///   フィールド: [
///     `min_f0_hz`, `max_f0_hz`: 探索するF0範囲
///     `integration_samples`: 差分関数の積分窓
///     `hop_samples`: frame間隔
///     threshold: CMNDの有声判定閾値
///     `silence_rms`: これ未満のRMSは無声
///   ]
/// }
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct YinConfig {
    pub min_f0_hz: f32,
    pub max_f0_hz: f32,
    pub integration_samples: u32,
    pub hop_samples: u32,
    pub threshold: f32,
    pub silence_rms: f32,
}

impl Default for YinConfig {
    /// {
    ///   責務: [default: 歌声向けの既定設定を返す]
    ///   処理: [1: 65..1100 Hz・約21 ms積分窓・10 ms hopを設定する]
    ///   引数: []
    ///   戻り値: [`YinConfig`: 既定設定]
    /// }
    fn default() -> Self {
        Self {
            min_f0_hz: 65.0,
            max_f0_hz: 1100.0,
            integration_samples: 1024,
            hop_samples: 480,
            threshold: 0.15,
            silence_rms: 0.001,
        }
    }
}

/// {
///   責務: [`YinEstimator`: YIN（累積平均正規化差分）でF0を決定的に推定する]
///   フィールド: [
///     config: 検証済み設定
///     `tau_min`, `tau_max`: 探索するlag範囲
///     buffer: 積分窓+最大lag長のPCM, filled: 有効sample数
///     difference: 差分関数とCMNDの作業領域
///     last: 直前sampleのepochとcapture位置
///   ]
/// }
#[derive(Clone, Debug)]
pub struct YinEstimator {
    config: YinConfig,
    tau_min: usize,
    tau_max: usize,
    buffer: Vec<f32>,
    filled: usize,
    difference: Vec<f64>,
    last: Option<(ClockEpoch, u64)>,
}

impl YinEstimator {
    /// {
    ///   責務: [new: 設定を検証し、解析bufferを事前確保する]
    ///   処理: [1: F0範囲・窓・閾値を検証する, 2: lag範囲を求める, 3: bufferを確保する]
    ///   引数: [config: YIN設定]
    ///   戻り値: [`Result<Self, EstimatorError>`: 推定器]
    /// }
    ///
    /// # Errors
    /// Returns `EstimatorError::InvalidConfig` naming the first invalid field.
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)] // lags are validated to 1..=4800.
    pub fn new(config: YinConfig) -> Result<Self, EstimatorError> {
        let invalid = |field, reason| Err(EstimatorError::InvalidConfig { field, reason });
        if !config.min_f0_hz.is_finite() || config.min_f0_hz < 10.0 {
            return invalid("min_f0_hz", "must be at least 10 Hz");
        }
        if !config.max_f0_hz.is_finite()
            || config.max_f0_hz <= config.min_f0_hz
            || config.max_f0_hz > 4_000.0
        {
            return invalid("max_f0_hz", "must exceed min_f0_hz and be at most 4000 Hz");
        }
        if !config.threshold.is_finite() || config.threshold <= 0.0 || config.threshold >= 1.0 {
            return invalid("threshold", "must be within (0, 1)");
        }
        if !config.silence_rms.is_finite() || !(0.0..1.0).contains(&config.silence_rms) {
            return invalid("silence_rms", "must be within 0..1");
        }
        if !(48..=48_000).contains(&config.hop_samples) {
            return invalid("hop_samples", "must be within 48..=48000");
        }
        let rate = f64::from(SONG_SAMPLE_RATE);
        let tau_max = (rate / f64::from(config.min_f0_hz)).ceil() as usize;
        let tau_min = ((rate / f64::from(config.max_f0_hz)).floor() as usize).max(2);
        let integration = config.integration_samples as usize;
        if integration < tau_max || integration > 48_000 {
            return invalid(
                "integration_samples",
                "must cover one period of min_f0_hz and be at most 48000",
            );
        }
        Ok(Self {
            config,
            tau_min,
            tau_max,
            buffer: vec![0.0; integration + tau_max + 1],
            filled: 0,
            difference: vec![0.0; tau_max + 2],
            last: None,
        })
    }

    /// {
    ///   責務: [analyze: 満ちた窓のF0を推定する]
    ///   処理: [
    ///     1: 積分窓RMSが無音閾値未満なら無声とする
    ///     2: 差分関数とCMNDを計算する
    ///     3: 閾値を下回る最初の谷を探し、放物線補間する
    ///   ]
    ///   引数: []
    ///   戻り値: [`PitchObservation`: 推定結果]
    /// }
    #[allow(clippy::cast_precision_loss)] // lags and window sizes are at most 48000.
    fn analyze(&mut self) -> PitchObservation {
        let w = self.config.integration_samples as usize;
        let window = &self.buffer[..w];
        let energy: f64 = window.iter().map(|x| f64::from(*x) * f64::from(*x)).sum();
        if (energy / w as f64).sqrt() < f64::from(self.config.silence_rms) {
            return PitchObservation::Unvoiced;
        }
        // Difference function d(tau) and cumulative mean normalized difference d'(tau).
        self.difference[0] = 1.0;
        let mut running = 0.0;
        for tau in 1..=self.tau_max + 1 {
            let mut sum = 0.0;
            for j in 0..w {
                let delta = f64::from(self.buffer[j]) - f64::from(self.buffer[j + tau]);
                sum += delta * delta;
            }
            running += sum;
            self.difference[tau] = if running > 0.0 {
                sum * tau as f64 / running
            } else {
                1.0
            };
        }
        let threshold = f64::from(self.config.threshold);
        let Some(mut tau) =
            (self.tau_min..=self.tau_max).find(|tau| self.difference[*tau] < threshold)
        else {
            return PitchObservation::Unvoiced;
        };
        while tau < self.tau_max && self.difference[tau + 1] < self.difference[tau] {
            tau += 1;
        }
        let (left, center, right) = (
            self.difference[tau - 1],
            self.difference[tau],
            self.difference[tau + 1],
        );
        let curvature = left - 2.0 * center + right;
        let shift = if curvature > 0.0 {
            (0.5 * (left - right) / curvature).clamp(-0.5, 0.5)
        } else {
            0.0
        };
        let period = tau as f64 + shift;
        #[allow(clippy::cast_possible_truncation)] // f0 is bounded by max_f0_hz.
        let f0_hz = (f64::from(SONG_SAMPLE_RATE) / period) as f32;
        #[allow(clippy::cast_possible_truncation)] // confidence is clamped to 0..=1.
        let confidence = (1.0 - center).clamp(0.0, 1.0) as f32;
        PitchObservation::Voiced { f0_hz, confidence }
    }
}

impl PitchEstimator for YinEstimator {
    /// {
    ///   責務: [`version`: 推定器の版名を返す]
    ///   処理: [1: 定数を返す]
    ///   引数: [self: 推定器]
    ///   戻り値: [`&str`: 版名]
    /// }
    fn version(&self) -> &str {
        YIN_ESTIMATOR_VERSION
    }

    /// {
    ///   責務: [`hop_samples`: フレームの間隔(sample数)を返す]
    ///   処理: [1: 設定値を返す]
    ///   引数: [self: 推定器]
    ///   戻り値: [`u32`: ホップ幅]
    /// }
    fn hop_samples(&self) -> u32 {
        self.config.hop_samples
    }

    /// {
    ///   責務: [`push`: 1 sampleを受け取り、窓が満ちたらF0フレームを返す]
    ///   処理: [1: 連続性を確認し途切れなら状態を捨てる, 2: 窓に追加する, 3: ホップごとに解析する]
    ///   引数: [self: 推定器, sample: 時刻付きマイク入力]
    ///   戻り値: [`Option<CapturePitchFrame>`: 解析結果。窓が満ちていなければNone]
    /// }
    fn push(&mut self, sample: TimestampedMicrophoneSample) -> Option<CapturePitchFrame> {
        let continuous = self.last.is_some_and(|(epoch, position)| {
            epoch == sample.epoch && position.checked_add(1) == Some(sample.stream_sample)
        });
        if !continuous {
            self.reset();
        }
        self.last = Some((sample.epoch, sample.stream_sample));
        let len = self.buffer.len();
        if self.filled == len {
            let hop = self.config.hop_samples as usize;
            self.buffer.copy_within(hop.min(len).., 0);
            self.filled = len - hop.min(len);
        }
        // Non-finite PCM must not poison the analysis; treat it as silence.
        self.buffer[self.filled] = if sample.sample.is_finite() {
            sample.sample
        } else {
            0.0
        };
        self.filled += 1;
        if self.filled < len {
            return None;
        }
        let first = sample.stream_sample - (len as u64 - 1);
        Some(CapturePitchFrame {
            epoch: sample.epoch,
            stream_sample: first + (len as u64 - 1) / 2,
            observation: self.analyze(),
        })
    }

    /// {
    ///   責務: [`reset`: 窓と連続性の状態を捨てる]
    ///   処理: [1: 充填数と直前位置を初期化する]
    ///   引数: [self: 推定器]
    ///   戻り値: [なし]
    /// }
    fn reset(&mut self) {
        self.filled = 0;
        self.last = None;
    }
}

/// {
///   責務: [`map_capture_frame`: capture timeline上のframeをepochのclock mappingでsong timelineへ写す]
///   処理: [1: Capture domainで`ClockSession::resolve`する, 2: song位置のframeを作る]
///   引数: [clock: 現epochの`ClockSession`, frame: capture frame]
///   戻り値: [`Result<(PitchFrame, MappedSongSample), ClockError>`: 採点frameと遅延補正の品質]
/// }
///
/// # Errors
/// Propagates `ClockError` (`StaleEpoch`, `UnanchoredDomain`, `BeforeSongStart`, overflow).
pub fn map_capture_frame(
    clock: &ClockSession,
    frame: CapturePitchFrame,
) -> Result<(PitchFrame, MappedSongSample), ClockError> {
    let mapped = clock.resolve(ClockDomain::Capture, frame.epoch, frame.stream_sample)?;
    Ok((
        PitchFrame {
            epoch: mapped.epoch,
            song_sample: mapped.song_sample,
            observation: frame.observation,
        },
        mapped,
    ))
}
