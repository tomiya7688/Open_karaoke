//! Deterministic synthetic tests for the scoring engine and YIN estimator.
//! These verify the algorithm contract only; real singing accuracy is Unverified.

use super::estimator::{
    CapturePitchFrame, PitchEstimator, YIN_ESTIMATOR_VERSION, YinConfig, YinEstimator,
    map_capture_frame,
};
use super::*;
use crate::audio_clock::{ClockAnchor, ClockDomain, ClockSession, LatencyCalibration};
use crate::audio_engine::TimestampedMicrophoneSample;
use crate::song_data::Note;

const HOP: u64 = 480;

/// {
///   責務: [epoch: テスト用のclock epochを作る]
///   処理: [1: 世代番号を包む]
///   引数: [sequence: 世代番号]
///   戻り値: [`ClockEpoch`: epoch]
/// }
fn epoch(sequence: u64) -> ClockEpoch {
    ClockEpoch::from_sequence(sequence)
}

/// {
///   責務: [note: テスト用ノートを作る]
///   処理: [1: 既定値でNoteを組み立てる]
///   引数: [id, start, end, midi: ノート属性]
///   戻り値: [`Note`: ノート]
/// }
fn note(id: &str, start: u64, end: u64, midi: u8) -> Note {
    Note {
        id: id.to_owned(),
        start_sample: start,
        end_sample: end,
        midi_note: midi,
        pitch_confidence: Some(0.9),
        lyric_segment_id: None,
    }
}

/// {
///   責務: [melody: 0.5秒ノート3つと0.1秒休符の固定譜面を返す]
///   処理: [1: 3ノートを並べる]
///   引数: []
///   戻り値: [`NotesDocument`: 譜面]
/// }
fn melody() -> NotesDocument {
    NotesDocument {
        format_version: 1,
        analysis_version: 3,
        notes: vec![
            note("n1", 48_000, 72_000, 60),
            note("n2", 76_800, 100_800, 62),
            note("n3", 105_600, 129_600, 64),
        ],
    }
}

/// {
///   責務: [`cents_to_hz`: MIDI centを周波数へ変換する]
///   処理: [1: A4基準で指数変換する]
///   引数: [cents: MIDI cent]
///   戻り値: [`f32`: 周波数]
/// }
#[allow(clippy::cast_possible_truncation)]
fn cents_to_hz(cents: f64) -> f32 {
    (440.0 * ((cents - 6900.0) / 1200.0).exp2()) as f32
}

/// {
///   責務: [Performance: 合成歌唱の条件を表す]
///   フィールド: [`cent_offset`: 一定の音程ずれ, `delay_samples`: 歌唱の遅れ, `sung_fraction`: 各ノートで歌う割合, `jitter_cents`: 交互に加える揺れ, confidence: 信頼度]
/// }
#[derive(Clone, Copy)]
struct Performance {
    cent_offset: f64,
    delay_samples: i64,
    sung_fraction: f64,
    jitter_cents: f64,
    confidence: f32,
}

impl Default for Performance {
    /// {
    ///   責務: [default: 完全な歌唱条件を返す]
    ///   処理: [1: ずれ0・全区間歌唱に設定する]
    ///   引数: []
    ///   戻り値: [`Performance`: 条件]
    /// }
    fn default() -> Self {
        Self {
            cent_offset: 0.0,
            delay_samples: 0,
            sung_fraction: 1.0,
            jitter_cents: 0.0,
            confidence: 0.95,
        }
    }
}

/// {
///   責務: [sing: 譜面に沿った合成frameを10 ms hopで流し込む]
///   処理: [1: 各hop位置の歌唱ノートを遅れ込みで求める, 2: 有声/無声frameをpushする]
///   引数: [session: 採点session, notes: 譜面, performance: 歌唱条件]
///   戻り値: []
/// }
#[allow(clippy::cast_precision_loss, clippy::cast_possible_wrap)]
fn sing(session: &mut ScoringSession, notes: &NotesDocument, performance: Performance) {
    let mut toggle = 1.0;
    for slot in 0..(140_000 / HOP) {
        let sample = slot * HOP;
        let sung_time = sample as i64 - performance.delay_samples;
        let target = notes.notes.iter().find(|note| {
            let sung_end = note.start_sample as f64
                + (note.end_sample - note.start_sample) as f64 * performance.sung_fraction;
            sung_time >= note.start_sample as i64 && (sung_time as f64) < sung_end
        });
        let observation = target.map_or(PitchObservation::Unvoiced, |note| {
            toggle = -toggle;
            PitchObservation::Voiced {
                f0_hz: cents_to_hz(
                    f64::from(note.midi_note) * 100.0
                        + performance.cent_offset
                        + toggle * performance.jitter_cents,
                ),
                confidence: performance.confidence,
            }
        });
        session
            .push_frame(PitchFrame {
                epoch: session.epoch(),
                song_sample: sample,
                observation,
            })
            .expect("synthetic frame is valid");
    }
}

