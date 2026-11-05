//! 歌詞・採点ノートの編集コマンドと検証（純関数）。保存・世代管理・APIは含まない（#38 スライス38.2以降）。
//!
//! 出自の表現（`format_version`は上げない）:
//! - 歌詞: 既存の `LyricSegment.source` に `"manual"` を入れる。
//! - ノート: `Note` に欄を足さず、`EvidenceDocument` の `target_id = note.id` の項目へ
//!   `evidence["edit_origin"] = {"source": "manual"}` を追記する（追加のみ・既存契約不変）。

#![allow(clippy::doc_markdown)] // JSON-style declaration comments name snake_case fields

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::song_data::{EvidenceItem, LyricSegment, Note, SongBundle, SongDataError};

/// 手編集の出自を表す`source`値。
pub const MANUAL_SOURCE: &str = "manual";
/// 出自を記録するevidenceのキー。
pub const EDIT_ORIGIN_KEY: &str = "edit_origin";
/// 1コマンドあたりの最大op数（異常入力による過大処理を防ぐ）。
pub const MAX_EDIT_OPS: usize = 1024;

/// {
///   責務: [`EditOp`: 1件の編集操作。sampleは48kHz整数のみ]
///   フィールド: [op(JSONタグ・snake_case), id, text, start_sample, end_sample, midi_note, lyric_segment_id]
/// }
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum EditOp {
    UpdateLyric {
        id: String,
        text: String,
        start_sample: u64,
        end_sample: u64,
    },
    UpdateNote {
        id: String,
        start_sample: u64,
        end_sample: u64,
        midi_note: u8,
    },
    AddLyric {
        id: String,
        text: String,
        start_sample: u64,
        end_sample: u64,
    },
    AddNote {
        id: String,
        start_sample: u64,
        end_sample: u64,
        midi_note: u8,
        lyric_segment_id: Option<String>,
    },
    DeleteLyric {
        id: String,
    },
    DeleteNote {
        id: String,
    },
}

/// {
///   責務: [`EditCommand`: 1曲に対する編集の一括要求。全opが通る時だけ適用される]
///   フィールド: [song_id, base_generation(競合判定は38.2。純関数では保持のみ), ops]
/// }
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct EditCommand {
    pub song_id: String,
    pub base_generation: u64,
    pub ops: Vec<EditOp>,
}

/// {
///   責務: [`EditViolationCode`: 拒否理由を呼び出し側が機械的に判別する分類]
/// }
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EditViolationCode {
    InvalidRange,
    InvalidMidiNote,
    UnknownId,
    DuplicateId,
    EmptyId,
    EmptyText,
    MissingLyricRef,
    BundleInvalid,
}

/// {
///   責務: [`EditViolation`: 拒否された1件の違反。op_indexは該当opの位置(束全体の違反はNone)]
///   フィールド: [op_index, code, message]
/// }
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct EditViolation {
    pub op_index: Option<usize>,
    pub code: EditViolationCode,
    pub message: String,
}

/// {
///   責務: [`EditError`: 編集全体の拒否理由。拒否時は元の束を一切変更しない]
/// }
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EditError {
    /// opが空。
    EmptyOps,
    /// opが`MAX_EDIT_OPS`超過。
    TooManyOps { count: usize, max: usize },
    /// 対象曲IDが束と一致しない。
    SongIdMismatch { expected: String, actual: String },
    /// 1件以上のop違反（全op不適用）。
    Rejected(Vec<EditViolation>),
}

impl std::fmt::Display for EditError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyOps => write!(formatter, "edit command has no ops"),
            Self::TooManyOps { count, max } => {
                write!(formatter, "edit command has {count} ops, max is {max}")
            }
            Self::SongIdMismatch { expected, actual } => {
                write!(
                    formatter,
                    "song_id mismatch: bundle {expected}, command {actual}"
                )
            }
            Self::Rejected(violations) => {
                let messages = violations
                    .iter()
                    .map(|violation| violation.message.as_str())
                    .collect::<Vec<_>>();
                write!(formatter, "edit rejected: {}", messages.join("; "))
            }
        }
    }
}

