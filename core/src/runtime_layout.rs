//! Bundled runtime layout and `runtime-manifest.json` (Issue #39 slice 39.1).
//!
//! { 責務: 同梱配布物の配置規約とパス解決、runtime-manifest.json の構造検証。
//!   非責務: ダウンロード、hash計算、インストール、署名。 }

use std::{
    collections::HashSet,
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};

/// { 処理: 同梱配置の manifest ファイル名。アプリ実行ファイルと同じディレクトリに置く。 }
pub const RUNTIME_MANIFEST_FILE: &str = "runtime-manifest.json";
const MANIFEST_MAX_BYTES: u64 = 256 * 1024;
const SUPPORTED_SCHEMA_VERSION: u32 = 1;

/// { 責務: 同梱ffmpeg系ツールの種別。 }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FfmpegTool {
    Ffmpeg,
    Ffprobe,
}

impl FfmpegTool {
    fn stem(self) -> &'static str {
        match self {
            Self::Ffmpeg => "ffmpeg",
            Self::Ffprobe => "ffprobe",
        }
    }
}

/// { 責務: アプリ配置ディレクトリ基準で同梱ランタイムの実体パスを解決する。 }
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeLayout {
    app_dir: PathBuf,
}

impl RuntimeLayout {
    /// { 処理: 配置ディレクトリ（Core実行ファイルの親）から配置を作る。存在確認はしない。 }
    #[must_use]
    pub fn new(app_dir: impl Into<PathBuf>) -> Self {
        Self {
            app_dir: app_dir.into(),
        }
    }

    /// { 戻り値: 同梱Python（`runtime/python/python.exe` または `bin/python3`）の予定パス。 }
    #[must_use]
    pub fn bundled_python(&self) -> PathBuf {
        self.app_dir.join("runtime/python").join(if cfg!(windows) {
            "python.exe"
        } else {
            "bin/python3"
        })
    }

    /// { 戻り値: 同梱ffmpeg/ffprobe（`tools/ffmpeg/`）の予定パス。 }
    #[must_use]
    pub fn bundled_ffmpeg(&self, tool: FfmpegTool) -> PathBuf {
        let name = if cfg!(windows) {
            format!("{}.exe", tool.stem())
        } else {
            tool.stem().to_owned()
        };
        self.app_dir.join("tools/ffmpeg").join(name)
    }

    /// { 戻り値: `runtime-manifest.json` の予定パス。 }
    #[must_use]
    pub fn manifest_path(&self) -> PathBuf {
        self.app_dir.join(RUNTIME_MANIFEST_FILE)
    }

    /// { 処理: 優先順位は 明示指定(環境変数) > 同梱 > PATH上の名前。
    ///   利用者にパス入力を求めない通常経路では同梱が選ばれる。
    ///   引数: explicit = 明示指定（空は未指定扱い）。 }
    #[must_use]
    pub fn resolve_python(&self, explicit: Option<PathBuf>) -> PathBuf {
        Self::resolve(explicit, self.bundled_python(), "python")
    }

    /// { 処理: `resolve_python` と同じ優先順位でffmpeg系ツールを解決する。 }
    #[must_use]
    pub fn resolve_ffmpeg(&self, tool: FfmpegTool, explicit: Option<PathBuf>) -> PathBuf {
        Self::resolve(explicit, self.bundled_ffmpeg(tool), tool.stem())
    }

    fn resolve(explicit: Option<PathBuf>, bundled: PathBuf, path_name: &str) -> PathBuf {
        match explicit {
            Some(path) if !path.as_os_str().is_empty() => path,
            _ if bundled.is_file() => bundled,
            _ => PathBuf::from(path_name),
        }
    }
}

/// { 責務: runtime-manifest.json 全体。未知フィールドは拒否する。 }
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeManifest {
    pub schema_version: u32,
    pub app_version: String,
    pub components: Vec<ComponentEntry>,
    pub models: Vec<ModelEntry>,
}

