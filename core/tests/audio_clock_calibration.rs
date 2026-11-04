//! Synthetic checks for the #40.4 statistics helpers. No hardware is measured.

use open_karaoke_core::audio_clock::LatencyCalibration;
use open_karaoke_core::audio_clock_calibration::{
    CalibrationError, MIN_DRIFT_SPAN_SAMPLES, capture_calibration, estimate_drift,
    summarize_latency,
};

#[test]
fn latency_requires_thirty_impulses() {
    assert_eq!(
        summarize_latency(&[480; 29]),
        Err(CalibrationError::TooFewSamples)
    );
}

#[test]
fn latency_summary_reports_median_p95_and_std_dev() {
    let samples: Vec<i64> = (1..=40).map(|v| 1_000 + v).collect();
    let r = summarize_latency(&samples).unwrap();
    assert_eq!(r.count, 40);
    assert_eq!(r.median_samples, 1_020);
    assert_eq!(r.p95_samples, 1_038);
    assert!((r.std_dev_samples - 11.690_451).abs() < 1e-5);
    assert!((r.median_ms - 1_020.0 / 48.0).abs() < 1e-9);
    assert_eq!(
        capture_calibration(&r),
        LatencyCalibration::Measured {
            compensation_samples: -1_020,
            uncertainty_samples: 19,
        }
    );
}

#[test]
fn drift_recovers_known_ppm_with_zero_residual() {
    let step = MIN_DRIFT_SPAN_SAMPLES / 8;
    let obs: Vec<(u64, u64)> = (0..10_u64)
        .map(|i| (i * step, i * step + i * step / 10_000))
        .collect();
    let d = estimate_drift(&obs).unwrap();
    assert!((d.ppm - 100.0).abs() < 0.5, "{}", d.ppm);
    assert_eq!(d.rounded_ppm, 100);
    assert!(d.max_residual_samples < 1.0);
}

#[test]
fn drift_rejects_short_nonmonotonic_and_extreme_input() {
    let step = MIN_DRIFT_SPAN_SAMPLES / 8;
    let ok: Vec<(u64, u64)> = (0..10_u64).map(|i| (i * step, i * step)).collect();
    assert_eq!(
        estimate_drift(&ok[..3]),
        Err(CalibrationError::TooFewSamples)
    );
    let short: Vec<(u64, u64)> = (0..10_u64).map(|i| (i * 100, i * 100)).collect();
    assert_eq!(estimate_drift(&short), Err(CalibrationError::SpanTooShort));
    let mut back = ok.clone();
    back[5].1 = back[4].1;
    assert_eq!(estimate_drift(&back), Err(CalibrationError::NonMonotonic));
    let fast: Vec<(u64, u64)> = (0..10_u64)
        .map(|i| (i * step, i * step * 11 / 10))
        .collect();
    assert_eq!(
        estimate_drift(&fast),
        Err(CalibrationError::DriftOutOfRange)
    );
}
