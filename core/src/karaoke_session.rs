//! Core-owned karaoke session state machine (Issue #107, slice 107.1).
//!
//! Pure and device-free: no audio callback, thread, lock, network or Python is touched.
//! The future controller keeps these types and delegates to them; it does not inherit.
//! Times are 48 kHz integer song samples. Position is only ever set from *presented*
//! (audible) timestamps; no API accepts a submitted-frame count.

#![allow(clippy::doc_markdown)] // JSON-style declaration comments name snake_case fields

use std::collections::VecDeque;

use serde::Serialize;
use uuid::Uuid;

use crate::audio_clock::{
    ClockDomain, ClockEpoch, ClockError, ClockSession, LatencyCalibration, MappedSongSample,
};

/// Bounded `command_id` history per session.
pub const COMMAND_HISTORY_LIMIT: usize = 128;
/// Bounded state-event log per session.
pub const EVENT_LOG_LIMIT: usize = 64;
/// Bounded merged credit intervals per session.
pub const CREDIT_INTERVAL_LIMIT: usize = 256;

/// {
///   責務: [歌唱セッションの状態名]
///   フィールド: [preparing/ready/playing/paused/interrupted: 非終端, finished/stopped/failed: 終端]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KaraokeState {
    Preparing,
    Ready,
    Playing,
    Paused,
    Interrupted,
    Finished,
    Stopped,
    Failed,
}

impl KaraokeState {
    /// {
    ///   責務: [`is_terminal`: 終端状態かを返す]
    ///   処理: [1: finished/stopped/failedを判定する]
    ///   引数: []
    ///   戻り値: [`bool`: 終端ならtrue]
    /// }
    #[must_use]
    pub const fn is_terminal(self) -> bool {
        matches!(self, Self::Finished | Self::Stopped | Self::Failed)
    }
}

/// {
///   責務: [要求された採点モード]
///   フィールド: [practice: 再生優先でseek可, whole_song: 連続歌唱のみ全曲採点]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ScoringMode {
    Practice,
    WholeSong,
}

/// {
///   責務: [全曲採点から練習扱いへ格下げした理由]
///   フィールド: [seek: 再生開始後または非0位置へのseek, interrupted: 再生中/一時停止中の中断]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DowngradeReason {
    Seek,
    Interrupted,
}

/// {
///   責務: [最終採点として確定してよいかの現在の判定]
///   フィールド: [practice: 練習指定, whole_song: 連続で全曲採点可, downgraded: 格下げ済み(戻らない)]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case", tag = "kind", content = "reason")]
pub enum ScoreEligibility {
    Practice,
    WholeSong,
    Downgraded(DowngradeReason),
}

/// {
///   責務: [`position_song_sample`の由来。送信済みframe数と可聴時刻を混同しない]
///   フィールド: [none: 未設定, committed: seekで確定した目標位置, presented: 出力で提示済みの位置]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PositionSource {
    None,
    Committed,
    Presented,
}

/// {
///   責務: [提示済み位置の可聴時刻の品質(107.4)。未測定の遅延を0遅延や測定済みとして扱わない]
///   フィールド: [
///     unknown: 遅延補正が未測定(または提示timestampがまだ無い),
///     estimated: 推定した補正のみ(不確かさ不明),
///     measured{uncertainty_samples}: 測定した補正と不確かさ(48kHz sample)
///   ]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case", tag = "kind")]
pub enum ClockQuality {
    Unknown,
    Estimated,
    Measured { uncertainty_samples: u64 },
}

impl ClockQuality {
    /// {
    ///   責務: [`from_latency`: clockの遅延較正を品質へ写す(品質を昇格させない)]
    ///   処理: [1: Unknown→unknown 2: Estimated→estimated 3: Measured→measured(不確かさ保持)]
    ///   引数: [latency: `ClockSession`が返した遅延較正]
    ///   戻り値: [`ClockQuality`: 同じ品質段階]
    /// }
    #[must_use]
    pub const fn from_latency(latency: LatencyCalibration) -> Self {
        match latency {
            LatencyCalibration::Unknown => Self::Unknown,
            LatencyCalibration::Estimated { .. } => Self::Estimated,
            LatencyCalibration::Measured {
                uncertainty_samples,
                ..
            } => Self::Measured {
                uncertainty_samples,
            },
        }
    }
}

/// {
///   責務: [出力デバイスが実際に提示(可聴化)したstream sample。送信済み/書込済みframe数ではない]
///   フィールド: [0: 論理48kHz gridのoutput stream sample(デバイスの提示timestamp由来)]
///   補足: [型で「書き込んだ数」との取り違えを防ぐ。値の供給元は実機ゲート(#40/#19)で検証する]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PresentedStreamSample(pub u64);

/// {
///   責務: [クライアントが送る再生コマンド]
///   フィールド: [Play, Pause, Seek{song_sample}: 48kHz整数sample, Stop]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum KaraokeCommand {
    Play,
    Pause,
    Seek { song_sample: u64 },
    Stop,
}

