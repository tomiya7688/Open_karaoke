//! Offline statistics for the physical measurements required by Issue #40.4.
//! This module only summarises numbers a hardware run supplies; it never touches a
//! device and a passing synthetic test does not verify any hardware latency.

#![allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
use std::fmt;

use crate::audio_clock::{LatencyCalibration, SONG_SAMPLE_RATE};

/// 実測loopbackに必要な最小impulse数。
pub const MIN_LOOPBACK_IMPULSES: usize = 30;
/// drift評価に必要な最小観測数。
pub const MIN_DRIFT_OBSERVATIONS: usize = 8;
/// drift評価に必要な最小経過時間(48 kHz sample数、30分)。
pub const MIN_DRIFT_SPAN_SAMPLES: u64 = 30 * 60 * SONG_SAMPLE_RATE as u64;
const MAX_ABS_DRIFT_PPM: f64 = 2_000.0;

/// { 責務: 校正統計の入力不備を呼出元へ区別して返す }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CalibrationError {
    TooFewSamples,
    NonMonotonic,
    SpanTooShort,
    DriftOutOfRange,
}

impl fmt::Display for CalibrationError {
    /// { 責務: 人間向けの失敗理由を書き出す; 引数: [f: 出力先]; 戻り値: [fmt結果] }
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::TooFewSamples => "not enough samples for a calibration claim",
            Self::NonMonotonic => "stream observations are not strictly increasing",
            Self::SpanTooShort => "observation span is shorter than the required drift window",
            Self::DriftOutOfRange => "estimated drift exceeds the supported ppm range",
        })
    }
}

impl std::error::Error for CalibrationError {}

/// { 責務: loopback往復遅延のmedian/p95/標準偏差を48 kHz sampleとmsで保持する }
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LatencyReport {
    pub count: usize,
    pub median_samples: i64,
    pub p95_samples: i64,
    pub std_dev_samples: f64,
    pub median_ms: f64,
    pub p95_ms: f64,
    pub std_dev_ms: f64,
    /// medianからの絶対偏差のp95。`Measured`の不確かさに使う。
    pub p95_abs_deviation_samples: u64,
}

/// { 責務: `nearest-rank`方式でp番目(百分率)の値を返す; 引数: [sorted: 昇順の非空列, percent: 1..=100]; 戻り値: [値] }
fn nearest_rank<T: Copy>(sorted: &[T], percent: usize) -> T {
    let rank = (sorted.len() * percent).div_ceil(100).max(1);
    sorted[rank - 1]
}

/// { 責務: sample数をmsへ換算する; 引数: [samples]; 戻り値: [ms] }
fn to_ms(samples: f64) -> f64 {
    samples * 1_000.0 / f64::from(SONG_SAMPLE_RATE)
}

/// {
///   責務: 物理loopbackの往復遅延(48 kHz sample)を統計に要約する
///   処理: [1: 件数検査, 2: 昇順化, 3: median/p95/標準偏差/偏差p95]
///   引数: [`round_trip_samples`: impulseごとの出力から再捕捉までの遅延]
///   戻り値: [`LatencyReport`]、30件未満は`TooFewSamples`
/// }
///
/// # Errors
/// 30件未満なら`TooFewSamples`。
pub fn summarize_latency(round_trip_samples: &[i64]) -> Result<LatencyReport, CalibrationError> {
    let count = round_trip_samples.len();
    if count < MIN_LOOPBACK_IMPULSES {
        return Err(CalibrationError::TooFewSamples);
    }
    let mut sorted = round_trip_samples.to_vec();
    sorted.sort_unstable();
    let median = if count % 2 == 1 {
        sorted[count / 2]
    } else {
        let (a, b) = (sorted[count / 2 - 1], sorted[count / 2]);
        a + (b - a) / 2
    };
    let p95 = nearest_rank(&sorted, 95);
    let n = count as f64;
    let mean = sorted.iter().map(|&v| v as f64).sum::<f64>() / n;
    let variance = sorted
        .iter()
        .map(|&v| (v as f64 - mean).powi(2))
        .sum::<f64>()
        / (n - 1.0);
    let std_dev = variance.sqrt();
    let mut deviations: Vec<u64> = sorted.iter().map(|&v| v.abs_diff(median)).collect();
    deviations.sort_unstable();
    Ok(LatencyReport {
        count,
        median_samples: median,
        p95_samples: p95,
        std_dev_samples: std_dev,
        median_ms: to_ms(median as f64),
        p95_ms: to_ms(p95 as f64),
        std_dev_ms: to_ms(std_dev),
        p95_abs_deviation_samples: nearest_rank(&deviations, 95),
    })
}

