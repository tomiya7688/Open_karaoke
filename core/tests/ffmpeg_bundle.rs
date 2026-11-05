//! Issue #39 slice 39.3: buildconf inspection and NOTICE/inventory consistency.

use open_karaoke_core::{
    ffmpeg_bundle::{BUILDCONF_FILE, BundleError, MediaTools, NOTICE_FILE, verify_bundle},
    runtime_layout::{FfmpegTool, RuntimeLayout, RuntimeManifest},
};
use sha2::{Digest, Sha256};
use std::path::Path;

const LGPL_CONF: &str = "configuration: --disable-gpl --disable-nonfree --enable-shared\n";

/// { 処理: 偽のffmpegバイト列で同梱ディレクトリを作る。戻り値: (配置, ffmpegのsha256)。 }
fn stage(root: &Path, buildconf: &str, notice_extra: &str) -> (RuntimeLayout, String) {
    let layout = RuntimeLayout::new(root);
    for tool in [FfmpegTool::Ffmpeg, FfmpegTool::Ffprobe] {
        let path = layout.bundled_ffmpeg(tool);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, b"fake-binary").unwrap();
    }
    let dir = layout.bundled_ffmpeg(FfmpegTool::Ffmpeg);
    let dir = dir.parent().unwrap();
    let hash = format!("{:x}", Sha256::digest(b"fake-binary"));
    std::fs::write(dir.join(BUILDCONF_FILE), buildconf).unwrap();
    std::fs::write(
        dir.join(NOTICE_FILE),
        format!("FFmpeg 7.1 LGPL-2.1-or-later sha256 {hash} {notice_extra}"),
    )
    .unwrap();
    (layout, hash)
}

fn manifest(hash: &str, license: &str) -> RuntimeManifest {
    RuntimeManifest::parse(&format!(
        r#"{{"schema_version":1,"app_version":"0.1.0","components":[
{{"name":"ffmpeg","version":"7.1","sha256":"{hash}","license":"{license}","source_url":"https://ffmpeg.org/"}}],"models":[]}}"#
    ))
    .unwrap()
}

/// { 処理: LGPL構成のbuildconf・一致する台帳・NOTICEなら合格する。 }
#[test]
fn consistent_lgpl_bundle_passes() {
    let root = tempfile::tempdir().unwrap();
    let (layout, hash) = stage(root.path(), LGPL_CONF, "");
    verify_bundle(&layout, &manifest(&hash, "LGPL-2.1-or-later")).unwrap();
}

/// { 処理: buildconfにGPL/nonfreeが含まれる成果物は拒否する。 }
#[test]
fn gpl_or_nonfree_buildconf_is_rejected() {
    for conf in ["--enable-gpl --enable-libx264", "--enable-nonfree"] {
        let root = tempfile::tempdir().unwrap();
        let (layout, hash) = stage(root.path(), conf, "");
        let err = verify_bundle(&layout, &manifest(&hash, "LGPL-2.1-or-later")).unwrap_err();
        assert!(matches!(err, BundleError::Policy(_)), "{conf}: {err}");
    }
}

/// { 処理: buildconf記録・NOTICE・実体のいずれかが欠けると拒否する。 }
#[test]
fn missing_buildconf_notice_or_binary_is_rejected() {
    for file in [BUILDCONF_FILE, NOTICE_FILE] {
        let root = tempfile::tempdir().unwrap();
        let (layout, hash) = stage(root.path(), LGPL_CONF, "");
        let dir = layout.bundled_ffmpeg(FfmpegTool::Ffmpeg);
        std::fs::remove_file(dir.parent().unwrap().join(file)).unwrap();
        let err = verify_bundle(&layout, &manifest(&hash, "LGPL-2.1-or-later")).unwrap_err();
        assert!(matches!(err, BundleError::Missing(_)), "{file}: {err}");
    }
    let root = tempfile::tempdir().unwrap();
    let (layout, hash) = stage(root.path(), LGPL_CONF, "");
    std::fs::remove_file(layout.bundled_ffmpeg(FfmpegTool::Ffprobe)).unwrap();
    assert!(matches!(
        verify_bundle(&layout, &manifest(&hash, "LGPL-2.1-or-later")),
        Err(BundleError::Missing(_))
    ));
}

/// { 処理: 台帳のhash/ライセンス/ffmpeg項目とNOTICEが成果物とずれると拒否する。 }
#[test]
fn inventory_and_notice_must_match_the_artifact() {
    let root = tempfile::tempdir().unwrap();
    let (layout, hash) = stage(root.path(), LGPL_CONF, "");
    let wrong = "0".repeat(64);
    assert!(matches!(
        verify_bundle(&layout, &manifest(&wrong, "LGPL-2.1-or-later")),
        Err(BundleError::Mismatch(_))
    ));
    assert!(matches!(
        verify_bundle(&layout, &manifest(&hash, "GPL-2.0-or-later")),
        Err(BundleError::Policy(_))
    ));
    // 台帳のlicenseがNOTICEに書かれていない
    assert!(matches!(
        verify_bundle(&layout, &manifest(&hash, "LGPL-3.0-or-later")),
        Err(BundleError::Mismatch(_))
    ));
    let empty = RuntimeManifest::parse(
        r#"{"schema_version":1,"app_version":"0.1.0","components":[],"models":[]}"#,
    )
    .unwrap();
    assert!(matches!(
        verify_bundle(&layout, &empty),
        Err(BundleError::Mismatch(_))
    ));
}

/// { 処理: Coreが使う解決は 明示指定 > 同梱 > PATH名 の順になる。 }
#[test]
fn media_tools_resolve_bundled_before_path_and_explicit_first() {
    let root = tempfile::tempdir().unwrap();
    let empty = RuntimeLayout::new(root.path());
    let none = MediaTools::resolve(&empty, None, None);
    assert_eq!(none.ffmpeg, Path::new("ffmpeg"));
    assert_eq!(none.ffprobe, Path::new("ffprobe"));

    let (layout, _) = stage(root.path(), LGPL_CONF, "");
    let bundled = MediaTools::resolve(&layout, None, None);
    assert_eq!(bundled.ffmpeg, layout.bundled_ffmpeg(FfmpegTool::Ffmpeg));
    let explicit = MediaTools::resolve(&layout, Some("custom-ffmpeg".into()), None);
    assert_eq!(explicit.ffmpeg, Path::new("custom-ffmpeg"));
    assert_eq!(explicit.ffprobe, layout.bundled_ffmpeg(FfmpegTool::Ffprobe));
}

/// { 処理: リポジトリのNOTICE台帳に`FFmpeg`のLGPL方針と未確認事項が明記されている。 }
#[test]
fn repository_notice_states_ffmpeg_policy_and_unverified_scope() {
    let notices = include_str!("../../THIRD_PARTY_NOTICES.md");
    let row = notices
        .lines()
        .find(|l| l.contains("FFmpeg"))
        .expect("THIRD_PARTY_NOTICES.md must have an FFmpeg row");
    assert!(row.contains("LGPL"), "{row}");
    assert!(row.contains("Unverified"), "{row}");
}
