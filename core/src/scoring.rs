//! Realtime pitch scoring engine (#14).
//!
//! The scorer runs on a non-realtime worker. It consumes pitch frames that are
//! already mapped onto the song's 48 kHz integer timeline (see `audio_clock`) and
//! compares them with the normalized target notes from `notes.json`.
//! `ScoringSession::push_frame` performs no allocation, locking or I/O so it can be
//! driven at frame rate; result aggregation allocates and belongs to the worker.

pub mod estimator;

use std::fmt;

use serde::{Deserialize, Serialize};

use crate::audio_clock::ClockEpoch;
use crate::song_data::NotesDocument;

/// Version of the scoring algorithm whose metrics are stored in results.
pub const SCORING_VERSION: &str = "realtime-pitch-v1";
/// Version of the persisted [`ScoreResult`] JSON layout.
pub const SCORE_RESULT_FORMAT_VERSION: u32 = 1;
/// Calibration label for the provisional, human-uncalibrated default weights.
pub const DEFAULT_CALIBRATION_VERSION: &str = "uncalibrated-provisional-1";
/// Upper bound on timeline slots so one session cannot allocate without limit.
pub const MAX_TIMELINE_SLOTS: u64 = 1_000_000;
const MAX_VERSION_LABEL_CHARS: usize = 64;
const CENTS_PER_OCTAVE: f64 = 1200.0;

/// {
///   責務: [`OctavePolicy`: 目標音とのcent誤差でオクターブ違いを許容するかを表す]
///   フィールド: []
///   補足: [`FoldOctaves`は誤差を最寄りのオクターブへ折り返し、折り返した割合を別指標で保持する]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OctavePolicy {
    FoldOctaves,
    Strict,
}

/// {
///   責務: [`ScoringWeights`: 総合点で各指標を合成する重みを保持する]
///   フィールド: [pitch, timing, coverage, stability: 非負の相対重み]
///   補足: [合成時に合計で正規化する。既定値は人間評価で未校正の仮値]
/// }
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct ScoringWeights {
    pub pitch: f64,
    pub timing: f64,
    pub coverage: f64,
    pub stability: f64,
}

impl Default for ScoringWeights {
    /// {
    ///   責務: [default: 未校正の仮重みを返す]
    ///   処理: [1: 音程重視の仮重みを組み立てる]
    ///   引数: []
    ///   戻り値: [`ScoringWeights`: 仮重み]
    /// }
    fn default() -> Self {
        Self {
            pitch: 0.4,
            timing: 0.2,
            coverage: 0.25,
            stability: 0.15,
        }
    }
}

/// {
///   責務: [`ScoringConfig`: 採点閾値・重み・遅延補正を保持し結果へ保存できる形にする]
///   フィールド: [
///     `frame_hop_samples`: 1 slotの48 kHz sample幅（F0 estimatorのhopと揃える）
///     `latency_offset_samples`: 採点専用の追加補正。正で歌唱を後ろへずらす
///     `min_confidence`: これ未満の有声frameは無声扱い
///     `octave_policy`: オクターブ違いの扱い
///     `transition_samples`: ノート冒頭/末尾の遷移部の長さ
///     `pitch_*`, `in_tune_cents`, `gross_error_cents`: 音程指標の閾値
///     `onset_tolerance_cents`, `timing_*`: 発声開始検出と時間指標の閾値
///     `stability_*`: 安定度指標の閾値
///     weights: 総合点の重み
///     `calibration_version`: 閾値と重みの校正版
///   ]
/// }
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ScoringConfig {
    pub frame_hop_samples: u32,
    pub latency_offset_samples: i64,
    pub min_confidence: f32,
    pub octave_policy: OctavePolicy,
    pub transition_samples: u64,
    pub pitch_full_credit_cents: f64,
    pub pitch_zero_credit_cents: f64,
    pub in_tune_cents: f64,
    pub gross_error_cents: f64,
    pub onset_tolerance_cents: f64,
    pub timing_full_credit_samples: u64,
    pub timing_window_samples: u64,
    pub stability_full_credit_cents: f64,
    pub stability_zero_credit_cents: f64,
    pub weights: ScoringWeights,
    pub calibration_version: String,
}

impl Default for ScoringConfig {
    /// {
    ///   責務: [default: 10 ms hopの未校正既定設定を返す]
    ///   処理: [1: 各閾値と仮重みを設定する]
    ///   引数: []
    ///   戻り値: [`ScoringConfig`: 既定設定]
    /// }
    fn default() -> Self {
        Self {
            frame_hop_samples: 480,
            latency_offset_samples: 0,
            min_confidence: 0.5,
            octave_policy: OctavePolicy::FoldOctaves,
            transition_samples: 4_800,
            pitch_full_credit_cents: 30.0,
            pitch_zero_credit_cents: 150.0,
            in_tune_cents: 50.0,
            gross_error_cents: 300.0,
            onset_tolerance_cents: 100.0,
            timing_full_credit_samples: 2_400,
            timing_window_samples: 9_600,
            stability_full_credit_cents: 10.0,
            stability_zero_credit_cents: 80.0,
            weights: ScoringWeights::default(),
            calibration_version: DEFAULT_CALIBRATION_VERSION.to_owned(),
        }
    }
}

