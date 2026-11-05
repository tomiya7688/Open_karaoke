//! Device-free synthetic tests for Issue #107 slice 107.4 (presented timestamp + `clock_quality`).
//! Hardware audibility is NOT measured here; see docs/design/issue-107-karaoke-session.md.

use open_karaoke_core::audio_clock::{ClockAnchor, ClockDomain, ClockSession, LatencyCalibration};
use open_karaoke_core::karaoke_session::{
    ClockQuality, CommandEnvelope, EventOrigin, KaraokeCommand, KaraokeError, KaraokeSession,
    KaraokeSessionRegistry, KaraokeState, PositionSource, PresentedStreamSample, ScoringMode,
};
use uuid::Uuid;

const DURATION: u64 = 48_000 * 10;

/// { 責務: [Playing状態のセッションとそのinstance IDを作る] }
fn playing() -> (KaraokeSession, Uuid) {
    let instance = Uuid::new_v4();
    let mut s = KaraokeSession::new(
        Uuid::new_v4(),
        instance,
        Uuid::new_v4(),
        DURATION,
        ScoringMode::Practice,
    )
    .unwrap();
    assert!(s.finish_preparing(Ok(())));
    send(&mut s, instance, KaraokeCommand::Play);
    (s, instance)
}

/// { 責務: [現revisionでコマンドを送り成功を要求する] }
fn send(s: &mut KaraokeSession, instance: Uuid, command: KaraokeCommand) {
    let snap = s.snapshot();
    s.execute(CommandEnvelope {
        core_instance_id: instance,
        session_id: snap.session_id,
        command_id: Uuid::new_v4(),
        expected_revision: snap.state_revision,
        command,
    })
    .unwrap();
}

/// { 責務: [output anchorを持つClockSessionを作る(stream 0 が song 0)] }
fn clock_with(latency: LatencyCalibration) -> ClockSession {
    let mut clock = ClockSession::new();
    clock
        .bind(ClockAnchor {
            epoch: clock.epoch(),
            domain: ClockDomain::Output,
            stream_sample: 0,
            song_sample: 0,
            drift_ppm: 0,
            latency,
        })
        .unwrap();
    clock
}

#[test]
fn t1_unknown_latency_is_reported_as_unknown_not_zero_or_measured() {
    let (mut s, _) = playing();
    let clock = clock_with(LatencyCalibration::Unknown);
    let epoch = s.snapshot().transport_epoch;
    let got = s
        .report_presented_from_clock(epoch, &clock, clock.epoch(), PresentedStreamSample(4_800))
        .unwrap();
    assert_eq!(got.position_song_sample, 4_800);
    assert_eq!(got.position_source, PositionSource::Presented);
    assert_eq!(got.clock_quality, ClockQuality::Unknown);
}

#[test]
fn t2_estimated_and_measured_quality_are_preserved_without_promotion() {
    let (mut s, _) = playing();
    let epoch = s.snapshot().transport_epoch;
    let est = clock_with(LatencyCalibration::Estimated {
        compensation_samples: 0,
    });
    let got = s
        .report_presented_from_clock(epoch, &est, est.epoch(), PresentedStreamSample(100))
        .unwrap();
    assert_eq!(got.clock_quality, ClockQuality::Estimated);
    let measured = clock_with(LatencyCalibration::Measured {
        compensation_samples: 0,
        uncertainty_samples: 96,
    });
    let got = s
        .report_presented_from_clock(
            epoch,
            &measured,
            measured.epoch(),
            PresentedStreamSample(200),
        )
        .unwrap();
    assert_eq!(
        got.clock_quality,
        ClockQuality::Measured {
            uncertainty_samples: 96
        }
    );
}

