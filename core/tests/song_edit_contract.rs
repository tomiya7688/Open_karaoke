//! #38 スライス38.1: 編集コマンドの検証・原子性・決定性・出自の契約テスト。

use std::collections::BTreeMap;

use open_karaoke_core::song_data::{
    CURRENT_FORMAT_VERSION, EvidenceDocument, LyricSegment, LyricsDocument, Note, NotesDocument,
    SongBundle, SongFiles, SongManifest,
};
use open_karaoke_core::song_edit::{
    EDIT_ORIGIN_KEY, EditCommand, EditError, EditOp, EditViolationCode, MANUAL_SOURCE,
    MAX_EDIT_OPS, apply_edit,
};

/// {
///   責務: [`bundle`: 歌詞1件・ノート1件の基準束(48kHz, 4秒)を作る]
/// }
fn bundle() -> SongBundle {
    SongBundle {
        song: SongManifest {
            format_version: CURRENT_FORMAT_VERSION,
            song_id: "song-1".to_owned(),
            title: "t".to_owned(),
            artist: None,
            sample_rate: 48_000,
            duration_samples: 192_000,
            analysis_version: 1,
            files: SongFiles::default(),
            artifacts: BTreeMap::new(),
        },
        lyrics: LyricsDocument {
            format_version: CURRENT_FORMAT_VERSION,
            analysis_version: 1,
            segments: vec![LyricSegment {
                id: "l1".to_owned(),
                text: "hello".to_owned(),
                start_sample: 48_000,
                end_sample: 96_000,
                text_confidence: Some(0.9),
                timing_confidence: Some(0.8),
                source: Some("asr".to_owned()),
            }],
        },
        notes: NotesDocument {
            format_version: CURRENT_FORMAT_VERSION,
            analysis_version: 1,
            notes: vec![Note {
                id: "n1".to_owned(),
                start_sample: 48_000,
                end_sample: 72_000,
                midi_note: 60,
                pitch_confidence: Some(0.7),
                lyric_segment_id: Some("l1".to_owned()),
            }],
        },
        evidence: EvidenceDocument {
            format_version: CURRENT_FORMAT_VERSION,
            analysis_version: 1,
            items: Vec::new(),
        },
    }
}

fn command(ops: Vec<EditOp>) -> EditCommand {
    EditCommand {
        song_id: "song-1".to_owned(),
        base_generation: 1,
        ops,
    }
}

fn codes(error: EditError) -> Vec<EditViolationCode> {
    match error {
        EditError::Rejected(violations) => violations.iter().map(|v| v.code).collect(),
        other => panic!("expected Rejected, got {other:?}"),
    }
}

fn update_note(start: u64, end: u64, midi: u8) -> EditOp {
    EditOp::UpdateNote {
        id: "n1".to_owned(),
        start_sample: start,
        end_sample: end,
        midi_note: midi,
    }
}

#[test]
fn rejects_invalid_sample_ranges() {
    for (start, end) in [(10, 10), (20, 10), (0, 192_001)] {
        let error = apply_edit(&bundle(), &command(vec![update_note(start, end, 60)])).unwrap_err();
        assert_eq!(codes(error), vec![EditViolationCode::InvalidRange]);
    }
    let lyric = EditOp::AddLyric {
        id: "l2".to_owned(),
        text: "x".to_owned(),
        start_sample: 5,
        end_sample: 5,
    };
    assert_eq!(
        codes(apply_edit(&bundle(), &command(vec![lyric])).unwrap_err()),
        vec![EditViolationCode::InvalidRange]
    );
}

#[test]
fn rejects_invalid_midi_and_empty_text_or_id() {
    assert_eq!(
        codes(apply_edit(&bundle(), &command(vec![update_note(0, 10, 128)])).unwrap_err()),
        vec![EditViolationCode::InvalidMidiNote]
    );
    let ops = vec![EditOp::AddLyric {
        id: " ".to_owned(),
        text: " ".to_owned(),
        start_sample: 0,
        end_sample: 10,
    }];
    let found = codes(apply_edit(&bundle(), &command(ops)).unwrap_err());
    assert!(found.contains(&EditViolationCode::EmptyId));
    assert!(found.contains(&EditViolationCode::EmptyText));
}

#[test]
fn rejects_unknown_duplicate_and_dangling_references() {
    let unknown = vec![
        EditOp::UpdateLyric {
            id: "nope".to_owned(),
            text: "a".to_owned(),
            start_sample: 0,
            end_sample: 10,
        },
        EditOp::DeleteNote {
            id: "nope".to_owned(),
        },
        EditOp::DeleteLyric {
            id: "nope".to_owned(),
        },
        EditOp::UpdateNote {
            id: "nope".to_owned(),
            start_sample: 0,
            end_sample: 10,
            midi_note: 60,
        },
    ];
    assert_eq!(
        codes(apply_edit(&bundle(), &command(unknown)).unwrap_err()),
        vec![EditViolationCode::UnknownId; 4]
    );

    let duplicate = vec![EditOp::AddNote {
        id: "n1".to_owned(),
        start_sample: 0,
        end_sample: 10,
        midi_note: 60,
        lyric_segment_id: None,
    }];
    assert_eq!(
        codes(apply_edit(&bundle(), &command(duplicate)).unwrap_err()),
        vec![EditViolationCode::DuplicateId]
    );

    let dangling_add = vec![EditOp::AddNote {
        id: "n2".to_owned(),
        start_sample: 0,
        end_sample: 10,
        midi_note: 60,
        lyric_segment_id: Some("ghost".to_owned()),
    }];
    assert_eq!(
        codes(apply_edit(&bundle(), &command(dangling_add)).unwrap_err()),
        vec![EditViolationCode::MissingLyricRef]
    );

    // 参照されている歌詞を消すと、束全体の検証で拒否される。
    let delete_referenced = vec![EditOp::DeleteLyric {
        id: "l1".to_owned(),
    }];
    assert_eq!(
        codes(apply_edit(&bundle(), &command(delete_referenced)).unwrap_err()),
        vec![EditViolationCode::BundleInvalid]
    );
}