/// {
///   責務: [冪等キーとrevision期待値を伴うコマンド封筒]
///   フィールド: [core_instance_id: 宛先Core世代, session_id, command_id: 再送キー, expected_revision, command]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CommandEnvelope {
    pub core_instance_id: Uuid,
    pub session_id: Uuid,
    pub command_id: Uuid,
    pub expected_revision: u64,
    pub command: KaraokeCommand,
}

/// {
///   責務: [コマンド結果の種別]
///   フィールド: [applied: 状態を変えた, no_op: 既に目的の状態, replayed: 同じcommand_idの再送で前回結果を返した]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Disposition {
    Applied,
    NoOp,
    Replayed,
}

/// {
///   責務: [クライアントへ渡す状態の写し]
///   フィールド: [型・単位は設計書 #107 §107.1。stale_rejections: epoch等の不一致で除外した観測数]
/// }
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct SessionSnapshot {
    pub session_id: Uuid,
    pub core_instance_id: Uuid,
    pub song_id: Uuid,
    pub state: KaraokeState,
    pub state_revision: u64,
    pub transport_epoch: u64,
    pub position_song_sample: u64,
    pub position_source: PositionSource,
    /// 107.4: 直近の提示済み位置の可聴時刻品質。`presented`以外の位置では`unknown`。
    pub clock_quality: ClockQuality,
    pub duration_samples: u64,
    pub scoring_mode: ScoringMode,
    pub score_eligibility: ScoreEligibility,
    pub error_code: Option<String>,
    pub stale_rejections: u64,
}

/// {
///   責務: [コマンドの成功結果]
///   フィールド: [disposition: 種別, snapshot: 結果の状態]
/// }
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommandOutcome {
    pub disposition: Disposition,
    pub snapshot: SessionSnapshot,
}

/// {
///   責務: [状態遷移1回分の通知。購読者は旧instance/session/epochを捨てる]
///   フィールド: [event_seq: セッション内で単調増加, snapshot: 遷移後の状態]
/// }
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct SessionEvent {
    pub event_seq: u64,
    pub core_instance_id: Uuid,
    pub session_id: Uuid,
    pub state_revision: u64,
    pub transport_epoch: u64,
    pub snapshot: SessionSnapshot,
}

/// {
///   責務: [`events_after`の結果]
///   フィールド: [Events: 連続して取得, Gap: 取りこぼし(snapshotで復旧する)]
/// }
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum EventsAfter {
    Events(Vec<SessionEvent>),
    Gap { oldest_event_seq: u64 },
}

/// {
///   責務: [外部観測(提示位置・加点・割込み)の出所。旧instance/session/epochを除外する]
///   フィールド: [core_instance_id, session_id, epoch]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EventOrigin {
    pub core_instance_id: Uuid,
    pub session_id: Uuid,
    pub epoch: u64,
}

/// {
///   責務: [セッション操作の失敗。失敗は状態を変えない]
///   フィールド: [各variantは`code()`の安定コードに対応]
/// }
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum KaraokeError {
    NotReady,
    Terminal,
    DeviceUnavailable,
    StaleRevision {
        expected: u64,
        current: u64,
    },
    StaleInstance,
    StaleEpoch {
        observed: u64,
        current: u64,
    },
    OutOfRange,
    CommandIdConflict,
    SessionNotFound,
    SessionBusy,
    NotPlaying,
    PositionRegressed,
    InvalidRange,
    LedgerFull,
    InvalidDuration,
    /// 107.4: `ClockSession`が提示timestampを曲位置へ写せなかった(状態は変えない)。
    ClockRejected(ClockError),
}

impl KaraokeError {
    /// {
    ///   責務: [`code`: transportへ載せる安定したエラーコード]
    ///   処理: [1: variantを固定文字列へ対応づける]
    ///   引数: []
    ///   戻り値: [`&'static str`: snake_caseコード]
    /// }
    #[must_use]
    pub const fn code(&self) -> &'static str {
        match self {
            Self::NotReady => "not_ready",
            Self::Terminal => "terminal",
            Self::DeviceUnavailable => "device_unavailable",
            Self::StaleRevision { .. } => "stale_revision",
            Self::StaleInstance => "stale_instance",
            Self::StaleEpoch { .. } => "stale_epoch",
            Self::OutOfRange => "out_of_range",
            Self::CommandIdConflict => "command_id_conflict",
            Self::SessionNotFound => "session_not_found",
            Self::SessionBusy => "session_busy",
            Self::NotPlaying => "not_playing",
            Self::PositionRegressed => "position_regressed",
            Self::InvalidRange => "invalid_range",
            Self::LedgerFull => "ledger_full",
            Self::InvalidDuration => "invalid_duration",
            Self::ClockRejected(ClockError::StaleEpoch) => "clock_stale_epoch",
            Self::ClockRejected(_) => "clock_unavailable",
        }
    }
}

/// {
///   責務: [成功したコマンドの履歴1件(再送時に同じ結果を返す)]
///   フィールド: [command_id, command, snapshot: 当時の結果]
/// }
#[derive(Clone, Debug)]
struct CommandRecord {
    command_id: Uuid,
    command: KaraokeCommand,
    snapshot: SessionSnapshot,
}