#[test]
fn t3_position_follows_the_presented_mapping_not_the_raw_stream_count() {
    let (mut s, _) = playing();
    let epoch = s.snapshot().transport_epoch;
    // 2400 samples of output latency: what is audible now was written 2400 samples ago.
    let clock = clock_with(LatencyCalibration::Measured {
        compensation_samples: -2_400,
        uncertainty_samples: 48,
    });
    let got = s
        .report_presented_from_clock(epoch, &clock, clock.epoch(), PresentedStreamSample(10_000))
        .unwrap();
    assert_eq!(got.position_song_sample, 7_600);
    assert_ne!(got.position_song_sample, 10_000);
}

#[test]
fn t4_clock_failures_leave_state_unchanged_and_stale_clock_is_counted() {
    let (mut s, _) = playing();
    let epoch = s.snapshot().transport_epoch;
    let mut clock = clock_with(LatencyCalibration::Unknown);
    let old_clock_epoch = clock.epoch();
    clock.begin_new_epoch().unwrap();
    let before = s.snapshot();
    let err = s
        .report_presented_from_clock(epoch, &clock, old_clock_epoch, PresentedStreamSample(1))
        .unwrap_err();
    assert_eq!(err.code(), "clock_stale_epoch");
    let after = s.snapshot();
    assert_eq!(after.stale_rejections, before.stale_rejections + 1);
    assert_eq!(after.position_song_sample, before.position_song_sample);
    assert_eq!(after.position_source, PositionSource::None);
    // The new epoch has no output anchor yet.
    let err = s
        .report_presented_from_clock(epoch, &clock, clock.epoch(), PresentedStreamSample(1))
        .unwrap_err();
    assert_eq!(err.code(), "clock_unavailable");
    assert_eq!(s.snapshot().stale_rejections, before.stale_rejections + 1);
    // Pre-roll before song start is not clamped to zero.
    let pre = clock_with(LatencyCalibration::Estimated {
        compensation_samples: -500,
    });
    let err = s
        .report_presented_from_clock(epoch, &pre, pre.epoch(), PresentedStreamSample(100))
        .unwrap_err();
    assert!(matches!(err, KaraokeError::ClockRejected(_)));
    assert_eq!(s.snapshot().position_source, PositionSource::None);
}

#[test]
fn t5_session_rules_still_apply_stale_epoch_regression_range_and_state() {
    let (mut s, instance) = playing();
    let epoch = s.snapshot().transport_epoch;
    let clock = clock_with(LatencyCalibration::Unknown);
    s.report_presented_from_clock(epoch, &clock, clock.epoch(), PresentedStreamSample(5_000))
        .unwrap();
    let err = s
        .report_presented_from_clock(epoch, &clock, clock.epoch(), PresentedStreamSample(4_000))
        .unwrap_err();
    assert_eq!(err.code(), "position_regressed");
    let err = s
        .report_presented_from_clock(
            epoch,
            &clock,
            clock.epoch(),
            PresentedStreamSample(DURATION + 1),
        )
        .unwrap_err();
    assert_eq!(err.code(), "out_of_range");
    let before = s.snapshot().stale_rejections;
    let err = s
        .report_presented_from_clock(
            epoch + 9,
            &clock,
            clock.epoch(),
            PresentedStreamSample(6_000),
        )
        .unwrap_err();
    assert_eq!(err.code(), "stale_epoch");
    assert_eq!(s.snapshot().stale_rejections, before + 1);
    send(&mut s, instance, KaraokeCommand::Pause);
    let epoch = s.snapshot().transport_epoch;
    let err = s
        .report_presented_from_clock(epoch, &clock, clock.epoch(), PresentedStreamSample(6_000))
        .unwrap_err();
    assert_eq!(err.code(), "not_playing");
    assert_eq!(s.snapshot().position_song_sample, 5_000);
}