impl std::error::Error for EditError {}

/// {
///   責務: [`apply_edit`: 束のコピーへopを順に適用し、検証が全て通った時だけ新しい束を返す純関数]
///   処理: [song_id/件数確認 → 各op検証+適用(違反は収集し当該opは不適用) → 違反があれば全体拒否 → 出自記録 → start/end/id順に整列 → SongBundle::validate]
///   引数: [bundle: 編集元(不変), command: 編集コマンド]
///   戻り値: [Ok(新しい束) / Err(EditError)。入力は常に不変で原子的、同入力なら同出力]
/// }
///
/// # Errors
/// 不正range・不正MIDI・存在しない/重複ID・存在しない歌詞参照・束validate違反を`EditError`で返す。
pub fn apply_edit(bundle: &SongBundle, command: &EditCommand) -> Result<SongBundle, EditError> {
    if command.ops.is_empty() {
        return Err(EditError::EmptyOps);
    }
    if command.ops.len() > MAX_EDIT_OPS {
        return Err(EditError::TooManyOps {
            count: command.ops.len(),
            max: MAX_EDIT_OPS,
        });
    }
    if command.song_id != bundle.song.song_id {
        return Err(EditError::SongIdMismatch {
            expected: bundle.song.song_id.clone(),
            actual: command.song_id.clone(),
        });
    }

    let mut next = bundle.clone();
    let mut violations = Vec::new();
    let mut manual_notes = BTreeSet::new();
    for (index, op) in command.ops.iter().enumerate() {
        apply_op(&mut next, index, op, &mut manual_notes, &mut violations);
    }
    if !violations.is_empty() {
        return Err(EditError::Rejected(violations));
    }

    record_note_origins(&mut next, &manual_notes);
    next.lyrics.segments.sort_by(|a, b| {
        (a.start_sample, a.end_sample, &a.id).cmp(&(b.start_sample, b.end_sample, &b.id))
    });
    next.notes.notes.sort_by(|a, b| {
        (a.start_sample, a.end_sample, &a.id).cmp(&(b.start_sample, b.end_sample, &b.id))
    });

    if let Err(error) = next.validate() {
        let messages = match error {
            SongDataError::Validation(messages) => messages,
            other => vec![other.to_string()],
        };
        return Err(EditError::Rejected(
            messages
                .into_iter()
                .map(|message| EditViolation {
                    op_index: None,
                    code: EditViolationCode::BundleInvalid,
                    message,
                })
                .collect(),
        ));
    }
    Ok(next)
}

/// {
///   責務: [`apply_op`: 1opを検証し、違反が無ければ作業用の束へ適用する]
///   引数: [next(作業用コピー), index(op位置), op, manual_notes(出自記録対象のノートID), violations(違反の蓄積先)]
///   戻り値: [なし。違反時は当該opを適用せず violations へ追加]
/// }
fn apply_op(
    next: &mut SongBundle,
    index: usize,
    op: &EditOp,
    manual_notes: &mut BTreeSet<String>,
    violations: &mut Vec<EditViolation>,
) {
    match op {
        EditOp::UpdateLyric { .. } | EditOp::AddLyric { .. } | EditOp::DeleteLyric { .. } => {
            apply_lyric_op(next, index, op, violations);
        }
        EditOp::UpdateNote { .. } | EditOp::AddNote { .. } | EditOp::DeleteNote { .. } => {
            apply_note_op(next, index, op, manual_notes, violations);
        }
    }
}