/// {
///   責務: [1つの歌唱セッションの状態・epoch・履歴・加点台帳の唯一の所有者]
///   フィールド: [内部状態のみ。外部からは`snapshot`と各操作を通して変更する]
///   補足: [並行アクセスの直列化は呼び出し側(単一Controllerループ)の責務]
/// }
#[derive(Debug)]
#[allow(clippy::struct_excessive_bools)]
pub struct KaraokeSession {
    session_id: Uuid,
    core_instance_id: Uuid,
    song_id: Uuid,
    duration_samples: u64,
    scoring_mode: ScoringMode,
    state: KaraokeState,
    revision: u64,
    epoch: u64,
    position: u64,
    position_source: PositionSource,
    clock_quality: ClockQuality,
    resume_state: KaraokeState,
    has_played: bool,
    downgrade: Option<DowngradeReason>,
    error_code: Option<String>,
    stale_rejections: u64,
    credited: Vec<(u64, u64)>,
    history: VecDeque<CommandRecord>,
    events: VecDeque<SessionEvent>,
    next_event_seq: u64,
}

impl KaraokeSession {
    /// {
    ///   責務: [`new`: preparing状態の新規セッションを作る]
    ///   処理: [1: durationを検証する 2: revision=0/epoch=1で初期化し最初のイベントを記録する]
    ///   引数: [session_id, core_instance_id, song_id, duration_samples: 1以上, scoring_mode]
    ///   戻り値: [`Result<Self, KaraokeError>`: 作成したセッション]
    ///   エラー: [`InvalidDuration`: durationが0]
    /// }
    ///
    /// # Errors
    /// Returns `InvalidDuration` when `duration_samples` is zero.
    pub fn new(
        session_id: Uuid,
        core_instance_id: Uuid,
        song_id: Uuid,
        duration_samples: u64,
        scoring_mode: ScoringMode,
    ) -> Result<Self, KaraokeError> {
        if duration_samples == 0 {
            return Err(KaraokeError::InvalidDuration);
        }
        let mut session = Self {
            session_id,
            core_instance_id,
            song_id,
            duration_samples,
            scoring_mode,
            state: KaraokeState::Preparing,
            revision: 0,
            epoch: 1,
            position: 0,
            position_source: PositionSource::None,
            clock_quality: ClockQuality::Unknown,
            resume_state: KaraokeState::Ready,
            has_played: false,
            downgrade: None,
            error_code: None,
            stale_rejections: 0,
            credited: Vec::new(),
            history: VecDeque::new(),
            events: VecDeque::new(),
            next_event_seq: 1,
        };
        session.record_event();
        Ok(session)
    }

    /// {
    ///   責務: [`snapshot`: 現在の状態の写しを返す]
    ///   処理: [1: 内部状態からSessionSnapshotを組み立てる]
    ///   引数: []
    ///   戻り値: [`SessionSnapshot`: 現在の状態]
    /// }
    #[must_use]
    pub fn snapshot(&self) -> SessionSnapshot {
        let score_eligibility = match (self.scoring_mode, self.downgrade) {
            (ScoringMode::Practice, _) => ScoreEligibility::Practice,
            (ScoringMode::WholeSong, None) => ScoreEligibility::WholeSong,
            (ScoringMode::WholeSong, Some(reason)) => ScoreEligibility::Downgraded(reason),
        };
        SessionSnapshot {
            session_id: self.session_id,
            core_instance_id: self.core_instance_id,
            song_id: self.song_id,
            state: self.state,
            state_revision: self.revision,
            transport_epoch: self.epoch,
            position_song_sample: self.position,
            position_source: self.position_source,
            clock_quality: self.clock_quality,
            duration_samples: self.duration_samples,
            scoring_mode: self.scoring_mode,
            score_eligibility,
            error_code: self.error_code.clone(),
            stale_rejections: self.stale_rejections,
        }
    }