impl ScoringConfig {
    /// {
    ///   責務: [validate: 外部から渡された設定の範囲と単位を検証する]
    ///   処理: [1: hopと時間幅を検証する, 2: cent閾値の順序を検証する, 3: 重みと版名を検証する]
    ///   引数: []
    ///   戻り値: [`Result<(), ScoringError>`: 不正な項目名を含む`InvalidConfig`]
    /// }
    ///
    /// # Errors
    /// Returns `ScoringError::InvalidConfig` naming the first invalid field.
    pub fn validate(&self) -> Result<(), ScoringError> {
        let invalid = |field: &'static str, reason: &'static str| {
            Err(ScoringError::InvalidConfig { field, reason })
        };
        if !(48..=48_000).contains(&self.frame_hop_samples) {
            return invalid("frame_hop_samples", "must be within 48..=48000");
        }
        if !self.min_confidence.is_finite() || !(0.0..=1.0).contains(&self.min_confidence) {
            return invalid("min_confidence", "must be within 0..=1");
        }
        if self.timing_window_samples == 0
            || self.timing_full_credit_samples >= self.timing_window_samples
        {
            return invalid(
                "timing_window_samples",
                "must be positive and larger than timing_full_credit_samples",
            );
        }
        if self.transition_samples > 48_000 {
            return invalid("transition_samples", "must not exceed one second");
        }
        let positive = |value: f64| value.is_finite() && value > 0.0;
        if !positive(self.pitch_full_credit_cents)
            || !positive(self.pitch_zero_credit_cents)
            || self.pitch_full_credit_cents >= self.pitch_zero_credit_cents
        {
            return invalid(
                "pitch_full_credit_cents",
                "must be positive and below pitch_zero_credit_cents",
            );
        }
        if !positive(self.in_tune_cents) {
            return invalid("in_tune_cents", "must be positive");
        }
        if !positive(self.gross_error_cents) {
            return invalid("gross_error_cents", "must be positive");
        }
        if !positive(self.onset_tolerance_cents) {
            return invalid("onset_tolerance_cents", "must be positive");
        }
        if !positive(self.stability_full_credit_cents)
            || !positive(self.stability_zero_credit_cents)
            || self.stability_full_credit_cents >= self.stability_zero_credit_cents
        {
            return invalid(
                "stability_full_credit_cents",
                "must be positive and below stability_zero_credit_cents",
            );
        }
        let w = self.weights;
        let parts = [w.pitch, w.timing, w.coverage, w.stability];
        if parts.iter().any(|part| !part.is_finite() || *part < 0.0)
            || parts.iter().sum::<f64>() <= 0.0
        {
            return invalid("weights", "must be finite, non-negative and not all zero");
        }
        if !valid_label(&self.calibration_version) {
            return invalid("calibration_version", "must be 1..=64 visible ASCII chars");
        }
        Ok(())
    }
}

/// {
///   責務: [`PitchObservation`: 1 frameのF0推定結果を表す]
///   フィールド: [`f0_hz`: 有声時の基本周波数, confidence: F0推定の信頼度0..1]
/// }
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum PitchObservation {
    Unvoiced,
    Voiced { f0_hz: f32, confidence: f32 },
}

/// {
///   責務: [`PitchFrame`: song timeline上へ写像済みのF0 frameを採点へ渡す]
///   フィールド: [
///     epoch: frameのPCMが属したtransport epoch
///     `song_sample`: 解析窓中心の48 kHz song位置（`ClockSession::resolve`済み）
///     observation: F0推定結果
///   ]
/// }
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PitchFrame {
    pub epoch: ClockEpoch,
    pub song_sample: u64,
    pub observation: PitchObservation,
}

/// {
///   責務: [`LiveComparison`: 再生中の現在pitchとtarget noteの比較値を返す]
///   フィールド: [
///     `note_index`: 該当ノートの採点順index（休符ならNone）
///     `target_midi_note`: 該当ノートのMIDI番号
///     `cent_error`: octave policy適用後の誤差（無声・低信頼・休符ならNone）
///   ]
/// }
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LiveComparison {
    pub note_index: Option<usize>,
    pub target_midi_note: Option<u8>,
    pub cent_error: Option<f64>,
}

/// {
///   責務: [`FrameOutcome`: 1 frameを採点timelineへ取り込んだか、どの理由で捨てたかを返す]
///   フィールド: []
/// }
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum FrameOutcome {
    Accepted(LiveComparison),
    /// The frame belongs to an earlier transport epoch and was dropped.
    StaleEpochDropped,
    /// The compensated position precedes the song start (pre-roll, do not score).
    PreRoll,
    /// The compensated position lies after the scored timeline.
    OutsideTimeline,
}

/// {
///   責務: [`ScoringError`: 採点の入力契約違反を判別可能に返す]
///   フィールド: []
/// }
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ScoringError {
    InvalidConfig {
        field: &'static str,
        reason: &'static str,
    },
    InvalidDetectorVersion,
    NoNotes,
    InvalidNote {
        note_id: String,
        reason: &'static str,
    },
    DuplicateNoteId(String),
    OverlappingNotes {
        first: String,
        second: String,
    },
    TimelineTooLong {
        slots: u64,
        max: u64,
    },
    EpochRegression {
        current: u64,
        requested: u64,
    },
    FutureEpoch {
        current: u64,
        frame: u64,
    },
    InvalidFrame(&'static str),
}

impl ScoringError {
    /// {
    ///   責務: [code: 呼び出し側が分岐できる安定したerror codeを返す]
    ///   処理: [1: variantをcode文字列へ対応させる]
    ///   引数: []
    ///   戻り値: [`&'static str`: `scoring_*`形式のcode]
    /// }
    #[must_use]
    pub const fn code(&self) -> &'static str {
        match self {
            Self::InvalidConfig { .. } => "scoring_invalid_config",
            Self::InvalidDetectorVersion => "scoring_invalid_detector_version",
            Self::NoNotes => "scoring_no_notes",
            Self::InvalidNote { .. } => "scoring_invalid_note",
            Self::DuplicateNoteId(_) => "scoring_duplicate_note_id",
            Self::OverlappingNotes { .. } => "scoring_overlapping_notes",
            Self::TimelineTooLong { .. } => "scoring_timeline_too_long",
            Self::EpochRegression { .. } => "scoring_epoch_regression",
            Self::FutureEpoch { .. } => "scoring_future_epoch",
            Self::InvalidFrame(_) => "scoring_invalid_frame",
        }
    }
}