/// {
///   責務: [score: 既定設定で合成歌唱を採点する]
///   処理: [1: sessionを作る, 2: 歌唱を流す, 3: 結果を返す]
///   引数: [config: 採点設定, performance: 歌唱条件]
///   戻り値: [`ScoreResult`: 結果]
/// }
fn score(config: ScoringConfig, performance: Performance) -> ScoreResult {
    let notes = melody();
    let mut session = ScoringSession::new(&notes, config, "synthetic-v1", epoch(1)).unwrap();
    sing(&mut session, &notes, performance);
    session.result()
}

/// {
///   責務: [`assert_non_increasing`: 値列が増加しないことを検証する]
///   処理: [1: 隣接値を比較する]
///   引数: [label: 表示名, values: 値列]
///   戻り値: []
/// }
fn assert_non_increasing(label: &str, values: &[f64]) {
    for pair in values.windows(2) {
        assert!(pair[1] <= pair[0] + 1e-12, "{label} increased: {values:?}");
    }
}

/// {
///   責務: [`perfect_singing_scores_full_marks_on_every_metric`: 完全に合った歌唱が全指標で満点になることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn perfect_singing_scores_full_marks_on_every_metric() {
    let result = score(ScoringConfig::default(), Performance::default());
    let aggregate = result.aggregate;
    assert_eq!(aggregate.pitch, Some(1.0));
    assert_eq!(aggregate.timing, Some(1.0));
    assert_eq!(aggregate.stability, Some(1.0));
    assert!((aggregate.coverage - 1.0).abs() < 1e-12);
    assert!((aggregate.total - 1.0).abs() < 1e-12);
    assert_eq!(result.scoring_version, SCORING_VERSION);
    assert_eq!(result.pitch_detector_version, "synthetic-v1");
    assert_eq!(result.score_data.notes_analysis_version, 3);
    for note in &result.notes {
        assert_eq!(note.timing.unwrap().onset_error_samples, 0);
        assert_eq!(note.observed_slots, note.slots);
    }
}

/// {
///   責務: [`fixed_cent_offset_produces_exact_fixture_values`: 固定のセントずれが決定的な期待値になることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn fixed_cent_offset_produces_exact_fixture_values() {
    // credit(60) = (150 - 60) / (150 - 30) = 0.75; all other metrics are perfect.
    let result = score(
        ScoringConfig::default(),
        Performance {
            cent_offset: 60.0,
            ..Performance::default()
        },
    );
    let pitch = result.notes[0].pitch.unwrap();
    assert!((pitch.score - 0.75).abs() < 1e-6, "{pitch:?}");
    assert!((pitch.median_abs_cent_error - 60.0).abs() < 1e-3);
    assert!(pitch.in_tune_ratio.abs() < f64::EPSILON);
    assert!(pitch.gross_error_ratio.abs() < f64::EPSILON);
    // total = (0.4 * 0.75 + 0.2 + 0.25 + 0.15) / 1.0 = 0.9
    assert!((result.aggregate.total - 0.9).abs() < 1e-6);
    // Positive and negative offsets of equal size score the same.
    let flat = score(
        ScoringConfig::default(),
        Performance {
            cent_offset: -60.0,
            ..Performance::default()
        },
    );
    assert!((flat.aggregate.total - result.aggregate.total).abs() < 1e-6);
}

/// {
///   責務: [`larger_cent_offsets_never_score_higher`: セントずれが大きいほど点が上がらないこと(単調性)を検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn larger_cent_offsets_never_score_higher() {
    let mut pitch = Vec::new();
    let mut total = Vec::new();
    for offset in [
        0.0, 10.0, 25.0, 40.0, 60.0, 100.0, 149.0, 200.0, 400.0, 590.0,
    ] {
        let result = score(
            ScoringConfig::default(),
            Performance {
                cent_offset: offset,
                ..Performance::default()
            },
        );
        pitch.push(result.aggregate.pitch.unwrap());
        total.push(result.aggregate.total);
        // Pitch error never leaks into the coverage metric.
        assert!((result.aggregate.coverage - 1.0).abs() < 1e-12);
    }
    assert_non_increasing("pitch", &pitch);
    assert_non_increasing("total", &total);
    assert!(pitch[0] > *pitch.last().unwrap());
}

