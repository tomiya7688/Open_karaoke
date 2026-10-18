//! Audio-inclusive storage regressions use synthetic exports, not singing-quality fixtures.

#[path = "support/song_export.rs"]
mod fixture;

use open_karaoke_core::{
    Job,
    song_data::{SongBundle, SongStore},
    song_library::{ImportControl, ImportError, SongLibrary},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{fs, path::PathBuf};
use uuid::Uuid;

struct Harness {
    root: tempfile::TempDir,
    artifacts: PathBuf,
    songs: PathBuf,
    source: Value,
    export: Value,
    library: SongLibrary,
}
impl Harness {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let artifacts = root.path().join("artifacts");
        let songs = root.path().join("songs");
        let library = SongLibrary::open(&artifacts, &songs).unwrap();
        let (source, export) = fixture::build(&artifacts);
        Self {
            root,
            artifacts,
            songs,
            source,
            export,
            library,
        }
    }
    fn job(&self) -> Job {
        serde_json::from_value(self.source.clone()).unwrap()
    }
    fn id(&self) -> Uuid {
        Uuid::parse_str(self.export["bundle"]["song"]["song_id"].as_str().unwrap()).unwrap()
    }
    fn rewrite(&self) {
        fixture::write_result(&self.artifacts, &self.source, &self.export);
    }
    fn import(&self) -> Result<open_karaoke_core::song_library::ImportedSong, ImportError> {
        self.library
            .import(&self.job(), &ImportControl::default(), |_, _| {})
    }
    fn assert_unpublished(&self) {
        assert!(!self.songs.join(self.id().to_string()).exists());
        assert!(!fs::read_dir(&self.songs).unwrap().any(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".staging-")
        }));
        assert!(self.library.list(None, 100).unwrap().songs.is_empty());
    }
    fn replace_asset(&mut self, suffix: &str, change: impl FnOnce(&mut Vec<u8>)) {
        let name = self.export["assets"]
            .as_array()
            .unwrap()
            .iter()
            .find(|asset| asset["source"].as_str().unwrap().ends_with(suffix))
            .unwrap()["source"]
            .as_str()
            .unwrap()
            .to_owned();
        let path = self.artifacts.join(&name);
        let mut bytes = fs::read(&path).unwrap();
        change(&mut bytes);
        fs::write(path, &bytes).unwrap();
        let hash = format!("{:x}", Sha256::digest(&bytes));
        for asset in self.export["assets"].as_array_mut().unwrap() {
            if asset["source"] == name {
                asset["sha256"] = hash.clone().into();
                asset["bytes"] = json!(bytes.len());
            }
        }
        self.rewrite();
    }
}

#[test]
fn complete_audio_and_provenance_publish_together_and_round_trip() {
    let test = Harness::new();
    let mut progress = Vec::new();
    let control = ImportControl::default();
    let registered = test
        .library
        .import(&test.job(), &control, |position, _| {
            assert!(test.library.list(None, 100).unwrap().songs.is_empty());
            progress.push(position);
        })
        .unwrap();
    assert_eq!(registered.song_id, test.id());
    assert!(!registered.reused);
    assert!(!control.cancel());
    assert!(progress.windows(2).all(|pair| pair[0] <= pair[1]));
    assert!(progress.iter().all(|value| *value < 10_000));
    let target = test.songs.join(test.id().to_string());
    for asset in test.export["assets"].as_array().unwrap() {
        assert_eq!(
            fs::read(test.artifacts.join(asset["source"].as_str().unwrap())).unwrap(),
            fs::read(target.join(asset["destination"].as_str().unwrap())).unwrap()
        );
    }
    let expected: SongBundle = serde_json::from_value(test.export["bundle"].clone()).unwrap();
    assert_eq!(test.library.load(test.id()).unwrap(), expected);
    assert_eq!(SongStore::load(&target).unwrap(), expected);
    assert!(target.join("import-receipt.json").is_file());
    assert_eq!(test.library.list(None, 100).unwrap().songs.len(), 1);
}