#[test]
fn t6_seek_resets_quality_and_reaching_the_end_keeps_it() {
    let (mut s, instance) = playing();
    let epoch = s.snapshot().transport_epoch;
    let clock = clock_with(LatencyCalibration::Measured {
        compensation_samples: 0,
        uncertainty_samples: 10,
    });
    s.report_presented_from_clock(epoch, &clock, clock.epoch(), PresentedStreamSample(1_000))
        .unwrap();
    send(
        &mut s,
        instance,
        KaraokeCommand::Seek { song_sample: 2_000 },
    );
    let snap = s.snapshot();
    assert_eq!(snap.position_source, PositionSource::Committed);
    assert_eq!(snap.clock_quality, ClockQuality::Unknown);
    let end = s
        .report_presented_from_clock(
            snap.transport_epoch,
            &clock,
            clock.epoch(),
            PresentedStreamSample(DURATION),
        )
        .unwrap();
    assert_eq!(end.state, KaraokeState::Finished);
    assert_eq!(
        end.clock_quality,
        ClockQuality::Measured {
            uncertainty_samples: 10
        }
    );
}

#[test]
fn t7_plain_report_presented_has_unknown_quality_and_json_carries_the_field() {
    let (mut s, _) = playing();
    let epoch = s.snapshot().transport_epoch;
    let got = s.report_presented(epoch, 100).unwrap();
    assert_eq!(got.clock_quality, ClockQuality::Unknown);
    let json = serde_json::to_value(&got).unwrap();
    assert_eq!(json["clock_quality"]["kind"], "unknown");
    let clock = clock_with(LatencyCalibration::Measured {
        compensation_samples: 0,
        uncertainty_samples: 7,
    });
    let got = s
        .report_presented_from_clock(epoch, &clock, clock.epoch(), PresentedStreamSample(200))
        .unwrap();
    let json = serde_json::to_value(&got).unwrap();
    assert_eq!(json["clock_quality"]["kind"], "measured");
    assert_eq!(json["clock_quality"]["uncertainty_samples"], 7);
    assert_eq!(json["position_source"], "presented");
}

#[test]
fn t8_registry_rejects_foreign_origin_before_using_the_clock() {
    let instance = Uuid::new_v4();
    let mut reg = KaraokeSessionRegistry::new(instance);
    let snap = reg
        .create(Uuid::new_v4(), DURATION, ScoringMode::Practice)
        .unwrap();
    reg.session_mut(snap.session_id)
        .unwrap()
        .finish_preparing(Ok(()));
    let clock = clock_with(LatencyCalibration::Unknown);
    let origin = |core_instance_id, session_id, epoch| EventOrigin {
        core_instance_id,
        session_id,
        epoch,
    };
    let err = reg
        .report_presented_from_clock(
            origin(Uuid::new_v4(), snap.session_id, 1),
            &clock,
            clock.epoch(),
            PresentedStreamSample(1),
        )
        .unwrap_err();
    assert_eq!(err.code(), "stale_instance");
    let err = reg
        .report_presented_from_clock(
            origin(instance, Uuid::new_v4(), 1),
            &clock,
            clock.epoch(),
            PresentedStreamSample(1),
        )
        .unwrap_err();
    assert_eq!(err.code(), "session_not_found");
}

#[test]
fn quality_mapping_never_promotes() {
    assert_eq!(
        ClockQuality::from_latency(LatencyCalibration::Unknown),
        ClockQuality::Unknown
    );
    assert_eq!(
        ClockQuality::from_latency(LatencyCalibration::Estimated {
            compensation_samples: 5
        }),
        ClockQuality::Estimated
    );
}

/// { 責務: [clockを新世代へ進め、output anchorを張り直す(seek/recover後の再anchorを模す)] }
fn reanchor(clock: &mut ClockSession, song_sample: u64) {
    let epoch = clock.begin_new_epoch().unwrap();
    clock
        .bind(ClockAnchor {
            epoch,
            domain: ClockDomain::Output,
            stream_sample: 0,
            song_sample,
            drift_ppm: 0,
            latency: LatencyCalibration::Unknown,
        })
        .unwrap();
}