impl fmt::Display for ScoringError {
    /// {
    ///   責務: [fmt: 処理段階と回復に必要な値を含む説明を書く]
    ///   処理: [1: codeと詳細を書式化する]
    ///   引数: [f: 出力先]
    ///   戻り値: [`fmt::Result`: 書式化結果]
    /// }
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let code = self.code();
        match self {
            Self::InvalidConfig { field, reason } => write!(f, "{code}: {field} {reason}"),
            Self::InvalidDetectorVersion => {
                write!(
                    f,
                    "{code}: pitch detector version must be 1..=64 visible ASCII chars"
                )
            }
            Self::NoNotes => write!(f, "{code}: notes document has no notes to score"),
            Self::InvalidNote { note_id, reason } => write!(f, "{code}: note {note_id} {reason}"),
            Self::DuplicateNoteId(id) => write!(f, "{code}: note id {id} is duplicated"),
            Self::OverlappingNotes { first, second } => {
                write!(f, "{code}: note {second} overlaps note {first}")
            }
            Self::TimelineTooLong { slots, max } => {
                write!(f, "{code}: timeline needs {slots} slots, limit is {max}")
            }
            Self::EpochRegression { current, requested } => {
                write!(
                    f,
                    "{code}: epoch {requested} does not advance current {current}"
                )
            }
            Self::FutureEpoch { current, frame } => write!(
                f,
                "{code}: frame epoch {frame} is newer than session epoch {current}; call begin_epoch first"
            ),
            Self::InvalidFrame(reason) => write!(f, "{code}: {reason}"),
        }
    }
}

impl std::error::Error for ScoringError {}

/// {
///   責務: [`TargetNote`: 検証済みの採点対象ノートを保持する]
///   フィールド: [id: ノート番号, start/end: 48 kHz区間, midi: 目標MIDI番号]
/// }
#[derive(Clone, Debug)]
struct TargetNote {
    id: String,
    start: u64,
    end: u64,
    midi: u8,
}

/// {
///   責務: [`Slot`: 1 hop幅に最後に届いたframeの要約を保持する]
///   フィールド: [sample: 補正後のframe位置, cents: MIDI cent単位のF0]
/// }
#[derive(Clone, Copy, Debug, PartialEq)]
enum Slot {
    Empty,
    Unvoiced,
    LowConfidence,
    Voiced { sample: u64, cents: f64 },
}

/// {
///   責務: [`FrameCounters`: 取込・破棄したframe数を理由別に数える]
///   フィールド: [accepted, `stale_epoch_dropped`, `pre_roll`, `outside_timeline`: 各件数]
/// }
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct FrameCounters {
    pub accepted: u64,
    pub stale_epoch_dropped: u64,
    pub pre_roll: u64,
    pub outside_timeline: u64,
}

/// {
///   責務: [`ScoreDataVersion`: 採点に使った譜面文書の版を保存する]
///   フィールド: [`notes_format_version`, `notes_analysis_version`: notes.jsonの版]
/// }
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ScoreDataVersion {
    pub notes_format_version: u32,
    pub notes_analysis_version: u32,
}

/// {
///   責務: [`PitchMetrics`: ノートの音程精度を保持する]
///   フィールド: [
///     score: frame creditの平均0..1
///     `median_abs_cent_error`: octave policy適用後の誤差中央値
///     `in_tune_ratio`, `gross_error_ratio`, `octave_shifted_ratio`: 有声frameに対する割合
///     `voiced_frames`: 評価した有声frame数
///   ]
/// }
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct PitchMetrics {
    pub score: f64,
    pub median_abs_cent_error: f64,
    pub in_tune_ratio: f64,
    pub gross_error_ratio: f64,
    pub octave_shifted_ratio: f64,
    pub voiced_frames: u64,
}

/// {
///   責務: [`TimingMetrics`: ノートの発声開始ずれを保持する]
///   フィールド: [score: 0..1, `onset_error_samples`: 正で遅れ、負で早い]
/// }
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct TimingMetrics {
    pub score: f64,
    pub onset_error_samples: i64,
}

/// {
///   責務: [`StabilityMetrics`: ノート定常部のpitch揺れを保持する]
///   フィールド: [score: 0..1, `mean_abs_deviation_cents`: 歌唱自身の中央値からの平均絶対偏差（0点閾値で頭打ち）, frames: 評価frame数]
/// }
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct StabilityMetrics {
    pub score: f64,
    pub mean_abs_deviation_cents: f64,
    pub frames: u64,
}

/// {
///   責務: [`NoteScore`: 1ノートの独立した指標と合成点を保存する]
///   フィールド: [
///     `note_id`, `start_sample`, `end_sample`, `midi_note`: 対象ノート
///     slots: ノートと重なるslot数, `observed_slots`: frameが届いたslot数, `voiced_slots`: 有声slot数
///     coverage: `voiced_slots / slots`
///     pitch, timing, stability: 測定できない場合None
///     total: 重み付き合成点（Noneの指標は0として扱う）
///   ]
/// }
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct NoteScore {
    pub note_id: String,
    pub start_sample: u64,
    pub end_sample: u64,
    pub midi_note: u8,
    pub slots: u64,
    pub observed_slots: u64,
    pub voiced_slots: u64,
    pub coverage: f64,
    pub pitch: Option<PitchMetrics>,
    pub timing: Option<TimingMetrics>,
    pub stability: Option<StabilityMetrics>,
    pub total: f64,
}