#[test]
fn retry_reuses_identity_but_conflicting_results_never_overwrite() {
    let mut test = Harness::new();
    assert!(!test.import().unwrap().reused);
    assert!(test.import().unwrap().reused);
    assert_eq!(test.library.list(None, 100).unwrap().songs.len(), 1);
    let saved = test.library.load(test.id()).unwrap();
    test.export["bundle"]["song"]["title"] = "different analysis".into();
    test.rewrite();
    assert!(matches!(test.import(), Err(ImportError::Conflict)));
    assert_eq!(test.library.load(test.id()).unwrap(), saved);
}

#[test]
fn same_length_source_corruption_is_detected_and_staging_is_cleaned() {
    let test = Harness::new();
    let source = test.export["assets"][0]["source"].as_str().unwrap();
    let mut bytes = fs::read(test.artifacts.join(source)).unwrap();
    bytes[44] ^= 1;
    fs::write(test.artifacts.join(source), bytes).unwrap();
    assert!(test.import().unwrap_err().to_string().contains("checksum"));
    test.assert_unpublished();
}

#[test]
fn checksum_valid_but_invalid_audio_is_never_published() {
    for fault in [
        "rate",
        "channels",
        "format",
        "nan",
        "infinity",
        "truncated",
        "duration",
    ] {
        let mut test = Harness::new();
        test.replace_asset("stages/stems/accompaniment.wav", |bytes| match fault {
            "rate" => bytes[24..28].copy_from_slice(&44_100_u32.to_le_bytes()),
            "channels" => bytes[22..24].copy_from_slice(&1_u16.to_le_bytes()),
            "format" => bytes[20..22].copy_from_slice(&1_u16.to_le_bytes()),
            "nan" => bytes[44..48].copy_from_slice(&f32::NAN.to_le_bytes()),
            "infinity" => bytes[44..48].copy_from_slice(&f32::INFINITY.to_le_bytes()),
            "truncated" => bytes.truncate(44),
            _ => {
                bytes.truncate(bytes.len() - 8);
                bytes[4..8].copy_from_slice(&1052_u32.to_le_bytes());
                bytes[40..44].copy_from_slice(&1016_u32.to_le_bytes());
            }
        });
        assert!(test.import().is_err(), "{fault}");
        test.assert_unpublished();
    }
}

#[test]
fn canonical_document_must_match_the_verified_stage_output() {
    let mut test = Harness::new();
    test.export["bundle"]["notes"]["notes"][0]["midi_note"] = 70.into();
    test.rewrite();
    assert!(
        test.import()
            .unwrap_err()
            .to_string()
            .contains("stage outputs")
    );
    test.assert_unpublished();
}

#[test]
fn invalid_paths_identity_dependencies_and_references_are_rejected() {
    for fault in 0..13 {
        let mut test = Harness::new();
        match fault {
            0 => test.export["assets"][0]["destination"] = "../escape.wav".into(),
            1 => test.export["assets"][0]["destination"] = "song.json".into(),
            2 => test.export["assets"][0]["source"] = "other/not-owned.wav".into(),
            3 => test.export["assets"][0]["source"] = "C:/outside.wav".into(),
            4 => {
                let asset = test.export["assets"][0].clone();
                test.export["assets"].as_array_mut().unwrap().push(asset);
            }
            5 => {
                test.export["assets"].as_array_mut().unwrap().remove(0);
            }
            6 => test.export["bundle"]["song"]["song_id"] = json!(Uuid::new_v4()),
            7 => test.export["bundle"]["song"]["artifacts"]["vocals"]["valid"] = false.into(),
            8 => test.export["bundle"]["notes"]["analysis_version"] = 2.into(),
            9 => test.export["bundle"]["notes"]["notes"][0]["lyric_segment_id"] = "missing".into(),
            10 => test.export["bundle"]["notes"]["notes"][0]["end_sample"] = 129.into(),
            11 => test.export["bundle"]["notes"]["notes"][0]["id"] = "line-1".into(),
            _ => test.export["bundle"]["song"]["files"]["original"] = "analysis/notes.json".into(),
        }
        test.rewrite();
        assert!(test.import().is_err(), "fault {fault}");
        test.assert_unpublished();
    }
}

