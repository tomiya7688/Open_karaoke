//! Cross-language schema/persistence checks, not a production song-import endpoint.

use std::{fs, path::PathBuf, process::Command};

use open_karaoke_core::song_data::{SongBundle, SongStore};

#[test]
#[ignore = "Requires Python dev dependencies; explicitly run in song-regression CI"]
fn python_pipeline_bundle_round_trips_through_song_store() {
    let root = tempfile::tempdir().unwrap();
    let fixture = root.path().join("fixture");
    let script = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../python/tests/emit_song_bundle.py");
    let python = std::env::var_os("OPEN_KARAOKE_PYTHON")
        .map_or_else(|| PathBuf::from("python"), PathBuf::from);
    let output = Command::new(python)
        .arg(script)
        .arg(&fixture)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "fixture generation failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let bundle: SongBundle =
        serde_json::from_slice(&fs::read(fixture.join("bundle.json")).unwrap()).unwrap();
    bundle.validate().unwrap();
    assert_eq!(bundle.song.sample_rate, 48_000);
    assert_eq!(bundle.song.duration_samples, 9600);
    assert_eq!(bundle.song.analysis_version, 2);
    assert_eq!(bundle.notes.notes[0].lyric_segment_id.as_deref(), Some("line-1"));
    assert_eq!(bundle.song.artifacts.len(), 6);
    let stored = root.path().join("song-metadata");
    SongStore::save(&stored, &bundle).unwrap();
    assert_eq!(SongStore::load(&stored).unwrap(), bundle);
    // This proves document compatibility only. Asset verification and atomic
    // product registration are separate work; tests must not imply they exist.
    assert!(!stored.join("audio/original_48k.wav").exists());
    let mut invalid = bundle;
    invalid.notes.notes[0].end_sample = invalid.song.duration_samples + 1;
    assert!(invalid.validate().is_err());
}