#[test]
fn rejects_empty_oversized_and_foreign_commands() {
    assert_eq!(
        apply_edit(&bundle(), &command(vec![])).unwrap_err(),
        EditError::EmptyOps
    );
    let many = vec![update_note(0, 10, 60); MAX_EDIT_OPS + 1];
    assert!(matches!(
        apply_edit(&bundle(), &command(many)).unwrap_err(),
        EditError::TooManyOps { .. }
    ));
    let mut foreign = command(vec![update_note(0, 10, 60)]);
    foreign.song_id = "other".to_owned();
    assert!(matches!(
        apply_edit(&bundle(), &foreign).unwrap_err(),
        EditError::SongIdMismatch { .. }
    ));
}

#[test]
fn is_atomic_when_one_op_is_invalid() {
    let original = bundle();
    let ops = vec![
        update_note(0, 24_000, 61),
        EditOp::AddLyric {
            id: "l2".to_owned(),
            text: "ok".to_owned(),
            start_sample: 0,
            end_sample: 10,
        },
        update_note(5, 5, 60),
    ];
    let error = apply_edit(&original, &command(ops)).unwrap_err();
    assert_eq!(codes(error), vec![EditViolationCode::InvalidRange]);
    assert_eq!(original, bundle(), "入力束は不変");
}

#[test]
fn applies_valid_edits_and_records_manual_origin() {
    let ops = vec![
        EditOp::UpdateLyric {
            id: "l1".to_owned(),
            text: "world".to_owned(),
            start_sample: 48_000,
            end_sample: 100_000,
        },
        update_note(48_000, 72_000, 62),
        EditOp::AddNote {
            id: "n2".to_owned(),
            start_sample: 0,
            end_sample: 1000,
            midi_note: 70,
            lyric_segment_id: Some("l1".to_owned()),
        },
    ];
    let edited = apply_edit(&bundle(), &command(ops)).unwrap();

    let lyric = &edited.lyrics.segments[0];
    assert_eq!(lyric.text, "world");
    assert_eq!(lyric.source.as_deref(), Some(MANUAL_SOURCE));
    assert_eq!(lyric.text_confidence, None);
    assert_eq!(lyric.timing_confidence, None);

    // start順に整列され、n2が先頭になる。
    assert_eq!(edited.notes.notes[0].id, "n2");
    assert_eq!(edited.notes.notes[1].midi_note, 62);
    assert_eq!(edited.notes.notes[1].pitch_confidence, None);

    // ノートの出自はevidenceに記録され、format_versionは変わらない。
    let origin_ids: Vec<_> = edited
        .evidence
        .items
        .iter()
        .filter(|item| item.evidence.contains_key(EDIT_ORIGIN_KEY))
        .map(|item| item.target_id.as_str())
        .collect();
    assert_eq!(origin_ids, vec!["n1", "n2"]);
    assert_eq!(edited.song.format_version, CURRENT_FORMAT_VERSION);
    edited.validate().unwrap();
}

#[test]
fn keeps_confidence_when_value_is_unchanged_and_deleted_note_has_no_origin() {
    let ops = vec![
        EditOp::UpdateLyric {
            id: "l1".to_owned(),
            text: "hello".to_owned(),
            start_sample: 48_000,
            end_sample: 96_000,
        },
        update_note(48_000, 72_000, 60),
        EditOp::DeleteNote {
            id: "n1".to_owned(),
        },
    ];
    let edited = apply_edit(&bundle(), &command(ops)).unwrap();
    assert_eq!(edited.lyrics.segments[0].text_confidence, Some(0.9));
    assert_eq!(edited.lyrics.segments[0].timing_confidence, Some(0.8));
    assert!(edited.notes.notes.is_empty());
    assert!(edited.evidence.items.is_empty());
}

#[test]
fn output_is_deterministic_and_order_independent_for_adds() {
    let add = |id: &str, start: u64| EditOp::AddNote {
        id: id.to_owned(),
        start_sample: start,
        end_sample: start + 100,
        midi_note: 65,
        lyric_segment_id: None,
    };
    let a = apply_edit(&bundle(), &command(vec![add("a", 10), add("b", 500)])).unwrap();
    let a_again = apply_edit(&bundle(), &command(vec![add("a", 10), add("b", 500)])).unwrap();
    let b = apply_edit(&bundle(), &command(vec![add("b", 500), add("a", 10)])).unwrap();
    assert_eq!(a, a_again);
    assert_eq!(
        serde_json::to_string(&a.notes).unwrap(),
        serde_json::to_string(&b.notes).unwrap()
    );
    assert_eq!(
        serde_json::to_string(&a).unwrap(),
        serde_json::to_string(&a_again).unwrap()
    );
}

#[test]
fn edit_command_round_trips_as_json() {
    let json = r#"{"song_id":"song-1","base_generation":3,"ops":[
        {"op":"update_note","id":"n1","start_sample":0,"end_sample":10,"midi_note":60},
        {"op":"delete_lyric","id":"l1"}]}"#;
    let parsed: EditCommand = serde_json::from_str(json).unwrap();
    assert_eq!(parsed.base_generation, 3);
    assert_eq!(parsed.ops.len(), 2);
    let text = serde_json::to_string(&parsed).unwrap();
    let again: EditCommand = serde_json::from_str(&text).unwrap();
    assert_eq!(parsed, again);
}