#[test]
fn incomplete_or_wrong_job_kind_is_not_importable() {
    for (field, value) in [
        ("status", "running"),
        ("status", "failed"),
        ("kind", "analysis.mock"),
    ] {
        let mut test = Harness::new();
        test.source[field] = value.into();
        assert!(test.import().is_err());
        test.assert_unpublished();
    }
}

#[test]
fn missing_or_truncated_assets_leave_no_partial_song() {
    for remove in [false, true] {
        let test = Harness::new();
        let source = test.export["assets"][4]["source"].as_str().unwrap();
        if remove {
            fs::remove_file(test.artifacts.join(source)).unwrap();
        } else {
            fs::write(test.artifacts.join(source), b"{}").unwrap();
        }
        assert!(test.import().is_err());
        test.assert_unpublished();
    }
}

#[test]
fn cancellation_at_each_publication_boundary_cleans_only_private_staging() {
    for cancel_at in ["before", "song_import.copying", "song_import.publishing"] {
        let test = Harness::new();
        let control = ImportControl::default();
        if cancel_at == "before" {
            assert!(control.cancel());
        }
        let outcome = test.library.import(&test.job(), &control, |_, stage| {
            if stage == cancel_at {
                assert!(control.cancel());
            }
        });
        assert!(
            matches!(outcome, Err(ImportError::Cancelled)),
            "{cancel_at}"
        );
        test.assert_unpublished();
        assert!(
            test.artifacts
                .join(test.export["assets"][0]["source"].as_str().unwrap())
                .is_file()
        );
    }
}

#[test]
fn separate_library_instances_obey_the_same_os_writer_lock() {
    let test = Harness::new();
    let second = SongLibrary::open(&test.artifacts, &test.songs).unwrap();
    let lock = fs::File::create(test.songs.join(".writer.lock")).unwrap();
    lock.try_lock().unwrap();
    assert!(matches!(
        second.import(&test.job(), &ImportControl::default(), |_, _| {}),
        Err(ImportError::Busy)
    ));
    drop(lock);
    assert!(
        !second
            .import(&test.job(), &ImportControl::default(), |_, _| {})
            .unwrap()
            .reused
    );
    assert!(test.import().unwrap().reused);
}

#[test]
fn published_song_survives_reopen_without_worker_artifacts() {
    let test = Harness::new();
    test.import().unwrap();
    let expected = test.library.load(test.id()).unwrap();
    fs::remove_dir_all(&test.artifacts).unwrap();
    let reopened = SongLibrary::open(&test.artifacts, &test.songs).unwrap();
    assert_eq!(reopened.load(test.id()).unwrap(), expected);
}

#[test]
fn damaged_existing_song_is_reported_and_never_silently_replaced() {
    let test = Harness::new();
    test.import().unwrap();
    let path = test
        .songs
        .join(test.id().to_string())
        .join("audio/vocals.wav");
    fs::write(path, b"damage").unwrap();
    assert!(test.library.load(test.id()).is_err());
    let page = test.library.list(None, 50).unwrap();
    assert_eq!(page.songs.len(), 1);
    assert!(page.songs[0].error.is_some());
    assert!(matches!(test.import(), Err(ImportError::Conflict)));
}

#[test]
fn listing_is_sorted_paginated_and_hides_crash_staging() {
    let test = Harness::new();
    test.import().unwrap();
    let (second, _) = fixture::build(&test.artifacts);
    let second: Job = serde_json::from_value(second).unwrap();
    test.library
        .import(&second, &ImportControl::default(), |_, _| {})
        .unwrap();
    fs::create_dir(test.songs.join(format!(".staging-{}", Uuid::new_v4()))).unwrap();
    let first = test.library.list(None, 1).unwrap();
    let next = test.library.list(first.next_after, 1).unwrap();
    assert_eq!(first.songs.len(), 1);
    assert_eq!(next.songs.len(), 1);
    assert!(first.songs[0].song_id < next.songs[0].song_id);
    assert!(next.next_after.is_none());
    assert!(
        test.library
            .list(Some(next.songs[0].song_id), 1)
            .unwrap()
            .songs
            .is_empty()
    );
    assert!(test.library.list(None, 0).is_err());
    assert!(test.library.list(None, 101).is_err());
    assert!(matches!(
        test.library.load(Uuid::new_v4()),
        Err(ImportError::NotFound)
    ));
}