/// { 責務: 同梱コンポーネント（Python/ffmpeg等）1件の版・hash・ライセンス台帳。 }
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComponentEntry {
    pub name: String,
    pub version: String,
    pub sha256: String,
    pub license: String,
    pub source_url: String,
}

/// { 責務: モデル重み1件の導入情報。コードと重みのライセンスは別に持つ。 }
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelEntry {
    pub id: String,
    pub role: String,
    pub sha256: String,
    pub size: u64,
    pub license: String,
    pub commercial_use: bool,
    pub redistribution: bool,
}

/// { 責務: manifest読込・検証の失敗理由。サービス全体は落とさず呼出側が「再取得/再導入」を示せる粒度。 }
#[derive(Debug, PartialEq, Eq)]
pub enum RuntimeManifestError {
    Io(String),
    TooLarge,
    Parse(String),
    UnsupportedSchema(u32),
    Invalid(String),
}

impl std::fmt::Display for RuntimeManifestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(m) => write!(f, "runtime manifest unreadable: {m}"),
            Self::TooLarge => write!(f, "runtime manifest exceeds {MANIFEST_MAX_BYTES} bytes"),
            Self::Parse(m) => write!(f, "runtime manifest is not valid: {m}"),
            Self::UnsupportedSchema(v) => write!(f, "unsupported runtime manifest schema {v}"),
            Self::Invalid(m) => write!(f, "runtime manifest rejected: {m}"),
        }
    }
}

impl std::error::Error for RuntimeManifestError {}

impl RuntimeManifest {
    /// { 処理: ファイルを上限付きで読み検証する。manifestを読むだけでコードは実行しない。
    ///   戻り値: 検証済みmanifest。 }
    ///
    /// # Errors
    /// Returns I/O, size, parse, schema or content validation errors.
    pub fn load(path: &Path) -> Result<Self, RuntimeManifestError> {
        use std::io::Read;
        let file =
            std::fs::File::open(path).map_err(|e| RuntimeManifestError::Io(e.to_string()))?;
        // Bound the bytes actually read so a file that grows after open cannot exceed the cap.
        let mut bytes = Vec::new();
        file.take(MANIFEST_MAX_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| RuntimeManifestError::Io(e.to_string()))?;
        if bytes.len() as u64 > MANIFEST_MAX_BYTES {
            return Err(RuntimeManifestError::TooLarge);
        }
        let text = String::from_utf8(bytes).map_err(|e| RuntimeManifestError::Io(e.to_string()))?;
        Self::parse(&text)
    }

    /// { 処理: JSON文字列を解析し、構造と値を検証する。 }
    ///
    /// # Errors
    /// Returns parse, schema-version or content validation errors.
    pub fn parse(text: &str) -> Result<Self, RuntimeManifestError> {
        let manifest: Self =
            serde_json::from_str(text).map_err(|e| RuntimeManifestError::Parse(e.to_string()))?;
        manifest.validate()?;
        Ok(manifest)
    }

    fn validate(&self) -> Result<(), RuntimeManifestError> {
        if self.schema_version != SUPPORTED_SCHEMA_VERSION {
            return Err(RuntimeManifestError::UnsupportedSchema(self.schema_version));
        }
        require_text("app_version", &self.app_version)?;
        let mut names = HashSet::new();
        for c in &self.components {
            require_text("component.name", &c.name)?;
            require_text("component.version", &c.version)?;
            require_text("component.license", &c.license)?;
            require_text("component.source_url", &c.source_url)?;
            require_sha256("component.sha256", &c.sha256)?;
            if !names.insert(c.name.as_str()) {
                return Err(invalid(format!("duplicate component {}", c.name)));
            }
        }
        let mut ids = HashSet::new();
        for m in &self.models {
            require_text("model.id", &m.id)?;
            require_text("model.role", &m.role)?;
            require_text("model.license", &m.license)?;
            require_sha256("model.sha256", &m.sha256)?;
            if m.size == 0 {
                return Err(invalid(format!("model {} has zero size", m.id)));
            }
            if !ids.insert(m.id.as_str()) {
                return Err(invalid(format!("duplicate model {}", m.id)));
            }
        }
        Ok(())
    }
}