/// {
///   責務: [`AggregateScore`: 曲全体の指標を独立に保持する]
///   フィールド: [
///     pitch, timing, stability: 測定できたノートだけの長さ加重平均（無ければNone）
///     coverage: 全ノートの長さ加重平均
///     total: ノート合成点の長さ加重平均
///     `notes_with_pitch`, `notes_with_timing`, `notes_with_stability`: 測定できたノート数
///   ]
/// }
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct AggregateScore {
    pub pitch: Option<f64>,
    pub timing: Option<f64>,
    pub coverage: f64,
    pub stability: Option<f64>,
    pub total: f64,
    pub notes_with_pitch: u64,
    pub notes_with_timing: u64,
    pub notes_with_stability: u64,
}

/// {
///   責務: [`ScoreResult`: 保存・再読込できる採点結果を表す]
///   フィールド: [
///     `result_format_version`, `scoring_version`, `pitch_detector_version`, `calibration_version`: 版
///     `score_data`: 譜面の版
///     config: 採点に使った閾値・重み・遅延補正
///     frames: frame件数
///     notes: ノート別結果, aggregate: 全体結果
///   ]
/// }
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ScoreResult {
    pub result_format_version: u32,
    pub scoring_version: String,
    pub pitch_detector_version: String,
    pub calibration_version: String,
    pub score_data: ScoreDataVersion,
    pub config: ScoringConfig,
    pub frames: FrameCounters,
    pub notes: Vec<NoteScore>,
    pub aggregate: AggregateScore,
}

/// {
///   責務: [`ScoringSession`: 1回の歌唱のframe timelineを所有し採点結果を作る]
///   フィールド: [
///     config: 検証済み設定
///     `pitch_detector_version`: frameを作ったF0推定器の版
///     `score_data`: 譜面の版
///     notes: 開始順に並べた重なりのないノート
///     slots: hop幅ごとの最新frame（構築時に確保）
///     epoch: 受け付けるtransport epoch
///     counters: frame件数
///   ]
///   補足: [seek後の新epochのframeは同じslotを上書きし、旧epochのframeは捨てる]
/// }
#[derive(Clone, Debug)]
pub struct ScoringSession {
    config: ScoringConfig,
    pitch_detector_version: String,
    score_data: ScoreDataVersion,
    notes: Vec<TargetNote>,
    slots: Vec<Slot>,
    epoch: ClockEpoch,
    counters: FrameCounters,
}

impl ScoringSession {
    /// {
    ///   責務: [new: 譜面と設定を検証し、採点timelineを事前確保する]
    ///   処理: [
    ///     1: 設定と推定器版を検証する
    ///     2: ノートを検証し開始順に並べ、重複IDと重なりを拒否する
    ///     3: 最終ノート終端+timing窓までのslotを確保する
    ///   ]
    ///   引数: [notes: notes.json文書, config: 採点設定, `pitch_detector_version`: F0推定器の版, epoch: 開始epoch]
    ///   戻り値: [`Result<Self, ScoringError>`: 採点session]
    /// }
    ///
    /// # Errors
    /// Returns `InvalidConfig`, `InvalidDetectorVersion`, `NoNotes`, `InvalidNote`,
    /// `DuplicateNoteId`, `OverlappingNotes`, or `TimelineTooLong`.
    pub fn new(
        notes: &NotesDocument,
        config: ScoringConfig,
        pitch_detector_version: &str,
        epoch: ClockEpoch,
    ) -> Result<Self, ScoringError> {
        config.validate()?;
        if !valid_label(pitch_detector_version) {
            return Err(ScoringError::InvalidDetectorVersion);
        }
        if notes.notes.is_empty() {
            return Err(ScoringError::NoNotes);
        }
        let mut targets = Vec::with_capacity(notes.notes.len());
        for note in &notes.notes {
            let invalid = |reason| ScoringError::InvalidNote {
                note_id: note.id.clone(),
                reason,
            };
            if note.id.is_empty() {
                return Err(invalid("has an empty id"));
            }
            if note.start_sample >= note.end_sample {
                return Err(invalid("must have start_sample < end_sample"));
            }
            if note.midi_note > 127 {
                return Err(invalid("must have midi_note within 0..=127"));
            }
            targets.push(TargetNote {
                id: note.id.clone(),
                start: note.start_sample,
                end: note.end_sample,
                midi: note.midi_note,
            });
        }
        targets.sort_by(|a, b| a.start.cmp(&b.start).then_with(|| a.id.cmp(&b.id)));
        let mut ids: Vec<&str> = targets.iter().map(|note| note.id.as_str()).collect();
        ids.sort_unstable();
        if let Some(pair) = ids.windows(2).find(|pair| pair[0] == pair[1]) {
            return Err(ScoringError::DuplicateNoteId(pair[0].to_owned()));
        }
        if let Some(pair) = targets.windows(2).find(|pair| pair[1].start < pair[0].end) {
            return Err(ScoringError::OverlappingNotes {
                first: pair[0].id.clone(),
                second: pair[1].id.clone(),
            });
        }
        let last_end = targets.iter().map(|note| note.end).max().unwrap_or(0);
        let hop = u64::from(config.frame_hop_samples);
        let slots = last_end
            .checked_add(config.timing_window_samples)
            .map_or(u64::MAX, |end| end.div_ceil(hop));
        if slots > MAX_TIMELINE_SLOTS {
            return Err(ScoringError::TimelineTooLong {
                slots,
                max: MAX_TIMELINE_SLOTS,
            });
        }
        let slot_count = usize::try_from(slots).map_err(|_| ScoringError::TimelineTooLong {
            slots,
            max: MAX_TIMELINE_SLOTS,
        })?;
        Ok(Self {
            config,
            pitch_detector_version: pitch_detector_version.to_owned(),
            score_data: ScoreDataVersion {
                notes_format_version: notes.format_version,
                notes_analysis_version: notes.analysis_version,
            },
            notes: targets,
            slots: vec![Slot::Empty; slot_count],
            epoch,
            counters: FrameCounters::default(),
        })
    }