#[test]
fn unsafe_roots_oversized_json_and_invalid_utf8_are_rejected() {
    let mut test = Harness::new();
    assert!(SongLibrary::open(&test.artifacts, test.artifacts.join("songs")).is_err());
    assert!(SongLibrary::open(test.root.path().join(".."), &test.songs).is_err());
    test.replace_asset("source/reference.txt", |bytes| {
        bytes.clear();
        bytes.push(0xff);
    });
    assert!(test.import().is_err());
    test.assert_unpublished();
    let path = test.artifacts.join(
        test.source["artifacts"]
            .as_array()
            .unwrap()
            .last()
            .unwrap()
            .as_str()
            .unwrap(),
    );
    fs::write(&path, b"{broken").unwrap();
    assert!(test.import().is_err());
    fs::File::create(&path)
        .unwrap()
        .set_len(16 * 1024 * 1024 + 1)
        .unwrap();
    assert!(matches!(test.import(), Err(ImportError::TooLarge)));
    test.assert_unpublished();
}

#[cfg(unix)]
#[test]
fn symlink_sources_roots_and_lockfiles_cannot_escape_storage() {
    use std::os::unix::fs::symlink;
    for location in ["source", "root", "lock"] {
        let test = Harness::new();
        let outside = test.root.path().join("outside");
        fs::create_dir(&outside).unwrap();
        let sentinel = outside.join("keep");
        fs::write(&sentinel, b"unchanged").unwrap();
        match location {
            "source" => {
                let source = test
                    .artifacts
                    .join(test.export["assets"][0]["source"].as_str().unwrap());
                fs::remove_file(&source).unwrap();
                symlink(&sentinel, source).unwrap();
            }
            "root" => {
                fs::remove_dir(&test.songs).unwrap();
                symlink(&outside, &test.songs).unwrap();
            }
            _ => {
                symlink(&sentinel, test.songs.join(".writer.lock")).unwrap();
            }
        }
        assert!(test.import().is_err());
        assert_eq!(fs::read(&sentinel).unwrap(), b"unchanged");
    }
}

#[test]
#[ignore = "Requires Python dev dependencies; explicitly run in SongStore import CI"]
fn real_python_pipeline_export_is_registered_with_audio_and_reloaded() {
    let test = Harness::new();
    let script =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../python/tests/emit_song_import.py");
    let python = std::env::var_os("OPEN_KARAOKE_PYTHON").unwrap_or_else(|| "python".into());
    let output = std::process::Command::new(python)
        .arg(script)
        .arg(&test.artifacts)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let job: Job =
        serde_json::from_slice(&fs::read(test.artifacts.join("import-job.json")).unwrap()).unwrap();
    let imported = test
        .library
        .import(&job, &ImportControl::default(), |_, _| {})
        .unwrap();
    let bundle = test.library.load(imported.song_id).unwrap();
    assert_eq!(bundle.song.title, "合成・全曲登録");
    assert_eq!(bundle.song.duration_samples, 9600);
    assert!(!bundle.notes.notes.is_empty());
    let directory = test.songs.join(imported.song_id.to_string());
    for name in ["original_48k.wav", "vocals.wav", "accompaniment.wav"] {
        assert!(directory.join("audio").join(name).is_file());
    }
    assert_eq!(SongStore::load(&directory).unwrap(), bundle);
    assert!(
        test.library
            .import(&job, &ImportControl::default(), |_, _| {})
            .unwrap()
            .reused
    );
}