/// {
///   責務: [`apply_lyric_op`: 歌詞opを検証し、違反が無ければ作業用の束へ適用する]
///   引数: [next, index(op位置), op, violations]
///   戻り値: [なし。違反時は当該opを適用せず violations へ追加]
/// }
fn apply_lyric_op(
    next: &mut SongBundle,
    index: usize,
    op: &EditOp,
    violations: &mut Vec<EditViolation>,
) {
    let duration = next.song.duration_samples;
    let mut fail = |code: EditViolationCode, message: String| {
        violations.push(EditViolation {
            op_index: Some(index),
            code,
            message,
        });
    };
    match op {
        EditOp::UpdateLyric {
            id,
            text,
            start_sample,
            end_sample,
        } => {
            let ok = check_range(*start_sample, *end_sample, duration, "lyric", id, &mut fail)
                & check_text(text, id, &mut fail);
            let Some(segment) = next.lyrics.segments.iter_mut().find(|s| &s.id == id) else {
                fail(
                    EditViolationCode::UnknownId,
                    format!("lyric {id} does not exist"),
                );
                return;
            };
            if ok {
                if segment.text != *text {
                    segment.text_confidence = None;
                }
                if (segment.start_sample, segment.end_sample) != (*start_sample, *end_sample) {
                    segment.timing_confidence = None;
                }
                segment.text.clone_from(text);
                segment.start_sample = *start_sample;
                segment.end_sample = *end_sample;
                segment.source = Some(MANUAL_SOURCE.to_owned());
            }
        }
        EditOp::AddLyric {
            id,
            text,
            start_sample,
            end_sample,
        } => {
            let ok = check_id(id, &mut fail)
                & check_range(*start_sample, *end_sample, duration, "lyric", id, &mut fail)
                & check_text(text, id, &mut fail);
            if next.lyrics.segments.iter().any(|s| &s.id == id) {
                fail(
                    EditViolationCode::DuplicateId,
                    format!("lyric {id} already exists"),
                );
            } else if ok {
                next.lyrics.segments.push(LyricSegment {
                    id: id.clone(),
                    text: text.clone(),
                    start_sample: *start_sample,
                    end_sample: *end_sample,
                    text_confidence: None,
                    timing_confidence: None,
                    source: Some(MANUAL_SOURCE.to_owned()),
                });
            }
        }
        EditOp::DeleteLyric { id } => {
            let before = next.lyrics.segments.len();
            next.lyrics.segments.retain(|s| &s.id != id);
            if next.lyrics.segments.len() == before {
                fail(
                    EditViolationCode::UnknownId,
                    format!("lyric {id} does not exist"),
                );
            }
        }
        _ => {}
    }
}

/// {
///   責務: [`apply_note_op`: ノートopを検証し、違反が無ければ作業用の束へ適用する]
///   引数: [next, index(op位置), op, manual_notes, violations]
///   戻り値: [なし。違反時は当該opを適用せず violations へ追加]
/// }
fn apply_note_op(
    next: &mut SongBundle,
    index: usize,
    op: &EditOp,
    manual_notes: &mut BTreeSet<String>,
    violations: &mut Vec<EditViolation>,
) {
    let duration = next.song.duration_samples;
    let mut fail = |code: EditViolationCode, message: String| {
        violations.push(EditViolation {
            op_index: Some(index),
            code,
            message,
        });
    };
    match op {
        EditOp::UpdateNote {
            id,
            start_sample,
            end_sample,
            midi_note,
        } => {
            let ok = check_range(*start_sample, *end_sample, duration, "note", id, &mut fail)
                & check_midi(*midi_note, id, &mut fail);
            let Some(note) = next.notes.notes.iter_mut().find(|n| &n.id == id) else {
                fail(
                    EditViolationCode::UnknownId,
                    format!("note {id} does not exist"),
                );
                return;
            };
            if ok {
                if note.midi_note != *midi_note {
                    note.pitch_confidence = None;
                }
                note.start_sample = *start_sample;
                note.end_sample = *end_sample;
                note.midi_note = *midi_note;
                manual_notes.insert(id.clone());
            }
        }
        EditOp::AddNote {
            id,
            start_sample,
            end_sample,
            midi_note,
            lyric_segment_id,
        } => {
            let mut ok = check_id(id, &mut fail)
                & check_range(*start_sample, *end_sample, duration, "note", id, &mut fail)
                & check_midi(*midi_note, id, &mut fail);
            if let Some(segment_id) = lyric_segment_id
                && !next.lyrics.segments.iter().any(|s| &s.id == segment_id)
            {
                fail(
                    EditViolationCode::MissingLyricRef,
                    format!("note {id} references missing lyric segment {segment_id}"),
                );
                ok = false;
            }
            if next.notes.notes.iter().any(|n| &n.id == id) {
                fail(
                    EditViolationCode::DuplicateId,
                    format!("note {id} already exists"),
                );
            } else if ok {
                next.notes.notes.push(Note {
                    id: id.clone(),
                    start_sample: *start_sample,
                    end_sample: *end_sample,
                    midi_note: *midi_note,
                    pitch_confidence: None,
                    lyric_segment_id: lyric_segment_id.clone(),
                });
                manual_notes.insert(id.clone());
            }
        }
        EditOp::DeleteNote { id } => {
            let before = next.notes.notes.len();
            next.notes.notes.retain(|n| &n.id != id);
            manual_notes.remove(id);
            if next.notes.notes.len() == before {
                fail(
                    EditViolationCode::UnknownId,
                    format!("note {id} does not exist"),
                );
            }
        }
        _ => {}
    }
}

