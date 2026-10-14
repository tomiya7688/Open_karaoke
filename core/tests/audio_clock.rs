//! Hardware-independent clock-contract regression fixtures for Issue #40.

use open_karaoke_core::audio_clock::{
    ClockAnchor, ClockDomain, ClockError, ClockSession, LatencyCalibration, SONG_SAMPLE_RATE,
};

fn anchor(
    session: &ClockSession,
    domain: ClockDomain,
    stream_sample: u64,
    song_sample: u64,
    drift_ppm: i32,
    latency: LatencyCalibration,
) -> ClockAnchor {
    ClockAnchor {
        epoch: session.epoch(),
        domain,
        stream_sample,
        song_sample,
        drift_ppm,
        latency,
    }
}

#[test]
fn unmeasured_offset_is_not_reported_as_zero_latency() {
    let mut session = ClockSession::new();
    assert_eq!(SONG_SAMPLE_RATE, 48_000);
    let e = session.epoch();
    session
        .bind(anchor(
            &session,
            ClockDomain::Output,
            96_000,
            0,
            0,
            LatencyCalibration::Unknown,
        ))
        .unwrap();
    let resolved = session.resolve(ClockDomain::Output, e, 144_000).unwrap();
    assert_eq!(resolved.song_sample, 48_000);
    assert_eq!(resolved.latency, LatencyCalibration::Unknown);
    assert_eq!(
        session.resolve(ClockDomain::Capture, e, 144_000),
        Err(ClockError::UnanchoredDomain)
    );
}

#[test]
fn signed_latency_compensation_preserves_quality_and_pre_roll() {
    let mut session = ClockSession::new();
    let e = session.epoch();
    let measured = LatencyCalibration::Measured {
        compensation_samples: -2_400,
        uncertainty_samples: 48,
    };
    session
        .bind(anchor(
            &session,
            ClockDomain::Capture,
            20_000,
            12_000,
            0,
            measured,
        ))
        .unwrap();
    let sample = session.resolve(ClockDomain::Capture, e, 20_000).unwrap();
    assert_eq!(sample.song_sample, 9_600);
    assert_eq!(sample.latency, measured);
    assert_eq!(
        session.resolve(ClockDomain::Capture, e, 0),
        Err(ClockError::BeforeSongStart)
    );
}

#[test]
fn positive_and_negative_drift_are_applied_in_integer_samples() {
    let mut plus = ClockSession::new();
    let ep = plus.epoch();
    plus.bind(anchor(
        &plus,
        ClockDomain::Output,
        10_000,
        20_000,
        100,
        LatencyCalibration::Estimated {
            compensation_samples: 100,
        },
    ))
    .unwrap();
    // 10 seconds * 48 kHz, +100 ppm => +48 samples.
    assert_eq!(
        plus.resolve(ClockDomain::Output, ep, 490_000)
            .unwrap()
            .song_sample,
        500_148
    );
    assert_eq!(
        plus.resolve(ClockDomain::Output, ep, 9_999)
            .unwrap()
            .song_sample,
        20_099
    );

    let mut minus = ClockSession::new();
    let ep = minus.epoch();
    minus
        .bind(anchor(
            &minus,
            ClockDomain::Capture,
            10_000,
            20_000,
            -100,
            LatencyCalibration::Unknown,
        ))
        .unwrap();
    assert_eq!(
        minus
            .resolve(ClockDomain::Capture, ep, 490_000)
            .unwrap()
            .song_sample,
        499_952
    );
}

#[test]
fn epoch_change_clears_both_domains_and_rejects_stale_frames() {
    let mut session = ClockSession::new();
    let old = session.epoch();
    session
        .bind(anchor(
            &session,
            ClockDomain::Output,
            0,
            0,
            0,
            LatencyCalibration::Unknown,
        ))
        .unwrap();
    session
        .bind(anchor(
            &session,
            ClockDomain::Capture,
            0,
            0,
            0,
            LatencyCalibration::Estimated {
                compensation_samples: 500,
            },
        ))
        .unwrap();

    // Seek / pause-resume / device reset / dropped stream: new epoch.
    let new = session.begin_new_epoch().unwrap();
    assert_ne!(new, old);
    for domain in [ClockDomain::Output, ClockDomain::Capture] {
        assert_eq!(
            session.resolve(domain, old, 24_000),
            Err(ClockError::StaleEpoch)
        );
        assert_eq!(
            session.resolve(domain, new, 24_000),
            Err(ClockError::UnanchoredDomain)
        );
    }
    let old_anchor = ClockAnchor {
        epoch: old,
        domain: ClockDomain::Capture,
        stream_sample: 0,
        song_sample: 0,
        drift_ppm: 0,
        latency: LatencyCalibration::Unknown,
    };
    assert_eq!(session.bind(old_anchor), Err(ClockError::StaleEpoch));
    session
        .bind(anchor(
            &session,
            ClockDomain::Capture,
            20_000,
            96_000,
            0,
            LatencyCalibration::Unknown,
        ))
        .unwrap();
    assert_eq!(
        session
            .resolve(ClockDomain::Capture, new, 20_000)
            .unwrap()
            .song_sample,
        96_000
    );
}

#[test]
fn conflicting_anchors_and_implausible_drift_are_rejected() {
    let mut session = ClockSession::new();
    let valid = anchor(
        &session,
        ClockDomain::Output,
        0,
        0,
        0,
        LatencyCalibration::Unknown,
    );
    session.bind(valid).unwrap();
    assert_eq!(session.bind(valid), Err(ClockError::DomainAlreadyAnchored));
    assert_eq!(
        session.bind(anchor(
            &session,
            ClockDomain::Capture,
            0,
            0,
            2_001,
            LatencyCalibration::Unknown,
        )),
        Err(ClockError::DriftOutOfRange)
    );
}

#[test]
fn large_counters_are_checked_not_saturated_or_wrapped() {
    let mut session = ClockSession::new();
    let ep = session.epoch();
    session
        .bind(anchor(
            &session,
            ClockDomain::Output,
            0,
            u64::MAX,
            0,
            LatencyCalibration::Unknown,
        ))
        .unwrap();
    assert_eq!(
        session.resolve(ClockDomain::Output, ep, 1),
        Err(ClockError::ArithmeticOverflow)
    );
}