/// {
///   責務: 往復遅延の実測をcapture streamの`Measured`校正へ変換する
///   処理: [1: medianの符号を反転して補正量にする, 2: 偏差p95を不確かさにする]
///   引数: [report: `summarize_latency`の結果]
///   戻り値: [`LatencyCalibration::Measured`]
///   補足: [往復遅延はcaptureが曲より遅れる量なので補正は負。音響伝播の分離は呼出元の責務]
/// }
#[must_use]
pub fn capture_calibration(report: &LatencyReport) -> LatencyCalibration {
    LatencyCalibration::Measured {
        compensation_samples: -report.median_samples,
        uncertainty_samples: report.p95_abs_deviation_samples,
    }
}

/// { 責務: 2つのclock domainの相対driftと当てはめ残差を保持する }
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct DriftEstimate {
    pub ppm: f64,
    /// `ClockAnchor::drift_ppm`へ渡せる丸め値。
    pub rounded_ppm: i32,
    pub span_samples: u64,
    pub max_residual_samples: f64,
}

/// {
///   責務: 同時観測した基準/対象counterから相対drift(ppm)を最小二乗で推定する
///   処理: [1: 件数・単調性・30分以上の経過を検査, 2: 中心化した最小二乗で傾き, 3: 残差最大値]
///   引数: [observations: (基準stream sample, 対象stream sample)の同時観測、同一epoch内]
///   戻り値: [`DriftEstimate`]、対象が基準より速ければppmは正
/// }
///
/// # Errors
/// 件数不足、非単調、30分未満、範囲外driftをそれぞれ区別して返す。
pub fn estimate_drift(observations: &[(u64, u64)]) -> Result<DriftEstimate, CalibrationError> {
    if observations.len() < MIN_DRIFT_OBSERVATIONS {
        return Err(CalibrationError::TooFewSamples);
    }
    if observations
        .windows(2)
        .any(|w| w[1].0 <= w[0].0 || w[1].1 <= w[0].1)
    {
        return Err(CalibrationError::NonMonotonic);
    }
    let first = observations[0];
    let last = observations[observations.len() - 1];
    let span = last.0 - first.0;
    if span < MIN_DRIFT_SPAN_SAMPLES {
        return Err(CalibrationError::SpanTooShort);
    }
    let points: Vec<(f64, f64)> = observations
        .iter()
        .map(|&(x, y)| ((x - first.0) as f64, (y - first.1) as f64))
        .collect();
    let n = points.len() as f64;
    let mean_x = points.iter().map(|p| p.0).sum::<f64>() / n;
    let mean_y = points.iter().map(|p| p.1).sum::<f64>() / n;
    let sxy: f64 = points.iter().map(|p| (p.0 - mean_x) * (p.1 - mean_y)).sum();
    let sxx: f64 = points.iter().map(|p| (p.0 - mean_x).powi(2)).sum();
    let slope = sxy / sxx;
    let ppm = (slope - 1.0) * 1_000_000.0;
    if !ppm.is_finite() || ppm.abs() > MAX_ABS_DRIFT_PPM {
        return Err(CalibrationError::DriftOutOfRange);
    }
    let max_residual = points
        .iter()
        .map(|p| (p.1 - (mean_y + slope * (p.0 - mean_x))).abs())
        .fold(0.0_f64, f64::max);
    Ok(DriftEstimate {
        ppm,
        rounded_ppm: ppm.round() as i32,
        span_samples: span,
        max_residual_samples: max_residual,
    })
}