    /// {
    ///   責務: [epoch: 現在受け付けるtransport epochを返す]
    ///   処理: [1: 保持値を返す]
    ///   引数: []
    ///   戻り値: [`ClockEpoch`: 現epoch]
    /// }
    #[must_use]
    pub const fn epoch(&self) -> ClockEpoch {
        self.epoch
    }

    /// {
    ///   責務: [`note_id`: 採点順の位置に対応するノート番号を返す]
    ///   処理: [1: indexで参照する]
    ///   引数: [index: `LiveComparison::note_index`]
    ///   戻り値: [`Option<&str>`: 範囲外ならNone]
    /// }
    #[must_use]
    pub fn note_id(&self, index: usize) -> Option<&str> {
        self.notes.get(index).map(|note| note.id.as_str())
    }

    /// {
    ///   責務: [`begin_epoch`: seekや不連続後の新しいepochへ切り替え、旧epochのframeを以後捨てる]
    ///   処理: [1: epochが単調増加か検証する, 2: 現epochを更新する]
    ///   引数: [epoch: `ClockSession`が確定した新epoch]
    ///   戻り値: [`Result<(), ScoringError>`: 後退・同値なら`EpochRegression`]
    ///   補足: [取込済みslotは保持し、新epochのframeで上書きされる]
    /// }
    ///
    /// # Errors
    /// Returns `EpochRegression` if `epoch` does not advance the current epoch.
    pub fn begin_epoch(&mut self, epoch: ClockEpoch) -> Result<(), ScoringError> {
        if epoch.sequence() <= self.epoch.sequence() {
            return Err(ScoringError::EpochRegression {
                current: self.epoch.sequence(),
                requested: epoch.sequence(),
            });
        }
        self.epoch = epoch;
        Ok(())
    }

    /// {
    ///   責務: [`push_frame`: 1 frameを検証してslotへ記録し、現在のtarget比較を返す]
    ///   処理: [
    ///     1: 旧epochなら数えて捨て、未来epochは拒否する
    ///     2: F0と信頼度を検証する
    ///     3: 採点遅延補正を加えpre-roll/timeline外を判別する
    ///     4: slotを上書きし、該当ノートとの誤差を返す
    ///   ]
    ///   引数: [frame: song timeline上のF0 frame]
    ///   戻り値: [`Result<FrameOutcome, ScoringError>`: 取込結果または破棄理由]
    ///   補足: [allocation・lock・I/Oを行わない]
    /// }
    ///
    /// # Errors
    /// Returns `FutureEpoch` for a frame newer than the session epoch, or
    /// `InvalidFrame` for a non-finite/non-positive F0 or confidence outside 0..=1.
    pub fn push_frame(&mut self, frame: PitchFrame) -> Result<FrameOutcome, ScoringError> {
        let current = self.epoch.sequence();
        let frame_epoch = frame.epoch.sequence();
        if frame_epoch < current {
            self.counters.stale_epoch_dropped += 1;
            return Ok(FrameOutcome::StaleEpochDropped);
        }
        if frame_epoch > current {
            return Err(ScoringError::FutureEpoch {
                current,
                frame: frame_epoch,
            });
        }
        let voiced = match frame.observation {
            PitchObservation::Unvoiced => None,
            PitchObservation::Voiced { f0_hz, confidence } => {
                if !f0_hz.is_finite() || f0_hz <= 0.0 {
                    return Err(ScoringError::InvalidFrame(
                        "f0_hz must be finite and positive",
                    ));
                }
                if !confidence.is_finite() || !(0.0..=1.0).contains(&confidence) {
                    return Err(ScoringError::InvalidFrame(
                        "confidence must be within 0..=1",
                    ));
                }
                Some((f0_hz, confidence))
            }
        };
        let compensated =
            i128::from(frame.song_sample) + i128::from(self.config.latency_offset_samples);
        if compensated < 0 {
            self.counters.pre_roll += 1;
            return Ok(FrameOutcome::PreRoll);
        }
        let Some(index) = u64::try_from(compensated)
            .ok()
            .map(|sample| (sample, sample / u64::from(self.config.frame_hop_samples)))
            .and_then(|(sample, slot)| usize::try_from(slot).ok().map(|slot| (sample, slot)))
            .filter(|(_, slot)| *slot < self.slots.len())
        else {
            self.counters.outside_timeline += 1;
            return Ok(FrameOutcome::OutsideTimeline);
        };
        let (sample, slot_index) = index;
        let slot = match voiced {
            None => Slot::Unvoiced,
            Some((_, confidence)) if confidence < self.config.min_confidence => Slot::LowConfidence,
            Some((f0_hz, _)) => Slot::Voiced {
                sample,
                cents: hz_to_midi_cents(f64::from(f0_hz)),
            },
        };
        self.slots[slot_index] = slot;
        self.counters.accepted += 1;

        let note_index = self.note_at(sample);
        let target_midi_note = note_index.map(|index| self.notes[index].midi);
        let cent_error = match (slot, target_midi_note) {
            (Slot::Voiced { cents, .. }, Some(midi)) => Some(self.cent_error(cents, midi).0),
            _ => None,
        };
        Ok(FrameOutcome::Accepted(LiveComparison {
            note_index,
            target_midi_note,
            cent_error,
        }))
    }