/// {
///   責務: [`larger_timing_offsets_never_score_higher`: タイミングずれが大きいほど点が上がらないこと(単調性)を検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn larger_timing_offsets_never_score_higher() {
    let mut timing = Vec::new();
    let mut total = Vec::new();
    for delay in [0, 1_200, 2_400, 4_800, 7_200, 9_600, 14_400] {
        let result = score(
            ScoringConfig::default(),
            Performance {
                delay_samples: delay,
                ..Performance::default()
            },
        );
        timing.push(result.aggregate.timing.unwrap_or(0.0));
        total.push(result.aggregate.total);
        if delay < 9_600 {
            let onset = result.notes[1].timing.unwrap().onset_error_samples;
            assert!((onset - delay).abs() < 480, "delay {delay} onset {onset}");
        }
    }
    assert_non_increasing("timing", &timing);
    assert_non_increasing("total", &total);
    assert!((timing[0] - 1.0).abs() < 1e-12);
    assert!(timing[5].abs() < 1e-12);
}

/// {
///   責務: [`early_singing_is_measured_as_negative_onset_error`: 早く歌い出した場合に負の開始誤差として測られることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn early_singing_is_measured_as_negative_onset_error() {
    let result = score(
        ScoringConfig::default(),
        Performance {
            delay_samples: -4_800,
            ..Performance::default()
        },
    );
    let onset = result.notes[1].timing.unwrap().onset_error_samples;
    assert!((onset + 4_800).abs() < 480, "onset {onset}");
}

/// {
///   責務: [`latency_offset_compensates_late_capture`: 遅延補正で遅れて届いた入力が補償されることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn latency_offset_compensates_late_capture() {
    let config = ScoringConfig {
        latency_offset_samples: -4_800,
        ..ScoringConfig::default()
    };
    let result = score(
        config,
        Performance {
            delay_samples: 4_800,
            ..Performance::default()
        },
    );
    assert!((result.aggregate.total - 1.0).abs() < 1e-12);
    assert_eq!(result.config.latency_offset_samples, -4_800);
    // The first frames move before song start and are counted as pre-roll.
    assert_eq!(result.frames.pre_roll, 10);
}

/// {
///   責務: [`silence_and_missing_frames_earn_no_credit`: 無音と欠落フレームに得点が付かないことを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn silence_and_missing_frames_earn_no_credit() {
    let notes = melody();
    let mut session =
        ScoringSession::new(&notes, ScoringConfig::default(), "synthetic-v1", epoch(1)).unwrap();
    let untouched = session.result();
    assert!(untouched.aggregate.total.abs() < f64::EPSILON);
    assert_eq!(untouched.notes[0].observed_slots, 0);
    for slot in 0..(140_000 / HOP) {
        session
            .push_frame(PitchFrame {
                epoch: epoch(1),
                song_sample: slot * HOP,
                observation: PitchObservation::Unvoiced,
            })
            .unwrap();
    }
    let result = session.result();
    assert!(result.aggregate.total.abs() < f64::EPSILON);
    assert!(result.aggregate.coverage.abs() < f64::EPSILON);
    assert_eq!(result.aggregate.pitch, None);
    assert_eq!(result.aggregate.timing, None);
    assert_eq!(result.aggregate.stability, None);
    assert_eq!(result.notes[0].observed_slots, result.notes[0].slots);
}

/// {
///   責務: [`low_confidence_frames_are_not_credited`: 確信度の低いフレームに得点が付かないことを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn low_confidence_frames_are_not_credited() {
    let result = score(
        ScoringConfig::default(),
        Performance {
            confidence: 0.2,
            ..Performance::default()
        },
    );
    assert!(result.aggregate.total.abs() < f64::EPSILON);
    assert!(result.aggregate.coverage.abs() < f64::EPSILON);
}