/// {
///   責務: [`check_range`: start<end かつ end<=duration を検査する]
///   戻り値: [正しければtrue。違反はfailで通知]
/// }
fn check_range(
    start: u64,
    end: u64,
    duration: u64,
    kind: &str,
    id: &str,
    fail: &mut impl FnMut(EditViolationCode, String),
) -> bool {
    if start >= end {
        fail(
            EditViolationCode::InvalidRange,
            format!("{kind} {id} must have start_sample < end_sample"),
        );
        return false;
    }
    if end > duration {
        fail(
            EditViolationCode::InvalidRange,
            format!("{kind} {id} ends at {end}, beyond song duration {duration}"),
        );
        return false;
    }
    true
}

/// {
///   責務: [`check_midi`: MIDI番号が0..=127か検査する]
/// }
fn check_midi(midi: u8, id: &str, fail: &mut impl FnMut(EditViolationCode, String)) -> bool {
    if midi > 127 {
        fail(
            EditViolationCode::InvalidMidiNote,
            format!("note {id} has invalid MIDI note {midi}"),
        );
        return false;
    }
    true
}

/// {
///   責務: [`check_id`: 追加IDが空白のみでないか検査する]
/// }
fn check_id(id: &str, fail: &mut impl FnMut(EditViolationCode, String)) -> bool {
    if id.trim().is_empty() {
        fail(
            EditViolationCode::EmptyId,
            "id must not be empty".to_owned(),
        );
        return false;
    }
    true
}

/// {
///   責務: [`check_text`: 歌詞本文が空白のみでないか検査する]
/// }
fn check_text(text: &str, id: &str, fail: &mut impl FnMut(EditViolationCode, String)) -> bool {
    if text.trim().is_empty() {
        fail(
            EditViolationCode::EmptyText,
            format!("lyric {id} text must not be empty"),
        );
        return false;
    }
    true
}

/// {
///   責務: [`record_note_origins`: 手編集したノートのevidenceへ`edit_origin`を追記する（既存の他キーは保持、新規は末尾追加）]
///   引数: [bundle, manual_notes(最終的に残る手編集ノートID)]
/// }
fn record_note_origins(bundle: &mut SongBundle, manual_notes: &BTreeSet<String>) {
    for id in manual_notes {
        let origin = json!({ "source": MANUAL_SOURCE });
        if let Some(item) = bundle
            .evidence
            .items
            .iter_mut()
            .find(|item| &item.target_id == id)
        {
            item.evidence.insert(EDIT_ORIGIN_KEY.to_owned(), origin);
        } else {
            let mut evidence = std::collections::BTreeMap::new();
            evidence.insert(EDIT_ORIGIN_KEY.to_owned(), origin);
            bundle.evidence.items.push(EvidenceItem {
                target_id: id.clone(),
                evidence,
            });
        }
    }
}