    /// {
    ///   責務: [`execute`: コマンド封筒を検証して適用する]
    ///   処理: [
    ///     1: instance/session不一致を拒否する
    ///     2: command_id履歴を確認し同内容は前回結果を返し別内容は拒否する
    ///     3: Stop以外はexpected_revisionを照合する
    ///     4: 遷移表に従い適用またはno-opにする
    ///     5: 成功だけを履歴に記録する
    ///   ]
    ///   引数: [envelope: コマンド封筒]
    ///   戻り値: [`Result<CommandOutcome, KaraokeError>`: 結果。失敗時は状態不変]
    ///   エラー: [`StaleInstance` `SessionNotFound` `CommandIdConflict` `StaleRevision` `NotReady` `Terminal` `DeviceUnavailable` `OutOfRange`]
    /// }
    ///
    /// # Errors
    /// Returns the rejection described in the transition table; state is unchanged.
    pub fn execute(&mut self, envelope: CommandEnvelope) -> Result<CommandOutcome, KaraokeError> {
        if envelope.core_instance_id != self.core_instance_id {
            return Err(KaraokeError::StaleInstance);
        }
        if envelope.session_id != self.session_id {
            return Err(KaraokeError::SessionNotFound);
        }
        if let Some(record) = self
            .history
            .iter()
            .find(|record| record.command_id == envelope.command_id)
        {
            return if record.command == envelope.command {
                Ok(CommandOutcome {
                    disposition: Disposition::Replayed,
                    snapshot: record.snapshot.clone(),
                })
            } else {
                Err(KaraokeError::CommandIdConflict)
            };
        }
        if envelope.command != KaraokeCommand::Stop && envelope.expected_revision != self.revision {
            return Err(KaraokeError::StaleRevision {
                expected: envelope.expected_revision,
                current: self.revision,
            });
        }
        let disposition = self.apply(envelope.command)?;
        let snapshot = self.snapshot();
        if self.history.len() == COMMAND_HISTORY_LIMIT {
            self.history.pop_front();
        }
        self.history.push_back(CommandRecord {
            command_id: envelope.command_id,
            command: envelope.command,
            snapshot: snapshot.clone(),
        });
        Ok(CommandOutcome {
            disposition,
            snapshot,
        })
    }

    /// {
    ///   責務: [`apply`: 遷移表に従い1コマンドを適用する(内部)]
    ///   処理: [1: 状態×コマンドで分岐する 2: 変更時のみ`transition`でrevision/epochを進める]
    ///   引数: [command]
    ///   戻り値: [`Result<Disposition, KaraokeError>`: applied/no_op]
    ///   エラー: [遷移表の拒否]
    /// }
    fn apply(&mut self, command: KaraokeCommand) -> Result<Disposition, KaraokeError> {
        use KaraokeCommand as C;
        use KaraokeState as S;
        match (self.state, command) {
            (S::Preparing | S::Ready | S::Playing | S::Paused | S::Interrupted, C::Stop) => {
                self.transition(S::Stopped);
                Ok(Disposition::Applied)
            }
            (S::Finished | S::Stopped | S::Failed, C::Stop) => Ok(Disposition::NoOp),
            (state, _) if state.is_terminal() => Err(KaraokeError::Terminal),
            (S::Preparing, _) => Err(KaraokeError::NotReady),
            (S::Interrupted, _) => Err(KaraokeError::DeviceUnavailable),
            (S::Ready | S::Paused, C::Play) => {
                self.has_played = true;
                self.transition(S::Playing);
                Ok(Disposition::Applied)
            }
            (S::Playing, C::Play) | (S::Ready | S::Paused, C::Pause) => Ok(Disposition::NoOp),
            (S::Playing, C::Pause) => {
                self.transition(S::Paused);
                Ok(Disposition::Applied)
            }
            (state, C::Seek { song_sample }) => {
                if song_sample >= self.duration_samples {
                    return Err(KaraokeError::OutOfRange);
                }
                if state != S::Playing && song_sample == self.position {
                    return Ok(Disposition::NoOp);
                }
                if self.has_played || song_sample != 0 {
                    self.downgrade_whole_song(DowngradeReason::Seek);
                }
                self.position = song_sample;
                self.position_source = PositionSource::Committed;
                self.clock_quality = ClockQuality::Unknown;
                self.transition(state);
                Ok(Disposition::Applied)
            }
            // Unreachable by the arms above (Stop handled first); fail closed regardless.
            _ => Err(KaraokeError::NotReady),
        }
    }

    /// {
    ///   責務: [`finish_preparing`: 準備結果でpreparingからready/failedへ進める]
    ///   処理: [1: preparing以外は何もしない(false) 2: 成功はready(epoch不変) 3: 失敗はfailed]
    ///   引数: [result: Ok=準備完了, Err(code)=失敗の安定コード]
    ///   戻り値: [`bool`: 遷移したか]
    /// }
    pub fn finish_preparing(&mut self, result: Result<(), &str>) -> bool {
        if self.state != KaraokeState::Preparing {
            return false;
        }
        match result {
            Ok(()) => {
                self.state = KaraokeState::Ready;
                self.revision += 1;
                self.record_event();
            }
            Err(code) => self.fail_now(code),
        }
        true
    }

    /// {
    ///   責務: [`report_presented`: 出力で提示済みになった曲位置を報告する]
    ///   処理: [
    ///     1: epoch不一致は除外して件数に数える
    ///     2: playing以外/後退/範囲外を拒否する
    ///     3: 位置を更新(revisionは増やさない)し、曲末到達でfinishedへ遷移する
    ///   ]
    ///   引数: [epoch: 報告元の世代, presented_song_sample: 提示済みsample(送信済みframe数ではない)]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`: 更新後の状態]
    ///   エラー: [`StaleEpoch` `NotPlaying` `PositionRegressed` `OutOfRange`]
    /// }
    ///
    /// # Errors
    /// Returns the rejection; state is unchanged except the stale counter.
    pub fn report_presented(
        &mut self,
        epoch: u64,
        presented_song_sample: u64,
    ) -> Result<SessionSnapshot, KaraokeError> {
        self.apply_presented(epoch, presented_song_sample, ClockQuality::Unknown)
    }

