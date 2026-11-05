//! Issue #39 slice 39.1: a bundled layout resolves without any developer tool on PATH.

use open_karaoke_core::runtime_layout::{
    FfmpegTool, RUNTIME_MANIFEST_FILE, RuntimeLayout, RuntimeManifest,
};

/// { 処理: 開発ツール無しの環境を模した配置（PATH/環境変数に依存しない）で同梱が解決されることを確認する。 }
#[test]
fn bundled_layout_resolves_without_path_or_environment() {
    let root = tempfile::tempdir().unwrap();
    let layout = RuntimeLayout::new(root.path());
    for path in [
        layout.bundled_python(),
        layout.bundled_ffmpeg(FfmpegTool::Ffmpeg),
        layout.bundled_ffmpeg(FfmpegTool::Ffprobe),
    ] {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, b"").unwrap();
    }
    assert_eq!(layout.resolve_python(None), layout.bundled_python());
    assert_eq!(
        layout.resolve_ffmpeg(FfmpegTool::Ffmpeg, None),
        layout.bundled_ffmpeg(FfmpegTool::Ffmpeg)
    );
    assert_eq!(
        layout.resolve_ffmpeg(FfmpegTool::Ffprobe, None),
        layout.bundled_ffmpeg(FfmpegTool::Ffprobe)
    );
}

/// { 処理: 欠落/巨大/不正manifestはエラー（落とさない）、正常なものは読める。 }
#[test]
fn manifest_file_is_loaded_and_bad_files_are_rejected() {
    let root = tempfile::tempdir().unwrap();
    let layout = RuntimeLayout::new(root.path());
    assert!(RuntimeManifest::load(&layout.manifest_path()).is_err());
    let path = root.path().join(RUNTIME_MANIFEST_FILE);
    std::fs::write(
        &path,
        br#"{"schema_version":1,"app_version":"0.1.0","components":[],"models":[]}"#,
    )
    .unwrap();
    assert_eq!(RuntimeManifest::load(&path).unwrap().app_version, "0.1.0");
    std::fs::write(&path, vec![b' '; 300 * 1024]).unwrap();
    assert!(RuntimeManifest::load(&path).is_err());
    std::fs::write(&path, b"not json").unwrap();
    assert!(RuntimeManifest::load(&path).is_err());
}
