//! #38 スライス38.2: 公開済み曲への編集保存・世代管理・下流invalidateの契約テスト。
//! 合成フィクスチャのみ（歌唱精度の検証ではない）。

#[path = "support/song_export.rs"]
mod fixture;

use open_karaoke_core::{
    Job,
    song_data::ArtifactKind,
    song_edit::{EditCommand, EditOp},
    song_library::{ImportControl, ImportError, SongEditSaveError, SongLibrary, edit_generation},
};
use std::{collections::BTreeMap, fs, path::PathBuf};
use uuid::Uuid;

struct Harness {
    _root: tempfile::TempDir,
    artifacts: PathBuf,
    songs: PathBuf,
    library: SongLibrary,
    id: Uuid,
    job: Job,
}

impl Harness {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let artifacts = root.path().join("artifacts");
        let songs = root.path().join("songs");
        let library = SongLibrary::open(&artifacts, &songs).unwrap();
        let (source, export) = fixture::build(&artifacts);
        let job: Job = serde_json::from_value(source).unwrap();
        let id = Uuid::parse_str(export["bundle"]["song"]["song_id"].as_str().unwrap()).unwrap();
        library
            .import(&job, &ImportControl::default(), |_, _| {})
            .unwrap();
        Self {
            _root: root,
            artifacts,
            songs,
            library,
            id,
            job,
        }
    }

    fn command(&self, base_generation: u64, ops: Vec<EditOp>) -> EditCommand {
        EditCommand {
            song_id: self.id.to_string(),
            base_generation,
            ops,
        }
    }

    fn song_dir(&self) -> PathBuf {
        self.songs.join(self.id.to_string())
    }

    /// 曲ディレクトリ内の全ファイルの内容を相対パス順に取る（不変性の比較用）。
    fn snapshot(&self) -> BTreeMap<String, Vec<u8>> {
        fn walk(base: &PathBuf, dir: &PathBuf, out: &mut BTreeMap<String, Vec<u8>>) {
            for entry in fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    walk(base, &path, out);
                } else {
                    let name = path
                        .strip_prefix(base)
                        .unwrap()
                        .to_string_lossy()
                        .replace('\\', "/");
                    out.insert(name, fs::read(&path).unwrap());
                }
            }
        }
        let dir = self.song_dir();
        let mut out = BTreeMap::new();
        walk(&dir, &dir, &mut out);
        out
    }

    fn leftovers(&self) -> Vec<String> {
        fs::read_dir(&self.songs)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|name| name.starts_with(".staging-") || name.starts_with(".retired-"))
            .collect()
    }
}

fn lyric_edit(text: &str) -> EditOp {
    EditOp::UpdateLyric {
        id: "line-1".to_owned(),
        text: text.to_owned(),
        start_sample: 0,
        end_sample: 128,
    }
}

fn note_edit(midi_note: u8) -> EditOp {
    EditOp::UpdateNote {
        id: "note-1".to_owned(),
        start_sample: 0,
        end_sample: 128,
        midi_note,
    }
}

#[test]
fn edit_survives_reload_and_new_library_instance() {
    let harness = Harness::new();
    let before = harness.library.load(harness.id).unwrap();
    assert_eq!(edit_generation(&before), 1);
    let audio_before = harness.snapshot()["audio/vocals.wav"].clone();

    let saved = harness
        .library
        .edit_song(
            harness.id,
            &harness.command(1, vec![lyric_edit("い"), note_edit(72)]),
        )
        .unwrap();
    assert_eq!(saved.generation, 2);
    assert_eq!(saved.pending, Vec::<ArtifactKind>::new());

    let reopened = SongLibrary::open(&harness.artifacts, &harness.songs).unwrap();
    let after = reopened.load(harness.id).unwrap();
    assert_eq!(after.lyrics.segments[0].text, "い");
    assert_eq!(after.lyrics.segments[0].source.as_deref(), Some("manual"));
    assert_eq!(after.notes.notes[0].midi_note, 72);
    assert_eq!(edit_generation(&after), 2);
    assert_eq!(harness.snapshot()["audio/vocals.wav"], audio_before);
    let page = reopened.list(None, 10).unwrap();
    assert!(page.songs[0].error.is_none());
    assert_eq!(harness.leftovers(), Vec::<String>::new());
}

