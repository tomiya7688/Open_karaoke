use open_karaoke_core::song_data::{EvidenceDocument, LyricsDocument};

#[test]
fn python_lyric_candidate_fixture_matches_rust_schema() {
    let text = include_str!("fixtures/whisper_lyrics.json");
    let lyrics: LyricsDocument = serde_json::from_str(text).unwrap();
    assert_eq!(lyrics.format_version, 1);
    assert_eq!(lyrics.segments[0].text, "青い空");
    assert_eq!(lyrics.segments[0].start_sample, 4_800);
    assert_eq!(lyrics.segments[0].end_sample, 38_400);
    assert!(lyrics.segments[0].text_confidence.is_none());
    let original: serde_json::Value = serde_json::from_str(text).unwrap();
    assert_eq!(serde_json::to_value(lyrics).unwrap(), original);
}

#[test]
fn python_lyric_evidence_is_readable_without_schema_migration() {
    let evidence: EvidenceDocument =
        serde_json::from_str(include_str!("fixtures/whisper_evidence.json")).unwrap();
    assert_eq!(evidence.format_version, 1);
    assert_eq!(evidence.items[1].target_id, "lyric-000001");
    assert_eq!(evidence.items[1].evidence["confidence_calibrated"], false);
}
