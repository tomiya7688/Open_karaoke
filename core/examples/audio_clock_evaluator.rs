//! Synthetic, deterministic audio clock evaluator. No hardware is measured.

use std::{env, error::Error, fs, io, path::PathBuf};

use open_karaoke_core::audio_clock::{
    ClockAnchor, ClockDomain, ClockError, ClockSession, LatencyCalibration,
};

fn main() -> Result<(), Box<dyn Error>> {
    let mut session = ClockSession::new();
    let epoch = session.epoch();
    session.bind(ClockAnchor {
        epoch,
        domain: ClockDomain::Output,
        stream_sample: 10_000,
        song_sample: 20_000,
        drift_ppm: 100,
        latency: LatencyCalibration::Estimated {
            compensation_samples: 100,
        },
    })?;
    session.bind(ClockAnchor {
        epoch,
        domain: ClockDomain::Capture,
        stream_sample: 20_000,
        song_sample: 12_000,
        drift_ppm: -100,
        latency: LatencyCalibration::Measured {
            compensation_samples: -2_400,
            uncertainty_samples: 48,
        },
    })?;

    let cases = [
        (ClockDomain::Output, 10_000, 20_100),
        (ClockDomain::Output, 490_000, 500_148),
        (ClockDomain::Output, 9_999, 20_099),
        (ClockDomain::Capture, 20_000, 9_600),
        (ClockDomain::Capture, 500_000, 489_552),
    ];
    let mut max_absolute_error_samples = 0_u64;
    for (domain, source_frame, expected) in cases {
        let actual = session.resolve(domain, epoch, source_frame)?.song_sample;
        max_absolute_error_samples = max_absolute_error_samples.max(actual.abs_diff(expected));
    }

    let before_start_rejected = matches!(
        session.resolve(ClockDomain::Capture, epoch, 0),
        Err(ClockError::BeforeSongStart)
    );
    let next_epoch = session.begin_new_epoch()?;
    let stale_epoch_rejected = matches!(
        session.resolve(ClockDomain::Output, epoch, 490_000),
        Err(ClockError::StaleEpoch)
    );
    let unanchored_new_epoch_rejected = matches!(
        session.resolve(ClockDomain::Capture, next_epoch, 500_000),
        Err(ClockError::UnanchoredDomain)
    );

    let mut unknown = ClockSession::new();
    let unknown_epoch = unknown.epoch();
    unknown.bind(ClockAnchor {
        epoch: unknown_epoch,
        domain: ClockDomain::Capture,
        stream_sample: 0,
        song_sample: 0,
        drift_ppm: 0,
        latency: LatencyCalibration::Unknown,
    })?;
    let unknown_preserved = unknown
        .resolve(ClockDomain::Capture, unknown_epoch, 0)?
        .latency
        == LatencyCalibration::Unknown;

    let passed = max_absolute_error_samples == 0
        && before_start_rejected
        && stale_epoch_rejected
        && unanchored_new_epoch_rejected
        && unknown_preserved;
    let report = serde_json::json!({
        "evaluator": "audio-clock-synthetic-v1",
        "dataset_version": "integer-drift-latency-fixtures-v1",
        "sample_rate": 48000,
        "quality_gate_applied": true,
        "hardware_latency_verified": false,
        "metrics": {
            "cases": cases.len(),
            "max_absolute_error_samples": max_absolute_error_samples,
            "before_start_rejected": before_start_rejected,
            "stale_epoch_rejected": stale_epoch_rejected,
            "unanchored_new_epoch_rejected": unanchored_new_epoch_rejected,
            "unknown_calibration_preserved": unknown_preserved
        },
        "passed": passed
    });

    let output = env::args()
        .nth(1)
        .unwrap_or_else(|| "artifacts/audio-clock-regression.json".to_owned());
    let output = PathBuf::from(output);
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(&output, serde_json::to_vec_pretty(&report)?)?;
    println!("{}", serde_json::to_string(&report)?);
    if !passed {
        return Err(io::Error::other("synthetic audio clock contract regression").into());
    }
    Ok(())
}