/// {
///   責務: [`random_pitch_noise_scores_far_below_a_real_attempt`: ランダムな音高ノイズが実際の歌唱より大幅に低い点になることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
fn random_pitch_noise_scores_far_below_a_real_attempt() {
    let notes = melody();
    let mut session =
        ScoringSession::new(&notes, ScoringConfig::default(), "synthetic-v1", epoch(1)).unwrap();
    let mut state = 0x2545_f491_4f6c_dd1d_u64;
    for slot in 0..(140_000 / HOP) {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        let unit = (state >> 11) as f64 / (1_u64 << 53) as f64;
        session
            .push_frame(PitchFrame {
                epoch: epoch(1),
                song_sample: slot * HOP,
                observation: PitchObservation::Voiced {
                    f0_hz: (80.0 + unit * 920.0) as f32,
                    confidence: 0.9,
                },
            })
            .unwrap();
    }
    let noise = session.result().aggregate;
    // Coverage is honestly reported (voiced), but pitch/stability get little credit.
    assert!(noise.pitch.unwrap() < 0.35, "{noise:?}");
    assert!(noise.stability.unwrap_or(0.0) < 0.15, "{noise:?}");
    let attempt = score(
        ScoringConfig::default(),
        Performance {
            cent_offset: 90.0,
            ..Performance::default()
        },
    );
    assert!(noise.total < attempt.aggregate.total);
}

/// {
///   責務: [`partial_coverage_never_scores_higher`: 歌った範囲が狭いほど点が上がらないことを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn partial_coverage_never_scores_higher() {
    let mut coverage = Vec::new();
    let mut total = Vec::new();
    for fraction in [1.0, 0.9, 0.75, 0.5, 0.25, 0.1, 0.0] {
        let result = score(
            ScoringConfig::default(),
            Performance {
                sung_fraction: fraction,
                ..Performance::default()
            },
        );
        coverage.push(result.aggregate.coverage);
        total.push(result.aggregate.total);
    }
    assert_non_increasing("coverage", &coverage);
    assert_non_increasing("total", &total);
    assert!(total.last().unwrap().abs() < f64::EPSILON);
}

/// {
///   責務: [`larger_pitch_jitter_never_scores_higher_on_stability`: 音高の揺れが大きいほど安定度が上がらないことを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn larger_pitch_jitter_never_scores_higher_on_stability() {
    let mut stability = Vec::new();
    for jitter in [0.0, 5.0, 10.0, 20.0, 40.0, 80.0, 120.0] {
        let result = score(
            ScoringConfig::default(),
            Performance {
                jitter_cents: jitter,
                ..Performance::default()
            },
        );
        stability.push(result.aggregate.stability.unwrap());
    }
    assert_non_increasing("stability", &stability);
    assert!((stability[0] - 1.0).abs() < 1e-12);
    assert!(stability.last().unwrap().abs() < 1e-12);
}

/// {
///   責務: [`octave_policy_is_explicit`: オクターブ方針が明示的に効くことを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn octave_policy_is_explicit() {
    let octave_down = Performance {
        cent_offset: -1200.0,
        ..Performance::default()
    };
    let folded = score(ScoringConfig::default(), octave_down);
    let pitch = folded.notes[0].pitch.unwrap();
    assert!((pitch.score - 1.0).abs() < 1e-6);
    assert!((pitch.octave_shifted_ratio - 1.0).abs() < 1e-12);
    let strict = score(
        ScoringConfig {
            octave_policy: OctavePolicy::Strict,
            ..ScoringConfig::default()
        },
        octave_down,
    );
    assert!(strict.notes[0].pitch.unwrap().score.abs() < f64::EPSILON);
    assert!((strict.notes[0].pitch.unwrap().gross_error_ratio - 1.0).abs() < 1e-12);
}

/// {
///   責務: [`live_comparison_reports_current_target_and_error`: 再生中に現在の目標音と誤差を比較できることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn live_comparison_reports_current_target_and_error() {
    let notes = melody();
    let mut session =
        ScoringSession::new(&notes, ScoringConfig::default(), "synthetic-v1", epoch(1)).unwrap();
    let outcome = session
        .push_frame(PitchFrame {
            epoch: epoch(1),
            song_sample: 80_000,
            observation: PitchObservation::Voiced {
                f0_hz: cents_to_hz(6_220.0),
                confidence: 0.9,
            },
        })
        .unwrap();
    let FrameOutcome::Accepted(live) = outcome else {
        panic!("frame should be accepted: {outcome:?}");
    };
    assert_eq!(live.note_index, Some(1));
    assert_eq!(session.note_id(1), Some("n2"));
    assert_eq!(live.target_midi_note, Some(62));
    assert!((live.cent_error.unwrap() - 20.0).abs() < 1e-3);
    let rest = session
        .push_frame(PitchFrame {
            epoch: epoch(1),
            song_sample: 74_000,
            observation: PitchObservation::Unvoiced,
        })
        .unwrap();
    assert_eq!(
        rest,
        FrameOutcome::Accepted(LiveComparison {
            note_index: None,
            target_midi_note: None,
            cent_error: None,
        })
    );
}