#[test]
fn stale_base_generation_is_rejected_and_nothing_changes() {
    let harness = Harness::new();
    harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit("い")]))
        .unwrap();
    let before = harness.snapshot();

    let error = harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit("う")]))
        .unwrap_err();
    assert!(matches!(
        error,
        SongEditSaveError::GenerationConflict {
            expected: 1,
            current: 2
        }
    ));
    assert_eq!(harness.snapshot(), before);
    assert_eq!(
        harness.library.load(harness.id).unwrap().lyrics.segments[0].text,
        "い"
    );
}

#[test]
fn invalidate_propagates_and_is_cleared_by_regeneration() {
    let harness = Harness::new();
    let first = harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit("い")]))
        .unwrap();
    assert_eq!(first.pending, vec![ArtifactKind::Notes]);
    let bundle = harness.library.load(harness.id).unwrap();
    let artifacts = &bundle.song.artifacts;
    assert_eq!(artifacts[&ArtifactKind::Lyrics].generation, 2);
    assert!(artifacts[&ArtifactKind::Lyrics].valid);
    assert_eq!(artifacts[&ArtifactKind::Notes].generation, 1);
    assert!(!artifacts[&ArtifactKind::Notes].valid);
    assert!(artifacts[&ArtifactKind::Vocals].valid);
    assert!(artifacts[&ArtifactKind::Original].valid);
    // 再生成待ちでも一覧・読込から消えない。
    assert!(
        harness.library.list(None, 10).unwrap().songs[0]
            .error
            .is_none()
    );

    let second = harness
        .library
        .edit_song(harness.id, &harness.command(2, vec![note_edit(60)]))
        .unwrap();
    assert_eq!(second.pending, Vec::<ArtifactKind>::new());
    let bundle = harness.library.load(harness.id).unwrap();
    assert_eq!(bundle.song.artifacts[&ArtifactKind::Notes].generation, 2);
    assert_eq!(edit_generation(&bundle), 3);
}

#[test]
fn rejected_edit_leaves_every_file_untouched() {
    let harness = Harness::new();
    let before = harness.snapshot();
    let bad = EditOp::UpdateNote {
        id: "note-1".to_owned(),
        start_sample: 100,
        end_sample: 100,
        midi_note: 60,
    };
    let error = harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit("い"), bad]))
        .unwrap_err();
    assert!(matches!(error, SongEditSaveError::Edit(_)));
    assert_eq!(harness.snapshot(), before);
    assert_eq!(harness.leftovers(), Vec::<String>::new());
}

#[test]
fn oversized_document_is_rejected_before_any_write() {
    let harness = Harness::new();
    let before = harness.snapshot();
    let huge = "あ".repeat(6 * 1024 * 1024);
    let error = harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit(&huge)]))
        .unwrap_err();
    assert!(matches!(
        error,
        SongEditSaveError::Library(ImportError::TooLarge)
    ));
    assert_eq!(harness.snapshot(), before);
    assert_eq!(harness.leftovers(), Vec::<String>::new());
}

#[test]
fn unknown_song_and_foreign_song_id_are_rejected() {
    let harness = Harness::new();
    let missing = Uuid::new_v4();
    let error = harness
        .library
        .edit_song(missing, &harness.command(1, vec![lyric_edit("い")]))
        .unwrap_err();
    assert!(matches!(
        error,
        SongEditSaveError::Library(ImportError::NotFound)
    ));
    let mut foreign = harness.command(1, vec![lyric_edit("い")]);
    foreign.song_id = Uuid::new_v4().to_string();
    assert!(matches!(
        harness.library.edit_song(harness.id, &foreign).unwrap_err(),
        SongEditSaveError::Edit(_)
    ));
}

