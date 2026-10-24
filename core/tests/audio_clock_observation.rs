//! Issue #40.2: logical counter observations through the public audio messages.

use open_karaoke_core::{
    audio_clock::{
        ClockAnchor, ClockDiscontinuity, ClockDomain, ClockEpoch, ClockError, ClockInvalidation,
        ClockSession, LatencyCalibration,
    },
    audio_engine::{AudioCounters, AudioIo},
};

/// {
///   責務: [`bind_domains`: 両 domain に未校正の合成 anchor を設定する]
///   処理: [1: 現在の epoch に同じ原点の独立した anchor を登録する]
///   引数: [session: 検証対象の時計]
///   戻り値: []
/// }
fn bind_domains(session: &mut ClockSession) {
    for domain in [ClockDomain::Output, ClockDomain::Capture] {
        session
            .bind(ClockAnchor {
                epoch: session.epoch(),
                domain,
                stream_sample: 0,
                song_sample: 0,
                drift_ppm: 0,
                latency: LatencyCalibration::Unknown,
            })
            .unwrap();
    }
}

/// {
///   責務: [`assert_invalidated`: 両 domain の旧 epoch と未設定 anchor を検査する]
///   処理: [1: 旧 epoch の拒否を確認する, 2: 新 epoch の anchor 破棄を確認する]
///   引数: [session: 無効化後の時計, old: 無効化前の epoch]
///   戻り値: []
/// }
fn assert_invalidated(session: &ClockSession, old: ClockEpoch) {
    assert_eq!(session.epoch().sequence(), old.sequence() + 1);
    for domain in [ClockDomain::Output, ClockDomain::Capture] {
        assert_eq!(session.resolve(domain, old, 0), Err(ClockError::StaleEpoch));
        assert_eq!(
            session.resolve(domain, session.epoch(), 0),
            Err(ClockError::UnanchoredDomain)
        );
    }
}

/// {
///   責務: [`play_pause_resume_invalidates_once_per_transition`: 再生制御ごとの世代分離を検査する]
///   処理: [1: 初期観測を行う, 2: 再生と一時停止と再開で anchor の破棄と再設定を検査する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn play_pause_resume_invalidates_once_per_transition() {
    let audio = AudioIo::new(32).unwrap();
    let mut session = ClockSession::new();
    assert_eq!(session.observe_audio(audio.counters()).unwrap(), None);
    for playing in [true, false, true] {
        bind_domains(&mut session);
        let old = session.epoch();
        audio.set_playing(playing);
        assert_eq!(
            session.observe_audio(audio.counters()).unwrap(),
            Some(ClockInvalidation {
                epoch: session.epoch(),
                reason: ClockDiscontinuity::PlaybackTransition,
            })
        );
        assert_invalidated(&session, old);
        assert_eq!(session.observe_audio(audio.counters()).unwrap(), None);
    }
    bind_domains(&mut session);
    assert_eq!(
        session
            .resolve(ClockDomain::Capture, session.epoch(), 48)
            .unwrap()
            .song_sample,
        48
    );
}

/// {
///   責務: [`sticky_device_errors_invalidate_only_on_new_flags`: 入出力 error の立上りを検査する]
///   処理: [1: 出力 error を観測する, 2: 継続 error の重複を防ぐ, 3: 新しい入力 error を観測する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn sticky_device_errors_invalidate_only_on_new_flags() {
    let audio = AudioIo::new(32).unwrap();
    let mut session = ClockSession::new();
    session.observe_audio(audio.counters()).unwrap();
    audio.mark_output_device_error();
    for input_error in [false, true] {
        if input_error {
            audio.mark_input_device_error();
        }
        bind_domains(&mut session);
        let old = session.epoch();
        let invalidation = session.observe_audio(audio.counters()).unwrap().unwrap();
        assert_eq!(invalidation.reason, ClockDiscontinuity::DeviceError);
        assert_eq!(invalidation.epoch, session.epoch());
        assert_invalidated(&session, old);
        assert_eq!(session.observe_audio(audio.counters()).unwrap(), None);
    }
}

/// {
///   責務: [`first_faulty_snapshot_invalidates_existing_anchors`: 初回の異常観測で anchor を破棄する]
///   処理: [1: error と capture 欠落を個別に作る, 2: baseline 未設定の両 anchor 破棄を確認する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn first_faulty_snapshot_invalidates_existing_anchors() {
    for reason in [
        ClockDiscontinuity::DeviceError,
        ClockDiscontinuity::CaptureGap,
    ] {
        let audio = AudioIo::new(32).unwrap();
        if reason == ClockDiscontinuity::DeviceError {
            audio.mark_input_device_error();
        } else {
            audio.capture_mock(&[0.25; 40], 1);
        }
        let mut session = ClockSession::new();
        bind_domains(&mut session);
        let old = session.epoch();
        assert_eq!(
            session
                .observe_audio(audio.counters())
                .unwrap()
                .unwrap()
                .reason,
            reason
        );
        assert_invalidated(&session, old);
        assert_eq!(session.observe_audio(audio.counters()).unwrap(), None);
    }
}