/// {
///   責務: [`old_epoch_frames_are_dropped_after_an_epoch_switch`: 世代切替後に古い世代のフレームが捨てられることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn old_epoch_frames_are_dropped_after_an_epoch_switch() {
    let notes = melody();
    let mut session =
        ScoringSession::new(&notes, ScoringConfig::default(), "synthetic-v1", epoch(1)).unwrap();
    sing(&mut session, &notes, Performance::default());
    let before = session.result();
    session.begin_epoch(epoch(2)).unwrap();
    // Queued wrong-pitch frames from epoch 1 must not change the result.
    for slot in 0..(140_000 / HOP) {
        let outcome = session
            .push_frame(PitchFrame {
                epoch: epoch(1),
                song_sample: slot * HOP,
                observation: PitchObservation::Voiced {
                    f0_hz: 100.0,
                    confidence: 1.0,
                },
            })
            .unwrap();
        assert_eq!(outcome, FrameOutcome::StaleEpochDropped);
    }
    let after = session.result();
    assert_eq!(after.notes, before.notes);
    assert_eq!(after.frames.stale_epoch_dropped, 140_000 / HOP);
    assert_eq!(
        session.push_frame(PitchFrame {
            epoch: epoch(3),
            song_sample: 0,
            observation: PitchObservation::Unvoiced,
        }),
        Err(ScoringError::FutureEpoch {
            current: 2,
            frame: 3
        })
    );
    assert_eq!(
        session.begin_epoch(epoch(2)),
        Err(ScoringError::EpochRegression {
            current: 2,
            requested: 2
        })
    );
    // New-epoch frames (e.g. after a seek back) overwrite the same slots.
    sing(
        &mut session,
        &notes,
        Performance {
            cent_offset: 60.0,
            ..Performance::default()
        },
    );
    let resung = session.result();
    assert!((resung.notes[0].pitch.unwrap().score - 0.75).abs() < 1e-6);
}

/// {
///   責務: [`results_are_deterministic_and_round_trip_through_json`: 結果が決定的でJSONに保存して復元できることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn results_are_deterministic_and_round_trip_through_json() {
    let performance = Performance {
        cent_offset: 37.0,
        delay_samples: 1_500,
        sung_fraction: 0.8,
        jitter_cents: 12.0,
        confidence: 0.9,
    };
    let first = score(ScoringConfig::default(), performance);
    let second = score(ScoringConfig::default(), performance);
    let json = serde_json::to_string_pretty(&first).unwrap();
    assert_eq!(json, serde_json::to_string_pretty(&second).unwrap());
    let restored: ScoreResult = serde_json::from_str(&json).unwrap();
    assert_eq!(restored, first);
    let value: serde_json::Value = serde_json::from_str(&json).unwrap();
    assert_eq!(value["result_format_version"], 1);
    assert_eq!(value["scoring_version"], "realtime-pitch-v1");
    assert_eq!(value["config"]["octave_policy"], "fold_octaves");
    assert_eq!(value["calibration_version"], DEFAULT_CALIBRATION_VERSION);
}

/// {
///   責務: [`custom_weights_change_only_the_total`: 重みの変更が総合点だけに影響することを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn custom_weights_change_only_the_total() {
    let performance = Performance {
        cent_offset: 100.0,
        ..Performance::default()
    };
    let default = score(ScoringConfig::default(), performance);
    let coverage_only = score(
        ScoringConfig {
            weights: ScoringWeights {
                pitch: 0.0,
                timing: 0.0,
                coverage: 1.0,
                stability: 0.0,
            },
            calibration_version: "test-coverage-only".to_owned(),
            ..ScoringConfig::default()
        },
        performance,
    );
    assert_eq!(coverage_only.aggregate.pitch, default.aggregate.pitch);
    assert!((coverage_only.aggregate.total - 1.0).abs() < 1e-12);
    assert!(default.aggregate.total < 1.0);
    assert_eq!(coverage_only.calibration_version, "test-coverage-only");
}