    /// {
    ///   責務: [result: 現時点のslotからノート別・全体の採点結果を作る]
    ///   処理: [1: 各ノートの指標を計算する, 2: 長さ加重で全体指標を合成する, 3: 版と設定を添える]
    ///   引数: []
    ///   戻り値: [`ScoreResult`: 保存可能な結果]
    ///   補足: [途中経過にも最終結果にも使える。worker上で呼び、audio callbackでは呼ばない]
    /// }
    #[must_use]
    pub fn result(&self) -> ScoreResult {
        let mut scratch = Vec::new();
        let notes: Vec<NoteScore> = (0..self.notes.len())
            .map(|index| self.score_note(index, &mut scratch))
            .collect();
        ScoreResult {
            result_format_version: SCORE_RESULT_FORMAT_VERSION,
            scoring_version: SCORING_VERSION.to_owned(),
            pitch_detector_version: self.pitch_detector_version.clone(),
            calibration_version: self.config.calibration_version.clone(),
            score_data: self.score_data,
            config: self.config.clone(),
            frames: self.counters,
            aggregate: aggregate(&notes),
            notes,
        }
    }

    /// {
    ///   責務: [`note_at`: 補正後のsample位置を含むノートを探す]
    ///   処理: [1: 開始位置で二分探索する, 2: 直前ノートの区間内か確認する]
    ///   引数: [sample: 補正後のsong位置]
    ///   戻り値: [`Option<usize>`: 該当ノートindex]
    /// }
    fn note_at(&self, sample: u64) -> Option<usize> {
        let after = self.notes.partition_point(|note| note.start <= sample);
        after
            .checked_sub(1)
            .filter(|index| sample < self.notes[*index].end)
    }

    /// {
    ///   責務: [`cent_error`: F0 centと目標ノートの誤差へoctave policyを適用する]
    ///   処理: [1: 生の誤差を求める, 2: `FoldOctaves`なら最寄りオクターブへ折り返す]
    ///   引数: [cents: MIDI cent単位のF0, midi: 目標ノート]
    ///   戻り値: [`(f64, bool)`: 適用後の誤差とオクターブを折り返したか]
    /// }
    fn cent_error(&self, cents: f64, midi: u8) -> (f64, bool) {
        let raw = cents - f64::from(midi) * 100.0;
        match self.config.octave_policy {
            OctavePolicy::Strict => (raw, false),
            OctavePolicy::FoldOctaves => {
                let octaves = (raw / CENTS_PER_OCTAVE).round();
                (raw - octaves * CENTS_PER_OCTAVE, octaves != 0.0)
            }
        }
    }

    /// {
    ///   責務: [`slot_range`: sample区間と重なるslot index範囲を返す]
    ///   処理: [1: hopで開始slotと終端slotを求める, 2: timeline長へ制限する]
    ///   引数: [start, end: 48 kHz半開区間]
    ///   戻り値: [`std::ops::Range<usize>`: slot範囲]
    /// }
    fn slot_range(&self, start: u64, end: u64) -> std::ops::Range<usize> {
        let hop = u64::from(self.config.frame_hop_samples);
        let len = self.slots.len();
        let first = usize::try_from(start / hop).unwrap_or(len).min(len);
        let last = usize::try_from(end.div_ceil(hop)).unwrap_or(len).min(len);
        first..last.max(first)
    }