/// {
///   責務: [`capture_overrun_invalidates_both_domains`: capture queue の欠落と両時計の無効化を検査する]
///   処理: [1: 容量を超える capture を投入する, 2: 欠落数と単一 epoch 切替を確認する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn capture_overrun_invalidates_both_domains() {
    let audio = AudioIo::new(32).unwrap();
    let mut session = ClockSession::new();
    session.observe_audio(audio.counters()).unwrap();
    bind_domains(&mut session);
    let old = session.epoch();
    audio.capture_mock(&[0.25; 40], 1);
    let counters = audio.counters();
    assert_eq!(counters.microphone_frames_received, 40);
    assert_eq!(counters.input_overrun_frames, 8);
    assert_eq!(counters.input_queued_frames, 32);
    assert_eq!(
        session.observe_audio(counters).unwrap().unwrap().reason,
        ClockDiscontinuity::CaptureGap
    );
    assert_invalidated(&session, old);
    assert_eq!(session.observe_audio(counters).unwrap(), None);
}

/// {
///   責務: [`output_underrun_preserves_continuous_uncalibrated_clock`: 無音補完時の論理時計を検査する]
///   処理: [1: 空 queue から無音を生成する, 2: epoch と未校正状態を維持した時刻変換を確認する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn output_underrun_preserves_continuous_uncalibrated_clock() {
    let audio = AudioIo::new(32).unwrap();
    audio.set_playing(true);
    let mut session = ClockSession::new();
    session.observe_audio(audio.counters()).unwrap();
    bind_domains(&mut session);
    let epoch = session.epoch();
    let mut output = [1.0; 960];
    audio.render_mock(&mut output, 2);
    assert!(output.iter().all(|sample| *sample == 0.0));
    let counters = audio.counters();
    assert_eq!(counters.playback_sample_position, 480);
    assert_eq!(counters.output_underrun_frames, 480);
    assert_eq!(session.observe_audio(counters).unwrap(), None);
    assert_eq!(session.epoch(), epoch);
    let mapped = session.resolve(ClockDomain::Output, epoch, 480).unwrap();
    assert_eq!(mapped.song_sample, 480);
    assert_eq!(mapped.latency, LatencyCalibration::Unknown);
}

/// {
///   責務: [`replacement_with_counter_rollback_invalidates_once`: counter 巻き戻りで世代を分離する]
///   処理: [1: 旧音声の counter を進める, 2: 初期状態の音声へ交換して両 anchor の破棄を確認する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn replacement_with_counter_rollback_invalidates_once() {
    let audio = AudioIo::new(32).unwrap();
    audio.set_playing(true);
    audio.render_mock(&mut [0.0; 64], 2);
    audio.capture_mock(&[0.25; 40], 1);
    let mut session = ClockSession::new();
    session.observe_audio(audio.counters()).unwrap();
    bind_domains(&mut session);
    let old = session.epoch();
    let replacement = AudioIo::new(32).unwrap();
    assert_eq!(
        session
            .observe_audio(replacement.counters())
            .unwrap()
            .unwrap()
            .reason,
        ClockDiscontinuity::CounterReset
    );
    assert_invalidated(&session, old);
    assert_eq!(session.observe_audio(replacement.counters()).unwrap(), None);
}

/// {
///   責務: [`each_cumulative_counter_detects_rollback`: 累積 counter の巻き戻りを個別に検査する]
///   処理: [1: 各 counter だけを巻き戻した snapshot を作る, 2: 両 anchor の破棄を確認する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn each_cumulative_counter_detects_rollback() {
    let audio = AudioIo::new(32).unwrap();
    let mut baseline = audio.counters();
    baseline.playback_sample_position = 100;
    baseline.microphone_frames_received = 100;
    baseline.output_underrun_frames = 10;
    baseline.input_overrun_frames = 10;
    let rollbacks = [
        AudioCounters {
            playback_sample_position: 99,
            ..baseline
        },
        AudioCounters {
            microphone_frames_received: 99,
            ..baseline
        },
        AudioCounters {
            output_underrun_frames: 9,
            ..baseline
        },
        AudioCounters {
            input_overrun_frames: 9,
            ..baseline
        },
    ];
    for counters in rollbacks {
        let mut session = ClockSession::new();
        session.observe_audio(baseline).unwrap();
        bind_domains(&mut session);
        let old = session.epoch();
        assert_eq!(
            session.observe_audio(counters).unwrap().unwrap().reason,
            ClockDiscontinuity::CounterReset
        );
        assert_invalidated(&session, old);
    }
}