    /// {
    ///   責務: [`report_presented_from_clock`: 出力の提示済みstream sampleを`ClockSession`で曲位置へ写し、品質付きで報告する(107.4)]
    ///   処理: [
    ///     1: session epochを確認する(不一致は除外して件数に数える)
    ///     2: output domainのanchorで`clock_epoch`のstream sampleを曲sampleへ写す
    ///     3: clockのepoch不一致は除外件数に数え、他のclock失敗は状態を変えず拒否する
    ///     4: 写した位置と遅延品質(昇格なし)を`report_presented`と同じ規則で適用する
    ///   ]
    ///   引数: [epoch: session側の世代, clock: 所有するClockSession, clock_epoch: stream sampleを観測したclock世代, presented: 提示済みoutput stream sample(送信済みframe数ではない)]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`: 更新後の状態]
    ///   エラー: [`StaleEpoch` `ClockRejected` `NotPlaying` `PositionRegressed` `OutOfRange`]
    /// }
    ///
    /// # Errors
    /// Returns the rejection; state is unchanged except the stale counter.
    pub fn report_presented_from_clock(
        &mut self,
        epoch: u64,
        clock: &ClockSession,
        clock_epoch: ClockEpoch,
        presented: PresentedStreamSample,
    ) -> Result<SessionSnapshot, KaraokeError> {
        self.check_epoch(epoch)?;
        let mapped: MappedSongSample =
            match clock.resolve(ClockDomain::Output, clock_epoch, presented.0) {
                Ok(mapped) => mapped,
                Err(error) => {
                    if error == ClockError::StaleEpoch {
                        self.stale_rejections += 1;
                    }
                    return Err(KaraokeError::ClockRejected(error));
                }
            };
        self.apply_presented(
            epoch,
            mapped.song_sample,
            ClockQuality::from_latency(mapped.latency),
        )
    }

    /// {
    ///   責務: [`apply_presented`: 提示済み位置と品質を検証して反映する(内部)]
    ///   処理: [
    ///     1: epoch不一致は除外して件数に数える
    ///     2: playing以外/後退/範囲外を拒否する
    ///     3: 位置と品質を更新(revisionは増やさない)し、曲末到達でfinishedへ遷移する
    ///   ]
    ///   引数: [epoch, presented_song_sample, quality]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`]
    ///   エラー: [`StaleEpoch` `NotPlaying` `PositionRegressed` `OutOfRange`]
    /// }
    fn apply_presented(
        &mut self,
        epoch: u64,
        presented_song_sample: u64,
        quality: ClockQuality,
    ) -> Result<SessionSnapshot, KaraokeError> {
        self.check_epoch(epoch)?;
        if self.state != KaraokeState::Playing {
            return Err(KaraokeError::NotPlaying);
        }
        if presented_song_sample < self.position {
            return Err(KaraokeError::PositionRegressed);
        }
        if presented_song_sample > self.duration_samples {
            return Err(KaraokeError::OutOfRange);
        }
        self.position = presented_song_sample;
        self.position_source = PositionSource::Presented;
        self.clock_quality = quality;
        if presented_song_sample == self.duration_samples {
            self.transition(KaraokeState::Finished);
        }
        Ok(self.snapshot())
    }

    /// {
    ///   責務: [`interrupt`: 機器抜去・stream errorを明示的な中断として扱う]
    ///   処理: [
    ///     1: epoch不一致は除外する
    ///     2: preparingはfailed、ready/playing/pausedはinterruptedへ遷移する
    ///     3: 再生開始後なら全曲採点を格下げする
    ///   ]
    ///   引数: [epoch: 報告元の世代, code: 診断コード]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`: 更新後の状態]
    ///   エラー: [`StaleEpoch` `Terminal`]
    /// }
    ///
    /// # Errors
    /// Returns `StaleEpoch` or `Terminal`; an already interrupted session is an idempotent success.
    pub fn interrupt(&mut self, epoch: u64, code: &str) -> Result<SessionSnapshot, KaraokeError> {
        self.check_epoch(epoch)?;
        match self.state {
            KaraokeState::Preparing => self.fail_now(code),
            KaraokeState::Ready | KaraokeState::Playing | KaraokeState::Paused => {
                self.resume_state = if self.state == KaraokeState::Ready {
                    KaraokeState::Ready
                } else {
                    KaraokeState::Paused
                };
                if self.has_played {
                    self.downgrade_whole_song(DowngradeReason::Interrupted);
                }
                self.error_code = Some(code.to_owned());
                self.transition(KaraokeState::Interrupted);
            }
            KaraokeState::Interrupted => {}
            _ => return Err(KaraokeError::Terminal),
        }
        Ok(self.snapshot())
    }