    /// {
    ///   責務: [`voiced_in`: sample区間内の有声frameを列挙する]
    ///   処理: [1: 重なるslotを走査する, 2: frame位置が区間内の有声slotだけを返す]
    ///   引数: [start, end: 48 kHz半開区間]
    ///   戻り値: [`impl Iterator<Item = (u64, f64)>`: frame位置とMIDI cent]
    /// }
    fn voiced_in(&self, start: u64, end: u64) -> impl Iterator<Item = (u64, f64)> + '_ {
        self.slots[self.slot_range(start, end)]
            .iter()
            .filter_map(move |slot| match *slot {
                Slot::Voiced { sample, cents } if (start..end).contains(&sample) => {
                    Some((sample, cents))
                }
                _ => None,
            })
    }

    /// {
    ///   責務: [`score_note`: 1ノートのcoverage・pitch・timing・stabilityと合成点を計算する]
    ///   処理: [
    ///     1: 重なるslotからcoverageを数える
    ///     2: 冒頭遷移部を除いた有声frameで音程指標を求める
    ///     3: 発声開始ずれを求める
    ///     4: 両端遷移部を除いた定常部で安定度を求める
    ///     5: 重みで合成する
    ///   ]
    ///   引数: [index: ノートindex, scratch: 中央値計算用の再利用buffer]
    ///   戻り値: [`NoteScore`: ノート別結果]
    /// }
    #[allow(clippy::cast_precision_loss)] // frame counts are far below 2^52.
    fn score_note(&self, index: usize, scratch: &mut Vec<f64>) -> NoteScore {
        let note = &self.notes[index];
        let config = &self.config;
        let range = self.slot_range(note.start, note.end);
        let slots = range.len() as u64;
        let observed_slots = self.slots[range.clone()]
            .iter()
            .filter(|slot| **slot != Slot::Empty)
            .count() as u64;
        let voiced_slots = self.slots[range]
            .iter()
            .filter(|slot| matches!(slot, Slot::Voiced { .. }))
            .count() as u64;
        let coverage = if slots == 0 {
            0.0
        } else {
            voiced_slots as f64 / slots as f64
        };

        let transition = config.transition_samples.min((note.end - note.start) / 4);
        let pitch = self.pitch_metrics(note, note.start + transition, scratch);
        let timing = self.timing_metrics(index);
        let stability =
            self.stability_metrics(note.start + transition, note.end - transition, scratch);

        let weights = config.weights;
        let weight_sum = weights.pitch + weights.timing + weights.coverage + weights.stability;
        let total = (weights.pitch * pitch.map_or(0.0, |m| m.score)
            + weights.timing * timing.map_or(0.0, |m| m.score)
            + weights.coverage * coverage
            + weights.stability * stability.map_or(0.0, |m| m.score))
            / weight_sum;
        NoteScore {
            note_id: note.id.clone(),
            start_sample: note.start,
            end_sample: note.end,
            midi_note: note.midi,
            slots,
            observed_slots,
            voiced_slots,
            coverage,
            pitch,
            timing,
            stability,
            total,
        }
    }

    /// {
    ///   責務: [`pitch_metrics`: ノート中央部の有声frameで音程指標を求める]
    ///   処理: [1: 誤差とcreditを集計する, 2: 誤差絶対値の中央値を求める]
    ///   引数: [note: 対象ノート, start: 遷移部を除いた評価開始位置, scratch: 中央値buffer]
    ///   戻り値: [`Option<PitchMetrics>`: 有声frameが無ければNone]
    /// }
    #[allow(clippy::cast_precision_loss)] // frame counts are far below 2^52.
    fn pitch_metrics(
        &self,
        note: &TargetNote,
        start: u64,
        scratch: &mut Vec<f64>,
    ) -> Option<PitchMetrics> {
        let config = &self.config;
        scratch.clear();
        let (mut credit, mut in_tune, mut gross, mut shifted) = (0.0, 0_u64, 0_u64, 0_u64);
        for (_, cents) in self.voiced_in(start, note.end) {
            let (error, octave_shifted) = self.cent_error(cents, note.midi);
            let magnitude = error.abs();
            scratch.push(magnitude);
            credit += linear_credit(
                magnitude,
                config.pitch_full_credit_cents,
                config.pitch_zero_credit_cents,
            );
            in_tune += u64::from(magnitude <= config.in_tune_cents);
            gross += u64::from(magnitude > config.gross_error_cents);
            shifted += u64::from(octave_shifted);
        }
        let frames = scratch.len() as u64;
        if frames == 0 {
            return None;
        }
        let count = frames as f64;
        Some(PitchMetrics {
            score: credit / count,
            median_abs_cent_error: median(scratch),
            in_tune_ratio: in_tune as f64 / count,
            gross_error_ratio: gross as f64 / count,
            octave_shifted_ratio: shifted as f64 / count,
            voiced_frames: frames,
        })
    }

    /// {
    ///   責務: [`timing_metrics`: 目標開始位置周辺で最初に目標pitchへ入ったframeから開始ずれを求める]
    ///   処理: [
    ///     1: 探索窓を開始±timing窓とし、前ノート終端より前は探さない
    ///     2: 誤差がonset許容内の最初の有声frameを探す
    ///     3: ずれを線形creditへ変換する
    ///   ]
    ///   引数: [index: ノートindex]
    ///   戻り値: [`Option<TimingMetrics>`: 該当frameが無ければNone]
    ///   補足: [同音連続やlegatoで前ノートと接する場合、早い発声は検出できない（DTWは次段）]
    /// }
    #[allow(clippy::cast_precision_loss)] // sample offsets are bounded by the timeline limit.
    fn timing_metrics(&self, index: usize) -> Option<TimingMetrics> {
        let config = &self.config;
        let note = &self.notes[index];
        let previous_end = index
            .checked_sub(1)
            .map_or(0, |previous| self.notes[previous].end);
        let start = note
            .start
            .saturating_sub(config.timing_window_samples)
            .max(previous_end);
        let end = note
            .start
            .saturating_add(config.timing_window_samples)
            .min(note.end);
        let (onset, _) = self.voiced_in(start, end).find(|(_, cents)| {
            self.cent_error(*cents, note.midi).0.abs() <= config.onset_tolerance_cents
        })?;
        let error = i128::from(onset) - i128::from(note.start);
        let onset_error_samples = i64::try_from(error).ok()?;
        Some(TimingMetrics {
            score: linear_credit(
                onset_error_samples.unsigned_abs() as f64,
                config.timing_full_credit_samples as f64,
                config.timing_window_samples as f64,
            ),
            onset_error_samples,
        })
    }

    /// {
    ///   責務: [`stability_metrics`: 定常部の歌唱pitchの揺れを歌唱自身の中央値基準で求める]
    ///   処理: [1: 定常部の有声centを集める, 2: 中央値からの頭打ち平均絶対偏差を求める, 3: creditへ変換する]
    ///   引数: [start, end: 定常部の半開区間, scratch: 中央値buffer]
    ///   戻り値: [`Option<StabilityMetrics>`: 3 frame未満ならNone]
    ///   補足: [目標音との誤差ではないため音程指標と独立。ビブラートの別扱いは次段]
    /// }
    #[allow(clippy::cast_precision_loss)] // frame counts are far below 2^52.
    fn stability_metrics(
        &self,
        start: u64,
        end: u64,
        scratch: &mut Vec<f64>,
    ) -> Option<StabilityMetrics> {
        scratch.clear();
        scratch.extend(self.voiced_in(start, end).map(|(_, cents)| cents));
        if scratch.len() < 3 {
            return None;
        }
        let frames = scratch.len() as u64;
        let center = median(scratch);
        // Mean (not median) so alternating jitter is never hidden; each deviation is
        // capped at the zero-credit threshold so one outlier frame has bounded weight.
        let cap = self.config.stability_zero_credit_cents;
        let deviation = scratch
            .iter()
            .map(|value| (value - center).abs().min(cap))
            .sum::<f64>()
            / scratch.len() as f64;
        Some(StabilityMetrics {
            score: linear_credit(
                deviation,
                self.config.stability_full_credit_cents,
                self.config.stability_zero_credit_cents,
            ),
            mean_abs_deviation_cents: deviation,
            frames,
        })
    }
}