fn invalid(message: String) -> RuntimeManifestError {
    RuntimeManifestError::Invalid(message)
}

fn require_text(field: &str, value: &str) -> Result<(), RuntimeManifestError> {
    if value.trim().is_empty() || value.len() > 512 {
        return Err(invalid(format!(
            "{field} must be 1..=512 non-blank characters"
        )));
    }
    Ok(())
}

fn require_sha256(field: &str, value: &str) -> Result<(), RuntimeManifestError> {
    if value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        Ok(())
    } else {
        Err(invalid(format!(
            "{field} must be 64 lowercase hex characters"
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const HASH: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn sample() -> String {
        format!(
            r#"{{"schema_version":1,"app_version":"0.1.0",
"components":[{{"name":"python","version":"3.12.4","sha256":"{HASH}","license":"PSF-2.0","source_url":"https://www.python.org/"}}],
"models":[{{"id":"demucs","role":"stem","sha256":"{HASH}","size":10,"license":"MIT","commercial_use":true,"redistribution":true}}]}}"#
        )
    }

    #[test]
    fn accepts_valid_manifest() {
        let m = RuntimeManifest::parse(&sample()).unwrap();
        assert_eq!(m.components[0].name, "python");
        assert_eq!(m.models[0].size, 10);
    }

    #[test]
    fn rejects_unknown_field_bad_hash_schema_and_zero_size() {
        let with_extra = sample().replacen("\"app_version\"", "\"extra\":1,\"app_version\"", 1);
        assert!(matches!(
            RuntimeManifest::parse(&with_extra),
            Err(RuntimeManifestError::Parse(_))
        ));
        let bad_hash = sample().replace(HASH, "ABC");
        assert!(matches!(
            RuntimeManifest::parse(&bad_hash),
            Err(RuntimeManifestError::Invalid(_))
        ));
        let upper = sample().replacen(HASH, &HASH.to_uppercase(), 1);
        assert!(RuntimeManifest::parse(&upper).is_err());
        let v2 = sample().replace("\"schema_version\":1", "\"schema_version\":2");
        assert_eq!(
            RuntimeManifest::parse(&v2),
            Err(RuntimeManifestError::UnsupportedSchema(2))
        );
        let zero = sample().replace("\"size\":10", "\"size\":0");
        assert!(RuntimeManifest::parse(&zero).is_err());
    }

    #[test]
    fn rejects_duplicate_ids() {
        let one =
            r#"{"name":"python","version":"1","sha256":"HASH","license":"x","source_url":"u"}"#
                .replace("HASH", HASH);
        let text = format!(
            r#"{{"schema_version":1,"app_version":"1","components":[{one},{one}],"models":[]}}"#
        );
        assert!(RuntimeManifest::parse(&text).is_err());
    }

    #[test]
    fn resolves_explicit_then_bundled_then_path() {
        let dir = tempfile::tempdir().unwrap();
        let layout = RuntimeLayout::new(dir.path());
        assert_eq!(layout.resolve_python(None), PathBuf::from("python"));
        let bundled = layout.bundled_python();
        std::fs::create_dir_all(bundled.parent().unwrap()).unwrap();
        std::fs::write(&bundled, b"").unwrap();
        assert_eq!(layout.resolve_python(None), bundled);
        assert_eq!(layout.resolve_python(Some(PathBuf::new())), bundled);
        assert_eq!(
            layout.resolve_python(Some(PathBuf::from("custom"))),
            PathBuf::from("custom")
        );
    }
}