    /// {
    ///   責務: [`recover`: 利用者の明示操作で中断から復旧する(自動再生しない)]
    ///   処理: [1: interrupted以外はエラー 2: 中断前がreadyならready、他はpausedへ戻す]
    ///   引数: []
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`: 更新後の状態]
    ///   エラー: [`Terminal` 終端 / `NotReady` 中断中でない]
    /// }
    ///
    /// # Errors
    /// Returns `Terminal` or `NotReady` when the session is not interrupted.
    pub fn recover(&mut self) -> Result<SessionSnapshot, KaraokeError> {
        match self.state {
            KaraokeState::Interrupted => {
                self.error_code = None;
                self.transition(self.resume_state);
                Ok(self.snapshot())
            }
            state if state.is_terminal() => Err(KaraokeError::Terminal),
            _ => Err(KaraokeError::NotReady),
        }
    }

    /// {
    ///   責務: [`fail`: 復旧不能な異常でfailedへ進める]
    ///   処理: [1: 終端なら何もしない 2: failedへ遷移しエラーコードを残す]
    ///   引数: [code: 診断コード]
    ///   戻り値: [`SessionSnapshot`: 更新後の状態]
    /// }
    pub fn fail(&mut self, code: &str) -> SessionSnapshot {
        if !self.state.is_terminal() {
            self.fail_now(code);
        }
        self.snapshot()
    }

    /// {
    ///   責務: [`credit`: 採点対象区間のうち未加点の新規区間だけを登録して返す]
    ///   処理: [
    ///     1: epoch不一致/playing以外/不正区間を拒否する
    ///     2: 加点済み区間と重ならない部分を求める
    ///     3: 区間台帳へ結合して登録する(上限超過は拒否)
    ///   ]
    ///   引数: [epoch, start: 区間開始sample, end: 区間終端sample(排他)]
    ///   戻り値: [`Result<Vec<(u64, u64)>, KaraokeError>`: 新規に加点できる区間(空なら全て重複)]
    ///   エラー: [`StaleEpoch` `NotPlaying` `InvalidRange` `LedgerFull`]
    /// }
    ///
    /// # Errors
    /// Returns the rejection; the ledger is unchanged on error.
    pub fn credit(
        &mut self,
        epoch: u64,
        start: u64,
        end: u64,
    ) -> Result<Vec<(u64, u64)>, KaraokeError> {
        self.check_epoch(epoch)?;
        if self.state != KaraokeState::Playing {
            return Err(KaraokeError::NotPlaying);
        }
        if start >= end || end > self.duration_samples {
            return Err(KaraokeError::InvalidRange);
        }
        let mut fresh = Vec::new();
        let mut cursor = start;
        for &(a, b) in &self.credited {
            if b <= cursor {
                continue;
            }
            if a >= end {
                break;
            }
            if a > cursor {
                fresh.push((cursor, a));
            }
            cursor = cursor.max(b);
            if cursor >= end {
                break;
            }
        }
        if cursor < end {
            fresh.push((cursor, end));
        }
        let mut merged: Vec<(u64, u64)> = Vec::with_capacity(self.credited.len() + 1);
        let mut pending = (start, end);
        let mut inserted = false;
        for &(a, b) in &self.credited {
            if b < pending.0 {
                merged.push((a, b));
            } else if a > pending.1 {
                if !inserted {
                    merged.push(pending);
                    inserted = true;
                }
                merged.push((a, b));
            } else {
                pending = (pending.0.min(a), pending.1.max(b));
            }
        }
        if !inserted {
            merged.push(pending);
        }
        if merged.len() > CREDIT_INTERVAL_LIMIT {
            return Err(KaraokeError::LedgerFull);
        }
        self.credited = merged;
        Ok(fresh)
    }

    /// {
    ///   責務: [`events_after`: 指定seqより後のイベントを返すか、取りこぼしを通知する]
    ///   処理: [1: 最古seqが要求の次より後ならGap 2: それ以外は後続を返す]
    ///   引数: [after_seq: 受信済みの最後のevent_seq(0=未受信)]
    ///   戻り値: [`EventsAfter`: イベント列またはgap]
    /// }
    #[must_use]
    pub fn events_after(&self, after_seq: u64) -> EventsAfter {
        let oldest = self
            .events
            .front()
            .map_or(self.next_event_seq, |event| event.event_seq);
        if oldest > after_seq.saturating_add(1) {
            return EventsAfter::Gap {
                oldest_event_seq: oldest,
            };
        }
        EventsAfter::Events(
            self.events
                .iter()
                .filter(|event| event.event_seq > after_seq)
                .cloned()
                .collect(),
        )
    }

    /// {
    ///   責務: [`last_event_seq`: 記録済みの最後のevent_seqを返す(transportの取りこぼし検知の基準)]
    ///   処理: [1: 次に採番するseqから1を引く]
    ///   引数: []
    ///   戻り値: [`u64`: 最後のevent_seq(イベントが無ければ0)]
    /// }
    #[must_use]
    pub const fn last_event_seq(&self) -> u64 {
        self.next_event_seq - 1
    }

    /// {
    ///   責務: [`check_epoch`: 観測のepochが現在世代か確認し、古ければ数える(内部)]
    ///   処理: [1: 一致ならOk 2: 不一致ならstale_rejectionsを増やしてエラー]
    ///   引数: [epoch]
    ///   戻り値: [`Result<(), KaraokeError>`]
    ///   エラー: [`StaleEpoch`]
    /// }
    fn check_epoch(&mut self, epoch: u64) -> Result<(), KaraokeError> {
        if epoch == self.epoch {
            return Ok(());
        }
        self.stale_rejections += 1;
        Err(KaraokeError::StaleEpoch {
            observed: epoch,
            current: self.epoch,
        })
    }

    /// {
    ///   責務: [`transition`: 状態を変えrevisionとepochを進めイベントを記録する(内部)]
    ///   処理: [1: state更新 2: revision+1 3: epoch+1 4: イベント追加]
    ///   引数: [new_state]
    ///   戻り値: []
    /// }
    fn transition(&mut self, new_state: KaraokeState) {
        self.state = new_state;
        self.revision += 1;
        self.epoch += 1;
        self.record_event();
    }

    /// {
    ///   責務: [`fail_now`: failedへ遷移する(内部)]
    ///   処理: [1: error_code設定 2: transition]
    ///   引数: [code]
    ///   戻り値: []
    /// }
    fn fail_now(&mut self, code: &str) {
        self.error_code = Some(code.to_owned());
        self.transition(KaraokeState::Failed);
    }

    /// {
    ///   責務: [`downgrade_whole_song`: 全曲採点を練習扱いに格下げする(内部、戻さない)]
    ///   処理: [1: 未格下げのときだけ理由を記録]
    ///   引数: [reason]
    ///   戻り値: []
    /// }
    fn downgrade_whole_song(&mut self, reason: DowngradeReason) {
        if self.downgrade.is_none() {
            self.downgrade = Some(reason);
        }
    }

    /// {
    ///   責務: [`record_event`: 現在のsnapshotを上限付きログへ追加する(内部)]
    ///   処理: [1: 容量超過なら最古を捨てる 2: event_seqを採番して追加]
    ///   引数: []
    ///   戻り値: []
    /// }
    fn record_event(&mut self) {
        if self.events.len() == EVENT_LOG_LIMIT {
            self.events.pop_front();
        }
        let event = SessionEvent {
            event_seq: self.next_event_seq,
            core_instance_id: self.core_instance_id,
            session_id: self.session_id,
            state_revision: self.revision,
            transport_epoch: self.epoch,
            snapshot: self.snapshot(),
        };
        self.next_event_seq += 1;
        self.events.push_back(event);
    }
}

/// {
///   責務: [Core 1世代に対し非終端の歌唱セッションを1つだけ許す(device lease相当)]
///   フィールド: [core_instance_id: この世代のID。永続化せず再起動で旧セッションは消える, active: 現在のセッション]
/// }
#[derive(Debug)]
pub struct KaraokeSessionRegistry {
    core_instance_id: Uuid,
    active: Option<KaraokeSession>,
}

impl KaraokeSessionRegistry {
    /// {
    ///   責務: [`new`: 空のregistryをCore世代IDで作る]
    ///   処理: [1: activeなしで初期化]
    ///   引数: [core_instance_id]
    ///   戻り値: [`Self`]
    /// }
    #[must_use]
    pub const fn new(core_instance_id: Uuid) -> Self {
        Self {
            core_instance_id,
            active: None,
        }
    }

    /// {
    ///   責務: [`create`: 新しいセッションを作る。非終端が残っていれば拒否する]
    ///   処理: [1: activeが非終端ならsession_busy 2: 新規IDでpreparingのセッションに置換]
    ///   引数: [song_id, duration_samples, scoring_mode]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`: 作成直後のsnapshot]
    ///   エラー: [`SessionBusy` `InvalidDuration`]
    /// }
    ///
    /// # Errors
    /// Returns `SessionBusy` while another session is not terminal.
    pub fn create(
        &mut self,
        song_id: Uuid,
        duration_samples: u64,
        scoring_mode: ScoringMode,
    ) -> Result<SessionSnapshot, KaraokeError> {
        if self
            .active
            .as_ref()
            .is_some_and(|session| !session.state.is_terminal())
        {
            return Err(KaraokeError::SessionBusy);
        }
        let session = KaraokeSession::new(
            Uuid::new_v4(),
            self.core_instance_id,
            song_id,
            duration_samples,
            scoring_mode,
        )?;
        let snapshot = session.snapshot();
        self.active = Some(session);
        Ok(snapshot)
    }

    /// {
    ///   責務: [`current_snapshot`: 現行セッション(終端を含む)の状態の写しを返す]
    ///   処理: [1: activeがあればsnapshotを作る]
    ///   引数: []
    ///   戻り値: [`Option<SessionSnapshot>`: セッションが無ければNone]
    /// }
    #[must_use]
    pub fn current_snapshot(&self) -> Option<SessionSnapshot> {
        self.active.as_ref().map(KaraokeSession::snapshot)
    }

    /// {
    ///   責務: [`session_mut`: IDが一致する現行セッションを返す]
    ///   処理: [1: 現行セッションのIDを照合]
    ///   引数: [session_id]
    ///   戻り値: [`Result<&mut KaraokeSession, KaraokeError>`]
    ///   エラー: [`SessionNotFound`]
    /// }
    ///
    /// # Errors
    /// Returns `SessionNotFound` when the id is not the current session.
    pub fn session_mut(&mut self, session_id: Uuid) -> Result<&mut KaraokeSession, KaraokeError> {
        match self.active.as_mut() {
            Some(session) if session.session_id == session_id => Ok(session),
            _ => Err(KaraokeError::SessionNotFound),
        }
    }

    /// {
    ///   責務: [`execute`: コマンド封筒を該当セッションへ渡す]
    ///   処理: [1: instance照合 2: session取得 3: 委譲]
    ///   引数: [envelope]
    ///   戻り値: [`Result<CommandOutcome, KaraokeError>`]
    ///   エラー: [`StaleInstance` `SessionNotFound`とセッションの拒否]
    /// }
    ///
    /// # Errors
    /// Propagates the session rejection.
    pub fn execute(&mut self, envelope: CommandEnvelope) -> Result<CommandOutcome, KaraokeError> {
        if envelope.core_instance_id != self.core_instance_id {
            return Err(KaraokeError::StaleInstance);
        }
        self.session_mut(envelope.session_id)?.execute(envelope)
    }

    /// {
    ///   責務: [`report_presented`: 出所を検証して提示位置を報告する]
    ///   処理: [1: instance/session照合 2: 委譲]
    ///   引数: [origin, presented_song_sample]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`]
    ///   エラー: [`StaleInstance` `SessionNotFound`とセッションの拒否]
    /// }
    ///
    /// # Errors
    /// Propagates the session rejection.
    pub fn report_presented(
        &mut self,
        origin: EventOrigin,
        presented_song_sample: u64,
    ) -> Result<SessionSnapshot, KaraokeError> {
        self.origin_session(origin)?
            .report_presented(origin.epoch, presented_song_sample)
    }

    /// {
    ///   責務: [`report_presented_from_clock`: 出所を検証し、clock変換済みの提示位置を品質付きで報告する(107.4)]
    ///   処理: [1: instance/session照合 2: 委譲]
    ///   引数: [origin, clock, clock_epoch, presented]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`]
    ///   エラー: [`StaleInstance` `SessionNotFound` `ClockRejected`とセッションの拒否]
    /// }
    ///
    /// # Errors
    /// Propagates the session rejection.
    pub fn report_presented_from_clock(
        &mut self,
        origin: EventOrigin,
        clock: &ClockSession,
        clock_epoch: ClockEpoch,
        presented: PresentedStreamSample,
    ) -> Result<SessionSnapshot, KaraokeError> {
        self.origin_session(origin)?.report_presented_from_clock(
            origin.epoch,
            clock,
            clock_epoch,
            presented,
        )
    }

    /// {
    ///   責務: [`credit`: 出所を検証して加点区間を登録する]
    ///   処理: [1: instance/session照合 2: 委譲]
    ///   引数: [origin, start, end]
    ///   戻り値: [`Result<Vec<(u64, u64)>, KaraokeError>`: 新規区間]
    ///   エラー: [`StaleInstance` `SessionNotFound`とセッションの拒否]
    /// }
    ///
    /// # Errors
    /// Propagates the session rejection.
    pub fn credit(
        &mut self,
        origin: EventOrigin,
        start: u64,
        end: u64,
    ) -> Result<Vec<(u64, u64)>, KaraokeError> {
        self.origin_session(origin)?
            .credit(origin.epoch, start, end)
    }

    /// {
    ///   責務: [`interrupt`: 出所を検証して中断を報告する]
    ///   処理: [1: instance/session照合 2: 委譲]
    ///   引数: [origin, code]
    ///   戻り値: [`Result<SessionSnapshot, KaraokeError>`]
    ///   エラー: [`StaleInstance` `SessionNotFound`とセッションの拒否]
    /// }
    ///
    /// # Errors
    /// Propagates the session rejection.
    pub fn interrupt(
        &mut self,
        origin: EventOrigin,
        code: &str,
    ) -> Result<SessionSnapshot, KaraokeError> {
        self.origin_session(origin)?.interrupt(origin.epoch, code)
    }

    /// {
    ///   責務: [`origin_session`: 出所のinstance/sessionを検証してセッションを返す(内部)]
    ///   処理: [1: instance照合 2: session取得]
    ///   引数: [origin]
    ///   戻り値: [`Result<&mut KaraokeSession, KaraokeError>`]
    ///   エラー: [`StaleInstance` `SessionNotFound`]
    /// }
    fn origin_session(&mut self, origin: EventOrigin) -> Result<&mut KaraokeSession, KaraokeError> {
        if origin.core_instance_id != self.core_instance_id {
            return Err(KaraokeError::StaleInstance);
        }
        self.session_mut(origin.session_id)
    }
}