/// {
///   責務: [aggregate: ノート別結果を長さ加重で全体指標へまとめる]
///   処理: [1: 長さで重み付けした合計を集める, 2: 測定できた指標だけ平均する]
///   引数: [notes: ノート別結果]
///   戻り値: [`AggregateScore`: 全体結果]
/// }
#[allow(clippy::cast_precision_loss)] // durations are bounded by the timeline limit.
fn aggregate(notes: &[NoteScore]) -> AggregateScore {
    /// {
    ///   責務: [`Mean`: 重み付き平均を足し込む集計用の作業領域]
    ///   フィールド: [sum: 値×重みの合計, weight: 重みの合計, notes: 値のあったノート数]
    /// }
    #[derive(Default)]
    struct Mean {
        sum: f64,
        weight: f64,
        notes: u64,
    }
    impl Mean {
        /// {
        ///   責務: [`add`: 値があれば重み付きで加算する]
        ///   処理: [1: Noneなら何もしない, 2: 合計と重みとノート数を更新する]
        ///   引数: [self: 集計, value: 値, weight: 重み]
        ///   戻り値: [なし]
        /// }
        fn add(&mut self, value: Option<f64>, weight: f64) {
            if let Some(value) = value {
                self.sum += value * weight;
                self.weight += weight;
                self.notes += 1;
            }
        }
        /// {
        ///   責務: [`value`: 重み付き平均を返す]
        ///   処理: [1: 重みが0なら None, 2: 合計÷重みを返す]
        ///   引数: [self: 集計]
        ///   戻り値: [`Option<f64>`: 平均。重みが0ならNone]
        /// }
        fn value(&self) -> Option<f64> {
            (self.weight > 0.0).then(|| self.sum / self.weight)
        }
    }
    let (mut pitch, mut timing, mut stability, mut coverage, mut total) = (
        Mean::default(),
        Mean::default(),
        Mean::default(),
        Mean::default(),
        Mean::default(),
    );
    for note in notes {
        let weight = (note.end_sample - note.start_sample) as f64;
        pitch.add(note.pitch.map(|m| m.score), weight);
        timing.add(note.timing.map(|m| m.score), weight);
        stability.add(note.stability.map(|m| m.score), weight);
        coverage.add(Some(note.coverage), weight);
        total.add(Some(note.total), weight);
    }
    AggregateScore {
        pitch: pitch.value(),
        timing: timing.value(),
        coverage: coverage.value().unwrap_or(0.0),
        stability: stability.value(),
        total: total.value().unwrap_or(0.0),
        notes_with_pitch: pitch.notes,
        notes_with_timing: timing.notes,
        notes_with_stability: stability.notes,
    }
}

/// {
///   責務: [`hz_to_midi_cents`: 周波数をMIDI cent（A4=440 Hz=6900）へ変換する]
///   処理: [1: 440 Hz比の対数をcentへ換算する]
///   引数: [hz: 正の周波数]
///   戻り値: [`f64`: MIDI cent]
/// }
#[must_use]
pub fn hz_to_midi_cents(hz: f64) -> f64 {
    6900.0 + CENTS_PER_OCTAVE * (hz / 440.0).log2()
}

/// {
///   責務: [`linear_credit`: 誤差を満点閾値から0点閾値まで線形に減る0..1へ変換する]
///   処理: [1: 満点閾値以下は1, 2: 0点閾値以上は0, 3: 間は線形補間する]
///   引数: [value: 非負の誤差, full: 満点閾値, zero: 0点閾値]
///   戻り値: [`f64`: credit]
/// }
fn linear_credit(value: f64, full: f64, zero: f64) -> f64 {
    if value <= full {
        1.0
    } else if value >= zero {
        0.0
    } else {
        (zero - value) / (zero - full)
    }
}

/// {
///   責務: [median: 値列の中央値を決定的に求める]
///   処理: [1: total orderで並べる, 2: 偶数個なら中央2値を平均する]
///   引数: [values: 1件以上の値（並べ替える）]
///   戻り値: [`f64`: 中央値]
/// }
fn median(values: &mut [f64]) -> f64 {
    values.sort_unstable_by(f64::total_cmp);
    let mid = values.len() / 2;
    if values.len().is_multiple_of(2) {
        f64::midpoint(values[mid - 1], values[mid])
    } else {
        values[mid]
    }
}

/// {
///   責務: [`valid_label`: 保存する版名が短い可視ASCIIか検証する]
///   処理: [1: 長さと文字種を確認する]
///   引数: [label: 版名]
///   戻り値: [`bool`: 有効ならtrue]
/// }
fn valid_label(label: &str) -> bool {
    (1..=MAX_VERSION_LABEL_CHARS).contains(&label.len())
        && label.bytes().all(|byte| byte.is_ascii_graphic())
}

#[cfg(test)]
mod tests;
