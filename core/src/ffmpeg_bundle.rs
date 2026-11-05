//! `FFmpeg` distribution bundle verification (Issue #39 slice 39.3).
//!
//! { 責務: 同梱 `tools/ffmpeg/` の buildconf 記録・NOTICE・runtime-manifest 台帳が
//!   実バイナリと一致することの検査、およびCoreが使う ffmpeg/ffprobe パスの解決。
//!   非責務: ffmpeg の入手・ビルド、ライセンスの法的判断（構成の機械検査のみ）。 }

use std::{
    fmt, fs,
    path::{Path, PathBuf},
};

use sha2::{Digest, Sha256};

use crate::{
    audio_import::{AudioImportError, validate_ffmpeg_build_configuration},
    runtime_layout::{FfmpegTool, RuntimeLayout, RuntimeManifest},
};

/// { 処理: 同梱ディレクトリ内の buildconf 全文の記録ファイル名。 }
pub const BUILDCONF_FILE: &str = "buildconf.txt";
/// { 処理: 同梱ディレクトリ内の NOTICE ファイル名。 }
pub const NOTICE_FILE: &str = "NOTICE.txt";
/// { 処理: runtime-manifest の ffmpeg コンポーネント名。 }
pub const COMPONENT_NAME: &str = "ffmpeg";
const BUILDCONF_MAX_BYTES: u64 = 256 * 1024;

/// { 責務: Coreが使う ffmpeg / ffprobe の解決済みパス。 }
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaTools {
    pub ffmpeg: PathBuf,
    pub ffprobe: PathBuf,
}

impl MediaTools {
    /// { 処理: 明示指定(環境変数) > 同梱 > PATH の順で解決する。
    ///   引数: layout = 配置, ffmpeg/ffprobe = 明示指定（空は未指定）。 }
    #[must_use]
    pub fn resolve(
        layout: &RuntimeLayout,
        ffmpeg: Option<PathBuf>,
        ffprobe: Option<PathBuf>,
    ) -> Self {
        Self {
            ffmpeg: layout.resolve_ffmpeg(FfmpegTool::Ffmpeg, ffmpeg),
            ffprobe: layout.resolve_ffmpeg(FfmpegTool::Ffprobe, ffprobe),
        }
    }
}

/// { 責務: 同梱 `FFmpeg` の検査失敗理由。 }
#[derive(Debug)]
pub enum BundleError {
    Missing(String),
    Io(String),
    Policy(String),
    Mismatch(String),
}

impl fmt::Display for BundleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing(m) => write!(f, "ffmpeg bundle incomplete: {m}"),
            Self::Io(m) => write!(f, "ffmpeg bundle unreadable: {m}"),
            Self::Policy(m) => write!(f, "ffmpeg bundle violates license policy: {m}"),
            Self::Mismatch(m) => write!(f, "ffmpeg bundle does not match inventory: {m}"),
        }
    }
}

impl std::error::Error for BundleError {}

/// { 処理: 同梱 `FFmpeg` を検査する。
///   (1) ffmpeg/ffprobe 実体が存在, (2) `buildconf.txt` が GPL/nonfree を含まない,
///   (3) manifest の `ffmpeg` 台帳の sha256 が実バイナリと一致し、ライセンスが LGPL 系,
///   (4) `NOTICE.txt` が台帳の版・sha256・ライセンスを含む。
///   戻り値: 成功時は何も返さない。manifest に ffmpeg 台帳が無い場合も失敗。 }
///
/// # Errors
/// Returns the first missing file, policy violation or inventory mismatch.
pub fn verify_bundle(
    layout: &RuntimeLayout,
    manifest: &RuntimeManifest,
) -> Result<(), BundleError> {
    let ffmpeg = layout.bundled_ffmpeg(FfmpegTool::Ffmpeg);
    let ffprobe = layout.bundled_ffmpeg(FfmpegTool::Ffprobe);
    for path in [&ffmpeg, &ffprobe] {
        if !path.is_file() {
            return Err(BundleError::Missing(path.display().to_string()));
        }
    }
    let dir = ffmpeg
        .parent()
        .ok_or_else(|| BundleError::Missing("tools/ffmpeg directory".to_owned()))?;

    let buildconf = read_text(&dir.join(BUILDCONF_FILE))?;
    validate_ffmpeg_build_configuration(&buildconf).map_err(|e| match e {
        AudioImportError::LicensePolicy(m) => BundleError::Policy(m),
        other => BundleError::Policy(other.to_string()),
    })?;

    let entry = manifest
        .components
        .iter()
        .find(|c| c.name == COMPONENT_NAME)
        .ok_or_else(|| {
            BundleError::Mismatch("runtime manifest has no ffmpeg component".to_owned())
        })?;
    if !entry.license.to_ascii_uppercase().starts_with("LGPL") {
        return Err(BundleError::Policy(format!(
            "manifest license `{}` is not LGPL",
            entry.license
        )));
    }
    let actual = sha256_file(&ffmpeg)?;
    if actual != entry.sha256 {
        return Err(BundleError::Mismatch(format!(
            "ffmpeg sha256 {actual} != manifest {}",
            entry.sha256
        )));
    }

    let notice = read_text(&dir.join(NOTICE_FILE))?;
    for needle in [&entry.version, &entry.sha256, &entry.license] {
        if !notice.contains(needle.as_str()) {
            return Err(BundleError::Mismatch(format!(
                "{NOTICE_FILE} does not mention `{needle}`"
            )));
        }
    }
    Ok(())
}

fn read_text(path: &Path) -> Result<String, BundleError> {
    use std::io::Read;
    let file = fs::File::open(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            BundleError::Missing(path.display().to_string())
        } else {
            BundleError::Io(format!("{}: {e}", path.display()))
        }
    })?;
    let mut bytes = Vec::new();
    file.take(BUILDCONF_MAX_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| BundleError::Io(e.to_string()))?;
    if bytes.len() as u64 > BUILDCONF_MAX_BYTES {
        return Err(BundleError::Io(format!("{} is too large", path.display())));
    }
    String::from_utf8(bytes).map_err(|e| BundleError::Io(e.to_string()))
}

fn sha256_file(path: &Path) -> Result<String, BundleError> {
    use std::io::Read;
    let mut file = fs::File::open(path).map_err(|e| BundleError::Io(e.to_string()))?;
    let mut hash = Sha256::new();
    let mut buffer = vec![0_u8; 64 * 1024];
    loop {
        let n = file
            .read(&mut buffer)
            .map_err(|e| BundleError::Io(e.to_string()))?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