/// {
///   責務: [`frames_outside_the_timeline_are_counted_not_scored`: 譜面の時間範囲外のフレームが数えられるだけで採点されないことを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn frames_outside_the_timeline_are_counted_not_scored() {
    let notes = melody();
    let mut session =
        ScoringSession::new(&notes, ScoringConfig::default(), "synthetic-v1", epoch(1)).unwrap();
    let outcome = session
        .push_frame(PitchFrame {
            epoch: epoch(1),
            song_sample: u64::MAX,
            observation: PitchObservation::Unvoiced,
        })
        .unwrap();
    assert_eq!(outcome, FrameOutcome::OutsideTimeline);
    assert_eq!(session.result().frames.outside_timeline, 1);
}

/// {
///   責務: [`invalid_inputs_are_rejected_with_distinct_errors`: 不正な入力が種類ごとに別のエラーで拒否されることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn invalid_inputs_are_rejected_with_distinct_errors() {
    let config = ScoringConfig::default();
    let new = |notes: Vec<Note>, config: ScoringConfig| {
        ScoringSession::new(
            &NotesDocument {
                format_version: 1,
                analysis_version: 1,
                notes,
            },
            config,
            "synthetic-v1",
            epoch(1),
        )
        .map(|_| ())
    };
    assert_eq!(new(vec![], config.clone()), Err(ScoringError::NoNotes));
    assert_eq!(
        new(
            vec![note("a", 0, 100, 60), note("b", 50, 150, 62)],
            config.clone()
        ),
        Err(ScoringError::OverlappingNotes {
            first: "a".to_owned(),
            second: "b".to_owned()
        })
    );
    assert_eq!(
        new(
            vec![note("a", 0, 100, 60), note("a", 100, 150, 62)],
            config.clone()
        ),
        Err(ScoringError::DuplicateNoteId("a".to_owned()))
    );
    assert!(matches!(
        new(vec![note("a", 100, 100, 60)], config.clone()),
        Err(ScoringError::InvalidNote { .. })
    ));
    assert!(matches!(
        new(vec![note("a", 0, 100, 128)], config.clone()),
        Err(ScoringError::InvalidNote { .. })
    ));
    let zero_weights = ScoringConfig {
        weights: ScoringWeights {
            pitch: 0.0,
            timing: 0.0,
            coverage: 0.0,
            stability: 0.0,
        },
        ..config.clone()
    };
    assert_eq!(
        new(vec![note("a", 0, 100, 60)], zero_weights)
            .unwrap_err()
            .code(),
        "scoring_invalid_config"
    );
    let bad_hop = ScoringConfig {
        frame_hop_samples: 0,
        ..config.clone()
    };
    assert!(new(vec![note("a", 0, 100, 60)], bad_hop).is_err());
    assert!(matches!(
        new(
            vec![note("a", 0, 48_000 * 60 * 60 * 24, 60)],
            config.clone()
        ),
        Err(ScoringError::TimelineTooLong { .. })
    ));
    assert_eq!(
        ScoringSession::new(&melody(), config.clone(), "", epoch(1)).map(|_| ()),
        Err(ScoringError::InvalidDetectorVersion)
    );
    let mut session = ScoringSession::new(&melody(), config, "synthetic-v1", epoch(1)).unwrap();
    for observation in [
        PitchObservation::Voiced {
            f0_hz: f32::NAN,
            confidence: 0.9,
        },
        PitchObservation::Voiced {
            f0_hz: -1.0,
            confidence: 0.9,
        },
        PitchObservation::Voiced {
            f0_hz: 220.0,
            confidence: 1.5,
        },
    ] {
        let error = session
            .push_frame(PitchFrame {
                epoch: epoch(1),
                song_sample: 50_000,
                observation,
            })
            .unwrap_err();
        assert_eq!(error.code(), "scoring_invalid_frame");
    }
    assert_eq!(session.result().frames.accepted, 0);
}

/// {
///   責務: [`sine_samples`: epoch付きの正弦波mic sampleを作る]
///   処理: [1: 48 kHzで正弦波を生成する]
///   引数: [hz: 周波数, start: 開始capture位置, count: sample数, epoch: epoch]
///   戻り値: [`Vec<TimestampedMicrophoneSample>`: sample列]
/// }
#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
fn sine_samples(
    hz: f64,
    start: u64,
    count: u64,
    epoch: ClockEpoch,
) -> Vec<TimestampedMicrophoneSample> {
    (start..start + count)
        .map(|position| TimestampedMicrophoneSample {
            epoch,
            stream_sample: position,
            sample: (0.3 * (std::f64::consts::TAU * hz * position as f64 / 48_000.0).sin()) as f32,
        })
        .collect()
}