#[test]
fn interrupted_swap_is_recovered_before_the_next_edit() {
    let harness = Harness::new();
    // 置換の途中（公開ディレクトリが退避済み）でクラッシュした状態を再現する。
    let retired = harness.songs.join(format!(".retired-{}-crash", harness.id));
    fs::rename(harness.song_dir(), &retired).unwrap();
    assert!(harness.library.load(harness.id).is_err());

    let saved = harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit("い")]))
        .unwrap();
    assert_eq!(saved.generation, 2);
    assert_eq!(harness.leftovers(), Vec::<String>::new());
    assert_eq!(
        harness.library.load(harness.id).unwrap().lyrics.segments[0].text,
        "い"
    );
}

#[test]
fn concurrent_writer_gets_busy_and_the_song_is_unchanged() {
    let harness = Harness::new();
    let before = harness.snapshot();
    let lock = fs::File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(harness.songs.join(".writer.lock"))
        .unwrap();
    lock.try_lock().unwrap();
    let error = harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit("い")]))
        .unwrap_err();
    assert!(matches!(
        error,
        SongEditSaveError::Library(ImportError::Busy)
    ));
    assert_eq!(harness.snapshot(), before);
}

#[test]
fn racing_edits_with_the_same_base_never_both_succeed() {
    let harness = Harness::new();
    let outcomes = std::thread::scope(|scope| {
        let handles = ["い", "う", "え", "お"].map(|text| {
            let command = harness.command(1, vec![lyric_edit(text)]);
            let library = harness.library.clone();
            let id = harness.id;
            scope.spawn(move || library.edit_song(id, &command))
        });
        handles.map(|handle| handle.join().unwrap())
    });
    let successes = outcomes.iter().filter(|outcome| outcome.is_ok()).count();
    assert_eq!(successes, 1);
    for outcome in outcomes.iter().filter_map(|outcome| outcome.as_ref().err()) {
        assert!(matches!(
            outcome,
            SongEditSaveError::GenerationConflict { .. }
                | SongEditSaveError::Library(ImportError::Busy)
        ));
    }
    let bundle = harness.library.load(harness.id).unwrap();
    assert_eq!(edit_generation(&bundle), 2);
    assert_eq!(harness.leftovers(), Vec::<String>::new());
}

#[test]
fn reimporting_the_original_analysis_never_overwrites_an_edited_song() {
    let harness = Harness::new();
    harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit("い")]))
        .unwrap();
    let error = harness
        .library
        .import(&harness.job, &ImportControl::default(), |_, _| {})
        .unwrap_err();
    assert!(matches!(error, ImportError::Conflict));
    assert_eq!(
        harness.library.load(harness.id).unwrap().lyrics.segments[0].text,
        "い"
    );
}

#[test]
fn reimport_after_a_crashed_swap_recovers_edits_instead_of_republishing() {
    let harness = Harness::new();
    harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit("い")]))
        .unwrap();
    // 置換の途中（公開ディレクトリが退避済み）でクラッシュした状態を再現する。
    let retired = harness.songs.join(format!(".retired-{}-crash", harness.id));
    fs::rename(harness.song_dir(), &retired).unwrap();

    let error = harness
        .library
        .import(&harness.job, &ImportControl::default(), |_, _| {})
        .unwrap_err();
    assert!(matches!(error, ImportError::Conflict));
    assert_eq!(harness.leftovers(), Vec::<String>::new());
    assert_eq!(
        harness.library.load(harness.id).unwrap().lyrics.segments[0].text,
        "い"
    );
}

#[test]
fn damaged_published_song_is_never_modified_by_an_edit() {
    let harness = Harness::new();
    fs::write(harness.song_dir().join("analysis/notes.json"), b"{ trunc").unwrap();
    let before = harness.snapshot();
    let error = harness
        .library
        .edit_song(harness.id, &harness.command(1, vec![lyric_edit("い")]))
        .unwrap_err();
    assert!(matches!(error, SongEditSaveError::Library(_)));
    assert_eq!(harness.snapshot(), before);
    assert_eq!(harness.leftovers(), Vec::<String>::new());
}