/// {
///   責務: [`explicit_epoch_clears_baseline_without_replaying_historical_gaps`: 明示境界後の基準再設定を検査する]
///   処理: [1: 欠落履歴を観測する, 2: 明示境界を設ける, 3: 過去の欠落で重複切替しないことを確認する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn explicit_epoch_clears_baseline_without_replaying_historical_gaps() {
    let audio = AudioIo::new(32).unwrap();
    audio.capture_mock(&[0.25; 40], 1);
    let mut session = ClockSession::new();
    assert_eq!(session.observe_audio(audio.counters()).unwrap(), None);
    bind_domains(&mut session);
    let old = session.epoch();
    let new = session.begin_new_epoch().unwrap();
    assert_invalidated(&session, old);
    assert_eq!(session.observe_audio(audio.counters()).unwrap(), None);
    assert_eq!(session.epoch(), new);
    bind_domains(&mut session);
    assert_eq!(session.observe_audio(audio.counters()).unwrap(), None);
    let replacement = AudioIo::new(32).unwrap();
    let new = session.begin_new_epoch().unwrap();
    assert_eq!(session.observe_audio(replacement.counters()).unwrap(), None);
    assert_eq!(session.epoch(), new);
    audio.mark_input_device_error();
    session.begin_new_epoch().unwrap();
    assert_eq!(
        session
            .observe_audio(audio.counters())
            .unwrap()
            .unwrap()
            .reason,
        ClockDiscontinuity::DeviceError
    );
}

/// {
///   責務: [`simultaneous_causes_advance_one_epoch_with_device_error_reason`: 複合異常を単一切替へまとめる]
///   処理: [1: 再生遷移と欠落と error を発生させる, 2: error 優先の単一通知を確認する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn simultaneous_causes_advance_one_epoch_with_device_error_reason() {
    let audio = AudioIo::new(32).unwrap();
    let mut session = ClockSession::new();
    session.observe_audio(audio.counters()).unwrap();
    bind_domains(&mut session);
    let old = session.epoch();
    audio.set_playing(true);
    audio.capture_mock(&[0.25; 40], 1);
    audio.mark_output_device_error();
    assert_eq!(
        session
            .observe_audio(audio.counters())
            .unwrap()
            .unwrap()
            .reason,
        ClockDiscontinuity::DeviceError
    );
    assert_invalidated(&session, old);
    assert_eq!(session.observe_audio(audio.counters()).unwrap(), None);
}

/// {
///   責務: [`clean_observation_does_not_invent_anchors`: counter 観測だけで時刻原点を作らないことを検査する]
///   処理: [1: 正常な初期 counter を観測する, 2: epoch 維持と未設定 anchor を確認する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn clean_observation_does_not_invent_anchors() {
    let audio = AudioIo::new(32).unwrap();
    let mut session = ClockSession::new();
    let epoch = session.epoch();
    assert_eq!(session.observe_audio(audio.counters()).unwrap(), None);
    assert_eq!(session.epoch(), epoch);
    for domain in [ClockDomain::Output, ClockDomain::Capture] {
        assert_eq!(
            session.resolve(domain, epoch, 0),
            Err(ClockError::UnanchoredDomain)
        );
    }
}

/// {
///   責務: [`clock_invalidation_is_applied_to_capture_messages`: `Processのepoch通知がPCM` consumerへ届くことを検査する]
///   処理: [1: 旧epoch captureをqueueへ入れる, 2: clockとAudioIoを新epochへ進める, 3: 旧frameを除き新frameだけ受け取る]
///   引数: []
///   戻り値: []
/// }
#[test]
fn clock_invalidation_is_applied_to_capture_messages() {
    let audio = AudioIo::new(32).unwrap();
    let mut session = ClockSession::new();
    session.observe_audio(audio.counters()).unwrap();
    audio.capture_mock(&[0.1], 1);
    audio.set_playing(true);
    let invalidation = session.observe_audio(audio.counters()).unwrap().unwrap();
    audio.set_clock_epoch(invalidation.epoch).unwrap();
    audio.capture_mock(&[0.6], 1);

    let mut received = [open_karaoke_core::audio_engine::TimestampedMicrophoneSample {
        epoch: invalidation.epoch,
        stream_sample: 0,
        sample: 0.0,
    }; 1];
    assert_eq!(audio.read_microphone_timestamped(&mut received), 1);
    assert_eq!(received[0].epoch, invalidation.epoch);
    assert_eq!(received[0].stream_sample, 1);
    assert!((received[0].sample - 0.6).abs() < 1e-6);
}