/// {
///   責務: [estimate: sample列を推定器へ流しframeを集める]
///   処理: [1: 各sampleをpushする, 2: 出たframeを集める]
///   引数: [estimator: 推定器, samples: sample列]
///   戻り値: [`Vec<CapturePitchFrame>`: frame列]
/// }
fn estimate(
    estimator: &mut YinEstimator,
    samples: &[TimestampedMicrophoneSample],
) -> Vec<CapturePitchFrame> {
    samples
        .iter()
        .filter_map(|sample| estimator.push(*sample))
        .collect()
}

/// {
///   責務: [`yin_estimates_synthetic_sines_within_a_few_cents`: YINが合成正弦波を数セント以内で推定できることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn yin_estimates_synthetic_sines_within_a_few_cents() {
    for hz in [110.0, 220.0, 261.63, 440.0, 880.0] {
        let mut estimator = YinEstimator::new(YinConfig::default()).unwrap();
        let frames = estimate(&mut estimator, &sine_samples(hz, 0, 4_800, epoch(1)));
        assert_ne!(frames.len(), 0);
        for frame in frames {
            let PitchObservation::Voiced { f0_hz, confidence } = frame.observation else {
                panic!("{hz} Hz should be voiced: {frame:?}");
            };
            let error = hz_to_midi_cents(f64::from(f0_hz)) - hz_to_midi_cents(hz);
            assert!(
                error.abs() < 3.0,
                "{hz} Hz estimated {f0_hz} ({error} cents)"
            );
            assert!(confidence > 0.9);
        }
    }
}

/// {
///   責務: [`yin_frames_are_spaced_by_hop_and_centered_on_the_window`: YINのフレームがホップ間隔で並び解析窓の中心に置かれることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn yin_frames_are_spaced_by_hop_and_centered_on_the_window() {
    let mut estimator = YinEstimator::new(YinConfig::default()).unwrap();
    assert_eq!(estimator.version(), YIN_ESTIMATOR_VERSION);
    assert_eq!(estimator.hop_samples(), 480);
    let frames = estimate(&mut estimator, &sine_samples(220.0, 1_000, 4_800, epoch(1)));
    let window = 1024 + 739 + 1;
    assert_eq!(frames[0].stream_sample, 1_000 + (window - 1) / 2);
    for pair in frames.windows(2) {
        assert_eq!(pair[1].stream_sample - pair[0].stream_sample, 480);
    }
}

/// {
///   責務: [`yin_reports_silence_and_white_noise_as_unvoiced`: YINが無音とホワイトノイズを無声と報告することを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
fn yin_reports_silence_and_white_noise_as_unvoiced() {
    let mut estimator = YinEstimator::new(YinConfig::default()).unwrap();
    let silence: Vec<_> = (0..4_800)
        .map(|position| TimestampedMicrophoneSample {
            epoch: epoch(1),
            stream_sample: position,
            sample: 0.0,
        })
        .collect();
    let frames = estimate(&mut estimator, &silence);
    assert_ne!(frames.len(), 0);
    assert!(
        frames
            .iter()
            .all(|frame| frame.observation == PitchObservation::Unvoiced)
    );

    let mut state = 0x9e37_79b9_7f4a_7c15_u64;
    let noise: Vec<_> = (10_000..14_800)
        .map(|position| {
            state = state
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            let unit = (state >> 11) as f64 / (1_u64 << 53) as f64;
            TimestampedMicrophoneSample {
                epoch: epoch(1),
                stream_sample: position,
                sample: (unit * 0.6 - 0.3) as f32,
            }
        })
        .collect();
    let frames = estimate(&mut estimator, &noise);
    assert_ne!(frames.len(), 0);
    assert!(
        frames
            .iter()
            .all(|frame| frame.observation == PitchObservation::Unvoiced),
        "{frames:?}"
    );
}

