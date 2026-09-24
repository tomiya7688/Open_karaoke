use open_karaoke_core::song_data::{EvidenceDocument, LyricsDocument};

#[test]
fn aligned_lyrics_keep_the_version_one_contract() {
    let document: LyricsDocument =
        serde_json::from_str(include_str!("fixtures/aligned_lyrics.json")).unwrap();
    assert_eq!(document.format_version, 1);
    let segment = &document.segments[0];
    assert_eq!(segment.text, "あい");
    assert_eq!(segment.start_sample, 6_857);
    assert_eq!(segment.end_sample, 41_143);
    assert!(segment.timing_confidence.is_none());
    let evidence: EvidenceDocument =
        serde_json::from_str(include_str!("fixtures/alignment_evidence.json")).unwrap();
    assert_eq!(evidence.items[0].target_id, segment.id);
    assert_eq!(evidence.items[0].evidence["timing_kind"], "ctc_forced");
}

#[test]
fn fractional_alignment_samples_are_not_accepted() {
    let invalid = include_str!("fixtures/aligned_lyrics.json").replace("6857", "6857.5");
    assert!(serde_json::from_str::<LyricsDocument>(&invalid).is_err());
}