/// Issue #133 AC1/AC2: seek後は受理clock世代と一致しない提示値を拒否し、世代更新後は受理する。
#[test]
fn t9_seek_then_old_or_unbound_clock_epoch_is_rejected_until_rebound() {
    let (mut s, instance) = playing();
    let mut clock = clock_with(LatencyCalibration::Unknown);
    let old_clock_epoch = clock.epoch();
    s.set_clock_epoch(old_clock_epoch);
    assert_eq!(s.clock_epoch(), Some(old_clock_epoch));
    let epoch = s.snapshot().transport_epoch;
    s.report_presented_from_clock(epoch, &clock, old_clock_epoch, PresentedStreamSample(100))
        .unwrap();

    send(
        &mut s,
        instance,
        KaraokeCommand::Seek {
            song_sample: 48_000,
        },
    );
    let epoch = s.snapshot().transport_epoch;
    reanchor(&mut clock, 48_000);
    let new_clock_epoch = clock.epoch();
    let before = s.snapshot();

    // sessionへ新世代を伝える前: 新clock世代の値は束縛不一致で拒否(古い世代の混入を防ぐ)。
    let err = s
        .report_presented_from_clock(epoch, &clock, new_clock_epoch, PresentedStreamSample(10))
        .unwrap_err();
    assert_eq!(err.code(), "clock_stale_epoch");
    assert_eq!(s.snapshot().stale_rejections, before.stale_rejections + 1);
    assert_eq!(
        s.snapshot().position_song_sample,
        before.position_song_sample
    );

    s.set_clock_epoch(new_clock_epoch);
    // 古いclock世代(seek前)の提示値は拒否される。
    let err = s
        .report_presented_from_clock(epoch, &clock, old_clock_epoch, PresentedStreamSample(10))
        .unwrap_err();
    assert_eq!(err.code(), "clock_stale_epoch");
    assert_eq!(s.snapshot().stale_rejections, before.stale_rejections + 2);
    assert_eq!(
        s.snapshot().position_song_sample,
        before.position_song_sample
    );

    // 新世代は受理され、新anchorで曲位置へ写る。
    let got = s
        .report_presented_from_clock(epoch, &clock, new_clock_epoch, PresentedStreamSample(480))
        .unwrap();
    assert_eq!(got.position_song_sample, 48_480);
}

/// Issue #133 AC3: 受理clock世代が未設定なら従来どおりclock側の検査のみ(互換)。
#[test]
fn t10_unbound_session_keeps_the_legacy_clock_only_check() {
    let (mut s, _) = playing();
    assert_eq!(s.clock_epoch(), None);
    let clock = clock_with(LatencyCalibration::Unknown);
    let epoch = s.snapshot().transport_epoch;
    let got = s
        .report_presented_from_clock(epoch, &clock, clock.epoch(), PresentedStreamSample(480))
        .unwrap();
    assert_eq!(got.position_song_sample, 480);
}

/// Issue #133: registryは出所を検証してから受理clock世代を更新する。
#[test]
fn t11_registry_set_clock_epoch_validates_origin_and_binds() {
    let instance = Uuid::new_v4();
    let mut reg = KaraokeSessionRegistry::new(instance);
    let snap = reg
        .create(Uuid::new_v4(), DURATION, ScoringMode::Practice)
        .unwrap();
    let clock = clock_with(LatencyCalibration::Unknown);
    let own = EventOrigin {
        core_instance_id: instance,
        session_id: snap.session_id,
        epoch: snap.transport_epoch,
    };
    let foreign = EventOrigin {
        core_instance_id: Uuid::new_v4(),
        ..own
    };
    assert_eq!(
        reg.set_clock_epoch(foreign, clock.epoch())
            .unwrap_err()
            .code(),
        "stale_instance"
    );
    reg.set_clock_epoch(own, clock.epoch()).unwrap();
    assert_eq!(
        reg.session_mut(snap.session_id).unwrap().clock_epoch(),
        Some(clock.epoch())
    );
}