/// {
///   責務: [`yin_never_mixes_pcm_across_epochs_or_capture_gaps`: YINが世代またぎや取り込みの欠落をまたいでPCMを混ぜないことを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn yin_never_mixes_pcm_across_epochs_or_capture_gaps() {
    let mut estimator = YinEstimator::new(YinConfig::default()).unwrap();
    let window = 1024 + 739 + 1;
    let partial = estimate(
        &mut estimator,
        &sine_samples(220.0, 0, window - 1, epoch(1)),
    );
    assert_eq!(partial.len(), 0);
    // An epoch change discards the partial window: a full new window is required.
    let after_epoch = sine_samples(220.0, window - 1, window, epoch(2));
    let frames = estimate(&mut estimator, &after_epoch);
    assert_eq!(frames.len(), 1);
    assert_eq!(frames[0].epoch, epoch(2));
    // A capture gap (dropped frames) also resets the window.
    let after_gap = estimate(&mut estimator, &sine_samples(220.0, 50_000, 100, epoch(2)));
    assert_eq!(after_gap.len(), 0);
}

/// {
///   責務: [`yin_rejects_invalid_configuration`: YINが不正な設定を拒否することを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn yin_rejects_invalid_configuration() {
    for config in [
        YinConfig {
            min_f0_hz: 0.0,
            ..YinConfig::default()
        },
        YinConfig {
            max_f0_hz: 50.0,
            ..YinConfig::default()
        },
        YinConfig {
            threshold: 1.0,
            ..YinConfig::default()
        },
        YinConfig {
            integration_samples: 100,
            ..YinConfig::default()
        },
        YinConfig {
            hop_samples: 0,
            ..YinConfig::default()
        },
    ] {
        assert!(YinEstimator::new(config).is_err(), "{config:?}");
    }
}

/// {
///   責務: [`microphone_to_score_pipeline_scores_a_synthetic_sine`: マイク入力から採点までの流れが合成正弦波で通ることを検証するテスト]
///   処理: [1: 入力を組み立てる, 2: 実行して期待値と比較する]
///   引数: []
///   戻り値: []
/// }
#[test]
fn microphone_to_score_pipeline_scores_a_synthetic_sine() {
    // Capture stream sample 0 corresponds to song sample 0 with no drift.
    let mut clock = ClockSession::default();
    clock
        .bind(ClockAnchor {
            epoch: clock.epoch(),
            domain: ClockDomain::Capture,
            stream_sample: 0,
            song_sample: 0,
            drift_ppm: 0,
            latency: LatencyCalibration::Unknown,
        })
        .unwrap();
    let notes = NotesDocument {
        format_version: 1,
        analysis_version: 1,
        notes: vec![note("a3", 9_600, 33_600, 57)],
    };
    let mut estimator = YinEstimator::new(YinConfig::default()).unwrap();
    let mut session = ScoringSession::new(
        &notes,
        ScoringConfig::default(),
        estimator.version(),
        clock.epoch(),
    )
    .unwrap();
    let mut samples = Vec::new();
    samples.extend((0..9_600).map(|position| TimestampedMicrophoneSample {
        epoch: clock.epoch(),
        stream_sample: position,
        sample: 0.0,
    }));
    samples.extend(sine_samples(220.0, 9_600, 24_000, clock.epoch()));
    samples.extend(
        (33_600..40_000).map(|position| TimestampedMicrophoneSample {
            epoch: clock.epoch(),
            stream_sample: position,
            sample: 0.0,
        }),
    );
    for frame in estimate(&mut estimator, &samples) {
        let (frame, mapped) = map_capture_frame(&clock, frame).unwrap();
        assert_eq!(mapped.latency, LatencyCalibration::Unknown);
        session.push_frame(frame).unwrap();
    }
    let result = session.result();
    assert_eq!(result.pitch_detector_version, YIN_ESTIMATOR_VERSION);
    let pitch = result.notes[0].pitch.unwrap();
    assert!(pitch.score > 0.99, "{pitch:?}");
    // The analysis window straddles the onset, so timing is within one window.
    let timing = result.notes[0].timing.unwrap();
    assert!(timing.onset_error_samples.abs() < 1_800, "{timing:?}");
    assert!(result.notes[0].coverage > 0.9, "{:?}", result.notes[0]);
    // A frame from a superseded clock epoch cannot be mapped.
    let stale_epoch = clock.epoch();
    clock.begin_new_epoch().unwrap();
    let stale = CapturePitchFrame {
        epoch: stale_epoch,
        stream_sample: 10_000,
        observation: PitchObservation::Unvoiced,
    };
    assert!(map_capture_frame(&clock, stale).is_err());
}
